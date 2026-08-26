"""Fixed assets, prepaids, accruals and recurring transactions.

All four share one idea: a schedule of dated amounts that each become a real
journal entry when their period arrives. The schedule itself is never a
substitute for a posting — the ledger stays the source of truth.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Money, Rate
from app.enums import AssetStatus, DepreciationMethod, ScheduleFrequency, ScheduleStatus, VoucherType
from app.models.base import Base, CompanyScoped, TimestampMixin, UUIDPk


class FixedAsset(Base, UUIDPk, CompanyScoped, TimestampMixin):
    __tablename__ = "fixed_assets"
    __table_args__ = (UniqueConstraint("company_id", "asset_code", name="uq_asset_code"),)

    asset_code: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(120), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    location: Mapped[str] = mapped_column(String(150), default="")
    branch_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cost_center_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    purchase_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    in_service_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    cost: Mapped[Decimal] = mapped_column(Money, nullable=False)
    residual_value: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    useful_life_months: Mapped[int] = mapped_column(Integer, default=60)
    depreciation_method: Mapped[str] = mapped_column(String(20), default=DepreciationMethod.STRAIGHT_LINE)
    depreciation_rate: Mapped[Decimal] = mapped_column(Rate, default=Decimal("0.000000"))  # WDV only

    accumulated_depreciation: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    net_book_value: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    status: Mapped[str] = mapped_column(String(25), default=AssetStatus.ACTIVE, index=True)

    asset_account_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    accumulated_depreciation_account_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    depreciation_expense_account_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    vendor_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    bill_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    serial_number: Mapped[str] = mapped_column(String(80), default="")

    disposal_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    disposal_proceeds: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    disposal_journal_entry_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    schedule: Mapped[list["DepreciationEntry"]] = relationship(
        cascade="all, delete-orphan", order_by="DepreciationEntry.period"
    )


class DepreciationEntry(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """One month of a depreciation schedule."""

    __tablename__ = "depreciation_entries"
    __table_args__ = (
        UniqueConstraint("asset_id", "period", name="uq_depreciation_period"),
        Index("ix_depreciation_period", "company_id", "period", "is_posted"),
    )

    asset_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("fixed_assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    period: Mapped[str] = mapped_column(String(7), nullable=False)  # 2025-04
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    accumulated_after: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    nbv_after: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    is_posted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    journal_entry_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class AmortisationSchedule(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """Prepaid expense or deferred revenue spread over future periods.

    Example: 120,000 insurance for 12 months => 10,000 recognised per month.
    """

    __tablename__ = "amortisation_schedules"
    __table_args__ = (Index("ix_amortisation_status", "company_id", "kind", "status"),)

    kind: Mapped[str] = mapped_column(String(20), default="PREPAID")  # PREPAID | DEFERRED_REVENUE
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")

    total_amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    recognised_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    periods: Mapped[int] = mapped_column(Integer, nullable=False)
    frequency: Mapped[str] = mapped_column(String(20), default=ScheduleFrequency.MONTHLY)

    balance_sheet_account_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    profit_loss_account_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    cost_center_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    source_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    source_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    vendor_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    status: Mapped[str] = mapped_column(String(20), default=ScheduleStatus.ACTIVE, index=True)
    detected_by_ai: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    entries: Mapped[list["ScheduleEntry"]] = relationship(
        cascade="all, delete-orphan", order_by="ScheduleEntry.period"
    )


class AccrualSchedule(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """Expense/revenue recognised before the invoice arrives; auto-reverses."""

    __tablename__ = "accrual_schedules"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(20), default="EXPENSE")  # EXPENSE | REVENUE

    amount_per_period: Mapped[Decimal] = mapped_column(Money, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    frequency: Mapped[str] = mapped_column(String(20), default=ScheduleFrequency.MONTHLY)

    expense_or_revenue_account_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    accrual_account_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    cost_center_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    vendor_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Standard practice: accrue on the last day of the period and reverse on the
    # first day of the next, so the real invoice posts cleanly.
    auto_reverse: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(20), default=ScheduleStatus.ACTIVE, index=True)
    detected_by_ai: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    entries: Mapped[list["ScheduleEntry"]] = relationship(
        cascade="all, delete-orphan", order_by="ScheduleEntry.period"
    )


class ScheduleEntry(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """One dated instalment of an amortisation or accrual schedule."""

    __tablename__ = "schedule_entries"
    __table_args__ = (
        Index("ix_schedule_entry_period", "company_id", "period", "is_posted"),
    )

    amortisation_schedule_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("amortisation_schedules.id", ondelete="CASCADE"), nullable=True, index=True
    )
    accrual_schedule_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("accrual_schedules.id", ondelete="CASCADE"), nullable=True, index=True
    )

    period: Mapped[str] = mapped_column(String(7), nullable=False)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    is_posted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    journal_entry_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reversal_journal_entry_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    posted_at: Mapped[date | None] = mapped_column(Date, nullable=True)


class RecurringTransaction(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """Template + cadence for rent, subscriptions, insurance, salary, etc."""

    __tablename__ = "recurring_transactions"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    doc_type: Mapped[str] = mapped_column(String(30), default=VoucherType.JOURNAL)
    frequency: Mapped[str] = mapped_column(String(20), default=ScheduleFrequency.MONTHLY)
    interval: Mapped[int] = mapped_column(Integer, default=1)
    day_of_month: Mapped[int] = mapped_column(Integer, default=1)

    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    next_run_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    last_run_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    occurrences_generated: Mapped[int] = mapped_column(Integer, default=0)
    max_occurrences: Mapped[int | None] = mapped_column(Integer, nullable=True)

    template: Mapped[dict] = mapped_column(default=dict)  # voucher payload
    amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    party_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    party_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Recurring entries are prepared automatically but still respect policy:
    # posting without review only happens if the policy explicitly allows it.
    auto_post: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default=ScheduleStatus.ACTIVE, index=True)
    created_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
