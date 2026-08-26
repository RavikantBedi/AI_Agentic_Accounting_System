"""FastAPI dependencies: authentication, company scoping and RBAC.

Every business endpoint depends on ``CurrentUser``, which carries the resolved
company and role. Permission checks are explicit at the route level — there is
no implicit "logged in means allowed".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Callable

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.enums import ActorType, AuditAction, UserRole
from app.errors import NotFoundError, PermissionDenied, Unauthorized
from app.models import Company, Membership, User
from app.security import decode_token
from app.services.policy import role_has_permission, role_permissions


@dataclass
class CurrentUser:
    """The authenticated principal plus its active company scope."""

    user: User
    company: Company
    role: str
    permissions: set[str]
    token_payload: dict

    @property
    def id(self) -> str:
        return self.user.id

    @property
    def company_id(self) -> str:
        return self.company.id

    @property
    def email(self) -> str:
        return self.user.email

    @property
    def full_name(self) -> str:
        return self.user.full_name

    def has(self, permission: str) -> bool:
        return permission in self.permissions

    def require(self, permission: str) -> None:
        if permission not in self.permissions:
            raise PermissionDenied(
                f"Your role ({self.role}) does not include the '{permission}' permission.",
                details={"required": permission, "role": self.role},
            )


def _extract_token(authorization: str | None) -> str:
    if not authorization:
        raise Unauthorized("Authentication is required.", code="missing_token")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise Unauthorized("Authorization header must be 'Bearer <token>'.", code="bad_auth_header")
    return parts[1].strip()


def get_current_user(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
    x_company_id: Annotated[str | None, Header(alias="X-Company-Id")] = None,
) -> CurrentUser:
    """Resolve the caller, their active company and their permissions."""
    token = _extract_token(authorization)
    payload = decode_token(token, expected_type="access")

    user = db.get(User, payload.get("sub", ""))
    if user is None or not user.is_active:
        raise Unauthorized("This account is no longer active.", code="user_inactive")

    # Bumping token_version invalidates every issued token for this user.
    if int(payload.get("tv", 0)) != int(user.token_version):
        raise Unauthorized(
            "Your session is no longer valid. Please sign in again.", code="token_revoked"
        )

    # The company header lets one session switch companies without re-login,
    # but only to a company the user actually belongs to.
    company_id = x_company_id or payload.get("company_id")
    membership = None

    if company_id:
        membership = db.execute(
            select(Membership).where(
                Membership.user_id == user.id, Membership.company_id == company_id
            )
        ).scalar_one_or_none()
        if membership is None:
            raise PermissionDenied(
                "You do not have access to that company.", code="company_access_denied"
            )
    else:
        membership = db.execute(
            select(Membership)
            .where(Membership.user_id == user.id)
            .order_by(Membership.is_default.desc(), Membership.created_at.asc())
        ).scalars().first()
        if membership is None:
            raise PermissionDenied(
                "Your account is not linked to any company yet.", code="no_company"
            )

    company = db.get(Company, membership.company_id)
    if company is None or not company.is_active:
        raise NotFoundError("Company not found.")

    permissions = role_permissions(membership.role)
    if user.is_superuser:
        from app.enums import Permission

        permissions = {p.value for p in Permission}

    request.state.user_id = user.id
    request.state.company_id = company.id

    return CurrentUser(
        user=user,
        company=company,
        role=membership.role,
        permissions=permissions,
        token_payload=payload,
    )


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]
DbDep = Annotated[Session, Depends(get_db)]


def require_permission(permission: str) -> Callable[..., CurrentUser]:
    """Route dependency factory enforcing one permission.

    A denial is recorded in the audit log — attempted privilege escalation is
    exactly the kind of thing an auditor needs to see.
    """

    def dependency(
        request: Request,
        db: DbDep,
        current: CurrentUserDep,
    ) -> CurrentUser:
        if permission not in current.permissions:
            from app.services.audit import record_event

            record_event(
                db,
                company_id=current.company_id,
                action=AuditAction.PERMISSION_DENIED,
                entity_type="Permission",
                entity_label=permission,
                summary=(
                    f"{current.email} ({current.role}) was denied '{permission}' on "
                    f"{request.method} {request.url.path}"
                ),
                actor_type=ActorType.USER,
                actor_id=current.id,
                actor_label=current.email,
                ip_address=_client_ip(request),
                request_id=getattr(request.state, "request_id", ""),
            )
            raise PermissionDenied(
                f"Your role ({current.role}) does not include the '{permission}' permission.",
                details={"required": permission, "role": current.role},
            )
        return current

    return dependency


def require_role(*roles: str) -> Callable[..., CurrentUser]:
    def dependency(current: CurrentUserDep) -> CurrentUser:
        if current.role not in roles and not current.user.is_superuser:
            raise PermissionDenied(
                f"This action requires one of: {', '.join(roles)}. Your role is {current.role}.",
                details={"required_roles": list(roles), "role": current.role},
            )
        return current

    return dependency


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    return request.client.host if request.client else ""


def audit_context(request: Request) -> dict:
    """Request metadata attached to audit events."""
    return {
        "request_id": getattr(request.state, "request_id", ""),
        "ip_address": _client_ip(request),
        "user_agent": request.headers.get("user-agent", "")[:300],
    }
