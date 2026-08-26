"""Sales service: invoices, receipts, credit notes and the AR sub-ledger.

Posting an invoice is one atomic operation that produces:

    Dr Accounts Receivable            gross
        Cr Revenue                        taxable value
        Cr Output CGST / SGST / IGST      tax
        Cr/Dr Round Off                   rounding
    (+ for stocked items)
    Dr Cost of Goods Sold             cost
        Cr Inventory                      cost

Totals are computed by the tax engine, never taken from the caller, so a bad
client (or an AI proposal) cannot inject wrong numbers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.accounting.engine import AccountingEngine, EntryInput, LineInput
from app.accounting.periods import require_open_period
from app.enums import (
    ActorType,
    AuditAction,
    DocumentStatus,
    EntrySource,
    PartyType,
    StockMoveType,
    SupplyType,
    VoucherType,
)
from app.errors import ConflictError, NotFoundError, ValidationError
from app.models import (
    Company,
    CreditNote,
    CreditNoteLine,
    Customer,
    Invoice,
    InvoiceLine,
    Item,
    Receipt,
    ReceiptAllocation,
)
from app.money import ZERO, D, money, quantity as to_qty, rate as to_rate
from app.services import inventory as inventory_service
from app.services.numbering import next_number
from app.services.tax import (
    TaxBreakdown,
    compute_gst,
    compute_round_off,
    determine_supply_type,
    resolve_gst_rate,
    sum_breakdowns,
)


@dataclass
class LineDraft:
    """Caller-supplied invoice line. Amounts are recomputed, never trusted."""

    item_id: str | None = None
    description: str = ""
    hsn_sac: str = ""
    uom: str = "NOS"
    quantity: Decimal = Decimal("1")
    rate: Decimal = ZERO
    discount_percent: Decimal = ZERO
    discount_amount: Decimal = ZERO
    gst_rate: Decimal | None = None
    cess_rate: Decimal = ZERO
    account_id: str | None = None
    cost_center_id: str | None = None
    warehouse_id: str | None = None


@dataclass
class ComputedLine:
    draft: LineDraft
    item: Item | None
    gross_amount: Decimal
    discount_amount: Decimal
    taxable_value: Decimal
    tax: TaxBreakdown
    line_total: Decimal


@dataclass
class DocumentTotals:
    lines: list[ComputedLine] = field(default_factory=list)
    subtotal: Decimal = ZERO
    discount_total: Decimal = ZERO
    taxable_value: Decimal = ZERO
    cgst: Decimal = ZERO
    sgst: Decimal = ZERO
    igst: Decimal = ZERO
    cess: Decimal = ZERO
    tax_total: Decimal = ZERO
    round_off: Decimal = ZERO
    total: Decimal = ZERO
    supply_type: str = SupplyType.INTRA_STATE
    #: Line numbers whose GST rate could not be resolved from any rule or item.
    unresolved_tax_lines: list[int] = field(default_factory=list)


def compute_totals(
    db: Session,
    company: Company,
    *,
    lines: list[LineDraft],
    doc_date: date,
    place_of_supply: str,
    gst_treatment: str,
    apply_round_off: bool = True,
) -> DocumentTotals:
    """Compute every monetary figure on a sales/purchase document."""
    if not lines:
        raise ValidationError("A document needs at least one line.")

    supply_type = determine_supply_type(
        company.state_code, place_of_supply, treatment=gst_treatment
    )
    computed: list[ComputedLine] = []
    unresolved_lines: list[int] = []

    for index, draft in enumerate(lines, start=1):
        qty = to_qty(draft.quantity)
        unit_rate = money(draft.rate)
        if qty <= 0:
            raise ValidationError(f"Line {index}: quantity must be greater than zero.")
        if unit_rate < 0:
            raise ValidationError(f"Line {index}: rate cannot be negative.")

        item = None
        if draft.item_id:
            item = db.get(Item, draft.item_id)
            if item is None or item.company_id != company.id:
                raise NotFoundError(f"Line {index}: item not found.")

        gross = money(qty * unit_rate)

        # Percentage discount takes precedence when both are supplied.
        if draft.discount_percent and D(draft.discount_percent) > 0:
            discount = money(gross * to_rate(draft.discount_percent) / Decimal(100))
        else:
            discount = money(draft.discount_amount)
        if discount > gross:
            raise ValidationError(f"Line {index}: discount exceeds the line amount.")

        taxable = money(gross - discount)

        hsn = draft.hsn_sac or (item.hsn_sac if item else "")
        # Only pass a fallback when one genuinely exists; passing ZERO would
        # make an unresolvable rate look like a deliberate nil rating.
        fallback = draft.gst_rate if draft.gst_rate is not None else (item.gst_rate if item else None)
        resolved = resolve_gst_rate(
            db, company.id, on_date=doc_date, hsn_sac=hsn, fallback_rate=fallback
        )
        cess_rate = resolved.cess_rate
        if draft.cess_rate and D(draft.cess_rate) > 0:
            cess_rate = to_rate(draft.cess_rate)

        tax = compute_gst(
            taxable,
            resolved.rate,
            supply_type,
            cess_rate=cess_rate,
            place_of_supply=place_of_supply,
            rule_id=resolved.rule_id,
        )
        if not resolved.resolved and supply_type not in (
            SupplyType.EXPORT,
            SupplyType.EXEMPT,
            SupplyType.NIL_RATED,
            SupplyType.NON_GST,
        ):
            tax.notes.append(
                f"Line {index}: no GST rate could be resolved"
                + (f" for HSN/SAC {hsn}" if hsn else " (no HSN/SAC and no item)")
                + ". The line was taxed at 0% — set a rate on the item, or add a "
                "tax rule for this HSN/SAC."
            )
            unresolved_lines.append(index)
        computed.append(
            ComputedLine(
                draft=draft,
                item=item,
                gross_amount=gross,
                discount_amount=discount,
                taxable_value=taxable,
                tax=tax,
                line_total=money(taxable + tax.total_tax),
            )
        )

    aggregate = sum_breakdowns([line.tax for line in computed])
    subtotal = money(sum((line.gross_amount for line in computed), ZERO))
    discount_total = money(sum((line.discount_amount for line in computed), ZERO))
    tax_total = aggregate.total_tax
    pre_round = money(aggregate.taxable_value + tax_total)

    if apply_round_off:
        rounded, adjustment = compute_round_off(pre_round)
    else:
        rounded, adjustment = pre_round, ZERO

    return DocumentTotals(
        lines=computed,
        subtotal=subtotal,
        discount_total=discount_total,
        taxable_value=aggregate.taxable_value,
        cgst=aggregate.cgst,
        sgst=aggregate.sgst,
        igst=aggregate.igst,
        cess=aggregate.cess,
        tax_total=tax_total,
        round_off=adjustment,
        total=rounded,
        supply_type=supply_type,
        unresolved_tax_lines=unresolved_lines,
    )


# ------------------------------------------------------------- invoices ----

def create_invoice(
    db: Session,
    company_id: str,
    *,
    customer_id: str,
    doc_date: date,
    lines: list[LineDraft],
    due_date: date | None = None,
    reference: str = "",
    notes: str = "",
    terms: str = "",
    place_of_supply: str = "",
    branch_id: str | None = None,
    bank_account_id: str | None = None,
    template: str = "PROFESSIONAL",
    number: str | None = None,
    user_id: str | None = None,
    post: bool = True,
    sales_order_id: str | None = None,
) -> Invoice:
    """Create a customer invoice and (by default) post it to the ledger."""
    company = db.get(Company, company_id)
    if company is None:
        raise NotFoundError("Company not found.")

    customer = db.get(Customer, customer_id)
    if customer is None or customer.company_id != company_id:
        raise NotFoundError("Customer not found.")

    pos = place_of_supply or customer.state_code or company.state_code
    totals = compute_totals(
        db,
        company,
        lines=lines,
        doc_date=doc_date,
        place_of_supply=pos,
        gst_treatment=customer.gst_treatment,
    )

    if due_date is None:
        due_date = doc_date + timedelta(days=int(customer.payment_terms_days or 30))
    if due_date < doc_date:
        raise ValidationError("Due date cannot be before the invoice date.")

    invoice = Invoice(
        company_id=company_id,
        number=number or next_number(db, company_id, "SALES_INVOICE", doc_date),
        customer_id=customer_id,
        doc_date=doc_date,
        due_date=due_date,
        reference=reference,
        branch_id=branch_id,
        currency=customer.currency or company.currency,
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
        terms=terms,
        template=template,
        bank_account_id=bank_account_id,
        sales_order_id=sales_order_id,
        status=DocumentStatus.DRAFT,
        payment_terms=f"Net {customer.payment_terms_days}" if customer.payment_terms_days else "",
        is_export=totals.supply_type == SupplyType.EXPORT,
        created_by_user_id=user_id,
    )
    db.add(invoice)
    db.flush()

    for index, computed in enumerate(totals.lines, start=1):
        draft = computed.draft
        db.add(
            InvoiceLine(
                company_id=company_id,
                invoice_id=invoice.id,
                line_number=index,
                item_id=draft.item_id,
                description=draft.description or (computed.item.name if computed.item else ""),
                hsn_sac=draft.hsn_sac or (computed.item.hsn_sac if computed.item else ""),
                uom=draft.uom or (computed.item.uom if computed.item else "NOS"),
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
            )
        )
    db.flush()

    if post:
        post_invoice(db, company_id, invoice.id, user_id=user_id)
    return invoice


def post_invoice(db: Session, company_id: str, invoice_id: str, *, user_id: str | None = None) -> Invoice:
    """Post an invoice: AR/revenue/tax entry, COGS entry and stock issue."""
    invoice = _get_invoice(db, company_id, invoice_id)
    if invoice.journal_entry_id:
        raise ConflictError(f"Invoice {invoice.number} is already posted.")
    if invoice.status == DocumentStatus.CANCELLED:
        raise ConflictError(f"Invoice {invoice.number} is cancelled.")

    require_open_period(db, company_id, invoice.doc_date)
    engine = AccountingEngine(db, company_id, actor_user_id=user_id)
    customer = db.get(Customer, invoice.customer_id)

    # A customer may have a dedicated control account; otherwise use the
    # company-wide AR control account.
    ar_account = engine.account_by_system_key("ACCOUNTS_RECEIVABLE")
    if customer.receivable_account_id:
        override = engine.get_account(account_id=customer.receivable_account_id)
        if override is not None and not override.is_group and override.is_active:
            ar_account = override

    lines: list[LineInput] = [
        LineInput(
            account_id=ar_account.id,
            debit=invoice.total,
            description=f"Invoice {invoice.number} — {customer.name}",
            party_type=PartyType.CUSTOMER,
            party_id=customer.id,
            branch_id=invoice.branch_id,
        )
    ]

    # Revenue, grouped by the account each line posts to.
    revenue_by_account: dict[str, Decimal] = {}
    cost_center_by_account: dict[str, str | None] = {}
    for line in invoice.lines:
        account_id = line.account_id
        if not account_id and line.item_id:
            item = db.get(Item, line.item_id)
            account_id = item.income_account_id if item and item.income_account_id else None
        if not account_id:
            account_id = engine.account_by_system_key("SALES_REVENUE").id
        revenue_by_account[account_id] = money(
            revenue_by_account.get(account_id, ZERO) + line.taxable_value
        )
        cost_center_by_account.setdefault(account_id, line.cost_center_id)

    for account_id, amount in revenue_by_account.items():
        if amount == ZERO:
            continue
        lines.append(
            LineInput(
                account_id=account_id,
                credit=amount,
                description=f"Revenue — invoice {invoice.number}",
                cost_center_id=cost_center_by_account.get(account_id),
                branch_id=invoice.branch_id,
            )
        )

    for component, amount, key in (
        ("CGST", invoice.cgst, "GST_OUTPUT_CGST"),
        ("SGST", invoice.sgst, "GST_OUTPUT_SGST"),
        ("IGST", invoice.igst, "GST_OUTPUT_IGST"),
        ("CESS", invoice.cess, "GST_OUTPUT_CESS"),
    ):
        if money(amount) != ZERO:
            lines.append(
                LineInput(
                    account_id=engine.account_by_system_key(key).id,
                    credit=money(amount),
                    description=f"Output {component} — invoice {invoice.number}",
                    tax_component=component,
                    taxable_value=invoice.taxable_value,
                    branch_id=invoice.branch_id,
                )
            )

    if invoice.round_off != ZERO:
        round_account = engine.account_by_system_key("ROUND_OFF")
        if invoice.round_off > ZERO:
            # Rounded up: a small gain, credited against the round-off expense.
            lines.append(
                LineInput(
                    account_id=round_account.id,
                    credit=invoice.round_off,
                    description=f"Rounding — invoice {invoice.number}",
                )
            )
        else:
            lines.append(
                LineInput(
                    account_id=round_account.id,
                    debit=-invoice.round_off,
                    description=f"Rounding — invoice {invoice.number}",
                )
            )

    journal = engine.create_and_post(
        EntryInput(
            entry_date=invoice.doc_date,
            voucher_type=VoucherType.SALES,
            narration=f"Sales invoice {invoice.number} to {customer.name}",
            reference=invoice.number,
            currency=invoice.currency,
            branch_id=invoice.branch_id,
            lines=lines,
            source=EntrySource.SYSTEM,
            source_type="Invoice",
            source_id=invoice.id,
        )
    )

    invoice.journal_entry_id = journal.id
    invoice.status = DocumentStatus.SENT
    invoice.posted_at = invoice.doc_date

    _issue_stock_and_cogs(db, company_id, invoice, engine, user_id=user_id)
    db.flush()

    _audit(
        db,
        company_id,
        "Invoice",
        invoice.id,
        invoice.number,
        AuditAction.POST,
        f"Posted invoice {invoice.number} for {invoice.total}",
        user_id,
        amount=invoice.total,
    )
    return invoice


def _issue_stock_and_cogs(
    db: Session,
    company_id: str,
    invoice: Invoice,
    engine: AccountingEngine,
    *,
    user_id: str | None,
) -> None:
    """Issue stock for goods lines and post the matching COGS entry."""
    cogs_lines: list[LineInput] = []
    total_cost = ZERO

    for line in invoice.lines:
        if not line.item_id:
            continue
        item = db.get(Item, line.item_id)
        if item is None or not item.track_inventory:
            continue

        result = inventory_service.record_move(
            db,
            company_id,
            item_id=item.id,
            warehouse_id=line.warehouse_id,
            move_date=invoice.doc_date,
            move_type=StockMoveType.SALE,
            quantity=-to_qty(line.quantity),
            source_type="Invoice",
            source_id=invoice.id,
            reference=invoice.number,
            narration=f"Sale — invoice {invoice.number}",
            user_id=user_id,
        )
        cost = money(-result.value)  # outward moves carry a negative value
        line.cogs_amount = cost
        total_cost = money(total_cost + cost)

    if total_cost <= ZERO:
        return

    cogs_account = engine.account_by_system_key("COGS")
    inventory_account = engine.account_by_system_key("INVENTORY")
    cogs_lines = [
        LineInput(
            account_id=cogs_account.id,
            debit=total_cost,
            description=f"Cost of goods sold — invoice {invoice.number}",
            branch_id=invoice.branch_id,
        ),
        LineInput(
            account_id=inventory_account.id,
            credit=total_cost,
            description=f"Stock issued — invoice {invoice.number}",
            branch_id=invoice.branch_id,
        ),
    ]
    engine.create_and_post(
        EntryInput(
            entry_date=invoice.doc_date,
            voucher_type=VoucherType.STOCK_JOURNAL,
            narration=f"COGS for invoice {invoice.number}",
            reference=invoice.number,
            lines=cogs_lines,
            source=EntrySource.SYSTEM,
            source_type="Invoice",
            source_id=invoice.id,
        )
    )


def cancel_invoice(
    db: Session, company_id: str, invoice_id: str, *, reason: str, user_id: str | None = None
) -> Invoice:
    """Cancel an invoice by reversing its journals — never by deleting rows."""
    invoice = _get_invoice(db, company_id, invoice_id)
    if invoice.amount_paid > ZERO:
        raise ConflictError(
            f"Invoice {invoice.number} has payments against it. "
            "Reverse the receipts before cancelling."
        )
    if invoice.status == DocumentStatus.CANCELLED:
        raise ConflictError("Invoice is already cancelled.")

    engine = AccountingEngine(db, company_id, actor_user_id=user_id)
    if invoice.journal_entry_id:
        engine.reverse(invoice.journal_entry_id, reason=f"Invoice {invoice.number} cancelled: {reason}")

    # Reverse the COGS entry and put the stock back.
    from app.models import JournalEntry, StockMove

    cogs_entries = list(
        db.execute(
            select(JournalEntry).where(
                JournalEntry.company_id == company_id,
                JournalEntry.source_type == "Invoice",
                JournalEntry.source_id == invoice.id,
                JournalEntry.voucher_type == VoucherType.STOCK_JOURNAL,
                JournalEntry.status == "POSTED",
            )
        ).scalars()
    )
    for entry in cogs_entries:
        engine.reverse(entry, reason=f"Invoice {invoice.number} cancelled")

    for move in db.execute(
        select(StockMove).where(
            StockMove.company_id == company_id,
            StockMove.source_type == "Invoice",
            StockMove.source_id == invoice.id,
            StockMove.move_type == StockMoveType.SALE,
        )
    ).scalars():
        inventory_service.record_move(
            db,
            company_id,
            item_id=move.item_id,
            warehouse_id=move.warehouse_id,
            move_date=date.today(),
            move_type=StockMoveType.SALE_RETURN,
            quantity=-move.quantity,
            rate=move.rate,
            source_type="InvoiceCancellation",
            source_id=invoice.id,
            reference=invoice.number,
            narration=f"Stock returned — invoice {invoice.number} cancelled",
            user_id=user_id,
        )

    invoice.status = DocumentStatus.CANCELLED
    db.flush()
    _audit(
        db,
        company_id,
        "Invoice",
        invoice.id,
        invoice.number,
        AuditAction.UPDATE,
        f"Cancelled invoice {invoice.number}",
        user_id,
        reason=reason,
        amount=invoice.total,
    )
    return invoice


# ------------------------------------------------------------- receipts ----

def record_receipt(
    db: Session,
    company_id: str,
    *,
    customer_id: str,
    receipt_date: date,
    amount: Decimal,
    deposit_account_id: str | None = None,
    bank_account_id: str | None = None,
    method: str = "BANK_TRANSFER",
    reference: str = "",
    allocations: list[dict] | None = None,
    tds_deducted: Decimal = ZERO,
    notes: str = "",
    number: str | None = None,
    user_id: str | None = None,
    auto_allocate: bool = True,
) -> Receipt:
    """Record money received from a customer and allocate it to invoices."""
    company = db.get(Company, company_id)
    customer = db.get(Customer, customer_id)
    if customer is None or customer.company_id != company_id:
        raise NotFoundError("Customer not found.")

    amount = money(amount)
    if amount <= ZERO:
        raise ValidationError("Receipt amount must be greater than zero.")
    require_open_period(db, company_id, receipt_date)

    engine = AccountingEngine(db, company_id, actor_user_id=user_id)
    if deposit_account_id is None:
        if bank_account_id:
            from app.models import BankAccount

            bank = db.get(BankAccount, bank_account_id)
            if bank is None or bank.company_id != company_id:
                raise NotFoundError("Bank account not found.")
            deposit_account_id = bank.gl_account_id
        else:
            deposit_account_id = engine.account_by_system_key("BANK_DEFAULT").id

    receipt = Receipt(
        company_id=company_id,
        number=number or next_number(db, company_id, "RECEIPT", receipt_date),
        customer_id=customer_id,
        receipt_date=receipt_date,
        amount=amount,
        unallocated_amount=amount,
        currency=customer.currency or company.currency,
        method=method,
        reference=reference,
        bank_account_id=bank_account_id,
        deposit_account_id=deposit_account_id,
        tds_deducted=money(tds_deducted),
        notes=notes,
        status=DocumentStatus.DRAFT,
        created_by_user_id=user_id,
    )
    db.add(receipt)
    db.flush()

    applied = _allocate_receipt(db, company_id, receipt, allocations, auto_allocate=auto_allocate)

    ar_account = engine.account_by_system_key("ACCOUNTS_RECEIVABLE")
    entry_lines = [
        LineInput(
            account_id=deposit_account_id,
            debit=amount,
            description=f"Receipt {receipt.number} from {customer.name}",
            bank_account_id=bank_account_id,
        )
    ]
    if money(tds_deducted) > ZERO:
        entry_lines.append(
            LineInput(
                account_id=engine.account_by_system_key("TDS_RECEIVABLE").id,
                debit=money(tds_deducted),
                description=f"TDS deducted by {customer.name} — receipt {receipt.number}",
                party_type=PartyType.CUSTOMER,
                party_id=customer.id,
            )
        )
    entry_lines.append(
        LineInput(
            account_id=ar_account.id,
            credit=money(amount + money(tds_deducted)),
            description=f"Against receivable — {customer.name}",
            party_type=PartyType.CUSTOMER,
            party_id=customer.id,
        )
    )

    journal = engine.create_and_post(
        EntryInput(
            entry_date=receipt_date,
            voucher_type=VoucherType.RECEIPT,
            narration=f"Receipt {receipt.number} from {customer.name}",
            reference=reference or receipt.number,
            lines=entry_lines,
            source=EntrySource.SYSTEM,
            source_type="Receipt",
            source_id=receipt.id,
        )
    )
    receipt.journal_entry_id = journal.id
    receipt.status = DocumentStatus.APPROVED
    db.flush()

    _audit(
        db,
        company_id,
        "Receipt",
        receipt.id,
        receipt.number,
        AuditAction.POST,
        f"Recorded receipt {receipt.number} of {amount} from {customer.name} "
        f"({len(applied)} invoice allocation(s))",
        user_id,
        amount=amount,
    )
    return receipt


def _allocate_receipt(
    db: Session,
    company_id: str,
    receipt: Receipt,
    allocations: list[dict] | None,
    *,
    auto_allocate: bool,
) -> list[ReceiptAllocation]:
    """Apply a receipt to invoices, oldest first when not told otherwise."""
    remaining = money(receipt.amount + receipt.tds_deducted)
    created: list[ReceiptAllocation] = []

    if allocations:
        for allocation in allocations:
            invoice = _get_invoice(db, company_id, allocation["invoice_id"])
            requested = money(allocation.get("amount", invoice.balance_due))
            if requested <= ZERO:
                continue
            if requested > remaining:
                raise ValidationError(
                    f"Allocations exceed the receipt amount by "
                    f"{money(requested - remaining)}."
                )
            if requested > invoice.balance_due:
                raise ValidationError(
                    f"Cannot allocate {requested} to invoice {invoice.number}: "
                    f"only {invoice.balance_due} is outstanding."
                )
            created.append(_apply_allocation(db, company_id, receipt, invoice, requested))
            remaining = money(remaining - requested)
    elif auto_allocate:
        open_invoices = list(
            db.execute(
                select(Invoice)
                .where(
                    Invoice.company_id == company_id,
                    Invoice.customer_id == receipt.customer_id,
                    Invoice.status.in_(
                        [
                            DocumentStatus.SENT,
                            DocumentStatus.APPROVED,
                            DocumentStatus.PARTIALLY_PAID,
                            DocumentStatus.OVERDUE,
                        ]
                    ),
                )
                .order_by(Invoice.due_date.asc(), Invoice.doc_date.asc())
            ).scalars()
        )
        for invoice in open_invoices:
            if remaining <= ZERO:
                break
            due = invoice.balance_due
            if due <= ZERO:
                continue
            applied = min(due, remaining)
            created.append(_apply_allocation(db, company_id, receipt, invoice, applied))
            remaining = money(remaining - applied)

    receipt.unallocated_amount = max(ZERO, remaining)
    db.flush()
    return created


def _apply_allocation(
    db: Session, company_id: str, receipt: Receipt, invoice: Invoice, amount: Decimal
) -> ReceiptAllocation:
    allocation = ReceiptAllocation(
        company_id=company_id, receipt_id=receipt.id, invoice_id=invoice.id, amount=amount
    )
    db.add(allocation)
    invoice.amount_paid = money(invoice.amount_paid + amount)
    _refresh_invoice_status(invoice)
    db.flush()
    return allocation


def _refresh_invoice_status(invoice: Invoice) -> None:
    balance = invoice.balance_due
    if balance <= ZERO:
        invoice.status = DocumentStatus.PAID
    elif invoice.amount_paid > ZERO or invoice.amount_credited > ZERO:
        invoice.status = DocumentStatus.PARTIALLY_PAID
    elif invoice.due_date < date.today():
        invoice.status = DocumentStatus.OVERDUE
    else:
        invoice.status = DocumentStatus.SENT


def refresh_overdue_status(db: Session, company_id: str, *, as_of: date | None = None) -> int:
    """Mark unpaid invoices past their due date as OVERDUE."""
    as_of = as_of or date.today()
    invoices = list(
        db.execute(
            select(Invoice).where(
                Invoice.company_id == company_id,
                Invoice.status.in_([DocumentStatus.SENT, DocumentStatus.PARTIALLY_PAID]),
                Invoice.due_date < as_of,
            )
        ).scalars()
    )
    for invoice in invoices:
        if invoice.balance_due > ZERO:
            invoice.status = DocumentStatus.OVERDUE
    db.flush()
    return len(invoices)


# ---------------------------------------------------------- credit notes ---

def create_credit_note(
    db: Session,
    company_id: str,
    *,
    customer_id: str,
    doc_date: date,
    lines: list[LineDraft],
    invoice_id: str | None = None,
    reason: str = "",
    is_sales_return: bool = False,
    user_id: str | None = None,
    post: bool = True,
) -> CreditNote:
    """Issue a credit note, optionally returning stock to inventory."""
    company = db.get(Company, company_id)
    customer = db.get(Customer, customer_id)
    if customer is None or customer.company_id != company_id:
        raise NotFoundError("Customer not found.")

    invoice = _get_invoice(db, company_id, invoice_id) if invoice_id else None
    pos = (invoice.place_of_supply if invoice else customer.state_code) or company.state_code

    totals = compute_totals(
        db,
        company,
        lines=lines,
        doc_date=doc_date,
        place_of_supply=pos,
        gst_treatment=customer.gst_treatment,
    )

    if invoice is not None:
        max_creditable = money(invoice.total - invoice.amount_credited)
        if totals.total > max_creditable:
            raise ValidationError(
                f"Credit note {totals.total} exceeds the {max_creditable} still "
                f"creditable against invoice {invoice.number}."
            )

    note = CreditNote(
        company_id=company_id,
        number=next_number(db, company_id, "CREDIT_NOTE", doc_date),
        customer_id=customer_id,
        invoice_id=invoice_id,
        doc_date=doc_date,
        reason=reason,
        is_sales_return=is_sales_return,
        currency=customer.currency or company.currency,
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
        status=DocumentStatus.DRAFT,
        created_by_user_id=user_id,
    )
    db.add(note)
    db.flush()

    for index, computed in enumerate(totals.lines, start=1):
        draft = computed.draft
        db.add(
            CreditNoteLine(
                company_id=company_id,
                credit_note_id=note.id,
                line_number=index,
                item_id=draft.item_id,
                description=draft.description or (computed.item.name if computed.item else ""),
                hsn_sac=draft.hsn_sac or (computed.item.hsn_sac if computed.item else ""),
                uom=draft.uom,
                quantity=to_qty(draft.quantity),
                rate=money(draft.rate),
                discount_amount=computed.discount_amount,
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
        post_credit_note(db, company_id, note.id, user_id=user_id)
    return note


def post_credit_note(
    db: Session, company_id: str, note_id: str, *, user_id: str | None = None
) -> CreditNote:
    """Post a credit note: reverse revenue and tax, restore stock if returned."""
    note = db.get(CreditNote, note_id)
    if note is None or note.company_id != company_id:
        raise NotFoundError("Credit note not found.")
    if note.journal_entry_id:
        raise ConflictError(f"Credit note {note.number} is already posted.")

    require_open_period(db, company_id, note.doc_date)
    engine = AccountingEngine(db, company_id, actor_user_id=user_id)
    customer = db.get(Customer, note.customer_id)

    lines: list[LineInput] = [
        LineInput(
            account_id=engine.get_account(code="4190").id
            if engine.get_account(code="4190")
            else engine.account_by_system_key("SALES_REVENUE").id,
            debit=note.taxable_value,
            description=f"Sales return — credit note {note.number}",
        )
    ]
    for component, amount, key in (
        ("CGST", note.cgst, "GST_OUTPUT_CGST"),
        ("SGST", note.sgst, "GST_OUTPUT_SGST"),
        ("IGST", note.igst, "GST_OUTPUT_IGST"),
        ("CESS", note.cess, "GST_OUTPUT_CESS"),
    ):
        if money(amount) != ZERO:
            lines.append(
                LineInput(
                    account_id=engine.account_by_system_key(key).id,
                    debit=money(amount),
                    description=f"Output {component} reversed — {note.number}",
                    tax_component=component,
                    taxable_value=-note.taxable_value,
                )
            )
    if note.round_off != ZERO:
        round_account = engine.account_by_system_key("ROUND_OFF")
        if note.round_off > ZERO:
            lines.append(LineInput(account_id=round_account.id, debit=note.round_off, description="Rounding"))
        else:
            lines.append(LineInput(account_id=round_account.id, credit=-note.round_off, description="Rounding"))

    lines.append(
        LineInput(
            account_id=engine.account_by_system_key("ACCOUNTS_RECEIVABLE").id,
            credit=note.total,
            description=f"Credit note {note.number} — {customer.name}",
            party_type=PartyType.CUSTOMER,
            party_id=customer.id,
        )
    )

    journal = engine.create_and_post(
        EntryInput(
            entry_date=note.doc_date,
            voucher_type=VoucherType.CREDIT_NOTE,
            narration=f"Credit note {note.number} for {customer.name}: {note.reason}",
            reference=note.number,
            lines=lines,
            source=EntrySource.SYSTEM,
            source_type="CreditNote",
            source_id=note.id,
        )
    )
    note.journal_entry_id = journal.id
    note.status = DocumentStatus.APPROVED

    if note.is_sales_return:
        _return_stock(db, company_id, note, engine, user_id=user_id)

    if note.invoice_id:
        invoice = _get_invoice(db, company_id, note.invoice_id)
        invoice.amount_credited = money(invoice.amount_credited + note.total)
        note.amount_applied = note.total
        _refresh_invoice_status(invoice)

    db.flush()
    _audit(
        db,
        company_id,
        "CreditNote",
        note.id,
        note.number,
        AuditAction.POST,
        f"Posted credit note {note.number} for {note.total}",
        user_id,
        amount=note.total,
    )
    return note


def _return_stock(
    db: Session, company_id: str, note: CreditNote, engine: AccountingEngine, *, user_id: str | None
) -> None:
    total_cost = ZERO
    for line in note.lines:
        if not line.item_id:
            continue
        item = db.get(Item, line.item_id)
        if item is None or not item.track_inventory:
            continue
        result = inventory_service.record_move(
            db,
            company_id,
            item_id=item.id,
            warehouse_id=line.warehouse_id,
            move_date=note.doc_date,
            move_type=StockMoveType.SALE_RETURN,
            quantity=to_qty(line.quantity),
            rate=money(item.average_cost) or money(item.purchase_price),
            source_type="CreditNote",
            source_id=note.id,
            reference=note.number,
            narration=f"Sales return — credit note {note.number}",
            user_id=user_id,
        )
        total_cost = money(total_cost + result.value)

    if total_cost <= ZERO:
        return

    engine.create_and_post(
        EntryInput(
            entry_date=note.doc_date,
            voucher_type=VoucherType.STOCK_JOURNAL,
            narration=f"Stock returned — credit note {note.number}",
            reference=note.number,
            lines=[
                LineInput(
                    account_id=engine.account_by_system_key("INVENTORY").id,
                    debit=total_cost,
                    description=f"Stock returned — {note.number}",
                ),
                LineInput(
                    account_id=engine.account_by_system_key("COGS").id,
                    credit=total_cost,
                    description=f"COGS reversed — {note.number}",
                ),
            ],
            source=EntrySource.SYSTEM,
            source_type="CreditNote",
            source_id=note.id,
        )
    )


# ------------------------------------------------------------- helpers -----

def _get_invoice(db: Session, company_id: str, invoice_id: str) -> Invoice:
    invoice = db.get(Invoice, invoice_id)
    if invoice is None or invoice.company_id != company_id:
        raise NotFoundError("Invoice not found.")
    return invoice


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


def customer_balance(db: Session, company_id: str, customer_id: str) -> Decimal:
    """Outstanding receivable for one customer, from invoice documents."""
    rows = db.execute(
        select(Invoice).where(
            Invoice.company_id == company_id,
            Invoice.customer_id == customer_id,
            Invoice.status.notin_([DocumentStatus.CANCELLED, DocumentStatus.DRAFT]),
        )
    ).scalars()
    return money(sum((invoice.balance_due for invoice in rows), ZERO))
