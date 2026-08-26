"""Company provisioning.

Creating a company sets up everything the engine needs to operate: chart of
accounts, fiscal calendar, tax rules, policies, approval routing, units and a
default warehouse. After this call the company can immediately post.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.accounting.coa import seed_chart_of_accounts
from app.accounting.periods import ensure_fiscal_year
from app.enums import ActorType, AuditAction, BusinessType, UserRole
from app.errors import ConflictError, NotFoundError, ValidationError
from app.models import (
    Branch,
    Company,
    CostCenter,
    Currency,
    Department,
    Membership,
    User,
    UnitOfMeasure,
    Warehouse,
)
from app.services.policy import seed_policies
from app.services.tax import seed_tax_rules, state_code_from_gstin, validate_gstin, validate_pan

DEFAULT_UOMS = [
    ("NOS", "Numbers", 0),
    ("PCS", "Pieces", 0),
    ("KG", "Kilogram", 3),
    ("GM", "Gram", 3),
    ("LTR", "Litre", 3),
    ("MTR", "Metre", 2),
    ("BOX", "Box", 0),
    ("SET", "Set", 0),
    ("HRS", "Hours", 2),
    ("DAY", "Days", 2),
    ("LIC", "Licence", 0),
    ("MON", "Month", 2),
]

DEFAULT_CURRENCIES = [
    ("INR", "Indian Rupee", "₹"),
    ("USD", "US Dollar", "$"),
    ("EUR", "Euro", "€"),
    ("GBP", "Pound Sterling", "£"),
]


def ensure_currencies(db: Session) -> None:
    """Currencies are global reference data, seeded once."""
    for code, name, symbol in DEFAULT_CURRENCIES:
        exists = db.execute(select(Currency).where(Currency.code == code)).scalar_one_or_none()
        if not exists:
            db.add(Currency(code=code, name=name, symbol=symbol))
    db.flush()


def create_company(
    db: Session,
    *,
    name: str,
    legal_name: str = "",
    business_type: str = BusinessType.PRIVATE_LIMITED,
    country: str = "IN",
    state: str = "",
    state_code: str = "",
    gstin: str = "",
    pan: str = "",
    tan: str = "",
    address_line1: str = "",
    city: str = "",
    pincode: str = "",
    email: str = "",
    phone: str = "",
    currency: str = "INR",
    fiscal_year_start_month: int = 4,
    timezone: str = "Asia/Kolkata",
    books_begin_on: date | None = None,
    is_demo: bool = False,
    owner_user_id: str | None = None,
    seed_defaults: bool = True,
) -> Company:
    """Create and fully provision a company."""
    if not name or not name.strip():
        raise ValidationError("Company name is required.")

    if gstin:
        valid, message = validate_gstin(gstin)
        if not valid:
            raise ValidationError(f"GSTIN rejected: {message}", code="invalid_gstin")
        derived = state_code_from_gstin(gstin)
        if derived and not state_code:
            state_code = derived
        elif derived and state_code and derived != state_code:
            raise ValidationError(
                f"GSTIN state code '{derived}' does not match the company state code "
                f"'{state_code}'.",
                code="gstin_state_mismatch",
            )

    if pan:
        valid, message = validate_pan(pan)
        if not valid:
            raise ValidationError(f"PAN rejected: {message}", code="invalid_pan")

    if not 1 <= int(fiscal_year_start_month) <= 12:
        raise ValidationError("Fiscal year start month must be between 1 and 12.")

    ensure_currencies(db)

    company = Company(
        name=name.strip(),
        legal_name=(legal_name or name).strip(),
        business_type=business_type,
        country=country,
        state=state,
        state_code=state_code,
        gstin=gstin.upper() if gstin else "",
        pan=pan.upper() if pan else "",
        tan=tan.upper() if tan else "",
        address_line1=address_line1,
        city=city,
        pincode=pincode,
        email=email,
        phone=phone,
        currency=currency,
        fiscal_year_start_month=int(fiscal_year_start_month),
        timezone=timezone,
        books_begin_on=books_begin_on,
        is_demo=is_demo,
    )
    db.add(company)
    db.flush()

    if seed_defaults:
        provision_company(db, company, books_begin_on=books_begin_on)

    if owner_user_id:
        add_member(db, company.id, owner_user_id, UserRole.OWNER, is_default=True)

    from app.services.audit import record_event

    record_event(
        db,
        company_id=company.id,
        action=AuditAction.CREATE,
        entity_type="Company",
        entity_id=company.id,
        entity_label=company.name,
        summary=f"Company '{company.name}' created",
        actor_type=ActorType.USER if owner_user_id else ActorType.SYSTEM,
        actor_id=owner_user_id,
        after={
            "name": company.name,
            "gstin": company.gstin,
            "currency": company.currency,
            "is_demo": company.is_demo,
        },
    )
    return company


def provision_company(db: Session, company: Company, *, books_begin_on: date | None = None) -> dict:
    """Seed the accounting scaffolding. Idempotent — safe to re-run."""
    accounts = seed_chart_of_accounts(db, company.id)
    policies = seed_policies(db, company.id)
    taxes = seed_tax_rules(db, company.id)

    start = books_begin_on or company.books_begin_on or date.today()
    fiscal_year = ensure_fiscal_year(db, company.id, start)
    # Also provision the current fiscal year so today's postings always land.
    ensure_fiscal_year(db, company.id, date.today())

    # Head office branch
    head_office = db.execute(
        select(Branch).where(Branch.company_id == company.id, Branch.is_head_office.is_(True))
    ).scalar_one_or_none()
    if head_office is None:
        head_office = Branch(
            company_id=company.id,
            code="HO",
            name="Head Office",
            state=company.state,
            state_code=company.state_code,
            gstin=company.gstin,
            is_head_office=True,
        )
        db.add(head_office)

    # Default warehouse
    warehouse = db.execute(
        select(Warehouse).where(Warehouse.company_id == company.id, Warehouse.is_default.is_(True))
    ).scalar_one_or_none()
    if warehouse is None:
        db.add(
            Warehouse(
                company_id=company.id,
                code="MAIN",
                name="Main Warehouse",
                state=company.state,
                state_code=company.state_code,
                is_default=True,
            )
        )

    # Units of measure
    existing_uoms = {
        uom.code
        for uom in db.execute(
            select(UnitOfMeasure).where(UnitOfMeasure.company_id == company.id)
        ).scalars()
    }
    for code, uom_name, places in DEFAULT_UOMS:
        if code not in existing_uoms:
            db.add(
                UnitOfMeasure(
                    company_id=company.id, code=code, name=uom_name, decimal_places=places
                )
            )

    # A general cost centre so every posting can carry a dimension.
    general_cc = db.execute(
        select(CostCenter).where(CostCenter.company_id == company.id, CostCenter.code == "GEN")
    ).scalar_one_or_none()
    if general_cc is None:
        db.add(
            CostCenter(
                company_id=company.id, code="GEN", name="General", category="DEPARTMENT"
            )
        )

    db.flush()
    return {
        "accounts": len(accounts),
        "policies": policies,
        "tax_rules": taxes,
        "fiscal_year": fiscal_year.name,
    }


def add_member(
    db: Session,
    company_id: str,
    user_id: str,
    role: str,
    *,
    is_default: bool = False,
    branch_id: str | None = None,
) -> Membership:
    """Grant a user a role in a company."""
    if role not in {r.value for r in UserRole}:
        raise ValidationError(f"'{role}' is not a valid role.")

    company = db.get(Company, company_id)
    if company is None:
        raise NotFoundError("Company not found.")
    user = db.get(User, user_id)
    if user is None:
        raise NotFoundError("User not found.")

    existing = db.execute(
        select(Membership).where(
            Membership.company_id == company_id, Membership.user_id == user_id
        )
    ).scalar_one_or_none()

    if existing:
        existing.role = role
        existing.branch_id = branch_id
        if is_default:
            _clear_default(db, user_id)
            existing.is_default = True
        db.flush()
        return existing

    if is_default:
        _clear_default(db, user_id)

    membership = Membership(
        company_id=company_id,
        user_id=user_id,
        role=role,
        branch_id=branch_id,
        is_default=is_default,
    )
    db.add(membership)
    db.flush()
    return membership


def _clear_default(db: Session, user_id: str) -> None:
    for membership in db.execute(
        select(Membership).where(Membership.user_id == user_id, Membership.is_default.is_(True))
    ).scalars():
        membership.is_default = False


def remove_member(db: Session, company_id: str, user_id: str) -> None:
    """Remove a user from a company, refusing to strand it without an owner."""
    membership = db.execute(
        select(Membership).where(
            Membership.company_id == company_id, Membership.user_id == user_id
        )
    ).scalar_one_or_none()
    if membership is None:
        raise NotFoundError("Membership not found.")

    if membership.role == UserRole.OWNER:
        owner_count = int(
            db.execute(
                select(func.count(Membership.id)).where(
                    Membership.company_id == company_id, Membership.role == UserRole.OWNER
                )
            ).scalar_one()
        )
        if owner_count <= 1:
            raise ConflictError("A company must always have at least one owner.")

    db.delete(membership)
    db.flush()


def user_companies(db: Session, user_id: str) -> list[tuple[Company, str]]:
    """Companies a user belongs to, with their role in each."""
    rows = db.execute(
        select(Company, Membership.role)
        .join(Membership, Membership.company_id == Company.id)
        .where(Membership.user_id == user_id, Company.is_active.is_(True))
        .order_by(Membership.is_default.desc(), Company.name.asc())
    ).all()
    return [(row[0], row[1]) for row in rows]


def get_company(db: Session, company_id: str) -> Company:
    company = db.get(Company, company_id)
    if company is None or not company.is_active:
        raise NotFoundError("Company not found.")
    return company


def update_company(db: Session, company_id: str, updates: dict, *, user_id: str | None) -> Company:
    """Update company profile, validating tax identifiers and auditing changes."""
    from app.services.audit import record_event

    company = get_company(db, company_id)
    before = {
        "name": company.name,
        "legal_name": company.legal_name,
        "gstin": company.gstin,
        "pan": company.pan,
        "state_code": company.state_code,
        "currency": company.currency,
    }

    if "gstin" in updates and updates["gstin"]:
        valid, message = validate_gstin(updates["gstin"])
        if not valid:
            raise ValidationError(f"GSTIN rejected: {message}", code="invalid_gstin")
    if "pan" in updates and updates["pan"]:
        valid, message = validate_pan(updates["pan"])
        if not valid:
            raise ValidationError(f"PAN rejected: {message}", code="invalid_pan")

    editable = {
        "name", "legal_name", "business_type", "state", "state_code", "address_line1",
        "address_line2", "city", "pincode", "email", "phone", "website", "gstin", "pan",
        "tan", "cin", "timezone", "logo_path", "signature_path",
    }
    for key, value in updates.items():
        if key in editable and value is not None:
            setattr(company, key, value)

    db.flush()
    record_event(
        db,
        company_id=company.id,
        action=AuditAction.UPDATE,
        entity_type="Company",
        entity_id=company.id,
        entity_label=company.name,
        summary="Company profile updated",
        actor_type=ActorType.USER if user_id else ActorType.SYSTEM,
        actor_id=user_id,
        before=before,
        after={
            "name": company.name,
            "legal_name": company.legal_name,
            "gstin": company.gstin,
            "pan": company.pan,
            "state_code": company.state_code,
            "currency": company.currency,
        },
    )
    return company
