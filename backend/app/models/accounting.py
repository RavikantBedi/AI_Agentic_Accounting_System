"""Chart of accounts, fiscal calendar and the general ledger.

The tables here are the source of truth for every number the product reports.
No AI output ever reaches them without passing the accounting engine first.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Money
from app.enums import (
    AccountType,
    EntrySource,
    JournalStatus,
    NormalBalance,
    PeriodStatus,
    VoucherType,
)
from app.models.base import Base, CompanyScoped, TimestampMixin, UTCDateTime, UUIDPk


class Account(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """A chart-of-accounts node. Hierarchical; only leaf accounts are postable."""

    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_account_code"),
        Index("ix_account_type", "company_id", "type", "is_active"),
        CheckConstraint("normal_balance IN ('DEBIT','CREDIT')", name="ck_account_normal_balance"),
    )

    code: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    subtype: Mapped[str] = mapped_column(String(40), nullable=False)
    normal_balance: Mapped[str] = mapped_column(String(6), nullable=False)

    parent_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    path: Mapped[str] = mapped_column(String(400), default="", index=True)  # 1000/1100/1110
    depth: Mapped[int] = mapped_column(Integer, default=0)
    is_group: Mapped[bool] = mapped_column(Boolean, default=False)  # group accounts are not postable

    currency: Mapped[str] = mapped_column(String(3), default="INR")
    tax_category: Mapped[str] = mapped_column(String(40), default="")
    hsn_sac: Mapped[str] = mapped_column(String(10), default="")

    # System accounts are referenced by the engine by role and cannot be deleted.
    system_key: Mapped[str | None] = mapped_column(String(60), nullable=True, index=True)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_reconcilable: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str] = mapped_column(Text, default="")

    parent: Mapped["Account | None"] = relationship(remote_side="Account.id", back_populates="children")
    children: Mapped[list["Account"]] = relationship(back_populates="parent")

    @property
    def is_debit_normal(self) -> bool:
        return self.normal_balance == NormalBalance.DEBIT

    @property
    def is_balance_sheet(self) -> bool:
        return self.type in (AccountType.ASSET, AccountType.LIABILITY, AccountType.EQUITY)

    @property
    def is_profit_and_loss(self) -> bool:
        return self.type in (AccountType.REVENUE, AccountType.EXPENSE)


class FiscalYear(Base, UUIDPk, CompanyScoped, TimestampMixin):
    __tablename__ = "fiscal_years"
    __table_args__ = (UniqueConstraint("company_id", "name", name="uq_fiscal_year"),)

    name: Mapped[str] = mapped_column(String(9), nullable=False)  # 2025-26
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(10), default=PeriodStatus.OPEN)

    periods: Mapped[list["AccountingPeriod"]] = relationship(
        back_populates="fiscal_year", cascade="all, delete-orphan"
    )


class AccountingPeriod(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """One accounting month. Postings are only permitted into OPEN periods."""

    __tablename__ = "accounting_periods"
    __table_args__ = (
        UniqueConstraint("company_id", "year", "month", name="uq_period"),
        Index("ix_period_range", "company_id", "start_date", "end_date"),
    )

    fiscal_year_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("fiscal_years.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(30), nullable=False)  # "Apr 2025"
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(10), default=PeriodStatus.OPEN, index=True)

    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    closed_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    close_notes: Mapped[str] = mapped_column(Text, default="")

    fiscal_year: Mapped[FiscalYear] = relationship(back_populates="periods")

    @property
    def is_open(self) -> bool:
        return self.status == PeriodStatus.OPEN


class JournalEntry(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """A balanced voucher. Once POSTED it is immutable — corrections reverse it."""

    __tablename__ = "journal_entries"
    __table_args__ = (
        UniqueConstraint("company_id", "entry_number", name="uq_journal_number"),
        Index("ix_journal_date", "company_id", "entry_date", "status"),
        Index("ix_journal_source", "company_id", "source_type", "source_id"),
        Index("ix_journal_voucher", "company_id", "voucher_type", "entry_date"),
    )

    entry_number: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    voucher_type: Mapped[str] = mapped_column(String(20), default=VoucherType.JOURNAL, nullable=False)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    period_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("accounting_periods.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    branch_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("branches.id", ondelete="SET NULL"), nullable=True, index=True
    )

    narration: Mapped[str] = mapped_column(Text, default="")
    reference: Mapped[str] = mapped_column(String(120), default="", index=True)
    currency: Mapped[str] = mapped_column(String(3), default="INR")

    total_debit: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"), nullable=False)
    total_credit: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"), nullable=False)

    status: Mapped[str] = mapped_column(String(20), default=JournalStatus.DRAFT, nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(20), default=EntrySource.MANUAL, nullable=False)

    # Link back to the business document that produced this entry.
    source_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    source_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # AI provenance — recorded, never trusted as authorisation.
    ai_proposal_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    ai_confidence: Mapped[Decimal | None] = mapped_column(Money, nullable=True)

    created_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    posted_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    # Correction chain: a posted entry is never edited, only reversed.
    reverses_entry_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("journal_entries.id", ondelete="SET NULL"), nullable=True
    )
    reversed_by_entry_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_reversal: Mapped[bool] = mapped_column(Boolean, default=False)
    is_opening: Mapped[bool] = mapped_column(Boolean, default=False)
    is_adjustment: Mapped[bool] = mapped_column(Boolean, default=False)

    lines: Mapped[list["JournalLine"]] = relationship(
        back_populates="entry",
        cascade="all, delete-orphan",
        order_by="JournalLine.line_number",
    )

    @property
    def is_posted(self) -> bool:
        return self.status == JournalStatus.POSTED

    @property
    def is_balanced(self) -> bool:
        return self.total_debit == self.total_credit


class JournalLine(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """One side of a double-entry. Exactly one of debit/credit is non-zero."""

    __tablename__ = "journal_lines"
    __table_args__ = (
        Index("ix_line_account", "company_id", "account_id", "entry_date"),
        Index("ix_line_entry", "entry_id", "line_number"),
        Index("ix_line_party", "company_id", "party_type", "party_id"),
        CheckConstraint("line_number > 0", name="ck_line_number_positive"),
    )

    entry_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("journal_entries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    account_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    debit: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"), nullable=False)
    credit: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"), nullable=False)

    description: Mapped[str] = mapped_column(Text, default="")

    # Denormalised for fast ledger queries without joining the header.
    entry_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default=JournalStatus.DRAFT, nullable=False, index=True)

    # Dimensions
    branch_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    cost_center_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("cost_centers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    department_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    project: Mapped[str] = mapped_column(String(120), default="")

    # Sub-ledger linkage (AR/AP/inventory/bank)
    party_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    party_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    bank_account_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    item_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Tax metadata for GST reporting straight off the ledger
    tax_component: Mapped[str | None] = mapped_column(String(10), nullable=True)
    tax_rate: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    taxable_value: Mapped[Decimal | None] = mapped_column(Money, nullable=True)

    # Multi-currency
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    fx_rate: Mapped[Decimal] = mapped_column(Money, default=Decimal("1.00"))
    fc_amount: Mapped[Decimal | None] = mapped_column(Money, nullable=True)

    reconciled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    reconciled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    entry: Mapped[JournalEntry] = relationship(back_populates="lines")
    account: Mapped[Account] = relationship()

    @property
    def signed_amount(self) -> Decimal:
        """Debit-positive signed amount."""
        return self.debit - self.credit


class OpeningBalance(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """Opening balances captured at migration, posted through a normal journal."""

    __tablename__ = "opening_balances"
    __table_args__ = (UniqueConstraint("company_id", "account_id", "as_of_date", name="uq_opening"),)

    account_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    debit: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    credit: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    journal_entry_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class JournalTemplate(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """Reusable journal skeleton for recurring/manual entries."""

    __tablename__ = "journal_templates"
    __table_args__ = (UniqueConstraint("company_id", "name", name="uq_journal_template"),)

    name: Mapped[str] = mapped_column(String(150), nullable=False)
    voucher_type: Mapped[str] = mapped_column(String(20), default=VoucherType.JOURNAL)
    narration: Mapped[str] = mapped_column(Text, default="")
    lines: Mapped[list] = mapped_column(default=list)  # [{account_code, debit, credit, description}]
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[str] = mapped_column(Text, default="")
