"""Company, organisational structure, users and system-wide policy."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Money, Rate
from app.enums import BusinessType, UserRole
from app.models.base import Base, CompanyScoped, SoftDelete, TimestampMixin, UTCDateTime, UUIDPk


class Company(Base, UUIDPk, TimestampMixin):
    __tablename__ = "companies"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    legal_name: Mapped[str] = mapped_column(String(250), nullable=False)
    business_type: Mapped[str] = mapped_column(String(30), default=BusinessType.PRIVATE_LIMITED)
    country: Mapped[str] = mapped_column(String(2), default="IN")
    state: Mapped[str] = mapped_column(String(100), default="")
    state_code: Mapped[str] = mapped_column(String(2), default="")  # GST state code
    address_line1: Mapped[str] = mapped_column(String(250), default="")
    address_line2: Mapped[str] = mapped_column(String(250), default="")
    city: Mapped[str] = mapped_column(String(100), default="")
    pincode: Mapped[str] = mapped_column(String(12), default="")
    email: Mapped[str] = mapped_column(String(200), default="")
    phone: Mapped[str] = mapped_column(String(30), default="")
    website: Mapped[str] = mapped_column(String(200), default="")

    gstin: Mapped[str] = mapped_column(String(15), default="", index=True)
    pan: Mapped[str] = mapped_column(String(10), default="")
    tan: Mapped[str] = mapped_column(String(10), default="")
    cin: Mapped[str] = mapped_column(String(21), default="")

    currency: Mapped[str] = mapped_column(String(3), default="INR")
    fiscal_year_start_month: Mapped[int] = mapped_column(Integer, default=4)  # April (India)
    fiscal_year_start_day: Mapped[int] = mapped_column(Integer, default=1)
    timezone: Mapped[str] = mapped_column(String(60), default="Asia/Kolkata")
    books_begin_on: Mapped[date | None] = mapped_column(Date, nullable=True)

    logo_path: Mapped[str] = mapped_column(String(400), default="")
    signature_path: Mapped[str] = mapped_column(String(400), default="")

    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    parent_company_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("companies.id", ondelete="SET NULL"), nullable=True
    )

    branches: Mapped[list["Branch"]] = relationship(back_populates="company", cascade="all, delete-orphan")


class Branch(Base, UUIDPk, CompanyScoped, TimestampMixin, SoftDelete):
    __tablename__ = "branches"
    __table_args__ = (UniqueConstraint("company_id", "code", name="uq_branch_code"),)

    code: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    state: Mapped[str] = mapped_column(String(100), default="")
    state_code: Mapped[str] = mapped_column(String(2), default="")
    gstin: Mapped[str] = mapped_column(String(15), default="")
    address: Mapped[str] = mapped_column(Text, default="")
    is_head_office: Mapped[bool] = mapped_column(Boolean, default=False)

    company: Mapped[Company] = relationship(back_populates="branches")


class Department(Base, UUIDPk, CompanyScoped, TimestampMixin, SoftDelete):
    __tablename__ = "departments"
    __table_args__ = (UniqueConstraint("company_id", "code", name="uq_department_code"),)

    code: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    head_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class CostCenter(Base, UUIDPk, CompanyScoped, TimestampMixin, SoftDelete):
    __tablename__ = "cost_centers"
    __table_args__ = (UniqueConstraint("company_id", "code", name="uq_cost_center_code"),)

    code: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    category: Mapped[str] = mapped_column(String(60), default="DEPARTMENT")  # DEPARTMENT/PROJECT/BRANCH
    parent_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("cost_centers.id", ondelete="SET NULL"), nullable=True
    )
    budget_owner: Mapped[str] = mapped_column(String(150), default="")


class User(Base, UUIDPk, TimestampMixin):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(250), unique=True, nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(150), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(400), nullable=False)
    phone: Mapped[str] = mapped_column(String(30), default="")

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)

    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    # Token invalidation: bumping this logs the user out everywhere.
    token_version: Mapped[int] = mapped_column(Integer, default=0)

    memberships: Mapped[list["Membership"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Membership(Base, UUIDPk, TimestampMixin):
    """A user's role within one company (multi-company support)."""

    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("user_id", "company_id", name="uq_membership"),
        Index("ix_membership_company", "company_id", "role"),
    )

    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    company_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(30), default=UserRole.VIEWER, nullable=False)
    branch_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("branches.id", ondelete="SET NULL"), nullable=True
    )
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped[User] = relationship(back_populates="memberships")
    company: Mapped[Company] = relationship()


class Currency(Base, UUIDPk, TimestampMixin):
    __tablename__ = "currencies"

    code: Mapped[str] = mapped_column(String(3), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    symbol: Mapped[str] = mapped_column(String(6), default="")
    decimal_places: Mapped[int] = mapped_column(Integer, default=2)


class ExchangeRate(Base, UUIDPk, TimestampMixin):
    """Exchange-rate abstraction. Rates are data, never hardcoded constants."""

    __tablename__ = "exchange_rates"
    __table_args__ = (
        UniqueConstraint("base_currency", "quote_currency", "rate_date", name="uq_fx_rate"),
        Index("ix_fx_lookup", "base_currency", "quote_currency", "rate_date"),
    )

    base_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    quote_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    rate_date: Mapped[date] = mapped_column(Date, nullable=False)
    rate: Mapped[Decimal] = mapped_column(Rate, nullable=False)
    source: Mapped[str] = mapped_column(String(60), default="MANUAL")


class NumberSequence(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """Gap-free, race-safe document numbering (INV-2025-0001 …)."""

    __tablename__ = "number_sequences"
    __table_args__ = (
        UniqueConstraint("company_id", "key", "fiscal_year", name="uq_number_sequence"),
    )

    key: Mapped[str] = mapped_column(String(50), nullable=False)  # e.g. SALES_INVOICE
    prefix: Mapped[str] = mapped_column(String(20), default="")
    fiscal_year: Mapped[str] = mapped_column(String(9), default="")  # 2025-26
    padding: Mapped[int] = mapped_column(Integer, default=4)
    next_value: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class Policy(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """Configurable business policy. Business rules are data, never code constants.

    Holds approval thresholds, AI autonomy limits, matching tolerances, etc.
    Every change is versioned and audited.
    """

    __tablename__ = "policies"
    __table_args__ = (UniqueConstraint("company_id", "key", "version", name="uq_policy_version"),)

    key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    value: Mapped[dict] = mapped_column(nullable=False, default=dict)
    description: Mapped[str] = mapped_column(Text, default="")
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    changed_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class ApprovalRule(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """Threshold-based approval routing. Amounts are configuration, not constants."""

    __tablename__ = "approval_rules"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    min_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    max_amount: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    required_role: Mapped[str] = mapped_column(String(30), default=UserRole.ACCOUNTANT)
    auto_approve: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_dual_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    min_ai_confidence: Mapped[Decimal] = mapped_column(Rate, default=Decimal("0.900000"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
