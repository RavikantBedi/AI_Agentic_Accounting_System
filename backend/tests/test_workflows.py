"""End-to-end business workflows: sales, purchases, inventory, banking, close."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.accounting.ledger import balances_by_system_key, trial_balance, verify_invariants
from app.enums import DocumentStatus, ReconStatus, StockMoveType
from app.errors import (
    ConflictError,
    DuplicateDocumentError,
    InsufficientStockError,
    PolicyViolation,
    ValidationError,
)
from app.money import ZERO, money
from app.services import banking as banking_service
from app.services import inventory as inventory_service
from app.services import purchases as purchase_service
from app.services import reconciliation as recon_service
from app.services import sales as sales_service
from app.services.sales import LineDraft


# ---------------------------------------------------------------- sales --

def test_invoice_posts_correct_double_entry(db, company, customer, today):
    invoice = sales_service.create_invoice(
        db,
        company.id,
        customer_id=customer.id,
        doc_date=today,
        lines=[LineDraft(description="Consulting", quantity=Decimal("1"), rate=Decimal("100000"), gst_rate=Decimal("18"))],
    )

    assert invoice.taxable_value == money("100000")
    assert invoice.cgst == money("9000")
    assert invoice.sgst == money("9000")
    assert invoice.igst == ZERO
    assert invoice.total == money("118000")
    assert invoice.journal_entry_id is not None

    balances = balances_by_system_key(
        db, company.id, ["ACCOUNTS_RECEIVABLE", "GST_OUTPUT_CGST", "GST_OUTPUT_SGST"]
    )
    assert balances["ACCOUNTS_RECEIVABLE"] == money("118000")
    assert balances["GST_OUTPUT_CGST"] == money("9000")
    assert trial_balance(db, company.id)["balanced"] is True


def test_interstate_invoice_charges_igst(db, company, interstate_customer, today):
    invoice = sales_service.create_invoice(
        db,
        company.id,
        customer_id=interstate_customer.id,
        doc_date=today,
        lines=[LineDraft(description="Consulting", quantity=Decimal("1"), rate=Decimal("50000"), gst_rate=Decimal("18"))],
    )
    assert invoice.igst == money("9000")
    assert invoice.cgst == ZERO
    assert invoice.supply_type == "INTER_STATE"


def test_receipt_settles_the_invoice(db, company, customer, bank_account, today):
    invoice = sales_service.create_invoice(
        db,
        company.id,
        customer_id=customer.id,
        doc_date=today,
        lines=[LineDraft(description="Service", quantity=Decimal("1"), rate=Decimal("10000"), gst_rate=Decimal("18"))],
    )
    sales_service.record_receipt(
        db,
        company.id,
        customer_id=customer.id,
        receipt_date=today,
        amount=invoice.total,
        bank_account_id=bank_account.id,
    )
    db.refresh(invoice)

    assert invoice.status == DocumentStatus.PAID
    assert invoice.balance_due == ZERO
    assert balances_by_system_key(db, company.id, ["ACCOUNTS_RECEIVABLE"])["ACCOUNTS_RECEIVABLE"] == ZERO
    assert trial_balance(db, company.id)["balanced"] is True


def test_partial_receipt_leaves_a_balance(db, company, customer, bank_account, today):
    invoice = sales_service.create_invoice(
        db,
        company.id,
        customer_id=customer.id,
        doc_date=today,
        lines=[LineDraft(description="Service", quantity=Decimal("1"), rate=Decimal("10000"), gst_rate=Decimal("18"))],
    )
    sales_service.record_receipt(
        db,
        company.id,
        customer_id=customer.id,
        receipt_date=today,
        amount=money("5000"),
        bank_account_id=bank_account.id,
    )
    db.refresh(invoice)
    assert invoice.status == DocumentStatus.PARTIALLY_PAID
    assert invoice.balance_due == money("6800")


def test_over_allocation_is_rejected(db, company, customer, bank_account, today):
    invoice = sales_service.create_invoice(
        db,
        company.id,
        customer_id=customer.id,
        doc_date=today,
        lines=[LineDraft(description="Service", quantity=Decimal("1"), rate=Decimal("1000"), gst_rate=Decimal("18"))],
    )
    with pytest.raises(ValidationError):
        sales_service.record_receipt(
            db,
            company.id,
            customer_id=customer.id,
            receipt_date=today,
            amount=money("99999"),
            bank_account_id=bank_account.id,
            allocations=[{"invoice_id": invoice.id, "amount": money("99999")}],
        )


def test_cancelling_an_invoice_reverses_it_rather_than_deleting(db, company, customer, today):
    invoice = sales_service.create_invoice(
        db,
        company.id,
        customer_id=customer.id,
        doc_date=today,
        lines=[LineDraft(description="Service", quantity=Decimal("1"), rate=Decimal("5000"), gst_rate=Decimal("18"))],
    )
    original_entry_id = invoice.journal_entry_id

    sales_service.cancel_invoice(db, company.id, invoice.id, reason="Customer cancelled")
    db.refresh(invoice)

    assert invoice.status == DocumentStatus.CANCELLED
    # The original entry still exists — reversed, not removed.
    from app.models import JournalEntry

    original = db.get(JournalEntry, original_entry_id)
    assert original is not None
    assert original.status == "REVERSED"
    assert original.reversed_by_entry_id is not None
    assert balances_by_system_key(db, company.id, ["ACCOUNTS_RECEIVABLE"])["ACCOUNTS_RECEIVABLE"] == ZERO


def test_paid_invoice_cannot_be_cancelled(db, company, customer, bank_account, today):
    invoice = sales_service.create_invoice(
        db,
        company.id,
        customer_id=customer.id,
        doc_date=today,
        lines=[LineDraft(description="Service", quantity=Decimal("1"), rate=Decimal("1000"), gst_rate=Decimal("18"))],
    )
    sales_service.record_receipt(
        db,
        company.id,
        customer_id=customer.id,
        receipt_date=today,
        amount=invoice.total,
        bank_account_id=bank_account.id,
    )
    with pytest.raises(ConflictError):
        sales_service.cancel_invoice(db, company.id, invoice.id, reason="too late")


# ------------------------------------------------------------ purchases --

def test_bill_posts_expense_input_tax_and_payable(db, company, vendor, today):
    bill = purchase_service.create_bill(
        db,
        company.id,
        vendor_id=vendor.id,
        doc_date=today,
        vendor_invoice_number="INV-001",
        lines=[LineDraft(description="Services", quantity=Decimal("1"), rate=Decimal("50000"), gst_rate=Decimal("18"))],
    )
    assert bill.total == money("59000")
    balances = balances_by_system_key(db, company.id, ["ACCOUNTS_PAYABLE", "GST_INPUT_CGST"])
    assert balances["ACCOUNTS_PAYABLE"] == money("59000")
    assert balances["GST_INPUT_CGST"] == money("4500")
    assert trial_balance(db, company.id)["balanced"] is True


def test_duplicate_vendor_invoice_number_is_refused(db, company, vendor, today):
    purchase_service.create_bill(
        db,
        company.id,
        vendor_id=vendor.id,
        doc_date=today,
        vendor_invoice_number="DUP-99",
        lines=[LineDraft(description="X", quantity=Decimal("1"), rate=Decimal("1000"), gst_rate=Decimal("18"))],
    )
    with pytest.raises(DuplicateDocumentError) as exc:
        purchase_service.create_bill(
            db,
            company.id,
            vendor_id=vendor.id,
            doc_date=today,
            vendor_invoice_number="DUP-99",
            lines=[LineDraft(description="X", quantity=Decimal("1"), rate=Decimal("1000"), gst_rate=Decimal("18"))],
        )
    assert "already booked" in str(exc.value)


def test_near_duplicate_warns_but_can_be_overridden(db, company, vendor, today):
    purchase_service.create_bill(
        db,
        company.id,
        vendor_id=vendor.id,
        doc_date=today,
        vendor_invoice_number="REF-A",
        lines=[LineDraft(description="X", quantity=Decimal("1"), rate=Decimal("2000"), gst_rate=Decimal("18"))],
    )
    # Same vendor, same amount, different reference -> blocked by default.
    with pytest.raises(DuplicateDocumentError):
        purchase_service.create_bill(
            db,
            company.id,
            vendor_id=vendor.id,
            doc_date=today,
            vendor_invoice_number="REF-B",
            lines=[LineDraft(description="X", quantity=Decimal("1"), rate=Decimal("2000"), gst_rate=Decimal("18"))],
        )
    # ...but a human can confirm it is genuinely separate.
    bill = purchase_service.create_bill(
        db,
        company.id,
        vendor_id=vendor.id,
        doc_date=today,
        vendor_invoice_number="REF-B",
        lines=[LineDraft(description="X", quantity=Decimal("1"), rate=Decimal("2000"), gst_rate=Decimal("18"))],
        allow_duplicate=True,
    )
    assert bill.id is not None


def test_tds_is_deducted_and_recorded(db, company, vendor, today):
    vendor.tds_applicable = True
    vendor.default_tds_section = "194J"
    db.flush()

    bill = purchase_service.create_bill(
        db,
        company.id,
        vendor_id=vendor.id,
        doc_date=today,
        vendor_invoice_number="PROF-1",
        lines=[LineDraft(description="Professional fees", quantity=Decimal("1"), rate=Decimal("100000"), gst_rate=Decimal("18"))],
    )
    assert bill.tds_section == "194J"
    assert bill.tds_amount == money("10000")
    # TDS reduces what is actually payable to the vendor.
    assert bill.balance_due == money("108000")

    from sqlalchemy import select

    from app.models import TDSEntry

    entry = db.execute(select(TDSEntry).where(TDSEntry.bill_id == bill.id)).scalar_one()
    assert entry.validated_by_rule is True
    assert entry.tds_amount == money("10000")
    assert trial_balance(db, company.id)["balanced"] is True


def test_payment_requires_a_human_releaser(db, company, vendor, today):
    """An AI agent has no user id — the payment path must refuse it."""
    with pytest.raises(PolicyViolation) as exc:
        purchase_service.record_payment(
            db,
            company.id,
            vendor_id=vendor.id,
            payment_date=today,
            amount=money("1000"),
            user_id=None,
            released_by_user_id=None,
        )
    assert "authenticated user" in str(exc.value)


def test_payment_is_held_after_a_bank_detail_change(db, company, vendor, owner, today):
    vendor.bank_details_updated_at = today - timedelta(days=1)
    db.flush()

    with pytest.raises(PolicyViolation) as exc:
        purchase_service.record_payment(
            db,
            company.id,
            vendor_id=vendor.id,
            payment_date=today,
            amount=money("50000"),
            user_id=owner.id,
            released_by_user_id=owner.id,
        )
    assert "bank details changed" in str(exc.value)


# ------------------------------------------------------------ inventory --

def test_purchase_then_sale_produces_correct_cogs(db, company, vendor, customer, item, warehouse, today):
    purchase_service.create_bill(
        db,
        company.id,
        vendor_id=vendor.id,
        doc_date=today,
        vendor_invoice_number="STOCK-1",
        lines=[LineDraft(item_id=item.id, quantity=Decimal("10"), rate=Decimal("1000"), warehouse_id=warehouse.id)],
    )
    db.refresh(item)
    assert item.current_qty == Decimal("10.0000")
    assert item.average_cost == money("1000")
    assert item.stock_value == money("10000")

    invoice = sales_service.create_invoice(
        db,
        company.id,
        customer_id=customer.id,
        doc_date=today,
        lines=[LineDraft(item_id=item.id, quantity=Decimal("3"), rate=Decimal("1500"), warehouse_id=warehouse.id)],
    )
    db.refresh(item)

    assert item.current_qty == Decimal("7.0000")
    assert item.stock_value == money("7000")
    assert invoice.lines[0].cogs_amount == money("3000")

    # Stock ledger and the inventory control account must agree exactly.
    inventory_balance = balances_by_system_key(db, company.id, ["INVENTORY"])["INVENTORY"]
    assert inventory_balance == item.stock_value
    assert verify_invariants(db, company.id)["all_passed"] is True


def test_weighted_average_updates_on_second_purchase(db, company, vendor, item, warehouse, today):
    purchase_service.create_bill(
        db, company.id, vendor_id=vendor.id, doc_date=today, vendor_invoice_number="WA-1",
        lines=[LineDraft(item_id=item.id, quantity=Decimal("10"), rate=Decimal("100"), warehouse_id=warehouse.id)],
    )
    purchase_service.create_bill(
        db, company.id, vendor_id=vendor.id, doc_date=today, vendor_invoice_number="WA-2",
        lines=[LineDraft(item_id=item.id, quantity=Decimal("10"), rate=Decimal("200"), warehouse_id=warehouse.id)],
    )
    db.refresh(item)
    assert item.current_qty == Decimal("20.0000")
    assert item.average_cost == money("150")
    assert item.stock_value == money("3000")


def test_selling_more_than_available_is_blocked(db, company, customer, item, warehouse, today):
    with pytest.raises(InsufficientStockError) as exc:
        sales_service.create_invoice(
            db,
            company.id,
            customer_id=customer.id,
            doc_date=today,
            lines=[LineDraft(item_id=item.id, quantity=Decimal("5"), rate=Decimal("1500"), warehouse_id=warehouse.id)],
        )
    assert "in stock" in str(exc.value)


def test_valuation_cache_matches_the_movement_history(db, company, vendor, customer, item, warehouse, today):
    purchase_service.create_bill(
        db, company.id, vendor_id=vendor.id, doc_date=today, vendor_invoice_number="V-1",
        lines=[LineDraft(item_id=item.id, quantity=Decimal("100"), rate=Decimal("333.33"), warehouse_id=warehouse.id)],
    )
    for _ in range(7):
        sales_service.create_invoice(
            db, company.id, customer_id=customer.id, doc_date=today,
            lines=[LineDraft(item_id=item.id, quantity=Decimal("3"), rate=Decimal("500"), warehouse_id=warehouse.id)],
        )

    result = inventory_service.rebuild_item_valuation(db, company.id, item.id)
    assert result["was_consistent"] is True, f"valuation drifted by {result['drift_value']}"


def test_backdated_stock_document_keeps_the_cache_provable(
    db, company, vendor, customer, item, warehouse, today
):
    """Documents entered out of date order must not desynchronise the ledger.

    Weighted-average cost is order dependent, so this is the case where a
    valuation cache quietly goes wrong: a purchase dated earlier than sales that
    were already recorded. What must hold is that the cache still equals the
    recorded movement history, and that the stock ledger still equals the
    inventory control account — because the COGS journals were posted with the
    values that were actually used at the time.
    """
    earlier = today - timedelta(days=20)

    purchase_service.create_bill(
        db, company.id, vendor_id=vendor.id, doc_date=today, vendor_invoice_number="BD-1",
        lines=[LineDraft(item_id=item.id, quantity=Decimal("30"), rate=Decimal("101.37"), warehouse_id=warehouse.id)],
    )
    for _ in range(3):
        sales_service.create_invoice(
            db, company.id, customer_id=customer.id, doc_date=today,
            lines=[LineDraft(item_id=item.id, quantity=Decimal("7"), rate=Decimal("250"), warehouse_id=warehouse.id)],
        )

    # Now a purchase dated three weeks *before* the sales just recorded.
    purchase_service.create_bill(
        db, company.id, vendor_id=vendor.id, doc_date=earlier, vendor_invoice_number="BD-2",
        lines=[LineDraft(item_id=item.id, quantity=Decimal("13"), rate=Decimal("87.91"), warehouse_id=warehouse.id)],
    )
    sales_service.create_invoice(
        db, company.id, customer_id=customer.id, doc_date=today,
        lines=[LineDraft(item_id=item.id, quantity=Decimal("11"), rate=Decimal("250"), warehouse_id=warehouse.id)],
    )

    result = inventory_service.rebuild_item_valuation(db, company.id, item.id)
    assert result["was_consistent"] is True, (
        f"valuation drifted by {result['drift_value']} after a backdated document"
    )

    db.refresh(item)
    inventory_balance = balances_by_system_key(db, company.id, ["INVENTORY"])["INVENTORY"]
    assert inventory_balance == item.stock_value
    assert verify_invariants(db, company.id)["all_passed"] is True


def test_stock_moves_are_numbered_in_application_order(
    db, company, vendor, customer, item, warehouse, today
):
    """The stock ledger records the order moves were applied, not inferred.

    ``created_at`` cannot be trusted for this: on platforms with coarse clock
    granularity several moves written for one document share a timestamp, and
    the replay order becomes arbitrary.
    """
    from sqlalchemy import select

    from app.models import StockMove

    purchase_service.create_bill(
        db, company.id, vendor_id=vendor.id, doc_date=today, vendor_invoice_number="SEQ-1",
        lines=[LineDraft(item_id=item.id, quantity=Decimal("40"), rate=Decimal("100"), warehouse_id=warehouse.id)],
    )
    for _ in range(4):
        sales_service.create_invoice(
            db, company.id, customer_id=customer.id, doc_date=today - timedelta(days=1),
            lines=[LineDraft(item_id=item.id, quantity=Decimal("2"), rate=Decimal("300"), warehouse_id=warehouse.id)],
        )

    sequences = list(
        db.execute(
            select(StockMove.sequence)
            .where(StockMove.company_id == company.id, StockMove.item_id == item.id)
            .order_by(StockMove.sequence.asc())
        ).scalars()
    )
    assert sequences == list(range(1, len(sequences) + 1))


def test_warehouse_transfer_is_value_neutral(db, company, vendor, item, warehouse, today):
    from sqlalchemy import select

    from app.models import Warehouse

    purchase_service.create_bill(
        db, company.id, vendor_id=vendor.id, doc_date=today, vendor_invoice_number="T-1",
        lines=[LineDraft(item_id=item.id, quantity=Decimal("20"), rate=Decimal("500"), warehouse_id=warehouse.id)],
    )
    db.refresh(item)
    value_before = item.stock_value

    other = Warehouse(company_id=company.id, code="ALT", name="Alternate")
    db.add(other)
    db.flush()

    inventory_service.transfer_stock(
        db, company.id, item_id=item.id, from_warehouse_id=warehouse.id,
        to_warehouse_id=other.id, quantity=Decimal("5"), move_date=today,
    )
    db.refresh(item)

    assert item.stock_value == value_before, "a transfer must not change total value"
    assert item.current_qty == Decimal("20.0000")
    assert inventory_service.warehouse_quantity(db, company.id, item.id, other.id) == Decimal("5.0000")


# -------------------------------------------------------------- banking --

def test_statement_import_is_idempotent(db, company, bank_account, today):
    rows = [
        banking_service.ImportRow(
            txn_date=today, description="NEFT-CR-ACME-REF123", amount=money("5000"),
            direction="CREDIT", reference="REF123",
        ),
        banking_service.ImportRow(
            txn_date=today, description="BANK CHARGES", amount=money("118"),
            direction="DEBIT", reference="CHG1",
        ),
    ]
    first = banking_service.import_transactions(db, company.id, bank_account.id, rows)
    assert first.imported == 2

    second = banking_service.import_transactions(db, company.id, bank_account.id, list(rows))
    assert second.imported == 0
    assert second.duplicates == 2


def test_identical_same_day_lines_are_both_kept(db, company, bank_account, today):
    """Two genuinely identical charges on one day are real, not duplicates."""
    rows = [
        banking_service.ImportRow(
            txn_date=today, description="ATM WITHDRAWAL", amount=money("500"),
            direction="DEBIT", reference="",
        )
        for _ in range(2)
    ]
    result = banking_service.import_transactions(db, company.id, bank_account.id, rows)
    assert result.imported == 2


def test_description_normalisation_strips_bank_noise():
    assert banking_service.normalise_description(
        "NEFT-CR-HDFC0001234-GLOBEX LTD-REF987654"
    ) == "globex ltd"
    assert "utr" not in banking_service.normalise_description("UPI/UTR123456/ACME")


def test_reconciliation_matches_a_receipt(db, company, customer, bank_account, today):
    invoice = sales_service.create_invoice(
        db, company.id, customer_id=customer.id, doc_date=today,
        lines=[LineDraft(description="Service", quantity=Decimal("1"), rate=Decimal("10000"), gst_rate=Decimal("18"))],
    )
    sales_service.record_receipt(
        db, company.id, customer_id=customer.id, receipt_date=today,
        amount=invoice.total, bank_account_id=bank_account.id, reference="UTR555",
    )
    banking_service.import_transactions(
        db, company.id, bank_account.id,
        [banking_service.ImportRow(
            txn_date=today, description=f"NEFT-CR-{customer.name}-REF UTR555",
            amount=invoice.total, direction="CREDIT", reference="UTR555",
        )],
    )

    session = recon_service.run_reconciliation(db, company.id, bank_account.id)
    assert session.matched_count == 1
    assert session.auto_matched_count == 1


def test_ai_match_proposal_is_verified_against_the_ledger(db, company, bank_account, today):
    """A hallucinated ledger line id must be rejected outright."""
    from app.errors import NotFoundError

    banking_service.import_transactions(
        db, company.id, bank_account.id,
        [banking_service.ImportRow(
            txn_date=today, description="MYSTERY CREDIT", amount=money("1000"),
            direction="CREDIT", reference="X1",
        )],
    )
    from sqlalchemy import select

    from app.models import BankTransaction

    txn = db.execute(
        select(BankTransaction).where(BankTransaction.company_id == company.id)
    ).scalars().first()

    with pytest.raises(NotFoundError):
        recon_service.propose_ai_match(
            db, company.id,
            bank_transaction_id=txn.id,
            journal_line_id="totally-made-up-id",
            confidence=Decimal("0.99"),
            reasoning="The model invented this id",
        )
