"""Purchase cycle: vendor, purchase order, goods receipt, bill, debit note, payment."""
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

from app.database import Money, Quantity, Rate
from app.enums import DocumentStatus, GSTTreatment, OrderStatus, PaymentMethod, SupplyType
from app.models.base import Base, CompanyScoped, SoftDelete, TimestampMixin, UUIDPk
from app.models.sales import _LineBase


class Vendor(Base, UUIDPk, CompanyScoped, TimestampMixin, SoftDelete):
    __tablename__ = "vendors"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_vendor_code"),
        Index("ix_vendor_name", "company_id", "name"),
    )

    code: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    legal_name: Mapped[str] = mapped_column(String(250), default="")
    email: Mapped[str] = mapped_column(String(200), default="")
    phone: Mapped[str] = mapped_column(String(30), default="")
    contact_person: Mapped[str] = mapped_column(String(150), default="")

    address: Mapped[str] = mapped_column(Text, default="")
    city: Mapped[str] = mapped_column(String(100), default="")
    state: Mapped[str] = mapped_column(String(100), default="")
    state_code: Mapped[str] = mapped_column(String(2), default="")
    pincode: Mapped[str] = mapped_column(String(12), default="")
    country: Mapped[str] = mapped_column(String(2), default="IN")

    gstin: Mapped[str] = mapped_column(String(15), default="", index=True)
    pan: Mapped[str] = mapped_column(String(10), default="")
    gst_treatment: Mapped[str] = mapped_column(String(30), default=GSTTreatment.REGISTERED)

    currency: Mapped[str] = mapped_column(String(3), default="INR")
    payment_terms_days: Mapped[int] = mapped_column(Integer, default=30)
    payable_account_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    default_expense_account_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    opening_balance: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))

    # TDS configuration — the *rate* comes from a versioned rule, not from here.
    tds_applicable: Mapped[bool] = mapped_column(Boolean, default=False)
    default_tds_section: Mapped[str] = mapped_column(String(20), default="")

    # Bank details. Changes are audited: a change just before a payment is a
    # classic fraud signal and is surfaced by the fraud monitor.
    bank_account_name: Mapped[str] = mapped_column(String(200), default="")
    bank_account_number: Mapped[str] = mapped_column(String(40), default="")
    bank_ifsc: Mapped[str] = mapped_column(String(11), default="")
    bank_details_updated_at: Mapped[date | None] = mapped_column(Date, nullable=True)

    is_msme: Mapped[bool] = mapped_column(Boolean, default=False)
    msme_registration: Mapped[str] = mapped_column(String(30), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list] = mapped_column(default=list)


class _PurchaseDocBase:
    vendor_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    doc_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    reference: Mapped[str] = mapped_column(String(120), default="", index=True)
    branch_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    fx_rate: Mapped[Decimal] = mapped_column(Rate, default=Decimal("1.000000"))

    place_of_supply: Mapped[str] = mapped_column(String(2), default="")
    supply_type: Mapped[str] = mapped_column(String(20), default=SupplyType.INTRA_STATE)

    subtotal: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    discount_total: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    taxable_value: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    cgst: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    sgst: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    igst: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    cess: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    tax_total: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    round_off: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    total: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))

    notes: Mapped[str] = mapped_column(Text, default="")
    terms: Mapped[str] = mapped_column(Text, default="")
    created_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class PurchaseOrder(Base, UUIDPk, CompanyScoped, TimestampMixin, _PurchaseDocBase):
    __tablename__ = "purchase_orders"
    __table_args__ = (UniqueConstraint("company_id", "number", name="uq_po_number"),)

    number: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    expected_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=OrderStatus.DRAFT, index=True)
    delivery_address: Mapped[str] = mapped_column(Text, default="")
    approved_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    lines: Mapped[list["PurchaseOrderLine"]] = relationship(
        cascade="all, delete-orphan", order_by="PurchaseOrderLine.line_number"
    )
    vendor: Mapped[Vendor] = relationship()


class PurchaseOrderLine(Base, UUIDPk, CompanyScoped, TimestampMixin, _LineBase):
    __tablename__ = "purchase_order_lines"

    purchase_order_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    received_qty: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0.0000"))
    billed_qty: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0.0000"))


class GoodsReceipt(Base, UUIDPk, CompanyScoped, TimestampMixin, _PurchaseDocBase):
    """Goods Receipt Note. Increases stock; the bill may arrive later."""

    __tablename__ = "goods_receipts"
    __table_args__ = (UniqueConstraint("company_id", "number", name="uq_grn_number"),)

    number: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    purchase_order_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default=DocumentStatus.DRAFT, index=True)
    warehouse_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    vehicle_number: Mapped[str] = mapped_column(String(20), default="")
    bill_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    journal_entry_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    lines: Mapped[list["GoodsReceiptLine"]] = relationship(
        cascade="all, delete-orphan", order_by="GoodsReceiptLine.line_number"
    )
    vendor: Mapped[Vendor] = relationship()


class GoodsReceiptLine(Base, UUIDPk, CompanyScoped, TimestampMixin, _LineBase):
    __tablename__ = "goods_receipt_lines"

    goods_receipt_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("goods_receipts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    purchase_order_line_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    accepted_qty: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0.0000"))
    rejected_qty: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0.0000"))


class Bill(Base, UUIDPk, CompanyScoped, TimestampMixin, _PurchaseDocBase):
    """Vendor bill (purchase invoice). Posting creates AP + expense/inventory."""

    __tablename__ = "bills"
    __table_args__ = (
        UniqueConstraint("company_id", "number", name="uq_bill_number"),
        # A vendor's own invoice number must be unique per vendor: this is the
        # database-level guarantee behind duplicate-bill detection.
        UniqueConstraint("company_id", "vendor_id", "vendor_invoice_number", name="uq_bill_vendor_ref"),
        Index("ix_bill_status", "company_id", "status", "due_date"),
        Index("ix_bill_vendor", "company_id", "vendor_id", "doc_date"),
    )

    number: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    vendor_invoice_number: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    vendor_invoice_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default=DocumentStatus.DRAFT, index=True)

    purchase_order_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    goods_receipt_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    amount_paid: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    amount_debited: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))

    tds_section: Mapped[str] = mapped_column(String(20), default="")
    tds_rate: Mapped[Decimal] = mapped_column(Rate, default=Decimal("0.000000"))
    tds_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    is_reverse_charge: Mapped[bool] = mapped_column(Boolean, default=False)
    itc_eligible: Mapped[bool] = mapped_column(Boolean, default=True)

    # Document AI provenance
    document_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    extraction_confidence: Mapped[Decimal | None] = mapped_column(Rate, nullable=True)
    duplicate_of_bill_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    approved_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    journal_entry_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    lines: Mapped[list["BillLine"]] = relationship(
        cascade="all, delete-orphan", order_by="BillLine.line_number", lazy="selectin"
    )
    vendor: Mapped[Vendor] = relationship(lazy="joined")

    @property
    def balance_due(self) -> Decimal:
        return self.total - self.amount_paid - self.amount_debited - self.tds_amount


class BillLine(Base, UUIDPk, CompanyScoped, TimestampMixin, _LineBase):
    __tablename__ = "bill_lines"

    bill_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("bills.id", ondelete="CASCADE"), nullable=False, index=True
    )
    purchase_order_line_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_inventory: Mapped[bool] = mapped_column(Boolean, default=False)


class DebitNote(Base, UUIDPk, CompanyScoped, TimestampMixin, _PurchaseDocBase):
    __tablename__ = "debit_notes"
    __table_args__ = (UniqueConstraint("company_id", "number", name="uq_debit_note_number"),)

    number: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    bill_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("bills.id", ondelete="SET NULL"), nullable=True, index=True
    )
    reason: Mapped[str] = mapped_column(String(250), default="")
    status: Mapped[str] = mapped_column(String(20), default=DocumentStatus.DRAFT, index=True)
    is_purchase_return: Mapped[bool] = mapped_column(Boolean, default=False)
    amount_applied: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    journal_entry_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    lines: Mapped[list["DebitNoteLine"]] = relationship(
        cascade="all, delete-orphan", order_by="DebitNoteLine.line_number"
    )
    vendor: Mapped[Vendor] = relationship()


class DebitNoteLine(Base, UUIDPk, CompanyScoped, TimestampMixin, _LineBase):
    __tablename__ = "debit_note_lines"

    debit_note_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("debit_notes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    bill_line_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class Payment(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """Payment made to a vendor. AI may prioritise; only humans release."""

    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("company_id", "number", name="uq_payment_number"),
        Index("ix_payment_vendor", "company_id", "vendor_id", "payment_date"),
    )

    number: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    vendor_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    payment_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    unallocated_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    currency: Mapped[str] = mapped_column(String(3), default="INR")

    method: Mapped[str] = mapped_column(String(20), default=PaymentMethod.BANK_TRANSFER)
    reference: Mapped[str] = mapped_column(String(120), default="", index=True)
    bank_account_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    payment_account_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    tds_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    notes: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default=DocumentStatus.DRAFT, index=True)

    # Release control: an AI agent can never set these.
    released_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    approved_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    journal_entry_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    created_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    allocations: Mapped[list["PaymentAllocation"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin"
    )
    vendor: Mapped[Vendor] = relationship(lazy="joined")


class PaymentAllocation(Base, UUIDPk, CompanyScoped, TimestampMixin):
    __tablename__ = "payment_allocations"
    __table_args__ = (Index("ix_payment_alloc_bill", "bill_id"),)

    payment_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("payments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    bill_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("bills.id", ondelete="CASCADE"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
