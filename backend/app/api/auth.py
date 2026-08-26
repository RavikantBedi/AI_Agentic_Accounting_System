"""Authentication endpoints.

Login is rate-limited per IP+email, failed attempts lock the account
progressively, and both success and failure are audited. Error messages never
reveal whether an email exists.
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select

from app.config import settings
from app.database import get_db
from app.deps import CurrentUserDep, DbDep, audit_context
from app.enums import ActorType, AuditAction, UserRole
from app.errors import ConflictError, RateLimited, Unauthorized, ValidationError
from app.models import Company, Membership, User
from app.models.base import utcnow
from app.schemas import (
    ChangePasswordRequest,
    CompanyOut,
    LoginRequest,
    MessageResponse,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserOut,
)
from app.security import (
    create_token,
    decode_token,
    hash_password,
    needs_rehash,
    password_strength,
    rate_limiter,
    verify_password,
)
from app.services.audit import record_event
from app.services.company import create_company, user_companies

router = APIRouter(prefix="/auth", tags=["Authentication"])

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15


@router.post("/register", response_model=TokenResponse, status_code=201)
def register(payload: RegisterRequest, request: Request, db: DbDep) -> TokenResponse:
    """Create the first user and, optionally, their company."""
    allowed, retry_after = rate_limiter.check(
        f"register:{_client_ip(request)}", limit=5, window_seconds=300
    )
    if not allowed:
        raise RateLimited(f"Too many sign-up attempts. Try again in {retry_after} seconds.")

    strong, problems = password_strength(payload.password)
    if not strong:
        raise ValidationError("Password is not strong enough.", details={"problems": problems})

    email = payload.email.lower().strip()
    existing = db.execute(select(User).where(func.lower(User.email) == email)).scalar_one_or_none()
    if existing is not None:
        raise ConflictError("An account with this email already exists.", code="email_taken")

    user = User(
        email=email,
        full_name=payload.full_name.strip(),
        password_hash=hash_password(payload.password),
    )
    db.add(user)
    db.flush()

    company = None
    role = None
    if payload.company_name:
        company = create_company(
            db,
            name=payload.company_name,
            legal_name=payload.company_name,
            owner_user_id=user.id,
        )
        role = UserRole.OWNER.value

    record_event(
        db,
        company_id=company.id if company else "",
        action=AuditAction.CREATE,
        entity_type="User",
        entity_id=user.id,
        entity_label=user.email,
        summary=f"Account created for {user.email}",
        actor_type=ActorType.USER,
        actor_id=user.id,
        actor_label=user.email,
        **audit_context(request),
    )

    return _issue_tokens(db, user, company, role)


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, request: Request, db: DbDep) -> TokenResponse:
    email = payload.email.lower().strip()
    ip = _client_ip(request)

    allowed, retry_after = rate_limiter.check(
        f"login:{ip}:{email}", limit=settings.AUTH_RATE_LIMIT_PER_MINUTE, window_seconds=60
    )
    if not allowed:
        raise RateLimited(
            f"Too many sign-in attempts. Try again in {retry_after} seconds.",
            details={"retry_after": retry_after},
        )

    user = db.execute(select(User).where(func.lower(User.email) == email)).scalar_one_or_none()

    # Uniform failure message: never disclose whether the email exists.
    invalid = Unauthorized("Incorrect email or password.", code="invalid_credentials")

    if user is None:
        # Still spend the hashing time so timing does not leak account existence.
        verify_password(payload.password, hash_password("timing-equaliser-value"))
        raise invalid

    if user.locked_until and user.locked_until > utcnow():
        remaining = int((user.locked_until - utcnow()).total_seconds() / 60) + 1
        raise Unauthorized(
            f"This account is temporarily locked after repeated failed sign-ins. "
            f"Try again in {remaining} minute(s).",
            code="account_locked",
        )

    if not user.is_active:
        raise Unauthorized("This account has been deactivated.", code="user_inactive")

    if not verify_password(payload.password, user.password_hash):
        user.failed_login_count += 1
        if user.failed_login_count >= MAX_FAILED_ATTEMPTS:
            user.locked_until = utcnow() + timedelta(minutes=LOCKOUT_MINUTES)
            user.failed_login_count = 0
        db.flush()

        memberships = user_companies(db, user.id)
        record_event(
            db,
            company_id=memberships[0][0].id if memberships else "",
            action=AuditAction.LOGIN_FAILED,
            entity_type="User",
            entity_id=user.id,
            entity_label=user.email,
            summary=f"Failed sign-in for {user.email}",
            actor_type=ActorType.USER,
            actor_id=user.id,
            actor_label=user.email,
            **audit_context(request),
        )
        raise invalid

    # Transparently upgrade the hash if the iteration count has been raised.
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = utcnow()
    db.flush()
    rate_limiter.reset(f"login:{ip}:{email}")

    company, role = _resolve_company(db, user, payload.company_id)

    if company:
        record_event(
            db,
            company_id=company.id,
            action=AuditAction.LOGIN,
            entity_type="User",
            entity_id=user.id,
            entity_label=user.email,
            summary=f"{user.email} signed in",
            actor_type=ActorType.USER,
            actor_id=user.id,
            actor_label=user.email,
            **audit_context(request),
        )

    return _issue_tokens(db, user, company, role)


@router.post("/refresh", response_model=TokenResponse)
def refresh(payload: RefreshRequest, db: DbDep) -> TokenResponse:
    claims = decode_token(payload.refresh_token, expected_type="refresh")
    user = db.get(User, claims.get("sub", ""))
    if user is None or not user.is_active:
        raise Unauthorized("This account is no longer active.", code="user_inactive")
    if int(claims.get("tv", 0)) != int(user.token_version):
        raise Unauthorized("This session has been revoked.", code="token_revoked")

    company, role = _resolve_company(db, user, claims.get("company_id"))
    return _issue_tokens(db, user, company, role)


@router.post("/logout", response_model=MessageResponse)
def logout(request: Request, db: DbDep, current: CurrentUserDep) -> MessageResponse:
    record_event(
        db,
        company_id=current.company_id,
        action=AuditAction.LOGOUT,
        entity_type="User",
        entity_id=current.id,
        entity_label=current.email,
        summary=f"{current.email} signed out",
        actor_type=ActorType.USER,
        actor_id=current.id,
        actor_label=current.email,
        **audit_context(request),
    )
    return MessageResponse(message="Signed out.")


@router.post("/change-password", response_model=MessageResponse)
def change_password(
    payload: ChangePasswordRequest, request: Request, db: DbDep, current: CurrentUserDep
) -> MessageResponse:
    if not verify_password(payload.current_password, current.user.password_hash):
        raise Unauthorized("Your current password is incorrect.", code="invalid_credentials")

    strong, problems = password_strength(payload.new_password)
    if not strong:
        raise ValidationError("Password is not strong enough.", details={"problems": problems})
    if payload.new_password == payload.current_password:
        raise ValidationError("The new password must be different from the current one.")

    current.user.password_hash = hash_password(payload.new_password)
    current.user.must_change_password = False
    # Invalidate every existing session, including other devices.
    current.user.token_version += 1
    db.flush()

    record_event(
        db,
        company_id=current.company_id,
        action=AuditAction.UPDATE,
        entity_type="User",
        entity_id=current.id,
        entity_label=current.email,
        summary=f"{current.email} changed their password; all sessions were revoked",
        actor_type=ActorType.USER,
        actor_id=current.id,
        actor_label=current.email,
        **audit_context(request),
    )
    return MessageResponse(
        message="Password changed. You have been signed out of all other sessions."
    )


@router.get("/me", response_model=dict)
def me(db: DbDep, current: CurrentUserDep) -> dict:
    companies = user_companies(db, current.id)
    return {
        "user": UserOut.model_validate(current.user).model_dump(),
        "company": CompanyOut.model_validate(current.company).model_dump(mode="json"),
        "role": current.role,
        "permissions": sorted(current.permissions),
        "companies": [
            {
                "id": company.id,
                "name": company.name,
                "role": role,
                "is_demo": company.is_demo,
            }
            for company, role in companies
        ],
    }


# ------------------------------------------------------------- helpers -----

def _resolve_company(db, user: User, company_id: str | None):
    stmt = select(Membership).where(Membership.user_id == user.id)
    if company_id:
        stmt = stmt.where(Membership.company_id == company_id)
    else:
        stmt = stmt.order_by(Membership.is_default.desc(), Membership.created_at.asc())

    membership = db.execute(stmt).scalars().first()
    if membership is None:
        return None, None
    return db.get(Company, membership.company_id), membership.role


def _issue_tokens(db, user: User, company, role) -> TokenResponse:
    from app.services.policy import role_permissions

    access = create_token(
        user.id,
        token_type="access",
        company_id=company.id if company else None,
        role=role,
        token_version=user.token_version,
    )
    refresh_token = create_token(
        user.id,
        token_type="refresh",
        company_id=company.id if company else None,
        token_version=user.token_version,
    )

    permissions = sorted(role_permissions(role)) if role else []
    if user.is_superuser:
        from app.enums import Permission

        permissions = sorted(p.value for p in Permission)

    return TokenResponse(
        access_token=access,
        refresh_token=refresh_token,
        expires_in=settings.ACCESS_TOKEN_TTL_MINUTES * 60,
        user=UserOut.model_validate(user),
        company=CompanyOut.model_validate(company) if company else None,
        role=role,
        permissions=permissions,
    )


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
