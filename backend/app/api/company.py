"""Company, users and organisational structure."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select

from app.deps import CurrentUserDep, DbDep, audit_context, require_permission
from app.enums import ActorType, AuditAction, Permission, UserRole
from app.errors import ConflictError, NotFoundError, ValidationError
from app.models import Branch, Company, CostCenter, Department, Membership, User
from app.schemas import (
    CompanyCreate,
    CompanyOut,
    CompanyUpdate,
    MembershipOut,
    MessageResponse,
    UserCreate,
    UserOut,
)
from app.security import hash_password, password_strength
from app.services.company import add_member, create_company, remove_member, update_company

router = APIRouter(prefix="/companies", tags=["Company"])


@router.get("", response_model=list[dict])
def list_my_companies(db: DbDep, current: CurrentUserDep) -> list[dict]:
    from app.services.company import user_companies

    return [
        {
            "id": company.id,
            "name": company.name,
            "legal_name": company.legal_name,
            "role": role,
            "is_demo": company.is_demo,
            "currency": company.currency,
            "is_current": company.id == current.company_id,
        }
        for company, role in user_companies(db, current.id)
    ]


@router.get("/current", response_model=CompanyOut)
def get_current_company(current: CurrentUserDep) -> CompanyOut:
    return CompanyOut.model_validate(current.company)


@router.post("", response_model=CompanyOut, status_code=201)
def create(payload: CompanyCreate, db: DbDep, current: CurrentUserDep) -> CompanyOut:
    """Create a company. The creator becomes its owner."""
    company = create_company(
        db,
        name=payload.name,
        legal_name=payload.legal_name or payload.name,
        business_type=payload.business_type,
        state=payload.state,
        state_code=payload.state_code,
        gstin=payload.gstin,
        pan=payload.pan,
        tan=payload.tan,
        address_line1=payload.address_line1,
        city=payload.city,
        pincode=payload.pincode,
        email=payload.email,
        phone=payload.phone,
        currency=payload.currency,
        fiscal_year_start_month=payload.fiscal_year_start_month,
        books_begin_on=payload.books_begin_on,
        owner_user_id=current.id,
    )
    return CompanyOut.model_validate(company)


@router.patch("/current", response_model=CompanyOut)
def update_current(
    payload: CompanyUpdate,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_SETTINGS.value))],
) -> CompanyOut:
    company = update_company(
        db,
        current.company_id,
        payload.model_dump(exclude_unset=True, exclude_none=True),
        user_id=current.id,
    )
    return CompanyOut.model_validate(company)


# ----------------------------------------------------------------- users --

@router.get("/current/users", response_model=list[MembershipOut])
def list_users(
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_USERS.value))],
) -> list[MembershipOut]:
    memberships = list(
        db.execute(
            select(Membership).where(Membership.company_id == current.company_id)
        ).scalars()
    )
    results = []
    for membership in memberships:
        out = MembershipOut.model_validate(membership)
        out.user = UserOut.model_validate(db.get(User, membership.user_id))
        results.append(out)
    return results


@router.post("/current/users", response_model=MembershipOut, status_code=201)
def invite_user(
    payload: UserCreate,
    request: Request,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_USERS.value))],
) -> MembershipOut:
    """Add a user to this company, creating the account if it does not exist."""
    from app.services.audit import record_event

    strong, problems = password_strength(payload.password)
    if not strong:
        raise ValidationError("Password is not strong enough.", details={"problems": problems})

    email = payload.email.lower().strip()
    user = db.execute(select(User).where(func.lower(User.email) == email)).scalar_one_or_none()

    if user is None:
        user = User(
            email=email,
            full_name=payload.full_name,
            phone=payload.phone,
            password_hash=hash_password(payload.password),
            must_change_password=True,
        )
        db.add(user)
        db.flush()
    else:
        existing = db.execute(
            select(Membership).where(
                Membership.company_id == current.company_id, Membership.user_id == user.id
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise ConflictError("That user is already a member of this company.")

    membership = add_member(db, current.company_id, user.id, payload.role.value)

    record_event(
        db,
        company_id=current.company_id,
        action=AuditAction.CREATE,
        entity_type="Membership",
        entity_id=membership.id,
        entity_label=user.email,
        summary=f"{user.email} added to the company as {payload.role.value}",
        actor_type=ActorType.USER,
        actor_id=current.id,
        actor_label=current.email,
        after={"email": user.email, "role": payload.role.value},
        **audit_context(request),
    )

    out = MembershipOut.model_validate(membership)
    out.user = UserOut.model_validate(user)
    return out


@router.patch("/current/users/{user_id}/role", response_model=MembershipOut)
def change_role(
    user_id: str,
    request: Request,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_USERS.value))],
    role: UserRole = Query(...),
) -> MembershipOut:
    from app.services.audit import record_event

    membership = db.execute(
        select(Membership).where(
            Membership.company_id == current.company_id, Membership.user_id == user_id
        )
    ).scalar_one_or_none()
    if membership is None:
        raise NotFoundError("That user is not a member of this company.")

    # Never let the last owner demote themselves and lock everyone out.
    if membership.role == UserRole.OWNER and role != UserRole.OWNER:
        owners = int(
            db.execute(
                select(func.count(Membership.id)).where(
                    Membership.company_id == current.company_id,
                    Membership.role == UserRole.OWNER,
                )
            ).scalar_one()
        )
        if owners <= 1:
            raise ConflictError("A company must always have at least one owner.")

    before = membership.role
    membership.role = role.value
    db.flush()

    record_event(
        db,
        company_id=current.company_id,
        action=AuditAction.UPDATE,
        entity_type="Membership",
        entity_id=membership.id,
        entity_label=user_id,
        summary=f"Role changed from {before} to {role.value}",
        actor_type=ActorType.USER,
        actor_id=current.id,
        actor_label=current.email,
        before={"role": before},
        after={"role": role.value},
        **audit_context(request),
    )

    out = MembershipOut.model_validate(membership)
    out.user = UserOut.model_validate(db.get(User, user_id))
    return out


@router.delete("/current/users/{user_id}", response_model=MessageResponse)
def remove_user(
    user_id: str,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_USERS.value))],
) -> MessageResponse:
    if user_id == current.id:
        raise ValidationError("You cannot remove yourself from the company.")
    remove_member(db, current.company_id, user_id)
    return MessageResponse(message="User removed from the company.")


# ------------------------------------------------------------ structure --

@router.get("/current/branches")
def list_branches(db: DbDep, current: CurrentUserDep) -> dict:
    branches = list(
        db.execute(
            select(Branch).where(
                Branch.company_id == current.company_id, Branch.is_active.is_(True)
            )
        ).scalars()
    )
    return {
        "branches": [
            {
                "id": branch.id,
                "code": branch.code,
                "name": branch.name,
                "state": branch.state,
                "state_code": branch.state_code,
                "gstin": branch.gstin,
                "is_head_office": branch.is_head_office,
            }
            for branch in branches
        ]
    }


@router.post("/current/branches", status_code=201)
def create_branch(
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_SETTINGS.value))],
    code: str = Query(..., min_length=1, max_length=30),
    name: str = Query(..., min_length=1, max_length=150),
    state: str = Query(default=""),
    state_code: str = Query(default=""),
    gstin: str = Query(default=""),
) -> dict:
    if gstin:
        from app.services.tax import validate_gstin

        valid, message = validate_gstin(gstin)
        if not valid:
            raise ValidationError(f"GSTIN rejected: {message}", code="invalid_gstin")

    existing = db.execute(
        select(Branch).where(Branch.company_id == current.company_id, Branch.code == code)
    ).scalar_one_or_none()
    if existing:
        raise ConflictError(f"Branch code '{code}' already exists.")

    branch = Branch(
        company_id=current.company_id,
        code=code,
        name=name,
        state=state,
        state_code=state_code,
        gstin=gstin.upper() if gstin else "",
    )
    db.add(branch)
    db.flush()
    return {"id": branch.id, "code": branch.code, "name": branch.name}


@router.get("/current/cost-centers")
def list_cost_centers(db: DbDep, current: CurrentUserDep) -> dict:
    centers = list(
        db.execute(
            select(CostCenter).where(
                CostCenter.company_id == current.company_id, CostCenter.is_active.is_(True)
            )
        ).scalars()
    )
    return {
        "cost_centers": [
            {
                "id": center.id,
                "code": center.code,
                "name": center.name,
                "category": center.category,
                "parent_id": center.parent_id,
            }
            for center in centers
        ]
    }


@router.post("/current/cost-centers", status_code=201)
def create_cost_center(
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_SETTINGS.value))],
    code: str = Query(..., min_length=1, max_length=30),
    name: str = Query(..., min_length=1, max_length=150),
    category: str = Query(default="DEPARTMENT"),
) -> dict:
    existing = db.execute(
        select(CostCenter).where(
            CostCenter.company_id == current.company_id, CostCenter.code == code
        )
    ).scalar_one_or_none()
    if existing:
        raise ConflictError(f"Cost centre code '{code}' already exists.")

    center = CostCenter(
        company_id=current.company_id, code=code, name=name, category=category
    )
    db.add(center)
    db.flush()
    return {"id": center.id, "code": center.code, "name": center.name}
