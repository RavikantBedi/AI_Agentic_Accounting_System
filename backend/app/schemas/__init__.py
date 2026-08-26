"""Request/response schemas for the HTTP API."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import Field, field_validator

from app.enums import (
    AccountType,
    BankAccountType,
    DocumentKind,
    PaymentMethod,
    UserRole,
    VoucherType,
)
from app.schemas.common import (
    AppModel,
    Email,
    ConfidenceBlock,
    DateRange,
    ErrorResponse,
    EvidenceItem,
    HealthResponse,
    IdResponse,
    MessageResponse,
    Money,
    Page,
    PaginationParams,
    Quantity,
    Rate,
    confidence_label,
)

# ----------------------------------------------------------------- auth ----


class LoginRequest(AppModel):
    email: Email
    password: str = Field(min_length=1, max_length=200)
    company_id: str | None = None


class RegisterRequest(AppModel):
    email: Email
    password: str = Field(min_length=8, max_length=200)
    full_name: str = Field(min_length=1, max_length=150)
    company_name: str | None = Field(default=None, max_length=200)


class TokenResponse(AppModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: "UserOut"
    company: "CompanyOut | None" = None
    role: str | None = None
    permissions: list[str] = []


class RefreshRequest(AppModel):
    refresh_token: str


class ChangePasswordRequest(AppModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=200)


class UserOut(AppModel):
    id: str
    email: str
    full_name: str
    phone: str = ""
    is_active: bool = True
    is_superuser: bool = False
    last_login_at: datetime | None = None
    created_at: datetime | None = None


class UserCreate(AppModel):
    email: Email
    full_name: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=8, max_length=200)
    role: UserRole = UserRole.VIEWER
    phone: str = ""


class MembershipOut(AppModel):
    id: str
    user_id: str
    company_id: str
    role: str
    is_default: bool = False
    user: UserOut | None = None


# -------------------------------------------------------------- company ----


class CompanyOut(AppModel):
    id: str
    name: str
    legal_name: str
    business_type: str
    country: str
    state: str = ""
    state_code: str = ""
    city: str = ""
    address_line1: str = ""
    pincode: str = ""
    email: str = ""
    phone: str = ""
    gstin: str = ""
    pan: str = ""
    tan: str = ""
    currency: str
    fiscal_year_start_month: int
    timezone: str
    is_demo: bool = False
    books_begin_on: date | None = None
    created_at: datetime | None = None


class CompanyCreate(AppModel):
    name: str = Field(min_length=1, max_length=200)
    legal_name: str = ""
    business_type: str = "PRIVATE_LIMITED"
    state: str = ""
    state_code: str = ""
    gstin: str = ""
    pan: str = ""
    tan: str = ""
    address_line1: str = ""
    city: str = ""
    pincode: str = ""
    email: str = ""
    phone: str = ""
    currency: str = "INR"
    fiscal_year_start_month: int = Field(default=4, ge=1, le=12)
    books_begin_on: date | None = None


class CompanyUpdate(AppModel):
    name: str | None = None
    legal_name: str | None = None
    business_type: str | None = None
    state: str | None = None
    state_code: str | None = None
    address_line1: str | None = None
    city: str | None = None
    pincode: str | None = None
    email: str | None = None
    phone: str | None = None
    website: str | None = None
    gstin: str | None = None
    pan: str | None = None
    tan: str | None = None
    timezone: str | None = None


# ----------------------------------------------------------- accounting ----


class AccountOut(AppModel):
    id: str
    code: str
    name: str
    type: str
    subtype: str
    normal_balance: str
    parent_id: str | None = None
    depth: int = 0
    is_group: bool = False
    is_active: bool = True
    is_system: bool = False
    system_key: str | None = None
    currency: str = "INR"
    tax_category: str = ""
    description: str = ""


class AccountWithBalance(AccountOut):
    balance: Money = Decimal("0.00")
    debit: Money = Decimal("0.00")
    credit: Money = Decimal("0.00")
    children: list["AccountWithBalance"] = []


class AccountCreate(AppModel):
    code: str = Field(min_length=1, max_length=30)
    name: str = Field(min_length=1, max_length=200)
    type: AccountType
    subtype: str
    parent_id: str | None = None
    is_group: bool = False
    tax_category: str = ""
    hsn_sac: str = ""
    description: str = ""
    is_reconcilable: bool = False


class JournalLineIn(AppModel):
    account_id: str | None = None
    account_code: str | None = None
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")
    description: str = ""
    cost_center_id: str | None = None
    party_type: str | None = None
    party_id: str | None = None
    branch_id: str | None = None
    project: str = ""

    @field_validator("debit", "credit")
    @classmethod
    def _non_negative(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("Amounts must be non-negative; use the other side instead.")
        return value


class JournalEntryCreate(AppModel):
    entry_date: date
    lines: list[JournalLineIn] = Field(min_length=2)
    voucher_type: VoucherType = VoucherType.JOURNAL
    narration: str = ""
    reference: str = ""
    branch_id: str | None = None
    post: bool = True


class JournalLineOut(AppModel):
    id: str
    line_number: int
    account_id: str
    account_code: str = ""
    account_name: str = ""
    debit: Money
    credit: Money
    description: str = ""
    cost_center_id: str | None = None
    party_type: str | None = None
    party_id: str | None = None
    reconciled: bool = False


class JournalEntryOut(AppModel):
    id: str
    entry_number: str
    voucher_type: str
    entry_date: date
    narration: str = ""
    reference: str = ""
    total_debit: Money
    total_credit: Money
    status: str
    source: str
    source_type: str | None = None
    source_id: str | None = None
    is_reversal: bool = False
    reverses_entry_id: str | None = None
    reversed_by_entry_id: str | None = None
    posted_at: datetime | None = None
    created_at: datetime | None = None
    lines: list[JournalLineOut] = []


class ReverseEntryRequest(AppModel):
    reason: str = Field(min_length=3, max_length=500)
    reversal_date: date | None = None


class PeriodOut(AppModel):
    id: str
    name: str
    year: int
    month: int
    start_date: date
    end_date: date
    status: str
    closed_at: datetime | None = None


class ClosePeriodRequest(AppModel):
    notes: str = ""
    force: bool = False


class ReopenPeriodRequest(AppModel):
    reason: str = Field(min_length=5, max_length=500)


# ---------------------------------------------------------------- sales ----


class CustomerOut(AppModel):
    id: str
    code: str
    name: str
    legal_name: str = ""
    email: str = ""
    phone: str = ""
    gstin: str = ""
    pan: str = ""
    gst_treatment: str
    state: str = ""
    state_code: str = ""
    city: str = ""
    billing_address: str = ""
    payment_terms_days: int = 30
    credit_limit: Money = Decimal("0.00")
    currency: str = "INR"
    is_active: bool = True
    balance: Money | None = None


class CustomerCreate(AppModel):
    name: str = Field(min_length=1, max_length=200)
    code: str | None = None
    legal_name: str = ""
    email: str = ""
    phone: str = ""
    gstin: str = ""
    pan: str = ""
    gst_treatment: str = "REGISTERED"
    billing_address: str = ""
    shipping_address: str = ""
    city: str = ""
    state: str = ""
    state_code: str = ""
    pincode: str = ""
    payment_terms_days: int = Field(default=30, ge=0, le=365)
    credit_limit: Decimal = Decimal("0")
    currency: str = "INR"
    notes: str = ""


class LineItemIn(AppModel):
    item_id: str | None = None
    description: str = ""
    hsn_sac: str = ""
    uom: str = "NOS"
    quantity: Decimal = Decimal("1")
    rate: Decimal = Decimal("0")
    discount_percent: Decimal = Decimal("0")
    discount_amount: Decimal = Decimal("0")
    gst_rate: Decimal | None = None
    cess_rate: Decimal = Decimal("0")
    account_id: str | None = None
    cost_center_id: str | None = None
    warehouse_id: str | None = None

    @field_validator("quantity")
    @classmethod
    def _positive_quantity(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("Quantity must be greater than zero.")
        return value

    @field_validator("rate")
    @classmethod
    def _non_negative_rate(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("Rate cannot be negative.")
        return value


class InvoiceLineOut(AppModel):
    id: str
    line_number: int
    item_id: str | None = None
    description: str = ""
    hsn_sac: str = ""
    uom: str = "NOS"
    quantity: Quantity
    rate: Money
    discount_amount: Money
    taxable_value: Money
    gst_rate: Rate
    cgst: Money
    sgst: Money
    igst: Money
    cess: Money
    line_total: Money


class InvoiceOut(AppModel):
    id: str
    number: str
    customer_id: str
    customer_name: str = ""
    doc_date: date
    due_date: date
    status: str
    reference: str = ""
    place_of_supply: str = ""
    supply_type: str = ""
    subtotal: Money
    discount_total: Money
    taxable_value: Money
    cgst: Money
    sgst: Money
    igst: Money
    cess: Money
    tax_total: Money
    round_off: Money
    total: Money
    amount_paid: Money
    amount_credited: Money
    balance_due: Money
    currency: str = "INR"
    notes: str = ""
    terms: str = ""
    template: str = "PROFESSIONAL"
    journal_entry_id: str | None = None
    einvoice_status: str = "NOT_APPLICABLE"
    ewaybill_status: str = "NOT_APPLICABLE"
    days_overdue: int = 0
    lines: list[InvoiceLineOut] = []
    created_at: datetime | None = None


class InvoiceCreate(AppModel):
    customer_id: str
    doc_date: date
    lines: list[LineItemIn] = Field(min_length=1)
    due_date: date | None = None
    reference: str = ""
    notes: str = ""
    terms: str = ""
    place_of_supply: str = ""
    bank_account_id: str | None = None
    template: str = "PROFESSIONAL"
    post: bool = True


class ReceiptAllocationIn(AppModel):
    invoice_id: str
    amount: Decimal


class ReceiptCreate(AppModel):
    customer_id: str
    receipt_date: date
    amount: Decimal = Field(gt=0)
    method: PaymentMethod = PaymentMethod.BANK_TRANSFER
    reference: str = ""
    bank_account_id: str | None = None
    tds_deducted: Decimal = Decimal("0")
    notes: str = ""
    allocations: list[ReceiptAllocationIn] | None = None
    auto_allocate: bool = True


class ReceiptOut(AppModel):
    id: str
    number: str
    customer_id: str
    customer_name: str = ""
    receipt_date: date
    amount: Money
    unallocated_amount: Money
    method: str
    reference: str = ""
    tds_deducted: Money
    status: str
    journal_entry_id: str | None = None
    created_at: datetime | None = None


class CreditNoteCreate(AppModel):
    customer_id: str
    doc_date: date
    lines: list[LineItemIn] = Field(min_length=1)
    invoice_id: str | None = None
    reason: str = ""
    is_sales_return: bool = False
    post: bool = True


# ------------------------------------------------------------ purchases ----


class VendorOut(AppModel):
    id: str
    code: str
    name: str
    legal_name: str = ""
    email: str = ""
    phone: str = ""
    gstin: str = ""
    pan: str = ""
    gst_treatment: str
    state: str = ""
    state_code: str = ""
    city: str = ""
    address: str = ""
    payment_terms_days: int = 30
    tds_applicable: bool = False
    default_tds_section: str = ""
    is_msme: bool = False
    currency: str = "INR"
    is_active: bool = True
    balance: Money | None = None


class VendorCreate(AppModel):
    name: str = Field(min_length=1, max_length=200)
    code: str | None = None
    legal_name: str = ""
    email: str = ""
    phone: str = ""
    gstin: str = ""
    pan: str = ""
    gst_treatment: str = "REGISTERED"
    address: str = ""
    city: str = ""
    state: str = ""
    state_code: str = ""
    pincode: str = ""
    payment_terms_days: int = Field(default=30, ge=0, le=365)
    tds_applicable: bool = False
    default_tds_section: str = ""
    is_msme: bool = False
    currency: str = "INR"
    notes: str = ""


class BillLineOut(AppModel):
    id: str
    line_number: int
    item_id: str | None = None
    description: str = ""
    hsn_sac: str = ""
    uom: str = "NOS"
    quantity: Quantity
    rate: Money
    taxable_value: Money
    gst_rate: Rate
    cgst: Money
    sgst: Money
    igst: Money
    line_total: Money


class BillOut(AppModel):
    id: str
    number: str
    vendor_invoice_number: str
    vendor_id: str
    vendor_name: str = ""
    doc_date: date
    due_date: date
    status: str
    taxable_value: Money
    cgst: Money
    sgst: Money
    igst: Money
    cess: Money
    tax_total: Money
    round_off: Money
    total: Money
    amount_paid: Money
    amount_debited: Money
    tds_amount: Money
    tds_section: str = ""
    balance_due: Money
    itc_eligible: bool = True
    currency: str = "INR"
    notes: str = ""
    journal_entry_id: str | None = None
    document_id: str | None = None
    days_overdue: int = 0
    lines: list[BillLineOut] = []
    created_at: datetime | None = None


class BillCreate(AppModel):
    vendor_id: str
    doc_date: date
    vendor_invoice_number: str = Field(min_length=1, max_length=60)
    lines: list[LineItemIn] = Field(min_length=1)
    vendor_invoice_date: date | None = None
    due_date: date | None = None
    reference: str = ""
    notes: str = ""
    place_of_supply: str = ""
    itc_eligible: bool = True
    tds_section: str | None = None
    apply_tds: bool = True
    document_id: str | None = None
    post: bool = True
    allow_duplicate: bool = False


class PaymentAllocationIn(AppModel):
    bill_id: str
    amount: Decimal


class PaymentCreate(AppModel):
    vendor_id: str
    payment_date: date
    amount: Decimal = Field(gt=0)
    method: PaymentMethod = PaymentMethod.BANK_TRANSFER
    reference: str = ""
    bank_account_id: str | None = None
    notes: str = ""
    allocations: list[PaymentAllocationIn] | None = None
    auto_allocate: bool = True


class PaymentOut(AppModel):
    id: str
    number: str
    vendor_id: str
    vendor_name: str = ""
    payment_date: date
    amount: Money
    unallocated_amount: Money
    method: str
    reference: str = ""
    status: str
    journal_entry_id: str | None = None
    released_by_user_id: str | None = None
    created_at: datetime | None = None


class DebitNoteCreate(AppModel):
    vendor_id: str
    doc_date: date
    lines: list[LineItemIn] = Field(min_length=1)
    bill_id: str | None = None
    reason: str = ""
    is_purchase_return: bool = False
    post: bool = True


# -------------------------------------------------------------- banking ----


class BankAccountOut(AppModel):
    id: str
    name: str
    bank_name: str
    masked_number: str = ""
    ifsc: str = ""
    account_type: str
    currency: str
    gl_account_id: str
    opening_balance: Money
    statement_balance: Money | None = None
    statement_balance_date: date | None = None
    book_balance: Money | None = None
    difference: Money | None = None
    provider: str = "file"
    is_active: bool = True


class BankAccountCreate(AppModel):
    name: str = Field(min_length=1, max_length=150)
    bank_name: str = Field(min_length=1, max_length=150)
    account_number: str = Field(min_length=4, max_length=40)
    ifsc: str = ""
    branch_name: str = ""
    account_type: BankAccountType = BankAccountType.CURRENT
    currency: str = "INR"
    opening_balance: Decimal = Decimal("0")
    opening_balance_date: date | None = None


class BankTransactionOut(AppModel):
    id: str
    bank_account_id: str
    txn_date: date
    description: str = ""
    reference: str = ""
    counterparty: str = ""
    direction: str
    amount: Money
    running_balance: Money | None = None
    recon_status: str
    matched_amount: Money
    suggested_account_id: str | None = None
    suggested_category: str = ""
    suggested_confidence: Rate | None = None
    categorised_by: str = ""
    journal_entry_id: str | None = None


class CategoriseRequest(AppModel):
    account_id: str
    description: str | None = None
    cost_center_id: str | None = None
    learn: bool = True


class ReconcileRequest(AppModel):
    bank_account_id: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    auto_confirm: bool = True


class MatchActionRequest(AppModel):
    reason: str = ""


# ------------------------------------------------------------ inventory ----


class ItemOut(AppModel):
    id: str
    sku: str
    name: str
    description: str = ""
    item_type: str
    category_id: str | None = None
    category_name: str | None = None
    uom: str
    hsn_sac: str = ""
    gst_rate: Rate
    purchase_price: Money
    selling_price: Money
    current_qty: Quantity
    average_cost: Money
    stock_value: Money
    reorder_level: Quantity
    reorder_quantity: Quantity
    track_inventory: bool = True
    is_low_stock: bool = False
    is_active: bool = True


class ItemCreate(AppModel):
    sku: str = Field(min_length=1, max_length=60)
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    item_type: str = "GOODS"
    category_id: str | None = None
    uom: str = "NOS"
    hsn_sac: str = ""
    gst_rate: Decimal = Decimal("18")
    purchase_price: Decimal = Decimal("0")
    selling_price: Decimal = Decimal("0")
    reorder_level: Decimal = Decimal("0")
    reorder_quantity: Decimal = Decimal("0")
    track_inventory: bool = True
    opening_qty: Decimal = Decimal("0")
    opening_rate: Decimal = Decimal("0")


class WarehouseOut(AppModel):
    id: str
    code: str
    name: str
    city: str = ""
    state: str = ""
    is_default: bool = False
    is_active: bool = True


class StockTransferRequest(AppModel):
    item_id: str
    from_warehouse_id: str
    to_warehouse_id: str
    quantity: Decimal = Field(gt=0)
    transfer_date: date
    reference: str = ""


class StockAdjustmentLineIn(AppModel):
    item_id: str
    counted_qty: Decimal
    reason: str = ""


class StockAdjustmentRequest(AppModel):
    warehouse_id: str
    adjustment_date: date
    lines: list[StockAdjustmentLineIn] = Field(min_length=1)
    reason: str = Field(min_length=3, max_length=250)


# ------------------------------------------------------------------ AI ----


class AskRequest(AppModel):
    question: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None


class AskResponse(AppModel):
    answer: str
    key_figures: list[dict[str, Any]] = []
    suggested_actions: list[str] = []
    evidence: list[EvidenceItem] = []
    source_records: list[dict[str, Any]] = []
    confidence: Rate = Decimal("0")
    confidence_label: str = ""
    intent: str = ""
    tools_used: list[str] = []
    used_llm: bool = False
    provider: str = ""
    insufficient_evidence: bool = False
    conversation_id: str | None = None
    message_id: str | None = None
    financial_impact: Money | None = None
    grounding_warning: str | None = None


class RunAgentRequest(AppModel):
    params: dict[str, Any] = {}


class AgentRunOut(AppModel):
    id: str
    agent_kind: str
    status: str
    trigger: str
    provider: str = ""
    model: str = ""
    used_llm: bool = False
    fallback_used: bool = False
    fallback_reason: str = ""
    items_processed: int = 0
    proposals_created: int = 0
    high_confidence_count: int = 0
    needs_review_count: int = 0
    duration_ms: int = 0
    error_message: str = ""
    output_summary: dict[str, Any] = {}
    findings: list[dict[str, Any]] = []
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime | None = None


class ProposalOut(AppModel):
    id: str
    agent_kind: str
    proposal_type: str
    status: str
    title: str
    summary: str = ""
    reasoning: str = ""
    payload: dict[str, Any] = {}
    evidence: list[dict[str, Any]] = []
    source_records: list[dict[str, Any]] = []
    confidence: Rate
    confidence_label: str = ""
    financial_impact: Money
    severity: str
    requires_approval: bool = True
    required_role: str = ""
    policy_reason: str = ""
    validation_errors: list[str] = []
    target_type: str | None = None
    target_id: str | None = None
    result_type: str | None = None
    result_id: str | None = None
    decided_by_user_id: str | None = None
    decided_at: datetime | None = None
    decision_reason: str = ""
    created_at: datetime | None = None


class ProposalDecision(AppModel):
    reason: str = ""
    edits: dict[str, Any] | None = None


class WorkflowRequest(AppModel):
    params: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------- exceptions / audit ----


class ExceptionOut(AppModel):
    id: str
    category: str
    severity: str
    status: str
    title: str
    reason: str = ""
    recommended_action: str = ""
    evidence: list[dict[str, Any]] = []
    amount: Money | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    action_url: str = ""
    detected_by: str = ""
    created_at: datetime | None = None
    resolved_at: datetime | None = None
    resolution_note: str = ""


class ExceptionResolve(AppModel):
    note: str = ""
    dismiss: bool = False


class AnomalyOut(AppModel):
    id: str
    kind: str
    severity: str
    status: str
    title: str
    reason: str = ""
    recommendation: str = ""
    evidence: list[dict[str, Any]] = []
    detection_method: str = "RULE"
    amount: Money | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    occurred_on: date | None = None
    baseline_value: Money | None = None
    observed_value: Money | None = None
    deviation_score: Rate | None = None
    created_at: datetime | None = None


class AuditEventOut(AppModel):
    id: str
    sequence: int
    occurred_at: datetime
    action: str
    actor_type: str
    actor_id: str | None = None
    actor_label: str = ""
    agent_kind: str = ""
    entity_type: str
    entity_id: str | None = None
    entity_label: str = ""
    summary: str = ""
    reason: str = ""
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    changed_fields: list[str] = []
    amount: Money | None = None
    ip_address: str = ""
    request_id: str = ""
    hash: str = ""


# ------------------------------------------------------------------ tax ----


class GSTReturnOut(AppModel):
    id: str
    return_type: str
    period: str
    gstin: str = ""
    status: str
    period_start: date
    period_end: date
    total_taxable_value: Money
    total_cgst: Money
    total_sgst: Money
    total_igst: Money
    total_cess: Money
    total_tax: Money
    itc_available: Money
    net_payable: Money
    invoice_count: int = 0
    validation_errors: list[dict[str, Any]] = []
    external_arn: str = ""
    prepared_at: datetime | None = None
    filed_externally_at: datetime | None = None


class MarkFiledRequest(AppModel):
    arn: str = Field(min_length=3, max_length=60)
    filed_on: date | None = None


class EInvoiceRequest(AppModel):
    invoice_id: str


class EWayBillRequest(AppModel):
    invoice_id: str
    distance_km: int = Field(default=0, ge=0, le=4000)
    vehicle_number: str = ""
    transporter_id: str = ""
    transporter_name: str = ""
    transport_mode: str = "ROAD"
    from_place: str = ""
    to_place: str = ""


class TDSEntryOut(AppModel):
    id: str
    vendor_id: str
    vendor_name: str = ""
    bill_id: str | None = None
    section: str
    rate: Rate
    base_amount: Money
    tds_amount: Money
    deduction_date: date
    deposited: bool = False
    deposited_date: date | None = None
    challan_number: str = ""
    quarter: str = ""


# -------------------------------------------------------------- settings ----


class PolicyOut(AppModel):
    key: str
    version: int
    value: dict[str, Any]
    description: str = ""
    is_active: bool = True


class PolicyUpdate(AppModel):
    value: dict[str, Any]
    description: str = ""


class ProviderStatusOut(AppModel):
    name: str
    model: str = ""
    is_local: bool = False
    requires_key: bool = True
    configured: bool = False
    available: bool = False
    detail: str = ""


class ApprovalRuleOut(AppModel):
    id: str
    name: str
    entity_type: str
    min_amount: Money
    max_amount: Money | None = None
    required_role: str
    auto_approve: bool = False
    min_ai_confidence: Rate
    priority: int = 100
    is_active: bool = True


# ------------------------------------------------------------ documents ----


class DocumentOut(AppModel):
    id: str
    filename: str
    mime_type: str = ""
    size_bytes: int = 0
    kind: str
    detected_kind: str = ""
    page_count: int = 0
    extraction_status: str
    extraction_method: str = ""
    extracted_fields: dict[str, Any] = {}
    field_confidence: dict[str, Any] = {}
    overall_confidence: Rate | None = None
    needs_review_reasons: list[str] = []
    extraction_errors: list[str] = []
    linked_type: str | None = None
    linked_id: str | None = None
    created_at: datetime | None = None


# --------------------------------------------------------------- imports ----


class ImportPreviewResponse(AppModel):
    columns: list[str]
    detected_mapping: dict[str, str]
    sample_rows: list[dict[str, Any]]
    total_rows: int
    warnings: list[str] = []


class ImportCommitRequest(AppModel):
    entity_type: str
    mapping: dict[str, str]
    bank_account_id: str | None = None
    dry_run: bool = False


class ImportResultOut(AppModel):
    total: int
    imported: int
    duplicates: int = 0
    rejected: int = 0
    warnings: list[str] = []
    errors: list[dict[str, Any]] = []
    batch_id: str | None = None


TokenResponse.model_rebuild()
AccountWithBalance.model_rebuild()

__all__ = [name for name in dir() if name[0].isupper()]
