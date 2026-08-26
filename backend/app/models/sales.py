"""Sales cycle: customer, quotation, order, delivery, invoice, credit note, receipt."""
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
from app.enums import (
    DocumentStatus,
    EDocStatus,
    GSTTreatment,
    OrderStatus,
    PaymentMethod,
    SupplyType,
)
from app.models.base import Base, CompanyScoped, SoftDelete, TimestampMixin, UUIDPk


class Customer(Base, UUIDPk, CompanyScoped, TimestampMixin, SoftDelete):
    __tablename__ = "customers"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_customer_code"),
        Index("ix_customer_name", "company_id", "name"),
    )

    code: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    legal_name: Mapped[str] = mapped_column(String(250), default="")
    email: Mapped[str] = mapped_column(String(200), default="")
    phone: Mapped[str] = mapped_column(String(30), default="")
    contact_person: Mapped[str] = mapped_column(String(150), default="")

    billing_address: Mapped[str] = mapped_column(Text, default="")
    shipping_address: Mapped[str] = mapped_column(Text, default="")
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
    credit_limit: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))

    receivable_account_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    opening_balance: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    tds_applicable: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list] = mapped_column(default=list)


class _SalesDocBase:
    """Shared header columns for every outbound sales document."""

    customer_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    doc_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    reference: Mapped[str] = mapped_column(String(120), default="")
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


class _LineBase:
    """Shared line columns across sales/purchase documents."""

    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    item_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    hsn_sac: Mapped[str] = mapped_column(String(10), default="")
    uom: Mapped[str] = mapped_column(String(20), default="NOS")

    quantity: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("1.0000"))
    rate: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    discount_percent: Mapped[Decimal] = mapped_column(Rate, default=Decimal("0.000000"))
    discount_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))

    gross_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    taxable_value: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    gst_rate: Mapped[Decimal] = mapped_column(Rate, default=Decimal("0.000000"))
    cgst: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    sgst: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    igst: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    cess_rate: Mapped[Decimal] = mapped_column(Rate, default=Decimal("0.000000"))
    cess: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    line_total: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))

    account_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cost_center_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    warehouse_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class Quotation(Base, UUIDPk, CompanyScoped, TimestampMixin, _SalesDocBase):
    __tablename__ = "quotations"
    __table_args__ = (UniqueConstraint("company_id", "number", name="uq_quotation_number"),)

    number: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=DocumentStatus.DRAFT, index=True)
    converted_order_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    lines: Mapped[list["QuotationLine"]] = relationship(
        cascade="all, delete-orphan", order_by="QuotationLine.line_number"
    )
    customer: Mapped[Customer] = relationship()


class QuotationLine(Base, UUIDPk, CompanyScoped, TimestampMixin, _LineBase):
    __tablename__ = "quotation_lines"

    quotation_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("quotations.id", ondelete="CASCADE"), nullable=False, index=True
    )


class SalesOrder(Base, UUIDPk, CompanyScoped, TimestampMixin, _SalesDocBase):
    __tablename__ = "sales_orders"
    __table_args__ = (UniqueConstraint("company_id", "number", name="uq_sales_order_number"),)

    number: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    expected_delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=OrderStatus.DRAFT, index=True)
    quotation_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    customer_po_number: Mapped[str] = mapped_column(String(60), default="")

    lines: Mapped[list["SalesOrderLine"]] = relationship(
        cascade="all, delete-orphan", order_by="SalesOrderLine.line_number"
    )
    customer: Mapped[Customer] = relationship()


class SalesOrderLine(Base, UUIDPk, CompanyScoped, TimestampMixin, _LineBase):
    __tablename__ = "sales_order_lines"

    sales_order_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("sales_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    delivered_qty: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0.0000"))
    invoiced_qty: Mapped[Decimal] = mapped_column(Quantity, default=Decimal("0.0000"))


class DeliveryNote(Base, UUIDPk, CompanyScoped, TimestampMixin, _SalesDocBase):
    __tablename__ = "delivery_notes"
    __table_args__ = (UniqueConstraint("company_id", "number", name="uq_delivery_note_number"),)

    number: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    sales_order_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default=DocumentStatus.DRAFT, index=True)
    vehicle_number: Mapped[str] = mapped_column(String(20), default="")
    transporter: Mapped[str] = mapped_column(String(150), default="")
    dispatched_from_warehouse_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    invoice_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    lines: Mapped[list["DeliveryNoteLine"]] = relationship(
        cascade="all, delete-orphan", order_by="DeliveryNoteLine.line_number"
    )
    customer: Mapped[Customer] = relationship()


class DeliveryNoteLine(Base, UUIDPk, CompanyScoped, TimestampMixin, _LineBase):
    __tablename__ = "delivery_note_lines"

    delivery_note_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("delivery_notes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sales_order_line_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class Invoice(Base, UUIDPk, CompanyScoped, TimestampMixin, _SalesDocBase):
    """Tax invoice issued to a customer. Posting creates the AR + revenue entry."""

    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("company_id", "number", name="uq_invoice_number"),
        Index("ix_invoice_status", "company_id", "status", "due_date"),
        Index("ix_invoice_customer", "company_id", "customer_id", "doc_date"),
    )

    number: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    due_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default=DocumentStatus.DRAFT, index=True)

    sales_order_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    delivery_note_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    amount_paid: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    amount_credited: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    tds_deducted: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    tcs_collected: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))

    payment_terms: Mapped[str] = mapped_column(String(120), default="")
    template: Mapped[str] = mapped_column(String(30), default="PROFESSIONAL")
    show_bank_details: Mapped[bool] = mapped_column(Boolean, default=True)
    bank_account_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    custom_fields: Mapped[dict] = mapped_column(default=dict)

    is_reverse_charge: Mapped[bool] = mapped_column(Boolean, default=False)
    is_export: Mapped[bool] = mapped_column(Boolean, default=False)

    # e-invoicing / e-way bill state lives on the tax records; these mirror status
    einvoice_status: Mapped[str] = mapped_column(String(20), default=EDocStatus.NOT_APPLICABLE)
    ewaybill_status: Mapped[str] = mapped_column(String(20), default=EDocStatus.NOT_APPLICABLE)

    journal_entry_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    posted_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    sent_at: Mapped[date | None] = mapped_column(Date, nullable=True)

    lines: Mapped[list["InvoiceLine"]] = relationship(
        cascade="all, delete-orphan", order_by="InvoiceLine.line_number", lazy="selectin"
    )
    customer: Mapped[Customer] = relationship(lazy="joined")

    @property
    def balance_due(self) -> Decimal:
        return self.total - self.amount_paid - self.amount_credited - self.tds_deducted


class InvoiceLine(Base, UUIDPk, CompanyScoped, TimestampMixin, _LineBase):
    __tablename__ = "invoice_lines"

    invoice_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sales_order_line_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cogs_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))


class CreditNote(Base, UUIDPk, CompanyScoped, TimestampMixin, _SalesDocBase):
    __tablename__ = "credit_notes"
    __table_args__ = (UniqueConstraint("company_id", "number", name="uq_credit_note_number"),)

    number: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    invoice_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True, index=True
    )
    reason: Mapped[str] = mapped_column(String(250), default="")
    status: Mapped[str] = mapped_column(String(20), default=DocumentStatus.DRAFT, index=True)
    is_sales_return: Mapped[bool] = mapped_column(Boolean, default=False)
    amount_applied: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    journal_entry_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    lines: Mapped[list["CreditNoteLine"]] = relationship(
        cascade="all, delete-orphan", order_by="CreditNoteLine.line_number"
    )
    customer: Mapped[Customer] = relationship()


class CreditNoteLine(Base, UUIDPk, CompanyScoped, TimestampMixin, _LineBase):
    __tablename__ = "credit_note_lines"

    credit_note_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("credit_notes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    invoice_line_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class Receipt(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """Customer payment received. Allocated against one or more invoices."""

    __tablename__ = "receipts"
    __table_args__ = (
        UniqueConstraint("company_id", "number", name="uq_receipt_number"),
        Index("ix_receipt_customer", "company_id", "customer_id", "receipt_date"),
    )

    number: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    customer_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    receipt_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    unallocated_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    currency: Mapped[str] = mapped_column(String(3), default="INR")

    method: Mapped[str] = mapped_column(String(20), default=PaymentMethod.BANK_TRANSFER)
    reference: Mapped[str] = mapped_column(String(120), default="", index=True)
    bank_account_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    deposit_account_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    tds_deducted: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    notes: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default=DocumentStatus.DRAFT, index=True)
    journal_entry_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    created_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    allocations: Mapped[list["ReceiptAllocation"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin"
    )
    customer: Mapped[Customer] = relationship(lazy="joined")


class ReceiptAllocation(Base, UUIDPk, CompanyScoped, TimestampMixin):
    __tablename__ = "receipt_allocations"
    __table_args__ = (Index("ix_receipt_alloc_invoice", "invoice_id"),)

    receipt_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("receipts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    invoice_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
