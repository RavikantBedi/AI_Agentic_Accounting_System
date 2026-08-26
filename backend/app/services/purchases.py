"""Purchase service: bills, TDS, payments, debit notes and the AP sub-ledger.

Posting a bill produces:

    Dr Expense / Inventory            taxable value
    Dr Input CGST / SGST / IGST       tax (only when ITC is eligible)
        Cr TDS Payable                    tax deducted at source
        Cr Accounts Payable               net payable
        Cr/Dr Round Off                   rounding

Duplicate detection runs *before* anything is written: the same vendor invoice
number can never be booked twice, and near-duplicates are surfaced as warnings.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.accounting.engine import AccountingEngine, EntryInput, LineInput
from app.accounting.periods import fiscal_year_bounds, require_open_period
from app.enums import (
    ActorType,
    AuditAction,
    DocumentStatus,
    EntrySource,
    PartyType,
    StockMoveType,
    VoucherType,
)
from app.errors import ConflictError, DuplicateDocumentError, NotFoundError, PolicyViolation, ValidationError
from app.models import (
    Bill,
    BillLine,
    Company,
    DebitNote,
    DebitNoteLine,
    Item,
    Payment,
    PaymentAllocation,
    TDSEntry,
    Vendor,
)
from app.money import ZERO, D, money, quantity as to_qty, rate as to_rate
from app.services import inventory as inventory_service
from app.services.numbering import next_number
from app.services.policy import get_policy
from app.services.sales import LineDraft, compute_totals
from app.services.tax import compute_tds


def check_duplicate_bill(
    db: Session,
    company_id: str,
    *,
    vendor_id: str,
    vendor_invoice_number: str,
    amount: Decimal,
    doc_date: date,
    exclude_bill_id: str | None = None,
) -> dict:
    """Look for exact and probable duplicates before booking a bill.

    Exact  : same vendor + same vendor invoice number.
    Likely : same vendor + same amount within the configured window.
    """
    result = {"is_duplicate": False, "exact_match": None, "similar": [], "reason": ""}
    window_days = int(get_policy(db, company_id, "anomaly.detection").get("duplicate_window_days", 90))

    reference = (vendor_invoice_number or "").strip()
    if reference:
        stmt = select(Bill).where(
            Bill.company_id == company_id,
            Bill.vendor_id == vendor_id,
            func.lower(Bill.vendor_invoice_number) == reference.lower(),
            Bill.status != DocumentStatus.CANCELLED,
        )
        if exclude_bill_id:
            stmt = stmt.where(Bill.id != exclude_bill_id)
        exact = db.execute(stmt).scalars().first()
        if exact:
            result.update(
                is_duplicate=True,
                exact_match={
                    "id": exact.id,
                    "number": exact.number,
                    "vendor_invoice_number": exact.vendor_invoice_number,
                    "date": exact.doc_date,
                    "total": exact.total,
                    "status": exact.status,
                },
                reason=(
                    f"Vendor invoice '{reference}' was already booked as {exact.number} "
                    f"on {exact.doc_date} for {exact.total}."
                ),
            )
            return result

    amount = money(amount)
    stmt = select(Bill).where(
        Bill.company_id == company_id,
        Bill.vendor_id == vendor_id,
        Bill.total == amount,
        Bill.doc_date >= doc_date - timedelta(days=window_days),
        Bill.doc_date <= doc_date + timedelta(days=window_days),
        Bill.status != DocumentStatus.CANCELLED,
    )
    if exclude_bill_id:
        stmt = stmt.where(Bill.id != exclude_bill_id)

    similar = list(db.execute(stmt.limit(5)).scalars())
    if similar:
        result["similar"] = [
            {
                "id": bill.id,
                "number": bill.number,
                "vendor_invoice_number": bill.vendor_invoice_number,
                "date": bill.doc_date,
                "total": bill.total,
                "status": bill.status,
            }
            for bill in similar
        ]
        result["reason"] = (
            f"{len(similar)} bill(s) from this vendor for exactly {amount} exist within "
            f"{window_days} days. Confirm this is not a re-submission."
        )
    return result


def create_bill(
    db: Session,
    company_id: str,
    *,
    vendor_id: str,
    doc_date: date,
    lines: list[LineDraft],
    vendor_invoice_number: str,
    vendor_invoice_date: date | None = None,
    due_date: date | None = None,
    reference: str = "",
    notes: str = "",
    place_of_supply: str = "",
    branch_id: str | None = None,
    itc_eligible: bool = True,
    tds_section: str | None = None,
    apply_tds: bool = True,
    document_id: str | None = None,
    extraction_confidence: Decimal | None = None,
    user_id: str | None = None,
    post: bool = True,
    allow_duplicate: bool = False,
) -> Bill:
    """Book a vendor bill, computing GST and TDS deterministically."""
    company = db.get(Company, company_id)
    vendor = db.get(Vendor, vendor_id)
    if vendor is None or vendor.company_id != company_id:
        raise NotFoundError("Vendor not found.")
    if not (vendor_invoice_number or "").strip():
        raise ValidationError("The vendor's own invoice number is required for a bill.")

    pos = place_of_supply or company.state_code
    totals = compute_totals(
        db,
        company,
        lines=lines,
        doc_date=doc_date,
        place_of_supply=pos,
        gst_treatment=vendor.gst_treatment,
    )

    duplicate = check_duplicate_bill(
        db,
        company_id,
        vendor_id=vendor_id,
        vendor_invoice_number=vendor_invoice_number,
        amount=totals.total,
        doc_date=doc_date,
    )
    # An exact repeat of a vendor's own invoice number is refused unconditionally.
    # ``allow_duplicate`` only waives the softer "same vendor, same amount"
    # warning — it cannot override the database uniqueness constraint, and
    # pretending otherwise would fail confusingly at flush time.
    if duplicate["is_duplicate"]:
        raise DuplicateDocumentError(
            duplicate["reason"]
            + " Vendor invoice numbers must be unique per vendor. If this is a genuinely "
            "different invoice, correct the reference; if the original was wrong, cancel it first.",
            details=duplicate,
        )
    if duplicate["similar"] and not allow_duplicate:
        raise DuplicateDocumentError(
            duplicate["reason"] + " Re-submit with allow_duplicate=true to book it anyway.",
            details=duplicate,
        )

    if due_date is None:
        due_date = doc_date + timedelta(days=int(vendor.payment_terms_days or 30))

    # TDS is computed on the taxable value (not on GST), from a configured rule.
    tds_amount = ZERO
    tds_rate = ZERO
    resolved_section = ""
    tds_result = None
    section = tds_section or vendor.default_tds_section
    if apply_tds and vendor.tds_applicable and section:
        fy_start, _, _ = fiscal_year_bounds(company, doc_date)
        tds_result = compute_tds(
            db,
            company_id,
            vendor=vendor,
            section=section,
            base_amount=totals.taxable_value,
            on_date=doc_date,
            fiscal_year_start=fy_start,
        )
        if tds_result.applicable:
            tds_amount = tds_result.tds_amount
            tds_rate = tds_result.rate
            resolved_section = tds_result.section

    bill = Bill(
        company_id=company_id,
        number=next_number(db, company_id, "BILL", doc_date),
        vendor_id=vendor_id,
        vendor_invoice_number=vendor_invoice_number.strip(),
        vendor_invoice_date=vendor_invoice_date or doc_date,
        doc_date=doc_date,
        due_date=due_date,
        reference=reference,
        branch_id=branch_id,
        currency=vendor.currency or company.currency,
        place_of_supply=pos,
        supply_type=totals.supply_type,
        subtotal=totals.subtotal,
        discount_total=totals.discount_total,
        taxable_value=totals.taxable_value,
        cgst=totals.cgst,
        sgst=totals.sgst,
        igst=totals.igst,
        cess=totals.cess,
        tax_total=totals.tax_total,
        round_off=totals.round_off,
        total=totals.total,
        notes=notes,
        itc_eligible=itc_eligible,
        tds_section=resolved_section,
        tds_rate=tds_rate,
        tds_amount=tds_amount,
        document_id=document_id,
        extraction_confidence=extraction_confidence,
        duplicate_of_bill_id=(duplicate["exact_match"] or {}).get("id") if allow_duplicate else None,
        status=DocumentStatus.DRAFT,
        created_by_user_id=user_id,
    )
    db.add(bill)
    db.flush()

    for index, computed in enumerate(totals.lines, start=1):
        draft = computed.draft
        item = computed.item
        db.add(
            BillLine(
                company_id=company_id,
                bill_id=bill.id,
                line_number=index,
                item_id=draft.item_id,
                description=draft.description or (item.name if item else ""),
                hsn_sac=draft.hsn_sac or (item.hsn_sac if item else ""),
                uom=draft.uom or (item.uom if item else "NOS"),
                quantity=to_qty(draft.quantity),
                rate=money(draft.rate),
                discount_percent=to_rate(draft.discount_percent),
                discount_amount=computed.discount_amount,
                gross_amount=computed.gross_amount,
                taxable_value=computed.taxable_value,
                gst_rate=computed.tax.rate,
                cgst=computed.tax.cgst,
                sgst=computed.tax.sgst,
                igst=computed.tax.igst,
                cess=computed.tax.cess,
                line_total=computed.line_total,
                account_id=draft.account_id,
                cost_center_id=draft.cost_center_id,
                warehouse_id=draft.warehouse_id,
                is_inventory=bool(item and item.track_inventory),
            )
        )
    db.flush()

    if tds_result is not None and tds_result.applicable:
        db.add(
            TDSEntry(
                company_id=company_id,
                vendor_id=vendor_id,
                bill_id=bill.id,
                section=tds_result.section,
                section_rule_id=tds_result.rule_id,
                rate=tds_result.rate,
                base_amount=tds_result.base_amount,
                tds_amount=tds_result.tds_amount,
                deduction_date=doc_date,
                quarter=_quarter_label(company, doc_date),
                validated_by_rule=True,
                suggested_by_ai=bool(tds_section and tds_section != vendor.default_tds_section),
                notes=tds_result.reason,
            )
        )
        db.flush()

    if post:
        post_bill(db, company_id, bill.id, user_id=user_id)
    return bill


def post_bill(db: Session, company_id: str, bill_id: str, *, user_id: str | None = None) -> Bill:
    """Post a bill to the ledger and receive stock for inventory lines."""
    bill = _get_bill(db, company_id, bill_id)
    if bill.journal_entry_id:
        raise ConflictError(f"Bill {bill.number} is already posted.")
    if bill.status == DocumentStatus.CANCELLED:
        raise ConflictError(f"Bill {bill.number} is cancelled.")

    require_open_period(db, company_id, bill.doc_date)
    engine = AccountingEngine(db, company_id, actor_user_id=user_id)
    vendor = db.get(Vendor, bill.vendor_id)

    lines: list[LineInput] = []

    # Debit side: expense or inventory, grouped by account.
    expense_by_account: dict[tuple[str, str | None], Decimal] = {}
    for line in bill.lines:
        account_id = line.account_id
        item = db.get(Item, line.item_id) if line.item_id else None
        if not account_id:
            if item is not None and item.track_inventory:
                account_id = item.inventory_account_id or engine.account_by_system_key("INVENTORY").id
            elif item is not None and item.expense_account_id:
                account_id = item.expense_account_id
            elif vendor.default_expense_account_id:
                account_id = vendor.default_expense_account_id
            else:
                account_id = engine.account_by_system_key("PURCHASES").id
        key = (account_id, line.cost_center_id)
        expense_by_account[key] = money(expense_by_account.get(key, ZERO) + line.taxable_value)

    for (account_id, cost_center_id), amount in expense_by_account.items():
        if amount == ZERO:
            continue
        lines.append(
            LineInput(
                account_id=account_id,
                debit=amount,
                description=f"Bill {bill.number} — {vendor.name}",
                cost_center_id=cost_center_id,
                branch_id=bill.branch_id,
            )
        )

    # Input tax credit. When ITC is not eligible the tax is a cost, not an asset.
    tax_components = (
        ("CGST", bill.cgst, "GST_INPUT_CGST"),
        ("SGST", bill.sgst, "GST_INPUT_SGST"),
        ("IGST", bill.igst, "GST_INPUT_IGST"),
        ("CESS", bill.cess, "GST_INPUT_CESS"),
    )
    if bill.itc_eligible:
        for component, amount, key in tax_components:
            if money(amount) != ZERO:
                lines.append(
                    LineInput(
                        account_id=engine.account_by_system_key(key).id,
                        debit=money(amount),
                        description=f"Input {component} — bill {bill.number}",
                        tax_component=component,
                        taxable_value=bill.taxable_value,
                        branch_id=bill.branch_id,
                    )
                )
    elif bill.tax_total != ZERO:
        first_account = next(iter(expense_by_account), (engine.account_by_system_key("PURCHASES").id, None))
        lines.append(
            LineInput(
                account_id=first_account[0],
                debit=bill.tax_total,
                description=f"GST (ITC not eligible) — bill {bill.number}",
                branch_id=bill.branch_id,
            )
        )

    if bill.round_off != ZERO:
        round_account = engine.account_by_system_key("ROUND_OFF")
        if bill.round_off > ZERO:
            lines.append(
                LineInput(account_id=round_account.id, debit=bill.round_off, description="Rounding")
            )
        else:
            lines.append(
                LineInput(account_id=round_account.id, credit=-bill.round_off, description="Rounding")
            )

    if bill.tds_amount > ZERO:
        lines.append(
            LineInput(
                account_id=engine.account_by_system_key("TDS_PAYABLE").id,
                credit=bill.tds_amount,
                description=f"TDS {bill.tds_section} @ {bill.tds_rate}% — bill {bill.number}",
                party_type=PartyType.VENDOR,
                party_id=vendor.id,
            )
        )

    payable = money(bill.total - bill.tds_amount)
    ap_account = engine.account_by_system_key("ACCOUNTS_PAYABLE")
    if vendor.payable_account_id:
        override = engine.get_account(account_id=vendor.payable_account_id)
        if override is not None and not override.is_group and override.is_active:
            ap_account = override

    lines.append(
        LineInput(
            account_id=ap_account.id,
            credit=payable,
            description=f"Payable — {vendor.name} ({bill.vendor_invoice_number})",
            party_type=PartyType.VENDOR,
            party_id=vendor.id,
            branch_id=bill.branch_id,
        )
    )

    journal = engine.create_and_post(
        EntryInput(
            entry_date=bill.doc_date,
            voucher_type=VoucherType.PURCHASE,
            narration=f"Bill {bill.number} from {vendor.name} ({bill.vendor_invoice_number})",
            reference=bill.vendor_invoice_number,
            currency=bill.currency,
            branch_id=bill.branch_id,
            lines=lines,
            source=EntrySource.SYSTEM,
            source_type="Bill",
            source_id=bill.id,
        )
    )

    bill.journal_entry_id = journal.id
    bill.status = DocumentStatus.APPROVED

    _receive_stock(db, company_id, bill, user_id=user_id)
    db.flush()

    _audit(
        db,
        company_id,
        "Bill",
        bill.id,
        bill.number,
        AuditAction.POST,
        f"Posted bill {bill.number} from {vendor.name} for {bill.total}",
        user_id,
        amount=bill.total,
    )
    return bill


def _receive_stock(db: Session, company_id: str, bill: Bill, *, user_id: str | None) -> None:
    """Increase stock for inventory lines at the purchase rate."""
    for line in bill.lines:
        if not line.item_id:
            continue
        item = db.get(Item, line.item_id)
        if item is None or not item.track_inventory:
            continue
        unit_cost = (
            money(line.taxable_value / to_qty(line.quantity))
            if to_qty(line.quantity) > 0
            else money(line.rate)
        )
        inventory_service.record_move(
            db,
            company_id,
            item_id=item.id,
            warehouse_id=line.warehouse_id,
            move_date=bill.doc_date,
            move_type=StockMoveType.PURCHASE,
            quantity=to_qty(line.quantity),
            rate=unit_cost,
            source_type="Bill",
            source_id=bill.id,
            reference=bill.number,
            narration=f"Purchase — bill {bill.number}",
            user_id=user_id,
        )


def cancel_bill(
    db: Session, company_id: str, bill_id: str, *, reason: str, user_id: str | None = None
) -> Bill:
    bill = _get_bill(db, company_id, bill_id)
    if bill.amount_paid > ZERO:
        raise ConflictError(
            f"Bill {bill.number} has payments against it. Reverse them before cancelling."
        )
    if bill.status == DocumentStatus.CANCELLED:
        raise ConflictError("Bill is already cancelled.")

    engine = AccountingEngine(db, company_id, actor_user_id=user_id)
    if bill.journal_entry_id:
        engine.reverse(bill.journal_entry_id, reason=f"Bill {bill.number} cancelled: {reason}")

    from app.models import StockMove

    for move in db.execute(
        select(StockMove).where(
            StockMove.company_id == company_id,
            StockMove.source_type == "Bill",
            StockMove.source_id == bill.id,
            StockMove.move_type == StockMoveType.PURCHASE,
        )
    ).scalars():
        inventory_service.record_move(
            db,
            company_id,
            item_id=move.item_id,
            warehouse_id=move.warehouse_id,
            move_date=date.today(),
            move_type=StockMoveType.PURCHASE_RETURN,
            quantity=-move.quantity,
            rate=move.rate,
            source_type="BillCancellation",
            source_id=bill.id,
            reference=bill.number,
            narration=f"Stock reversed — bill {bill.number} cancelled",
            user_id=user_id,
        )

    bill.status = DocumentStatus.CANCELLED
    db.flush()
    _audit(
        db,
        company_id,
        "Bill",
        bill.id,
        bill.number,
        AuditAction.UPDATE,
        f"Cancelled bill {bill.number}",
        user_id,
        reason=reason,
        amount=bill.total,
    )
    return bill


# ------------------------------------------------------------- payments ----

def record_payment(
    db: Session,
    company_id: str,
    *,
    vendor_id: str,
    payment_date: date,
    amount: Decimal,
    payment_account_id: str | None = None,
    bank_account_id: str | None = None,
    method: str = "BANK_TRANSFER",
    reference: str = "",
    allocations: list[dict] | None = None,
    notes: str = "",
    user_id: str | None = None,
    released_by_user_id: str | None = None,
    auto_allocate: bool = True,
) -> Payment:
    """Record a payment to a vendor.

    ``released_by_user_id`` must be a real user: an AI agent can prepare a
    payment but can never release one. The policy engine enforces this too, but
    the check is repeated here so the rule holds no matter the call path.
    """
    if released_by_user_id is None and user_id is None:
        raise PolicyViolation(
            "A payment must be released by an authenticated user. "
            "AI agents may prepare payments but never release them.",
            code="payment_requires_human",
        )

    vendor = db.get(Vendor, vendor_id)
    if vendor is None or vendor.company_id != company_id:
        raise NotFoundError("Vendor not found.")

    amount = money(amount)
    if amount <= ZERO:
        raise ValidationError("Payment amount must be greater than zero.")
    require_open_period(db, company_id, payment_date)

    # Fraud control: refuse to pay right after the bank details changed.
    ap_policy = get_policy(db, company_id, "ap.payments")
    block_days = int(ap_policy.get("block_payment_on_bank_change_days", 0))
    if block_days and vendor.bank_details_updated_at:
        days_since = (payment_date - vendor.bank_details_updated_at).days
        if 0 <= days_since < block_days:
            raise PolicyViolation(
                f"{vendor.name}'s bank details changed {days_since} day(s) ago. "
                f"Payments are held for {block_days} days after a bank-detail change. "
                "Verify the change with the vendor through a known contact, then override.",
                code="vendor_bank_change_hold",
            )

    engine = AccountingEngine(db, company_id, actor_user_id=user_id)
    if payment_account_id is None:
        if bank_account_id:
            from app.models import BankAccount

            bank = db.get(BankAccount, bank_account_id)
            if bank is None or bank.company_id != company_id:
                raise NotFoundError("Bank account not found.")
            payment_account_id = bank.gl_account_id
        else:
            payment_account_id = engine.account_by_system_key("BANK_DEFAULT").id

    payment = Payment(
        company_id=company_id,
        number=next_number(db, company_id, "PAYMENT", payment_date),
        vendor_id=vendor_id,
        payment_date=payment_date,
        amount=amount,
        unallocated_amount=amount,
        currency=vendor.currency or "INR",
        method=method,
        reference=reference,
        bank_account_id=bank_account_id,
        payment_account_id=payment_account_id,
        notes=notes,
        status=DocumentStatus.DRAFT,
        created_by_user_id=user_id,
        released_by_user_id=released_by_user_id or user_id,
    )
    db.add(payment)
    db.flush()

    _allocate_payment(db, company_id, payment, allocations, auto_allocate=auto_allocate)

    ap_account = engine.account_by_system_key("ACCOUNTS_PAYABLE")
    journal = engine.create_and_post(
        EntryInput(
            entry_date=payment_date,
            voucher_type=VoucherType.PAYMENT,
            narration=f"Payment {payment.number} to {vendor.name}",
            reference=reference or payment.number,
            lines=[
                LineInput(
                    account_id=ap_account.id,
                    debit=amount,
                    description=f"Payment to {vendor.name}",
                    party_type=PartyType.VENDOR,
                    party_id=vendor.id,
                ),
                LineInput(
                    account_id=payment_account_id,
                    credit=amount,
                    description=f"Payment {payment.number} to {vendor.name}",
                    bank_account_id=bank_account_id,
                ),
            ],
            source=EntrySource.SYSTEM,
            source_type="Payment",
            source_id=payment.id,
        )
    )
    payment.journal_entry_id = journal.id
    payment.status = DocumentStatus.APPROVED
    db.flush()

    _audit(
        db,
        company_id,
        "Payment",
        payment.id,
        payment.number,
        AuditAction.POST,
        f"Released payment {payment.number} of {amount} to {vendor.name}",
        released_by_user_id or user_id,
        amount=amount,
    )
    return payment


def _allocate_payment(
    db: Session,
    company_id: str,
    payment: Payment,
    allocations: list[dict] | None,
    *,
    auto_allocate: bool,
) -> list[PaymentAllocation]:
    remaining = money(payment.amount)
    created: list[PaymentAllocation] = []

    if allocations:
        for allocation in allocations:
            bill = _get_bill(db, company_id, allocation["bill_id"])
            requested = money(allocation.get("amount", bill.balance_due))
            if requested <= ZERO:
                continue
            if requested > remaining:
                raise ValidationError(
                    f"Allocations exceed the payment by {money(requested - remaining)}."
                )
            if requested > bill.balance_due:
                raise ValidationError(
                    f"Cannot allocate {requested} to bill {bill.number}: "
                    f"only {bill.balance_due} is outstanding."
                )
            created.append(_apply_payment_allocation(db, company_id, payment, bill, requested))
            remaining = money(remaining - requested)
    elif auto_allocate:
        open_bills = list(
            db.execute(
                select(Bill)
                .where(
                    Bill.company_id == company_id,
                    Bill.vendor_id == payment.vendor_id,
                    Bill.status.in_(
                        [DocumentStatus.APPROVED, DocumentStatus.PARTIALLY_PAID, DocumentStatus.OVERDUE]
                    ),
                )
                .order_by(Bill.due_date.asc(), Bill.doc_date.asc())
            ).scalars()
        )
        for bill in open_bills:
            if remaining <= ZERO:
                break
            due = bill.balance_due
            if due <= ZERO:
                continue
            applied = min(due, remaining)
            created.append(_apply_payment_allocation(db, company_id, payment, bill, applied))
            remaining = money(remaining - applied)

    payment.unallocated_amount = max(ZERO, remaining)
    db.flush()
    return created


def _apply_payment_allocation(
    db: Session, company_id: str, payment: Payment, bill: Bill, amount: Decimal
) -> PaymentAllocation:
    allocation = PaymentAllocation(
        company_id=company_id, payment_id=payment.id, bill_id=bill.id, amount=amount
    )
    db.add(allocation)
    bill.amount_paid = money(bill.amount_paid + amount)
    _refresh_bill_status(bill)
    db.flush()
    return allocation


def _refresh_bill_status(bill: Bill) -> None:
    if bill.balance_due <= ZERO:
        bill.status = DocumentStatus.PAID
    elif bill.amount_paid > ZERO or bill.amount_debited > ZERO:
        bill.status = DocumentStatus.PARTIALLY_PAID
    elif bill.due_date < date.today():
        bill.status = DocumentStatus.OVERDUE
    else:
        bill.status = DocumentStatus.APPROVED


def refresh_overdue_bills(db: Session, company_id: str, *, as_of: date | None = None) -> int:
    as_of = as_of or date.today()
    bills = list(
        db.execute(
            select(Bill).where(
                Bill.company_id == company_id,
                Bill.status.in_([DocumentStatus.APPROVED, DocumentStatus.PARTIALLY_PAID]),
                Bill.due_date < as_of,
            )
        ).scalars()
    )
    for bill in bills:
        if bill.balance_due > ZERO:
            bill.status = DocumentStatus.OVERDUE
    db.flush()
    return len(bills)


# ----------------------------------------------------------- debit notes ---

def create_debit_note(
    db: Session,
    company_id: str,
    *,
    vendor_id: str,
    doc_date: date,
    lines: list[LineDraft],
    bill_id: str | None = None,
    reason: str = "",
    is_purchase_return: bool = False,
    user_id: str | None = None,
    post: bool = True,
) -> DebitNote:
    """Raise a debit note against a vendor (purchase return or price correction)."""
    company = db.get(Company, company_id)
    vendor = db.get(Vendor, vendor_id)
    if vendor is None or vendor.company_id != company_id:
        raise NotFoundError("Vendor not found.")

    bill = _get_bill(db, company_id, bill_id) if bill_id else None
    pos = (bill.place_of_supply if bill else company.state_code) or company.state_code
    totals = compute_totals(
        db,
        company,
        lines=lines,
        doc_date=doc_date,
        place_of_supply=pos,
        gst_treatment=vendor.gst_treatment,
    )

    if bill is not None:
        max_debitable = money(bill.total - bill.amount_debited)
        if totals.total > max_debitable:
            raise ValidationError(
                f"Debit note {totals.total} exceeds the {max_debitable} still debitable "
                f"against bill {bill.number}."
            )

    note = DebitNote(
        company_id=company_id,
        number=next_number(db, company_id, "DEBIT_NOTE", doc_date),
        vendor_id=vendor_id,
        bill_id=bill_id,
        doc_date=doc_date,
        reason=reason,
        is_purchase_return=is_purchase_return,
        currency=vendor.currency or company.currency,
        place_of_supply=pos,
        supply_type=totals.supply_type,
        subtotal=totals.subtotal,
        taxable_value=totals.taxable_value,
        cgst=totals.cgst,
        sgst=totals.sgst,
        igst=totals.igst,
        cess=totals.cess,
        tax_total=totals.tax_total,
        round_off=totals.round_off,
        total=totals.total,
        status=DocumentStatus.DRAFT,
        created_by_user_id=user_id,
    )
    db.add(note)
    db.flush()

    for index, computed in enumerate(totals.lines, start=1):
        draft = computed.draft
        db.add(
            DebitNoteLine(
                company_id=company_id,
                debit_note_id=note.id,
                line_number=index,
                item_id=draft.item_id,
                description=draft.description or (computed.item.name if computed.item else ""),
                hsn_sac=draft.hsn_sac,
                uom=draft.uom,
                quantity=to_qty(draft.quantity),
                rate=money(draft.rate),
                gross_amount=computed.gross_amount,
                taxable_value=computed.taxable_value,
                gst_rate=computed.tax.rate,
                cgst=computed.tax.cgst,
                sgst=computed.tax.sgst,
                igst=computed.tax.igst,
                cess=computed.tax.cess,
                line_total=computed.line_total,
                warehouse_id=draft.warehouse_id,
            )
        )
    db.flush()

    if post:
        post_debit_note(db, company_id, note.id, user_id=user_id)
    return note


def post_debit_note(
    db: Session, company_id: str, note_id: str, *, user_id: str | None = None
) -> DebitNote:
    note = db.get(DebitNote, note_id)
    if note is None or note.company_id != company_id:
        raise NotFoundError("Debit note not found.")
    if note.journal_entry_id:
        raise ConflictError(f"Debit note {note.number} is already posted.")

    require_open_period(db, company_id, note.doc_date)
    engine = AccountingEngine(db, company_id, actor_user_id=user_id)
    vendor = db.get(Vendor, note.vendor_id)

    lines: list[LineInput] = [
        LineInput(
            account_id=engine.account_by_system_key("ACCOUNTS_PAYABLE").id,
            debit=note.total,
            description=f"Debit note {note.number} — {vendor.name}",
            party_type=PartyType.VENDOR,
            party_id=vendor.id,
        ),
        LineInput(
            account_id=engine.account_by_system_key("PURCHASES").id,
            credit=note.taxable_value,
            description=f"Purchase return — {note.number}",
        ),
    ]
    for component, amount, key in (
        ("CGST", note.cgst, "GST_INPUT_CGST"),
        ("SGST", note.sgst, "GST_INPUT_SGST"),
        ("IGST", note.igst, "GST_INPUT_IGST"),
        ("CESS", note.cess, "GST_INPUT_CESS"),
    ):
        if money(amount) != ZERO:
            lines.append(
                LineInput(
                    account_id=engine.account_by_system_key(key).id,
                    credit=money(amount),
                    description=f"Input {component} reversed — {note.number}",
                    tax_component=component,
                )
            )
    if note.round_off != ZERO:
        round_account = engine.account_by_system_key("ROUND_OFF")
        if note.round_off > ZERO:
            lines.append(LineInput(account_id=round_account.id, credit=note.round_off, description="Rounding"))
        else:
            lines.append(LineInput(account_id=round_account.id, debit=-note.round_off, description="Rounding"))

    journal = engine.create_and_post(
        EntryInput(
            entry_date=note.doc_date,
            voucher_type=VoucherType.DEBIT_NOTE,
            narration=f"Debit note {note.number} to {vendor.name}: {note.reason}",
            reference=note.number,
            lines=lines,
            source=EntrySource.SYSTEM,
            source_type="DebitNote",
            source_id=note.id,
        )
    )
    note.journal_entry_id = journal.id
    note.status = DocumentStatus.APPROVED

    if note.is_purchase_return:
        for line in note.lines:
            if not line.item_id:
                continue
            item = db.get(Item, line.item_id)
            if item is None or not item.track_inventory:
                continue
            inventory_service.record_move(
                db,
                company_id,
                item_id=item.id,
                warehouse_id=line.warehouse_id,
                move_date=note.doc_date,
                move_type=StockMoveType.PURCHASE_RETURN,
                quantity=-to_qty(line.quantity),
                rate=money(item.average_cost),
                source_type="DebitNote",
                source_id=note.id,
                reference=note.number,
                narration=f"Purchase return — {note.number}",
                user_id=user_id,
            )

    if note.bill_id:
        bill = _get_bill(db, company_id, note.bill_id)
        bill.amount_debited = money(bill.amount_debited + note.total)
        note.amount_applied = note.total
        _refresh_bill_status(bill)

    db.flush()
    _audit(
        db,
        company_id,
        "DebitNote",
        note.id,
        note.number,
        AuditAction.POST,
        f"Posted debit note {note.number} for {note.total}",
        user_id,
        amount=note.total,
    )
    return note


# -------------------------------------------------------------- helpers ----

def _quarter_label(company: Company, on_date: date) -> str:
    """Indian fiscal quarter (Apr-Jun = Q1) for TDS returns."""
    start_month = company.fiscal_year_start_month or 4
    offset = (on_date.month - start_month) % 12
    return f"Q{offset // 3 + 1}"


def _get_bill(db: Session, company_id: str, bill_id: str) -> Bill:
    bill = db.get(Bill, bill_id)
    if bill is None or bill.company_id != company_id:
        raise NotFoundError("Bill not found.")
    return bill


def _audit(
    db: Session,
    company_id: str,
    entity_type: str,
    entity_id: str,
    label: str,
    action: str,
    summary: str,
    user_id: str | None,
    reason: str = "",
    amount: Decimal | None = None,
) -> None:
    from app.services.audit import record_event

    record_event(
        db,
        company_id=company_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_label=label,
        summary=summary,
        reason=reason,
        actor_type=ActorType.USER if user_id else ActorType.SYSTEM,
        actor_id=user_id,
        amount=amount,
    )


def vendor_balance(db: Session, company_id: str, vendor_id: str) -> Decimal:
    bills = db.execute(
        select(Bill).where(
            Bill.company_id == company_id,
            Bill.vendor_id == vendor_id,
            Bill.status.notin_([DocumentStatus.CANCELLED, DocumentStatus.DRAFT]),
        )
    ).scalars()
    return money(sum((bill.balance_due for bill in bills), ZERO))
