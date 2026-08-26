"""Tax configuration and regulatory documents.

Every rate lives in a *versioned, dated rule row* — never a constant in code.
When the law changes you add a new version with an effective date; historical
documents keep computing with the rule that applied on their own date.
"""
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
from app.enums import EDocStatus, ProviderMode, ReturnStatus, TaxComponent
from app.models.base import Base, CompanyScoped, TimestampMixin, UTCDateTime, UUIDPk


class TaxRateRule(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """A GST rate slab, valid over a date range.

    Lookup is always ``(hsn_sac or category) x transaction_date``.
    """

    __tablename__ = "tax_rate_rules"
    __table_args__ = (
        Index("ix_tax_rule_lookup", "company_id", "hsn_sac", "effective_from", "effective_to"),
        UniqueConstraint("company_id", "code", "version", name="uq_tax_rule_version"),
    )

    code: Mapped[str] = mapped_column(String(60), nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1)

    hsn_sac: Mapped[str] = mapped_column(String(10), default="", index=True)
    category: Mapped[str] = mapped_column(String(60), default="")

    total_rate: Mapped[Decimal] = mapped_column(Rate, nullable=False)  # e.g. 18.000000
    cgst_rate: Mapped[Decimal] = mapped_column(Rate, default=Decimal("0.000000"))
    sgst_rate: Mapped[Decimal] = mapped_column(Rate, default=Decimal("0.000000"))
    igst_rate: Mapped[Decimal] = mapped_column(Rate, default=Decimal("0.000000"))
    cess_rate: Mapped[Decimal] = mapped_column(Rate, default=Decimal("0.000000"))

    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    legal_reference: Mapped[str] = mapped_column(String(250), default="")
    notes: Mapped[str] = mapped_column(Text, default="")


class TDSSection(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """A TDS section with its threshold and rates, valid over a date range."""

    __tablename__ = "tds_sections"
    __table_args__ = (
        UniqueConstraint("company_id", "section", "version", name="uq_tds_section_version"),
        Index("ix_tds_lookup", "company_id", "section", "effective_from"),
    )

    section: Mapped[str] = mapped_column(String(20), nullable=False, index=True)  # 194C, 194J …
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    nature_of_payment: Mapped[str] = mapped_column(String(250), default="")

    rate_with_pan: Mapped[Decimal] = mapped_column(Rate, nullable=False)
    rate_without_pan: Mapped[Decimal] = mapped_column(Rate, default=Decimal("20.000000"))
    rate_individual: Mapped[Decimal | None] = mapped_column(Rate, nullable=True)
    rate_company: Mapped[Decimal | None] = mapped_column(Rate, nullable=True)

    single_payment_threshold: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    annual_threshold: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))

    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    legal_reference: Mapped[str] = mapped_column(String(250), default="")


class TDSEntry(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """A single TDS deduction, computed deterministically from a TDSSection."""

    __tablename__ = "tds_entries"
    __table_args__ = (Index("ix_tds_entry_vendor", "company_id", "vendor_id", "deduction_date"),)

    vendor_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    bill_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("bills.id", ondelete="SET NULL"), nullable=True, index=True
    )
    payment_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    section: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    section_rule_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rate: Mapped[Decimal] = mapped_column(Rate, nullable=False)
    base_amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    tds_amount: Mapped[Decimal] = mapped_column(Money, nullable=False)

    deduction_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    deposited: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    deposited_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    challan_number: Mapped[str] = mapped_column(String(60), default="")
    quarter: Mapped[str] = mapped_column(String(10), default="", index=True)  # Q1..Q4

    # Provenance: AI may *suggest* a section; the rule engine validates it.
    suggested_by_ai: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_suggested_section: Mapped[str] = mapped_column(String(20), default="")
    ai_confidence: Mapped[Decimal | None] = mapped_column(Rate, nullable=True)
    validated_by_rule: Mapped[bool] = mapped_column(Boolean, default=False)

    journal_entry_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")


class TCSRule(Base, UUIDPk, CompanyScoped, TimestampMixin):
    __tablename__ = "tcs_rules"
    __table_args__ = (UniqueConstraint("company_id", "section", "version", name="uq_tcs_version"),)

    section: Mapped[str] = mapped_column(String(20), nullable=False)  # 206C(1H) …
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    rate: Mapped[Decimal] = mapped_column(Rate, nullable=False)
    rate_without_pan: Mapped[Decimal] = mapped_column(Rate, default=Decimal("1.000000"))
    threshold: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    applies_to: Mapped[str] = mapped_column(String(60), default="SALE_OF_GOODS")
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    legal_reference: Mapped[str] = mapped_column(String(250), default="")


class TCSEntry(Base, UUIDPk, CompanyScoped, TimestampMixin):
    __tablename__ = "tcs_entries"

    customer_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    invoice_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    section: Mapped[str] = mapped_column(String(20), nullable=False)
    rate: Mapped[Decimal] = mapped_column(Rate, nullable=False)
    base_amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    tcs_amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    collection_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    deposited: Mapped[bool] = mapped_column(Boolean, default=False)
    deposited_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    journal_entry_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class GSTReturn(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """A *prepared* GST return.

    Nothing here is filed with the government by this application. The status
    vocabulary deliberately stops at FILED_EXTERNALLY, which a human sets after
    filing on the GST portal themselves.
    """

    __tablename__ = "gst_returns"
    __table_args__ = (
        UniqueConstraint("company_id", "return_type", "period", "gstin", name="uq_gst_return"),
    )

    return_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)  # GSTR1 / GSTR3B
    period: Mapped[str] = mapped_column(String(7), nullable=False, index=True)  # 2025-04
    gstin: Mapped[str] = mapped_column(String(15), default="")
    status: Mapped[str] = mapped_column(String(25), default=ReturnStatus.NOT_STARTED, index=True)

    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)

    total_taxable_value: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    total_cgst: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    total_sgst: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    total_igst: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    total_cess: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    total_tax: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))

    itc_available: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    net_payable: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))

    invoice_count: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[dict] = mapped_column(default=dict)  # structured, portal-shaped data
    validation_errors: Mapped[list] = mapped_column(default=list)

    prepared_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    prepared_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reviewed_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    filed_externally_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    external_arn: Mapped[str] = mapped_column(String(60), default="")  # entered by a human
    notes: Mapped[str] = mapped_column(Text, default="")


class GSTReconciliationItem(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """Books vs GSTR-2A/2B comparison row."""

    __tablename__ = "gst_reconciliation_items"
    __table_args__ = (Index("ix_gstr_recon_period", "company_id", "period", "status"),)

    period: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(10), default="2B")  # 2A | 2B
    supplier_gstin: Mapped[str] = mapped_column(String(15), default="", index=True)
    supplier_name: Mapped[str] = mapped_column(String(200), default="")
    invoice_number: Mapped[str] = mapped_column(String(60), default="", index=True)
    invoice_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    portal_taxable_value: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    portal_tax: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    books_taxable_value: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    books_tax: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    difference: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))

    bill_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="UNMATCHED", index=True)
    # MATCHED | MISSING_IN_BOOKS | MISSING_IN_PORTAL | VALUE_MISMATCH | UNMATCHED
    action_required: Mapped[str] = mapped_column(String(250), default="")


class EInvoice(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """e-Invoice record.

    ``irn``/``ack_no`` are populated ONLY from a provider response. In mock mode
    ``provider_mode`` is MOCK and the identifiers are explicitly marked as
    non-government demo values so they can never be mistaken for real ones.
    """

    __tablename__ = "einvoices"
    __table_args__ = (UniqueConstraint("company_id", "invoice_id", name="uq_einvoice_invoice"),)

    invoice_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(20), default=EDocStatus.PENDING, index=True)
    provider: Mapped[str] = mapped_column(String(30), default="mock")
    provider_mode: Mapped[str] = mapped_column(String(10), default=ProviderMode.MOCK, index=True)

    request_payload: Mapped[dict] = mapped_column(default=dict)
    response_payload: Mapped[dict] = mapped_column(default=dict)

    irn: Mapped[str] = mapped_column(String(64), default="", index=True)
    ack_no: Mapped[str] = mapped_column(String(30), default="")
    ack_date: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    signed_qr_code: Mapped[str] = mapped_column(Text, default="")
    signed_invoice: Mapped[str] = mapped_column(Text, default="")

    error_code: Mapped[str] = mapped_column(String(30), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    cancel_reason: Mapped[str] = mapped_column(String(250), default="")

    @property
    def is_government_confirmed(self) -> bool:
        """True only for a real IRP acknowledgement — never for demo data."""
        return self.provider_mode == ProviderMode.LIVE and bool(self.irn)


class EWayBill(Base, UUIDPk, CompanyScoped, TimestampMixin):
    __tablename__ = "eway_bills"
    __table_args__ = (UniqueConstraint("company_id", "invoice_id", name="uq_ewaybill_invoice"),)

    invoice_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    delivery_note_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=EDocStatus.PENDING, index=True)
    provider: Mapped[str] = mapped_column(String(30), default="mock")
    provider_mode: Mapped[str] = mapped_column(String(10), default=ProviderMode.MOCK, index=True)

    transport_mode: Mapped[str] = mapped_column(String(20), default="ROAD")
    vehicle_number: Mapped[str] = mapped_column(String(20), default="")
    transporter_id: Mapped[str] = mapped_column(String(20), default="")
    transporter_name: Mapped[str] = mapped_column(String(200), default="")
    transport_doc_number: Mapped[str] = mapped_column(String(60), default="")
    distance_km: Mapped[int] = mapped_column(Integer, default=0)

    from_place: Mapped[str] = mapped_column(String(150), default="")
    from_state_code: Mapped[str] = mapped_column(String(2), default="")
    from_pincode: Mapped[str] = mapped_column(String(12), default="")
    to_place: Mapped[str] = mapped_column(String(150), default="")
    to_state_code: Mapped[str] = mapped_column(String(2), default="")
    to_pincode: Mapped[str] = mapped_column(String(12), default="")

    request_payload: Mapped[dict] = mapped_column(default=dict)
    response_payload: Mapped[dict] = mapped_column(default=dict)

    ewb_number: Mapped[str] = mapped_column(String(20), default="", index=True)
    ewb_date: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    error_message: Mapped[str] = mapped_column(Text, default="")

    @property
    def is_government_confirmed(self) -> bool:
        return self.provider_mode == ProviderMode.LIVE and bool(self.ewb_number)


class TaxComponentTotal(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """Per-period tax aggregation cache used by the tax dashboard."""

    __tablename__ = "tax_component_totals"
    __table_args__ = (
        UniqueConstraint("company_id", "period", "component", "direction", name="uq_tax_total"),
    )

    period: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    component: Mapped[str] = mapped_column(String(10), default=TaxComponent.CGST)
    direction: Mapped[str] = mapped_column(String(10), default="OUTPUT")  # OUTPUT | INPUT
    taxable_value: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    tax_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    computed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
