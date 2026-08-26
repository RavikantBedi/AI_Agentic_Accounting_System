"""Sales endpoints: customers, invoices, receipts and credit notes."""
from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.deps import CurrentUserDep, DbDep, require_permission
from app.enums import DocumentStatus, Permission
from app.errors import ConflictError, NotFoundError
from app.models import Customer, Invoice, Receipt
from app.money import ZERO, money
from app.schemas import (
    CreditNoteCreate,
    CustomerCreate,
    CustomerOut,
    InvoiceCreate,
    InvoiceLineOut,
    InvoiceOut,
    MessageResponse,
    Page,
    ReceiptCreate,
    ReceiptOut,
)
from app.services import sales as sales_service
from app.services.numbering import next_number
from app.services.sales import LineDraft, customer_balance

router = APIRouter(prefix="/sales", tags=["Sales"])


# ------------------------------------------------------------- customers --

@router.get("/customers", response_model=Page[CustomerOut])
def list_customers(
    db: DbDep,
    current: CurrentUserDep,
    search: str | None = Query(default=None),
    include_inactive: bool = Query(default=False),
    with_balance: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[CustomerOut]:
    current.require(Permission.READ_INVOICES.value)

    stmt = select(Customer).where(Customer.company_id == current.company_id)
    if not include_inactive:
        stmt = stmt.where(Customer.is_active.is_(True))
    if search:
        pattern = f"%{search.lower()}%"
        stmt = stmt.where(
            func.lower(Customer.name).like(pattern)
            | func.lower(Customer.code).like(pattern)
            | func.lower(Customer.email).like(pattern)
            | func.lower(Customer.gstin).like(pattern)
        )

    total = int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    customers = list(db.execute(stmt.order_by(Customer.name).limit(limit).offset(offset)).scalars())

    items = []
    for customer in customers:
        out = CustomerOut.model_validate(customer)
        if with_balance:
            out.balance = customer_balance(db, current.company_id, customer.id)
        items.append(out)

    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get("/customers/{customer_id}", response_model=CustomerOut)
def get_customer(customer_id: str, db: DbDep, current: CurrentUserDep) -> CustomerOut:
    current.require(Permission.READ_INVOICES.value)
    customer = db.get(Customer, customer_id)
    if customer is None or customer.company_id != current.company_id:
        raise NotFoundError("Customer not found.")
    out = CustomerOut.model_validate(customer)
    out.balance = customer_balance(db, current.company_id, customer.id)
    return out


@router.post("/customers", response_model=CustomerOut, status_code=201)
def create_customer(
    payload: CustomerCreate,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_SALES.value))],
) -> CustomerOut:
    from app.services.tax import state_code_from_gstin, validate_gstin

    if payload.gstin:
        valid, message = validate_gstin(payload.gstin)
        if not valid:
            from app.errors import ValidationError

            raise ValidationError(f"GSTIN rejected: {message}", code="invalid_gstin")

    code = payload.code or _next_party_code(db, current.company_id, Customer, "CUST")
    existing = db.execute(
        select(Customer).where(Customer.company_id == current.company_id, Customer.code == code)
    ).scalar_one_or_none()
    if existing:
        raise ConflictError(f"Customer code '{code}' is already in use.")

    state_code = payload.state_code or state_code_from_gstin(payload.gstin)
    customer = Customer(
        company_id=current.company_id,
        code=code,
        name=payload.name,
        legal_name=payload.legal_name or payload.name,
        email=payload.email,
        phone=payload.phone,
        gstin=payload.gstin.upper() if payload.gstin else "",
        pan=payload.pan.upper() if payload.pan else "",
        gst_treatment=payload.gst_treatment,
        billing_address=payload.billing_address,
        shipping_address=payload.shipping_address or payload.billing_address,
        city=payload.city,
        state=payload.state,
        state_code=state_code,
        pincode=payload.pincode,
        payment_terms_days=payload.payment_terms_days,
        credit_limit=money(payload.credit_limit),
        currency=payload.currency,
        notes=payload.notes,
    )
    db.add(customer)
    db.flush()
    return CustomerOut.model_validate(customer)


# -------------------------------------------------------------- invoices --

@router.get("/invoices", response_model=Page[InvoiceOut])
def list_invoices(
    db: DbDep,
    current: CurrentUserDep,
    status: str | None = Query(default=None),
    customer_id: str | None = Query(default=None),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    search: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[InvoiceOut]:
    current.require(Permission.READ_INVOICES.value)

    stmt = select(Invoice).where(Invoice.company_id == current.company_id)
    if status:
        stmt = stmt.where(Invoice.status == status.upper())
    if customer_id:
        stmt = stmt.where(Invoice.customer_id == customer_id)
    if start_date:
        stmt = stmt.where(Invoice.doc_date >= start_date)
    if end_date:
        stmt = stmt.where(Invoice.doc_date <= end_date)
    if search:
        pattern = f"%{search.lower()}%"
        stmt = stmt.where(
            func.lower(Invoice.number).like(pattern) | func.lower(Invoice.reference).like(pattern)
        )

    total = int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    invoices = list(
        db.execute(
            stmt.order_by(Invoice.doc_date.desc(), Invoice.created_at.desc())
            .limit(limit)
            .offset(offset)
        ).scalars()
    )
    return Page(
        items=[_invoice_out(invoice) for invoice in invoices],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/invoices/next-number")
def peek_invoice_number(db: DbDep, current: CurrentUserDep) -> dict:
    """Preview the next invoice number without consuming it."""
    current.require(Permission.READ_INVOICES.value)
    from app.services.numbering import peek_number

    return {"number": peek_number(db, current.company_id, "SALES_INVOICE")}


@router.get("/invoices/{invoice_id}", response_model=InvoiceOut)
def get_invoice(invoice_id: str, db: DbDep, current: CurrentUserDep) -> InvoiceOut:
    current.require(Permission.READ_INVOICES.value)
    invoice = db.get(Invoice, invoice_id)
    if invoice is None or invoice.company_id != current.company_id:
        raise NotFoundError("Invoice not found.")
    return _invoice_out(invoice)


@router.post("/invoices", response_model=InvoiceOut, status_code=201)
def create_invoice(
    payload: InvoiceCreate,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_SALES.value))],
) -> InvoiceOut:
    from app.accounting.periods import ensure_fiscal_year

    ensure_fiscal_year(db, current.company_id, payload.doc_date)
    invoice = sales_service.create_invoice(
        db,
        current.company_id,
        customer_id=payload.customer_id,
        doc_date=payload.doc_date,
        due_date=payload.due_date,
        lines=[_to_draft(line) for line in payload.lines],
        reference=payload.reference,
        notes=payload.notes,
        terms=payload.terms,
        place_of_supply=payload.place_of_supply,
        bank_account_id=payload.bank_account_id,
        template=payload.template,
        user_id=current.id,
        post=payload.post,
    )
    return _invoice_out(invoice)


@router.post("/invoices/{invoice_id}/post", response_model=InvoiceOut)
def post_invoice(
    invoice_id: str,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.POST_JOURNAL.value))],
) -> InvoiceOut:
    invoice = sales_service.post_invoice(db, current.company_id, invoice_id, user_id=current.id)
    return _invoice_out(invoice)


@router.post("/invoices/{invoice_id}/cancel", response_model=InvoiceOut)
def cancel_invoice(
    invoice_id: str,
    reason: Annotated[str, Query(min_length=3, max_length=500)],
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_SALES.value))],
) -> InvoiceOut:
    invoice = sales_service.cancel_invoice(
        db, current.company_id, invoice_id, reason=reason, user_id=current.id
    )
    return _invoice_out(invoice)


# -------------------------------------------------------------- receipts --

@router.get("/receipts", response_model=Page[ReceiptOut])
def list_receipts(
    db: DbDep,
    current: CurrentUserDep,
    customer_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[ReceiptOut]:
    current.require(Permission.READ_INVOICES.value)

    stmt = select(Receipt).where(Receipt.company_id == current.company_id)
    if customer_id:
        stmt = stmt.where(Receipt.customer_id == customer_id)

    total = int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    receipts = list(
        db.execute(stmt.order_by(Receipt.receipt_date.desc()).limit(limit).offset(offset)).scalars()
    )
    return Page(
        items=[
            ReceiptOut(
                **{
                    **ReceiptOut.model_validate(receipt).model_dump(),
                    "customer_name": receipt.customer.name if receipt.customer else "",
                }
            )
            for receipt in receipts
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/receipts", response_model=ReceiptOut, status_code=201)
def create_receipt(
    payload: ReceiptCreate,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_SALES.value))],
) -> ReceiptOut:
    receipt = sales_service.record_receipt(
        db,
        current.company_id,
        customer_id=payload.customer_id,
        receipt_date=payload.receipt_date,
        amount=payload.amount,
        method=payload.method.value,
        reference=payload.reference,
        bank_account_id=payload.bank_account_id,
        tds_deducted=payload.tds_deducted,
        notes=payload.notes,
        allocations=[a.model_dump() for a in payload.allocations] if payload.allocations else None,
        auto_allocate=payload.auto_allocate,
        user_id=current.id,
    )
    out = ReceiptOut.model_validate(receipt)
    out.customer_name = receipt.customer.name if receipt.customer else ""
    return out


# ---------------------------------------------------------- credit notes --

@router.post("/credit-notes", status_code=201)
def create_credit_note(
    payload: CreditNoteCreate,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_SALES.value))],
) -> dict:
    note = sales_service.create_credit_note(
        db,
        current.company_id,
        customer_id=payload.customer_id,
        doc_date=payload.doc_date,
        lines=[_to_draft(line) for line in payload.lines],
        invoice_id=payload.invoice_id,
        reason=payload.reason,
        is_sales_return=payload.is_sales_return,
        user_id=current.id,
        post=payload.post,
    )
    return {
        "id": note.id,
        "number": note.number,
        "total": str(note.total),
        "status": note.status,
        "journal_entry_id": note.journal_entry_id,
    }


@router.post("/refresh-overdue", response_model=MessageResponse)
def refresh_overdue(
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_SALES.value))],
) -> MessageResponse:
    count = sales_service.refresh_overdue_status(db, current.company_id)
    return MessageResponse(message=f"{count} invoice(s) reviewed for overdue status.")


# --------------------------------------------------------------- helpers --

def _to_draft(line) -> LineDraft:
    return LineDraft(
        item_id=line.item_id,
        description=line.description,
        hsn_sac=line.hsn_sac,
        uom=line.uom,
        quantity=line.quantity,
        rate=line.rate,
        discount_percent=line.discount_percent,
        discount_amount=line.discount_amount,
        gst_rate=line.gst_rate,
        cess_rate=line.cess_rate,
        account_id=line.account_id,
        cost_center_id=line.cost_center_id,
        warehouse_id=line.warehouse_id,
    )


def _invoice_out(invoice: Invoice) -> InvoiceOut:
    overdue_days = 0
    if invoice.balance_due > ZERO and invoice.due_date < date.today():
        overdue_days = (date.today() - invoice.due_date).days

    return InvoiceOut(
        id=invoice.id,
        number=invoice.number,
        customer_id=invoice.customer_id,
        customer_name=invoice.customer.name if invoice.customer else "",
        doc_date=invoice.doc_date,
        due_date=invoice.due_date,
        status=invoice.status,
        reference=invoice.reference,
        place_of_supply=invoice.place_of_supply,
        supply_type=invoice.supply_type,
        subtotal=invoice.subtotal,
        discount_total=invoice.discount_total,
        taxable_value=invoice.taxable_value,
        cgst=invoice.cgst,
        sgst=invoice.sgst,
        igst=invoice.igst,
        cess=invoice.cess,
        tax_total=invoice.tax_total,
        round_off=invoice.round_off,
        total=invoice.total,
        amount_paid=invoice.amount_paid,
        amount_credited=invoice.amount_credited,
        balance_due=invoice.balance_due,
        currency=invoice.currency,
        notes=invoice.notes,
        terms=invoice.terms,
        template=invoice.template,
        journal_entry_id=invoice.journal_entry_id,
        einvoice_status=invoice.einvoice_status,
        ewaybill_status=invoice.ewaybill_status,
        days_overdue=overdue_days,
        created_at=invoice.created_at,
        lines=[InvoiceLineOut.model_validate(line) for line in invoice.lines],
    )


def _next_party_code(db, company_id: str, model, prefix: str) -> str:
    count = int(
        db.execute(select(func.count(model.id)).where(model.company_id == company_id)).scalar_one()
    )
    candidate = f"{prefix}{count + 1:04d}"
    while db.execute(
        select(model).where(model.company_id == company_id, model.code == candidate)
    ).scalar_one_or_none():
        count += 1
        candidate = f"{prefix}{count + 1:04d}"
    return candidate
