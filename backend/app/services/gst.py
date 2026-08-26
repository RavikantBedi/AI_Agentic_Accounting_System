"""GST reporting: summaries, GSTR-1 / GSTR-3B preparation and 2B reconciliation.

**Scope statement.** This module *prepares* return data from the ledger. It does
not file anything with the government, and it never claims to. A prepared
return can be reviewed, exported and then filed by a human on the GST portal;
only a human can record the resulting ARN. See ``docs/TAX.md``.
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.enums import ActorType, AuditAction, DocumentStatus, ReturnStatus, SupplyType
from app.errors import NotFoundError, ValidationError
from app.models import (
    Bill,
    Company,
    CreditNote,
    Customer,
    DebitNote,
    GSTReconciliationItem,
    GSTReturn,
    Invoice,
    Vendor,
)
from app.money import ZERO, D, money

# Below this value, B2C supplies are reported in aggregate rather than per invoice.
B2C_LARGE_THRESHOLD = Decimal("250000.00")


def period_bounds(period: str) -> tuple[date, date]:
    try:
        year, month = (int(part) for part in period.split("-"))
        return date(year, month, 1), date(year, month, monthrange(year, month)[1])
    except (ValueError, TypeError) as exc:
        raise ValidationError(f"'{period}' is not a valid period. Use YYYY-MM.") from exc


def current_period() -> str:
    today = date.today()
    return f"{today.year}-{today.month:02d}"


def gst_summary(db: Session, company_id: str, *, period: str | None = None) -> dict:
    """Output GST vs input GST for a period, and the resulting net position."""
    period = period or current_period()
    start, end = period_bounds(period)

    invoices = list(
        db.execute(
            select(Invoice).where(
                Invoice.company_id == company_id,
                Invoice.doc_date >= start,
                Invoice.doc_date <= end,
                Invoice.status.notin_([DocumentStatus.CANCELLED, DocumentStatus.DRAFT]),
            )
        ).scalars()
    )
    credit_notes = list(
        db.execute(
            select(CreditNote).where(
                CreditNote.company_id == company_id,
                CreditNote.doc_date >= start,
                CreditNote.doc_date <= end,
                CreditNote.status.notin_([DocumentStatus.CANCELLED, DocumentStatus.DRAFT]),
            )
        ).scalars()
    )
    bills = list(
        db.execute(
            select(Bill).where(
                Bill.company_id == company_id,
                Bill.doc_date >= start,
                Bill.doc_date <= end,
                Bill.status.notin_([DocumentStatus.CANCELLED, DocumentStatus.DRAFT]),
                Bill.itc_eligible.is_(True),
            )
        ).scalars()
    )
    debit_notes = list(
        db.execute(
            select(DebitNote).where(
                DebitNote.company_id == company_id,
                DebitNote.doc_date >= start,
                DebitNote.doc_date <= end,
                DebitNote.status.notin_([DocumentStatus.CANCELLED, DocumentStatus.DRAFT]),
            )
        ).scalars()
    )

    def total(rows, field: str) -> Decimal:
        return money(sum((getattr(row, field) for row in rows), ZERO))

    output_cgst = money(total(invoices, "cgst") - total(credit_notes, "cgst"))
    output_sgst = money(total(invoices, "sgst") - total(credit_notes, "sgst"))
    output_igst = money(total(invoices, "igst") - total(credit_notes, "igst"))
    output_cess = money(total(invoices, "cess") - total(credit_notes, "cess"))
    output_taxable = money(total(invoices, "taxable_value") - total(credit_notes, "taxable_value"))

    input_cgst = money(total(bills, "cgst") - total(debit_notes, "cgst"))
    input_sgst = money(total(bills, "sgst") - total(debit_notes, "sgst"))
    input_igst = money(total(bills, "igst") - total(debit_notes, "igst"))
    input_cess = money(total(bills, "cess") - total(debit_notes, "cess"))
    input_taxable = money(total(bills, "taxable_value") - total(debit_notes, "taxable_value"))

    output_total = money(output_cgst + output_sgst + output_igst + output_cess)
    input_total = money(input_cgst + input_sgst + input_igst + input_cess)
    net = money(output_total - input_total)

    returns = {
        row.return_type: row
        for row in db.execute(
            select(GSTReturn).where(
                GSTReturn.company_id == company_id, GSTReturn.period == period
            )
        ).scalars()
    }

    return {
        "period": period,
        "start_date": start,
        "end_date": end,
        "output": {
            "taxable_value": output_taxable,
            "cgst": output_cgst,
            "sgst": output_sgst,
            "igst": output_igst,
            "cess": output_cess,
            "total": output_total,
            "invoice_count": len(invoices),
            "credit_note_count": len(credit_notes),
        },
        "input": {
            "taxable_value": input_taxable,
            "cgst": input_cgst,
            "sgst": input_sgst,
            "igst": input_igst,
            "cess": input_cess,
            "total": input_total,
            "bill_count": len(bills),
            "debit_note_count": len(debit_notes),
        },
        "net_payable": net if net > ZERO else ZERO,
        "credit_carried_forward": money(-net) if net < ZERO else ZERO,
        "net_position": net,
        "returns": {
            "GSTR1": returns["GSTR1"].status if "GSTR1" in returns else ReturnStatus.NOT_STARTED,
            "GSTR3B": returns["GSTR3B"].status if "GSTR3B" in returns else ReturnStatus.NOT_STARTED,
        },
        "filing_note": (
            "Prepared from your ledger. AI Ledger does not file returns with the GST "
            "portal — export this data and file it there."
        ),
    }


def prepare_gstr1(
    db: Session, company_id: str, *, period: str, user_id: str | None = None
) -> GSTReturn:
    """Build GSTR-1 (outward supplies) in portal-shaped sections."""
    company = db.get(Company, company_id)
    if company is None:
        raise NotFoundError("Company not found.")

    start, end = period_bounds(period)
    invoices = list(
        db.execute(
            select(Invoice).where(
                Invoice.company_id == company_id,
                Invoice.doc_date >= start,
                Invoice.doc_date <= end,
                Invoice.status.notin_([DocumentStatus.CANCELLED, DocumentStatus.DRAFT]),
            )
        ).scalars()
    )

    b2b: dict[str, dict] = {}
    b2cl: list[dict] = []
    b2cs: dict[str, dict] = {}
    exports: list[dict] = []
    hsn_summary: dict[str, dict] = {}
    validation_errors: list[dict] = []

    for invoice in invoices:
        customer = db.get(Customer, invoice.customer_id)
        gstin = (customer.gstin or "").strip() if customer else ""

        invoice_row = {
            "invoice_id": invoice.id,
            "invoice_number": invoice.number,
            "invoice_date": invoice.doc_date.isoformat(),
            "invoice_value": str(invoice.total),
            "place_of_supply": invoice.place_of_supply,
            "reverse_charge": "Y" if invoice.is_reverse_charge else "N",
            "taxable_value": str(invoice.taxable_value),
            "cgst": str(invoice.cgst),
            "sgst": str(invoice.sgst),
            "igst": str(invoice.igst),
            "cess": str(invoice.cess),
        }

        if invoice.supply_type == SupplyType.EXPORT:
            exports.append({**invoice_row, "export_type": "WOPAY" if invoice.igst == ZERO else "WPAY"})
        elif gstin:
            entry = b2b.setdefault(
                gstin,
                {
                    "gstin": gstin,
                    "customer_name": customer.name if customer else "",
                    "invoices": [],
                    "taxable_value": ZERO,
                    "total_tax": ZERO,
                },
            )
            entry["invoices"].append(invoice_row)
            entry["taxable_value"] = money(entry["taxable_value"] + invoice.taxable_value)
            entry["total_tax"] = money(entry["total_tax"] + invoice.tax_total)
        elif invoice.total > B2C_LARGE_THRESHOLD and invoice.supply_type == SupplyType.INTER_STATE:
            b2cl.append(invoice_row)
        else:
            key = f"{invoice.place_of_supply}|{invoice.supply_type}"
            entry = b2cs.setdefault(
                key,
                {
                    "place_of_supply": invoice.place_of_supply,
                    "supply_type": invoice.supply_type,
                    "taxable_value": ZERO,
                    "cgst": ZERO,
                    "sgst": ZERO,
                    "igst": ZERO,
                    "cess": ZERO,
                    "invoice_count": 0,
                },
            )
            entry["taxable_value"] = money(entry["taxable_value"] + invoice.taxable_value)
            entry["cgst"] = money(entry["cgst"] + invoice.cgst)
            entry["sgst"] = money(entry["sgst"] + invoice.sgst)
            entry["igst"] = money(entry["igst"] + invoice.igst)
            entry["cess"] = money(entry["cess"] + invoice.cess)
            entry["invoice_count"] += 1

        # HSN-wise summary, and flag anything missing an HSN code.
        for line in invoice.lines:
            hsn = (line.hsn_sac or "").strip()
            if not hsn:
                validation_errors.append(
                    {
                        "invoice_id": invoice.id,
                        "invoice_number": invoice.number,
                        "field": "hsn_sac",
                        "message": (
                            f"Line {line.line_number} ('{line.description[:40]}') has no "
                            "HSN/SAC code. GSTR-1 requires one."
                        ),
                    }
                )
                continue
            entry = hsn_summary.setdefault(
                hsn,
                {
                    "hsn_sac": hsn,
                    "description": line.description[:60],
                    "uom": line.uom,
                    "quantity": ZERO,
                    "taxable_value": ZERO,
                    "cgst": ZERO,
                    "sgst": ZERO,
                    "igst": ZERO,
                    "cess": ZERO,
                },
            )
            entry["quantity"] = D(entry["quantity"]) + D(line.quantity)
            entry["taxable_value"] = money(entry["taxable_value"] + line.taxable_value)
            entry["cgst"] = money(entry["cgst"] + line.cgst)
            entry["sgst"] = money(entry["sgst"] + line.sgst)
            entry["igst"] = money(entry["igst"] + line.igst)
            entry["cess"] = money(entry["cess"] + line.cess)

        if gstin:
            from app.services.tax import validate_gstin

            valid, message = validate_gstin(gstin)
            if not valid:
                validation_errors.append(
                    {
                        "invoice_id": invoice.id,
                        "invoice_number": invoice.number,
                        "field": "gstin",
                        "message": f"Customer GSTIN '{gstin}' is invalid: {message}",
                    }
                )

    credit_notes = list(
        db.execute(
            select(CreditNote).where(
                CreditNote.company_id == company_id,
                CreditNote.doc_date >= start,
                CreditNote.doc_date <= end,
                CreditNote.status.notin_([DocumentStatus.CANCELLED, DocumentStatus.DRAFT]),
            )
        ).scalars()
    )

    payload = {
        "gstin": company.gstin,
        "period": period.replace("-", ""),
        "b2b": list(b2b.values()),
        "b2cl": b2cl,
        "b2cs": list(b2cs.values()),
        "exports": exports,
        "cdnr": [
            {
                "note_number": note.number,
                "note_date": note.doc_date.isoformat(),
                "invoice_number": (
                    db.get(Invoice, note.invoice_id).number if note.invoice_id else ""
                ),
                "taxable_value": str(note.taxable_value),
                "cgst": str(note.cgst),
                "sgst": str(note.sgst),
                "igst": str(note.igst),
                "note_type": "C",
            }
            for note in credit_notes
        ],
        "hsn": [
            {**entry, **{k: str(v) for k, v in entry.items() if isinstance(v, Decimal)}}
            for entry in hsn_summary.values()
        ],
    }

    totals_taxable = money(sum((invoice.taxable_value for invoice in invoices), ZERO))
    totals = {
        "taxable_value": totals_taxable,
        "cgst": money(sum((invoice.cgst for invoice in invoices), ZERO)),
        "sgst": money(sum((invoice.sgst for invoice in invoices), ZERO)),
        "igst": money(sum((invoice.igst for invoice in invoices), ZERO)),
        "cess": money(sum((invoice.cess for invoice in invoices), ZERO)),
    }

    return _persist_return(
        db,
        company_id,
        return_type="GSTR1",
        period=period,
        start=start,
        end=end,
        gstin=company.gstin,
        totals=totals,
        payload=payload,
        invoice_count=len(invoices),
        validation_errors=validation_errors,
        user_id=user_id,
    )


def prepare_gstr3b(
    db: Session, company_id: str, *, period: str, user_id: str | None = None
) -> GSTReturn:
    """Build the GSTR-3B summary return from the same ledger data."""
    company = db.get(Company, company_id)
    if company is None:
        raise NotFoundError("Company not found.")

    summary = gst_summary(db, company_id, period=period)
    start, end = period_bounds(period)

    payload = {
        "gstin": company.gstin,
        "period": period.replace("-", ""),
        "table_3_1": {
            "description": "Outward taxable supplies (other than zero-rated, nil-rated, exempted)",
            "taxable_value": str(summary["output"]["taxable_value"]),
            "igst": str(summary["output"]["igst"]),
            "cgst": str(summary["output"]["cgst"]),
            "sgst": str(summary["output"]["sgst"]),
            "cess": str(summary["output"]["cess"]),
        },
        "table_4": {
            "description": "Eligible input tax credit",
            "igst": str(summary["input"]["igst"]),
            "cgst": str(summary["input"]["cgst"]),
            "sgst": str(summary["input"]["sgst"]),
            "cess": str(summary["input"]["cess"]),
            "total": str(summary["input"]["total"]),
        },
        "table_6_1": {
            "description": "Payment of tax",
            "net_payable": str(summary["net_payable"]),
            "credit_carried_forward": str(summary["credit_carried_forward"]),
        },
    }

    totals = {
        "taxable_value": summary["output"]["taxable_value"],
        "cgst": summary["output"]["cgst"],
        "sgst": summary["output"]["sgst"],
        "igst": summary["output"]["igst"],
        "cess": summary["output"]["cess"],
    }

    return _persist_return(
        db,
        company_id,
        return_type="GSTR3B",
        period=period,
        start=start,
        end=end,
        gstin=company.gstin,
        totals=totals,
        payload=payload,
        invoice_count=summary["output"]["invoice_count"],
        validation_errors=[],
        itc_available=summary["input"]["total"],
        net_payable=summary["net_payable"],
        user_id=user_id,
    )


def _persist_return(
    db: Session,
    company_id: str,
    *,
    return_type: str,
    period: str,
    start: date,
    end: date,
    gstin: str,
    totals: dict,
    payload: dict,
    invoice_count: int,
    validation_errors: list[dict],
    itc_available: Decimal = ZERO,
    net_payable: Decimal = ZERO,
    user_id: str | None = None,
) -> GSTReturn:
    from app.models.base import utcnow
    from app.services.audit import record_event

    existing = db.execute(
        select(GSTReturn).where(
            GSTReturn.company_id == company_id,
            GSTReturn.return_type == return_type,
            GSTReturn.period == period,
            GSTReturn.gstin == gstin,
        )
    ).scalar_one_or_none()

    if existing and existing.status == ReturnStatus.FILED_EXTERNALLY:
        raise ValidationError(
            f"{return_type} for {period} is already marked as filed. "
            "Re-preparing it would overwrite the filed snapshot.",
            code="return_already_filed",
        )

    gst_return = existing or GSTReturn(
        company_id=company_id,
        return_type=return_type,
        period=period,
        gstin=gstin,
        period_start=start,
        period_end=end,
    )

    total_tax = money(
        totals["cgst"] + totals["sgst"] + totals["igst"] + totals["cess"]
    )
    gst_return.total_taxable_value = totals["taxable_value"]
    gst_return.total_cgst = totals["cgst"]
    gst_return.total_sgst = totals["sgst"]
    gst_return.total_igst = totals["igst"]
    gst_return.total_cess = totals["cess"]
    gst_return.total_tax = total_tax
    gst_return.itc_available = itc_available
    gst_return.net_payable = net_payable
    gst_return.invoice_count = invoice_count
    gst_return.payload = payload
    gst_return.validation_errors = validation_errors
    gst_return.status = ReturnStatus.PREPARED
    gst_return.prepared_at = utcnow()
    gst_return.prepared_by_user_id = user_id

    if existing is None:
        db.add(gst_return)
    db.flush()

    record_event(
        db,
        company_id=company_id,
        action=AuditAction.CREATE,
        entity_type="GSTReturn",
        entity_id=gst_return.id,
        entity_label=f"{return_type} {period}",
        summary=(
            f"Prepared {return_type} for {period}: taxable {totals['taxable_value']}, "
            f"tax {total_tax}, {len(validation_errors)} validation issue(s)."
        ),
        actor_type=ActorType.USER if user_id else ActorType.SYSTEM,
        actor_id=user_id,
        amount=total_tax,
        after={"status": gst_return.status, "period": period, "return_type": return_type},
    )
    return gst_return


def mark_filed_externally(
    db: Session,
    company_id: str,
    return_id: str,
    *,
    arn: str,
    user_id: str,
    filed_on: date | None = None,
) -> GSTReturn:
    """Record that a human filed this return on the GST portal.

    AI Ledger never sets this itself — the ARN comes from the portal and is
    entered by a person who actually filed.
    """
    from app.models.base import utcnow
    from app.services.audit import record_event

    gst_return = db.get(GSTReturn, return_id)
    if gst_return is None or gst_return.company_id != company_id:
        raise NotFoundError("GST return not found.")
    if not (arn or "").strip():
        raise ValidationError("The acknowledgement reference number (ARN) is required.")

    gst_return.status = ReturnStatus.FILED_EXTERNALLY
    gst_return.external_arn = arn.strip()
    gst_return.filed_externally_at = utcnow()
    db.flush()

    record_event(
        db,
        company_id=company_id,
        action=AuditAction.UPDATE,
        entity_type="GSTReturn",
        entity_id=gst_return.id,
        entity_label=f"{gst_return.return_type} {gst_return.period}",
        summary=(
            f"{gst_return.return_type} {gst_return.period} recorded as filed on the GST "
            f"portal (ARN {arn.strip()}), entered by a user."
        ),
        actor_type=ActorType.USER,
        actor_id=user_id,
        after={"arn": arn.strip(), "status": gst_return.status},
    )
    return gst_return


def reconcile_gstr2b(
    db: Session,
    company_id: str,
    *,
    period: str,
    portal_rows: list[dict],
    source: str = "2B",
    user_id: str | None = None,
) -> dict:
    """Compare purchase records in the books against portal 2A/2B data.

    ``portal_rows`` comes from a file the user downloads from the GST portal —
    this product never logs in to the portal on their behalf.
    """
    start, end = period_bounds(period)

    bills = list(
        db.execute(
            select(Bill).where(
                Bill.company_id == company_id,
                Bill.doc_date >= start,
                Bill.doc_date <= end,
                Bill.status.notin_([DocumentStatus.CANCELLED, DocumentStatus.DRAFT]),
            )
        ).scalars()
    )

    # Index books by (supplier GSTIN, invoice number).
    books_index: dict[tuple[str, str], Bill] = {}
    for bill in bills:
        vendor = db.get(Vendor, bill.vendor_id)
        gstin = (vendor.gstin or "").strip().upper() if vendor else ""
        books_index[(gstin, bill.vendor_invoice_number.strip().upper())] = bill

    # Clear previous rows for this period so re-running is idempotent.
    for existing in db.execute(
        select(GSTReconciliationItem).where(
            GSTReconciliationItem.company_id == company_id,
            GSTReconciliationItem.period == period,
        )
    ).scalars():
        db.delete(existing)
    db.flush()

    matched = missing_in_books = value_mismatch = 0
    seen: set[tuple[str, str]] = set()

    for row in portal_rows:
        gstin = str(row.get("supplier_gstin", "")).strip().upper()
        number = str(row.get("invoice_number", "")).strip().upper()
        key = (gstin, number)
        seen.add(key)

        portal_taxable = money(row.get("taxable_value", 0))
        portal_tax = money(row.get("tax_amount", 0))
        bill = books_index.get(key)

        if bill is None:
            status = "MISSING_IN_BOOKS"
            action = (
                "This invoice appears on the portal but not in your books. "
                "Book it to claim the input tax credit."
            )
            missing_in_books += 1
            books_taxable = books_tax = None
            difference = portal_taxable
        else:
            books_taxable = bill.taxable_value
            books_tax = money(bill.cgst + bill.sgst + bill.igst + bill.cess)
            difference = money(portal_taxable - books_taxable)
            if difference == ZERO and money(portal_tax - books_tax) == ZERO:
                status = "MATCHED"
                action = ""
                matched += 1
            else:
                status = "VALUE_MISMATCH"
                action = (
                    f"Portal shows {portal_taxable} taxable / {portal_tax} tax; "
                    f"your books show {books_taxable} / {books_tax}. Investigate."
                )
                value_mismatch += 1

        db.add(
            GSTReconciliationItem(
                company_id=company_id,
                period=period,
                source=source,
                supplier_gstin=gstin,
                supplier_name=str(row.get("supplier_name", "")),
                invoice_number=number,
                invoice_date=_safe_date(row.get("invoice_date")),
                portal_taxable_value=portal_taxable,
                portal_tax=portal_tax,
                books_taxable_value=books_taxable,
                books_tax=books_tax,
                difference=difference,
                bill_id=bill.id if bill else None,
                status=status,
                action_required=action,
            )
        )

    # Anything in the books the portal does not show — ITC at risk.
    missing_in_portal = 0
    for key, bill in books_index.items():
        if key in seen:
            continue
        vendor = db.get(Vendor, bill.vendor_id)
        books_tax = money(bill.cgst + bill.sgst + bill.igst + bill.cess)
        db.add(
            GSTReconciliationItem(
                company_id=company_id,
                period=period,
                source=source,
                supplier_gstin=key[0],
                supplier_name=vendor.name if vendor else "",
                invoice_number=bill.vendor_invoice_number,
                invoice_date=bill.doc_date,
                portal_taxable_value=None,
                portal_tax=None,
                books_taxable_value=bill.taxable_value,
                books_tax=books_tax,
                difference=money(-bill.taxable_value),
                bill_id=bill.id,
                status="MISSING_IN_PORTAL",
                action_required=(
                    "You have booked this purchase but the supplier has not reported it. "
                    "Input tax credit may be blocked — follow up with the supplier."
                ),
            )
        )
        missing_in_portal += 1

    db.flush()

    return {
        "period": period,
        "source": source,
        "portal_rows": len(portal_rows),
        "book_rows": len(books_index),
        "matched": matched,
        "missing_in_books": missing_in_books,
        "missing_in_portal": missing_in_portal,
        "value_mismatch": value_mismatch,
        "itc_at_risk": money(
            sum(
                (
                    money(bill.cgst + bill.sgst + bill.igst)
                    for key, bill in books_index.items()
                    if key not in seen
                ),
                ZERO,
            )
        ),
    }


def _safe_date(value) -> date | None:
    if isinstance(value, date):
        return value
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d-%b-%Y"):
        try:
            from datetime import datetime

            return datetime.strptime(str(value), fmt).date()
        except ValueError:
            continue
    return None


def list_returns(db: Session, company_id: str, *, limit: int = 24) -> list[GSTReturn]:
    return list(
        db.execute(
            select(GSTReturn)
            .where(GSTReturn.company_id == company_id)
            .order_by(GSTReturn.period.desc())
            .limit(limit)
        ).scalars()
    )
