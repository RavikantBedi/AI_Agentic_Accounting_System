"""Purchase endpoints: vendors, bills, payments and debit notes."""
from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.deps import CurrentUserDep, DbDep, require_permission
from app.enums import Permission
from app.errors import ConflictError, NotFoundError, ValidationError
from app.models import Bill, Payment, Vendor
from app.money import ZERO, money
from app.schemas import (
    BillCreate,
    BillLineOut,
    BillOut,
    DebitNoteCreate,
    MessageResponse,
    Page,
    PaymentCreate,
    PaymentOut,
    VendorCreate,
    VendorOut,
)
from app.services import purchases as purchase_service
from app.services.purchases import check_duplicate_bill, vendor_balance
from app.services.sales import LineDraft

router = APIRouter(prefix="/purchases", tags=["Purchases"])


# --------------------------------------------------------------- vendors --

@router.get("/vendors", response_model=Page[VendorOut])
def list_vendors(
    db: DbDep,
    current: CurrentUserDep,
    search: str | None = Query(default=None),
    include_inactive: bool = Query(default=False),
    with_balance: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[VendorOut]:
    current.require(Permission.READ_BILLS.value)

    stmt = select(Vendor).where(Vendor.company_id == current.company_id)
    if not include_inactive:
        stmt = stmt.where(Vendor.is_active.is_(True))
    if search:
        pattern = f"%{search.lower()}%"
        stmt = stmt.where(
            func.lower(Vendor.name).like(pattern)
            | func.lower(Vendor.code).like(pattern)
            | func.lower(Vendor.gstin).like(pattern)
        )

    total = int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    vendors = list(db.execute(stmt.order_by(Vendor.name).limit(limit).offset(offset)).scalars())

    items = []
    for vendor in vendors:
        out = VendorOut.model_validate(vendor)
        if with_balance:
            out.balance = vendor_balance(db, current.company_id, vendor.id)
        items.append(out)

    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get("/vendors/{vendor_id}", response_model=VendorOut)
def get_vendor(vendor_id: str, db: DbDep, current: CurrentUserDep) -> VendorOut:
    current.require(Permission.READ_BILLS.value)
    vendor = db.get(Vendor, vendor_id)
    if vendor is None or vendor.company_id != current.company_id:
        raise NotFoundError("Vendor not found.")
    out = VendorOut.model_validate(vendor)
    out.balance = vendor_balance(db, current.company_id, vendor.id)
    return out


@router.post("/vendors", response_model=VendorOut, status_code=201)
def create_vendor(
    payload: VendorCreate,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_PURCHASES.value))],
) -> VendorOut:
    from app.services.tax import state_code_from_gstin, validate_gstin

    if payload.gstin:
        valid, message = validate_gstin(payload.gstin)
        if not valid:
            raise ValidationError(f"GSTIN rejected: {message}", code="invalid_gstin")

    code = payload.code or _next_code(db, current.company_id, "VEND")
    if db.execute(
        select(Vendor).where(Vendor.company_id == current.company_id, Vendor.code == code)
    ).scalar_one_or_none():
        raise ConflictError(f"Vendor code '{code}' is already in use.")

    vendor = Vendor(
        company_id=current.company_id,
        code=code,
        name=payload.name,
        legal_name=payload.legal_name or payload.name,
        email=payload.email,
        phone=payload.phone,
        gstin=payload.gstin.upper() if payload.gstin else "",
        pan=payload.pan.upper() if payload.pan else "",
        gst_treatment=payload.gst_treatment,
        address=payload.address,
        city=payload.city,
        state=payload.state,
        state_code=payload.state_code or state_code_from_gstin(payload.gstin),
        pincode=payload.pincode,
        payment_terms_days=payload.payment_terms_days,
        tds_applicable=payload.tds_applicable,
        default_tds_section=payload.default_tds_section,
        is_msme=payload.is_msme,
        currency=payload.currency,
        notes=payload.notes,
    )
    db.add(vendor)
    db.flush()
    return VendorOut.model_validate(vendor)


@router.patch("/vendors/{vendor_id}/bank-details", response_model=VendorOut)
def update_vendor_bank_details(
    vendor_id: str,
    account_name: Annotated[str, Query(max_length=200)],
    account_number: Annotated[str, Query(max_length=40)],
    ifsc: Annotated[str, Query(max_length=11)],
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_PURCHASES.value))],
) -> VendorOut:
    """Change vendor bank details.

    This is audited with before/after values and starts the payment hold window
    — bank-detail changes immediately before payment are a known fraud pattern.
    """
    from app.enums import ActorType, AuditAction
    from app.services.audit import record_event

    vendor = db.get(Vendor, vendor_id)
    if vendor is None or vendor.company_id != current.company_id:
        raise NotFoundError("Vendor not found.")

    before = {
        "bank_account_name": vendor.bank_account_name,
        "bank_account_number": _mask(vendor.bank_account_number),
        "bank_ifsc": vendor.bank_ifsc,
    }

    vendor.bank_account_name = account_name
    vendor.bank_account_number = account_number
    vendor.bank_ifsc = ifsc.upper()
    vendor.bank_details_updated_at = date.today()
    db.flush()

    record_event(
        db,
        company_id=current.company_id,
        action=AuditAction.UPDATE,
        entity_type="Vendor",
        entity_id=vendor.id,
        entity_label=vendor.name,
        summary=f"Bank details changed for vendor {vendor.name}",
        reason="Payments to this vendor are held until the change is verified.",
        actor_type=ActorType.USER,
        actor_id=current.id,
        actor_label=current.email,
        before=before,
        after={
            "bank_account_name": account_name,
            "bank_account_number": _mask(account_number),
            "bank_ifsc": ifsc.upper(),
        },
    )
    return VendorOut.model_validate(vendor)


# ----------------------------------------------------------------- bills --

@router.get("/bills", response_model=Page[BillOut])
def list_bills(
    db: DbDep,
    current: CurrentUserDep,
    status: str | None = Query(default=None),
    vendor_id: str | None = Query(default=None),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    search: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[BillOut]:
    current.require(Permission.READ_BILLS.value)

    stmt = select(Bill).where(Bill.company_id == current.company_id)
    if status:
        stmt = stmt.where(Bill.status == status.upper())
    if vendor_id:
        stmt = stmt.where(Bill.vendor_id == vendor_id)
    if start_date:
        stmt = stmt.where(Bill.doc_date >= start_date)
    if end_date:
        stmt = stmt.where(Bill.doc_date <= end_date)
    if search:
        pattern = f"%{search.lower()}%"
        stmt = stmt.where(
            func.lower(Bill.number).like(pattern)
            | func.lower(Bill.vendor_invoice_number).like(pattern)
        )

    total = int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    bills = list(
        db.execute(
            stmt.order_by(Bill.doc_date.desc(), Bill.created_at.desc()).limit(limit).offset(offset)
        ).scalars()
    )
    return Page(items=[_bill_out(bill) for bill in bills], total=total, limit=limit, offset=offset)


@router.get("/bills/{bill_id}", response_model=BillOut)
def get_bill(bill_id: str, db: DbDep, current: CurrentUserDep) -> BillOut:
    current.require(Permission.READ_BILLS.value)
    bill = db.get(Bill, bill_id)
    if bill is None or bill.company_id != current.company_id:
        raise NotFoundError("Bill not found.")
    return _bill_out(bill)


@router.post("/bills/check-duplicate")
def check_duplicate(
    vendor_id: Annotated[str, Query()],
    vendor_invoice_number: Annotated[str, Query()],
    amount: Annotated[float, Query()],
    doc_date: Annotated[date, Query()],
    db: DbDep,
    current: CurrentUserDep,
) -> dict:
    """Duplicate check the UI runs before a bill is submitted."""
    current.require(Permission.READ_BILLS.value)
    return check_duplicate_bill(
        db,
        current.company_id,
        vendor_id=vendor_id,
        vendor_invoice_number=vendor_invoice_number,
        amount=money(amount),
        doc_date=doc_date,
    )


@router.post("/bills", response_model=BillOut, status_code=201)
def create_bill(
    payload: BillCreate,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_PURCHASES.value))],
) -> BillOut:
    from app.accounting.periods import ensure_fiscal_year

    ensure_fiscal_year(db, current.company_id, payload.doc_date)
    bill = purchase_service.create_bill(
        db,
        current.company_id,
        vendor_id=payload.vendor_id,
        doc_date=payload.doc_date,
        vendor_invoice_number=payload.vendor_invoice_number,
        vendor_invoice_date=payload.vendor_invoice_date,
        due_date=payload.due_date,
        lines=[_to_draft(line) for line in payload.lines],
        reference=payload.reference,
        notes=payload.notes,
        place_of_supply=payload.place_of_supply,
        itc_eligible=payload.itc_eligible,
        tds_section=payload.tds_section,
        apply_tds=payload.apply_tds,
        document_id=payload.document_id,
        user_id=current.id,
        post=payload.post,
        allow_duplicate=payload.allow_duplicate,
    )
    return _bill_out(bill)


@router.post("/bills/{bill_id}/post", response_model=BillOut)
def post_bill(
    bill_id: str,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.POST_JOURNAL.value))],
) -> BillOut:
    bill = purchase_service.post_bill(db, current.company_id, bill_id, user_id=current.id)
    return _bill_out(bill)


@router.post("/bills/{bill_id}/cancel", response_model=BillOut)
def cancel_bill(
    bill_id: str,
    reason: Annotated[str, Query(min_length=3, max_length=500)],
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_PURCHASES.value))],
) -> BillOut:
    bill = purchase_service.cancel_bill(
        db, current.company_id, bill_id, reason=reason, user_id=current.id
    )
    return _bill_out(bill)


# -------------------------------------------------------------- payments --

@router.get("/payments", response_model=Page[PaymentOut])
def list_payments(
    db: DbDep,
    current: CurrentUserDep,
    vendor_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[PaymentOut]:
    current.require(Permission.READ_BILLS.value)

    stmt = select(Payment).where(Payment.company_id == current.company_id)
    if vendor_id:
        stmt = stmt.where(Payment.vendor_id == vendor_id)

    total = int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    payments = list(
        db.execute(stmt.order_by(Payment.payment_date.desc()).limit(limit).offset(offset)).scalars()
    )
    items = []
    for payment in payments:
        out = PaymentOut.model_validate(payment)
        out.vendor_name = payment.vendor.name if payment.vendor else ""
        items.append(out)
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.post("/payments", response_model=PaymentOut, status_code=201)
def create_payment(
    payload: PaymentCreate,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.RELEASE_PAYMENT.value))],
) -> PaymentOut:
    """Release a payment to a vendor.

    Requires the ``release_payment`` permission, which AI agents can never hold.
    """
    payment = purchase_service.record_payment(
        db,
        current.company_id,
        vendor_id=payload.vendor_id,
        payment_date=payload.payment_date,
        amount=payload.amount,
        method=payload.method.value,
        reference=payload.reference,
        bank_account_id=payload.bank_account_id,
        notes=payload.notes,
        allocations=[a.model_dump() for a in payload.allocations] if payload.allocations else None,
        auto_allocate=payload.auto_allocate,
        user_id=current.id,
        released_by_user_id=current.id,
    )
    out = PaymentOut.model_validate(payment)
    out.vendor_name = payment.vendor.name if payment.vendor else ""
    return out


@router.post("/debit-notes", status_code=201)
def create_debit_note(
    payload: DebitNoteCreate,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_PURCHASES.value))],
) -> dict:
    note = purchase_service.create_debit_note(
        db,
        current.company_id,
        vendor_id=payload.vendor_id,
        doc_date=payload.doc_date,
        lines=[_to_draft(line) for line in payload.lines],
        bill_id=payload.bill_id,
        reason=payload.reason,
        is_purchase_return=payload.is_purchase_return,
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
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_PURCHASES.value))],
) -> MessageResponse:
    count = purchase_service.refresh_overdue_bills(db, current.company_id)
    return MessageResponse(message=f"{count} bill(s) reviewed for overdue status.")


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


def _bill_out(bill: Bill) -> BillOut:
    overdue_days = 0
    if bill.balance_due > ZERO and bill.due_date < date.today():
        overdue_days = (date.today() - bill.due_date).days

    return BillOut(
        id=bill.id,
        number=bill.number,
        vendor_invoice_number=bill.vendor_invoice_number,
        vendor_id=bill.vendor_id,
        vendor_name=bill.vendor.name if bill.vendor else "",
        doc_date=bill.doc_date,
        due_date=bill.due_date,
        status=bill.status,
        taxable_value=bill.taxable_value,
        cgst=bill.cgst,
        sgst=bill.sgst,
        igst=bill.igst,
        cess=bill.cess,
        tax_total=bill.tax_total,
        round_off=bill.round_off,
        total=bill.total,
        amount_paid=bill.amount_paid,
        amount_debited=bill.amount_debited,
        tds_amount=bill.tds_amount,
        tds_section=bill.tds_section,
        balance_due=bill.balance_due,
        itc_eligible=bill.itc_eligible,
        currency=bill.currency,
        notes=bill.notes,
        journal_entry_id=bill.journal_entry_id,
        document_id=bill.document_id,
        days_overdue=overdue_days,
        created_at=bill.created_at,
        lines=[BillLineOut.model_validate(line) for line in bill.lines],
    )


def _mask(value: str) -> str:
    return f"****{value[-4:]}" if value and len(value) > 4 else "****"


def _next_code(db, company_id: str, prefix: str) -> str:
    count = int(
        db.execute(select(func.count(Vendor.id)).where(Vendor.company_id == company_id)).scalar_one()
    )
    candidate = f"{prefix}{count + 1:04d}"
    while db.execute(
        select(Vendor).where(Vendor.company_id == company_id, Vendor.code == candidate)
    ).scalar_one_or_none():
        count += 1
        candidate = f"{prefix}{count + 1:04d}"
    return candidate
