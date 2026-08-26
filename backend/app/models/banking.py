"""Bank accounts, imported statement lines and reconciliation state."""
from __future__ import annotations

from datetime import date, datetime
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
from app.enums import BankAccountType, MatchStrategy, ReconStatus
from app.models.base import Base, CompanyScoped, SoftDelete, TimestampMixin, UTCDateTime, UUIDPk


class BankAccount(Base, UUIDPk, CompanyScoped, TimestampMixin, SoftDelete):
    __tablename__ = "bank_accounts"
    __table_args__ = (
        UniqueConstraint("company_id", "account_number", name="uq_bank_account_number"),
    )

    name: Mapped[str] = mapped_column(String(150), nullable=False)
    bank_name: Mapped[str] = mapped_column(String(150), nullable=False)
    account_number: Mapped[str] = mapped_column(String(40), nullable=False)
    ifsc: Mapped[str] = mapped_column(String(11), default="")
    swift: Mapped[str] = mapped_column(String(11), default="")
    branch_name: Mapped[str] = mapped_column(String(150), default="")
    account_type: Mapped[str] = mapped_column(String(20), default=BankAccountType.CURRENT)
    currency: Mapped[str] = mapped_column(String(3), default="INR")

    # The GL account this bank account posts to. The ledger is the source of
    # truth for the balance; the fields below only describe the *bank's* view.
    gl_account_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    branch_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    opening_balance: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    opening_balance_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Last balance reported *by the bank* (statement closing balance), if known.
    statement_balance: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    statement_balance_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    provider: Mapped[str] = mapped_column(String(30), default="file")  # file | mock | <live provider>
    provider_account_ref: Mapped[str] = mapped_column(String(120), default="")
    last_synced_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    overdraft_limit: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    notes: Mapped[str] = mapped_column(Text, default="")


class BankStatement(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """One imported statement file (or provider sync batch)."""

    __tablename__ = "bank_statements"

    bank_account_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("bank_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    file_name: Mapped[str] = mapped_column(String(300), default="")
    file_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    source: Mapped[str] = mapped_column(String(30), default="CSV")  # CSV | XLSX | JSON | PROVIDER
    provider: Mapped[str] = mapped_column(String(30), default="file")
    provider_mode: Mapped[str] = mapped_column(String(10), default="MOCK")

    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    opening_balance: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    closing_balance: Mapped[Decimal | None] = mapped_column(Money, nullable=True)

    rows_total: Mapped[int] = mapped_column(Integer, default=0)
    rows_imported: Mapped[int] = mapped_column(Integer, default=0)
    rows_duplicate: Mapped[int] = mapped_column(Integer, default=0)
    rows_rejected: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list] = mapped_column(default=list)

    imported_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    transactions: Mapped[list["BankTransaction"]] = relationship(back_populates="statement")


class BankTransaction(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """A line as reported by the bank. Never edited — it is external evidence."""

    __tablename__ = "bank_transactions"
    __table_args__ = (
        # Idempotent imports: the same physical bank line can never land twice.
        UniqueConstraint("bank_account_id", "dedupe_hash", name="uq_bank_txn_dedupe"),
        Index("ix_bank_txn_date", "company_id", "bank_account_id", "txn_date"),
        Index("ix_bank_txn_status", "company_id", "recon_status", "txn_date"),
        Index("ix_bank_txn_amount", "company_id", "amount"),
    )

    bank_account_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("bank_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    statement_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("bank_statements.id", ondelete="SET NULL"), nullable=True
    )

    txn_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    value_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    normalised_description: Mapped[str] = mapped_column(Text, default="", index=True)
    reference: Mapped[str] = mapped_column(String(120), default="", index=True)
    counterparty: Mapped[str] = mapped_column(String(200), default="", index=True)

    direction: Mapped[str] = mapped_column(String(6), nullable=False)  # CREDIT (in) / DEBIT (out)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)  # always positive
    running_balance: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="INR")

    dedupe_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    recon_status: Mapped[str] = mapped_column(
        String(20), default=ReconStatus.UNMATCHED, nullable=False, index=True
    )
    matched_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))

    # AI categorisation suggestion — a *proposal*, never an authorisation.
    suggested_account_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    suggested_category: Mapped[str] = mapped_column(String(120), default="")
    suggested_confidence: Mapped[Decimal | None] = mapped_column(Rate, nullable=True)
    categorised_by: Mapped[str] = mapped_column(String(20), default="")  # RULE | AI | HUMAN

    journal_entry_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    raw: Mapped[dict] = mapped_column(default=dict)

    statement: Mapped[BankStatement | None] = relationship(back_populates="transactions")
    matches: Mapped[list["ReconciliationMatch"]] = relationship(
        back_populates="bank_transaction", cascade="all, delete-orphan"
    )

    @property
    def signed_amount(self) -> Decimal:
        return self.amount if self.direction == "CREDIT" else -self.amount

    @property
    def unmatched_amount(self) -> Decimal:
        return self.amount - self.matched_amount


class ReconciliationSession(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """One reconciliation run over a bank account and date window."""

    __tablename__ = "reconciliation_sessions"

    bank_account_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("bank_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)

    total_transactions: Mapped[int] = mapped_column(Integer, default=0)
    matched_count: Mapped[int] = mapped_column(Integer, default=0)
    partial_count: Mapped[int] = mapped_column(Integer, default=0)
    unmatched_count: Mapped[int] = mapped_column(Integer, default=0)
    ambiguous_count: Mapped[int] = mapped_column(Integer, default=0)
    exception_count: Mapped[int] = mapped_column(Integer, default=0)
    auto_matched_count: Mapped[int] = mapped_column(Integer, default=0)

    book_balance: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    bank_balance: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    difference: Mapped[Decimal | None] = mapped_column(Money, nullable=True)

    used_ai: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_provider: Mapped[str] = mapped_column(String(30), default="")
    run_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")


class ReconciliationMatch(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """Evidence-bearing link between a bank line and a ledger line.

    Never created silently by AI: anything below the auto-accept threshold lands
    in the review queue with its evidence attached.
    """

    __tablename__ = "reconciliation_matches"
    __table_args__ = (
        Index("ix_match_bank_txn", "bank_transaction_id", "is_confirmed"),
        Index("ix_match_line", "journal_line_id"),
    )

    session_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("reconciliation_sessions.id", ondelete="SET NULL"), nullable=True
    )
    bank_transaction_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("bank_transactions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    journal_line_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("journal_lines.id", ondelete="CASCADE"), nullable=True, index=True
    )

    # Optional business-document anchors (invoice/bill/receipt/payment)
    document_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    document_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    strategy: Mapped[str] = mapped_column(String(30), default=MatchStrategy.FUZZY_SCORE)
    confidence: Mapped[Decimal] = mapped_column(Rate, default=Decimal("0.000000"))
    status: Mapped[str] = mapped_column(String(20), default=ReconStatus.MATCHED)

    # Human-readable, click-through evidence rendered directly in the UI.
    evidence: Mapped[dict] = mapped_column(default=dict)
    reason: Mapped[str] = mapped_column(Text, default="")

    is_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    confirmed_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    proposed_by: Mapped[str] = mapped_column(String(20), default="ENGINE")  # ENGINE | AI | HUMAN

    bank_transaction: Mapped[BankTransaction] = relationship(back_populates="matches")


class CategorisationRule(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """Deterministic rules learned from confirmed human decisions.

    These run *before* the LLM: most bank lines never need a model at all.
    """

    __tablename__ = "categorisation_rules"
    __table_args__ = (Index("ix_rule_lookup", "company_id", "is_active", "priority"),)

    name: Mapped[str] = mapped_column(String(150), default="")
    match_type: Mapped[str] = mapped_column(String(20), default="CONTAINS")  # CONTAINS|REGEX|EXACT|PREFIX
    pattern: Mapped[str] = mapped_column(String(300), nullable=False)
    field: Mapped[str] = mapped_column(String(30), default="description")
    direction: Mapped[str | None] = mapped_column(String(6), nullable=True)

    account_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    party_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    party_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cost_center_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    priority: Mapped[int] = mapped_column(Integer, default=100)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    times_applied: Mapped[int] = mapped_column(Integer, default=0)
    times_corrected: Mapped[int] = mapped_column(Integer, default=0)
    created_from: Mapped[str] = mapped_column(String(20), default="HUMAN")  # HUMAN | LEARNED
