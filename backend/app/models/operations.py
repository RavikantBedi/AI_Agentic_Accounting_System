"""Expenses, documents, budgets, month-end close and notifications."""
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
from app.enums import (
    CloseTaskStatus,
    DocumentKind,
    DocumentStatus,
    ExtractionStatus,
    NotificationKind,
    PaymentMethod,
    PeriodStatus,
    Severity,
)
from app.models.base import Base, CompanyScoped, TimestampMixin, UTCDateTime, UUIDPk


# ------------------------------------------------------------- expenses ----

class ExpenseClaim(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """Employee or vendor expense, from submission through reimbursement."""

    __tablename__ = "expense_claims"
    __table_args__ = (
        UniqueConstraint("company_id", "number", name="uq_expense_number"),
        Index("ix_expense_status", "company_id", "status", "expense_date"),
    )

    number: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    expense_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    claimant_type: Mapped[str] = mapped_column(String(20), default="EMPLOYEE")  # EMPLOYEE | VENDOR
    employee_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    vendor_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    category: Mapped[str] = mapped_column(String(120), default="")
    expense_account_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cost_center_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    branch_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    total: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    is_billable: Mapped[bool] = mapped_column(Boolean, default=False)
    customer_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    payment_method: Mapped[str] = mapped_column(String(20), default=PaymentMethod.CASH)
    paid_by_company: Mapped[bool] = mapped_column(Boolean, default=False)
    reimbursed: Mapped[bool] = mapped_column(Boolean, default=False)
    reimbursed_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    status: Mapped[str] = mapped_column(String(20), default=DocumentStatus.DRAFT, index=True)
    document_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ai_category_confidence: Mapped[Decimal | None] = mapped_column(Rate, nullable=True)
    ai_suggested_category: Mapped[str] = mapped_column(String(120), default="")

    submitted_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    approved_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    journal_entry_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")


# ------------------------------------------------------------ documents ----

class StoredDocument(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """An uploaded file plus its extraction state.

    ``content_hash`` is unique per company: re-uploading the same PDF is
    detected instead of silently creating a second bill.
    """

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("company_id", "content_hash", name="uq_document_hash"),
        Index("ix_document_status", "company_id", "extraction_status", "created_at"),
    )

    filename: Mapped[str] = mapped_column(String(300), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(500), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(120), default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    kind: Mapped[str] = mapped_column(String(30), default=DocumentKind.OTHER, index=True)
    detected_kind: Mapped[str] = mapped_column(String(30), default="")
    page_count: Mapped[int] = mapped_column(Integer, default=0)

    extraction_status: Mapped[str] = mapped_column(
        String(20), default=ExtractionStatus.UPLOADED, index=True
    )
    extraction_method: Mapped[str] = mapped_column(String(30), default="")  # PDF_TEXT | OCR | AI
    extracted_text: Mapped[str] = mapped_column(Text, default="")
    extracted_fields: Mapped[dict] = mapped_column(default=dict)
    field_confidence: Mapped[dict] = mapped_column(default=dict)
    overall_confidence: Mapped[Decimal | None] = mapped_column(Rate, nullable=True)
    extraction_errors: Mapped[list] = mapped_column(default=list)
    needs_review_reasons: Mapped[list] = mapped_column(default=list)

    # Linkage to whatever the document became.
    linked_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    linked_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    party_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    party_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    uploaded_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reviewed_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


# --------------------------------------------------------------- budgets ---

class Budget(Base, UUIDPk, CompanyScoped, TimestampMixin):
    __tablename__ = "budgets"
    __table_args__ = (UniqueConstraint("company_id", "name", "fiscal_year", name="uq_budget"),)

    name: Mapped[str] = mapped_column(String(150), nullable=False)
    fiscal_year: Mapped[str] = mapped_column(String(9), nullable=False, index=True)
    scope: Mapped[str] = mapped_column(String(20), default="COMPANY")  # COMPANY|DEPARTMENT|COST_CENTER
    cost_center_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    department_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    branch_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    total_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str] = mapped_column(Text, default="")

    lines: Mapped[list["BudgetLine"]] = relationship(cascade="all, delete-orphan")


class BudgetLine(Base, UUIDPk, CompanyScoped, TimestampMixin):
    __tablename__ = "budget_lines"
    __table_args__ = (
        UniqueConstraint("budget_id", "account_id", "period", name="uq_budget_line"),
        Index("ix_budget_line_period", "company_id", "period"),
    )

    budget_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("budgets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    period: Mapped[str] = mapped_column(String(7), nullable=False)  # 2025-04
    amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    notes: Mapped[str] = mapped_column(String(250), default="")


# ----------------------------------------------------------------- close ---

class CloseChecklist(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """Month-end close run for one accounting period."""

    __tablename__ = "close_checklists"
    __table_args__ = (UniqueConstraint("company_id", "period_id", name="uq_close_period"),)

    period_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("accounting_periods.id", ondelete="CASCADE"), nullable=False, index=True
    )
    period_label: Mapped[str] = mapped_column(String(20), default="")
    status: Mapped[str] = mapped_column(String(20), default=PeriodStatus.OPEN, index=True)

    total_tasks: Mapped[int] = mapped_column(Integer, default=0)
    completed_tasks: Mapped[int] = mapped_column(Integer, default=0)
    blocked_tasks: Mapped[int] = mapped_column(Integer, default=0)
    progress_percent: Mapped[Decimal] = mapped_column(Rate, default=Decimal("0.000000"))

    ai_summary: Mapped[str] = mapped_column(Text, default="")
    ai_findings: Mapped[list] = mapped_column(default=list)
    ai_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    started_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    approved_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    tasks: Mapped[list["CloseTask"]] = relationship(
        cascade="all, delete-orphan", order_by="CloseTask.display_order"
    )


class CloseTask(Base, UUIDPk, CompanyScoped, TimestampMixin):
    __tablename__ = "close_tasks"
    __table_args__ = (Index("ix_close_task_status", "checklist_id", "status"),)

    checklist_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("close_checklists.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(60), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(40), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    display_order: Mapped[int] = mapped_column(Integer, default=100)

    status: Mapped[str] = mapped_column(String(25), default=CloseTaskStatus.NOT_STARTED, index=True)
    is_blocking: Mapped[bool] = mapped_column(Boolean, default=True)

    # Every task is backed by a real, re-runnable check — never a manual tick.
    check_result: Mapped[dict] = mapped_column(default=dict)
    issue_count: Mapped[int] = mapped_column(Integer, default=0)
    amount_at_risk: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    assigned_to_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    completed_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")


# --------------------------------------------------------- notifications ---

class Notification(Base, UUIDPk, CompanyScoped, TimestampMixin):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notification_unread", "company_id", "is_read", "created_at"),)

    kind: Mapped[str] = mapped_column(String(40), default=NotificationKind.SYSTEM, index=True)
    severity: Mapped[str] = mapped_column(String(10), default=Severity.INFO, index=True)
    title: Mapped[str] = mapped_column(String(250), nullable=False)
    message: Mapped[str] = mapped_column(Text, default="")

    entity_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action_url: Mapped[str] = mapped_column(String(300), default="")
    amount: Mapped[Decimal | None] = mapped_column(Money, nullable=True)

    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(200), default="", index=True)


class ImportBatch(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """One run of the universal import wizard."""

    __tablename__ = "import_batches"

    entity_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(300), default="")
    file_hash: Mapped[str] = mapped_column(String(64), default="")
    source_format: Mapped[str] = mapped_column(String(10), default="CSV")

    column_mapping: Mapped[dict] = mapped_column(default=dict)
    rows_total: Mapped[int] = mapped_column(Integer, default=0)
    rows_imported: Mapped[int] = mapped_column(Integer, default=0)
    rows_warning: Mapped[int] = mapped_column(Integer, default=0)
    rows_rejected: Mapped[int] = mapped_column(Integer, default=0)
    rows_duplicate: Mapped[int] = mapped_column(Integer, default=0)

    errors: Mapped[list] = mapped_column(default=list)
    warnings: Mapped[list] = mapped_column(default=list)
    status: Mapped[str] = mapped_column(String(20), default="COMPLETED")
    imported_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
