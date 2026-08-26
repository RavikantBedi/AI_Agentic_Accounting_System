"""Tax endpoints: GST summaries and returns, TDS/TCS, e-invoice and e-way bill."""
from __future__ import annotations

from datetime import date
from typing import Annotated

import json

from fastapi import APIRouter, Depends, File, Query, UploadFile
from sqlalchemy import func, select

from app.deps import CurrentUserDep, DbDep, require_permission
from app.enums import ActorType, AuditAction, EDocStatus, Permission, ProviderMode
from app.errors import ConflictError, NotFoundError, ValidationError
from app.models import Company, EInvoice, EWayBill, GSTReturn, Invoice, TDSEntry, TDSSection, Vendor
from app.models.base import utcnow
from app.money import ZERO, money
from app.schemas import (
    EInvoiceRequest,
    EWayBillRequest,
    GSTReturnOut,
    MarkFiledRequest,
    MessageResponse,
    TDSEntryOut,
)
from app.services import gst as gst_service
from app.services.providers import (
    build_einvoice_payload,
    build_ewaybill_payload,
    get_einvoice_provider,
    get_ewaybill_provider,
    provider_info,
    validate_einvoice_payload,
)

router = APIRouter(prefix="/tax", tags=["Tax"])


# ------------------------------------------------------------------- GST --

@router.get("/gst/summary")
def gst_summary(
    db: DbDep,
    current: CurrentUserDep,
    period: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
) -> dict:
    current.require(Permission.READ_TAX.value)
    return gst_service.gst_summary(db, current.company_id, period=period)


@router.get("/gst/returns", response_model=list[GSTReturnOut])
def list_gst_returns(
    db: DbDep, current: CurrentUserDep, limit: int = Query(default=24, ge=1, le=60)
) -> list[GSTReturnOut]:
    current.require(Permission.READ_TAX.value)
    return [
        GSTReturnOut.model_validate(row)
        for row in gst_service.list_returns(db, current.company_id, limit=limit)
    ]


@router.post("/gst/returns/{return_type}/prepare", response_model=GSTReturnOut)
def prepare_return(
    return_type: str,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_TAX.value))],
    period: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
) -> GSTReturnOut:
    """Prepare return data from the ledger. This does not file anything."""
    kind = return_type.upper().replace("-", "")
    if kind == "GSTR1":
        gst_return = gst_service.prepare_gstr1(
            db, current.company_id, period=period, user_id=current.id
        )
    elif kind == "GSTR3B":
        gst_return = gst_service.prepare_gstr3b(
            db, current.company_id, period=period, user_id=current.id
        )
    else:
        raise ValidationError(
            f"'{return_type}' is not supported. Use GSTR1 or GSTR3B.",
            details={"supported": ["GSTR1", "GSTR3B"]},
        )
    return GSTReturnOut.model_validate(gst_return)


@router.get("/gst/returns/{return_id}/payload")
def get_return_payload(return_id: str, db: DbDep, current: CurrentUserDep) -> dict:
    """The portal-shaped JSON, for review or upload to the GST portal."""
    current.require(Permission.READ_TAX.value)
    gst_return = db.get(GSTReturn, return_id)
    if gst_return is None or gst_return.company_id != current.company_id:
        raise NotFoundError("GST return not found.")
    return {
        "return_type": gst_return.return_type,
        "period": gst_return.period,
        "status": gst_return.status,
        "payload": gst_return.payload,
        "validation_errors": gst_return.validation_errors,
        "notice": (
            "This data is prepared for filing on the GST portal. AI Ledger does not "
            "submit it — download it, file on the portal, then record the ARN here."
        ),
    }


@router.post("/gst/returns/{return_id}/mark-filed", response_model=GSTReturnOut)
def mark_filed(
    return_id: str,
    payload: MarkFiledRequest,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_TAX.value))],
) -> GSTReturnOut:
    """Record that a human filed this return on the portal, with its ARN."""
    gst_return = gst_service.mark_filed_externally(
        db,
        current.company_id,
        return_id,
        arn=payload.arn,
        user_id=current.id,
        filed_on=payload.filed_on,
    )
    return GSTReturnOut.model_validate(gst_return)


@router.post("/gst/reconcile-2b")
async def reconcile_2b(
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_TAX.value))],
    period: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    file: UploadFile = File(...),
) -> dict:
    """Reconcile the books against a GSTR-2A/2B extract downloaded by the user.

    AI Ledger never signs in to the GST portal. Export the 2B JSON (or a CSV of
    it) from the portal and upload it here.
    """
    content = await file.read()
    rows = _parse_2b(content, file.filename or "")
    if not rows:
        raise ValidationError(
            "No supplier invoice rows were found in that file. Upload the GSTR-2B "
            "JSON from the portal, or a CSV with supplier_gstin, invoice_number, "
            "invoice_date, taxable_value and tax_amount columns.",
            code="empty_2b_file",
        )

    result = gst_service.reconcile_gstr2b(
        db,
        current.company_id,
        period=period,
        portal_rows=rows,
        user_id=current.id,
    )
    return {
        **result,
        "itc_at_risk": str(result["itc_at_risk"]),
        "notice": (
            "Rows marked MISSING_IN_PORTAL mean your supplier has not reported the "
            "invoice yet, so the input tax credit may be blocked."
        ),
    }


def _parse_2b(content: bytes, filename: str) -> list[dict]:
    """Read a GSTR-2B extract from portal JSON, or from CSV/XLSX."""
    name = (filename or "").lower()

    if name.endswith(".json"):
        try:
            payload = json.loads(content.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValidationError("The file is not valid JSON.", code="invalid_json") from exc

        # Portal shape: {"data": {"docdata": {"b2b": [{"ctin":..., "inv":[...]}]}}}
        rows: list[dict] = []
        b2b = (
            payload.get("data", {}).get("docdata", {}).get("b2b")
            or payload.get("docdata", {}).get("b2b")
            or payload.get("b2b")
        )
        if isinstance(b2b, list):
            for supplier in b2b:
                gstin = supplier.get("ctin", "")
                name_ = supplier.get("trdnm", "")
                for invoice in supplier.get("inv", []) or []:
                    rows.append(
                        {
                            "supplier_gstin": gstin,
                            "supplier_name": name_,
                            "invoice_number": invoice.get("inum", ""),
                            "invoice_date": invoice.get("dt", ""),
                            "taxable_value": invoice.get("txval", 0),
                            "tax_amount": (
                                float(invoice.get("igst", 0) or 0)
                                + float(invoice.get("cgst", 0) or 0)
                                + float(invoice.get("sgst", 0) or 0)
                                + float(invoice.get("cess", 0) or 0)
                            ),
                        }
                    )
            return rows

        # A plain array of already-flat rows is also accepted.
        if isinstance(payload, list):
            return payload
        return []

    if name.endswith((".xlsx", ".xls")):
        import io

        import pandas as pd

        frame = pd.read_excel(io.BytesIO(content), dtype=object)
        frame.columns = [str(column).strip().lower().replace(" ", "_") for column in frame.columns]
        return frame.where(frame.notna(), None).to_dict(orient="records")

    import csv
    import io

    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    return [
        {(key or "").strip().lower().replace(" ", "_"): value for key, value in row.items() if key}
        for row in reader
    ]


@router.get("/gst/reconciliation")
def gst_reconciliation(
    db: DbDep,
    current: CurrentUserDep,
    period: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
) -> dict:
    """Books vs portal comparison rows for a period."""
    current.require(Permission.READ_TAX.value)
    from app.models import GSTReconciliationItem

    items = list(
        db.execute(
            select(GSTReconciliationItem).where(
                GSTReconciliationItem.company_id == current.company_id,
                GSTReconciliationItem.period == period,
            )
        ).scalars()
    )

    by_status: dict[str, int] = {}
    for item in items:
        by_status[item.status] = by_status.get(item.status, 0) + 1

    return {
        "period": period,
        "total_rows": len(items),
        "by_status": by_status,
        "itc_at_risk": money(
            sum(
                (item.books_tax or ZERO for item in items if item.status == "MISSING_IN_PORTAL"),
                ZERO,
            )
        ),
        "rows": [
            {
                "id": item.id,
                "supplier_gstin": item.supplier_gstin,
                "supplier_name": item.supplier_name,
                "invoice_number": item.invoice_number,
                "invoice_date": item.invoice_date,
                "portal_taxable_value": item.portal_taxable_value,
                "portal_tax": item.portal_tax,
                "books_taxable_value": item.books_taxable_value,
                "books_tax": item.books_tax,
                "difference": item.difference,
                "status": item.status,
                "action_required": item.action_required,
                "bill_id": item.bill_id,
            }
            for item in items[:200]
        ],
        "notice": (
            "Download the 2A/2B extract from the GST portal and import it here. "
            "AI Ledger does not log in to the portal on your behalf."
        ),
    }


# ------------------------------------------------------------------- TDS --

@router.get("/tds/entries", response_model=list[TDSEntryOut])
def list_tds(
    db: DbDep,
    current: CurrentUserDep,
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    deposited: bool | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[TDSEntryOut]:
    current.require(Permission.READ_TAX.value)

    stmt = select(TDSEntry).where(TDSEntry.company_id == current.company_id)
    if start_date:
        stmt = stmt.where(TDSEntry.deduction_date >= start_date)
    if end_date:
        stmt = stmt.where(TDSEntry.deduction_date <= end_date)
    if deposited is not None:
        stmt = stmt.where(TDSEntry.deposited.is_(deposited))

    entries = list(
        db.execute(stmt.order_by(TDSEntry.deduction_date.desc()).limit(limit)).scalars()
    )

    vendor_ids = {entry.vendor_id for entry in entries}
    vendors = (
        {
            vendor.id: vendor.name
            for vendor in db.execute(select(Vendor).where(Vendor.id.in_(vendor_ids))).scalars()
        }
        if vendor_ids
        else {}
    )

    results = []
    for entry in entries:
        out = TDSEntryOut.model_validate(entry)
        out.vendor_name = vendors.get(entry.vendor_id, "")
        results.append(out)
    return results


@router.get("/tds/summary")
def tds_summary(
    db: DbDep,
    current: CurrentUserDep,
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
) -> dict:
    current.require(Permission.READ_TAX.value)
    from app.ai.tools import get_tds_summary

    return get_tds_summary(db, current.company_id, start_date=start_date, end_date=end_date)


@router.get("/tds/sections")
def list_tds_sections(db: DbDep, current: CurrentUserDep) -> dict:
    """The configured, versioned TDS sections. Rates are data, not code."""
    current.require(Permission.READ_TAX.value)
    sections = list(
        db.execute(
            select(TDSSection)
            .where(TDSSection.company_id == current.company_id)
            .order_by(TDSSection.section, TDSSection.version.desc())
        ).scalars()
    )
    return {
        "sections": [
            {
                "id": section.id,
                "section": section.section,
                "name": section.name,
                "version": section.version,
                "rate_with_pan": str(section.rate_with_pan),
                "rate_without_pan": str(section.rate_without_pan),
                "single_payment_threshold": str(section.single_payment_threshold),
                "annual_threshold": str(section.annual_threshold),
                "effective_from": section.effective_from,
                "effective_to": section.effective_to,
                "is_active": section.is_active,
                "legal_reference": section.legal_reference,
            }
            for section in sections
        ],
        "notice": (
            "These are editable, dated configuration values seeded for convenience. "
            "Verify them against the current Finance Act before relying on them."
        ),
    }


@router.post("/tds/entries/{entry_id}/mark-deposited", response_model=TDSEntryOut)
def mark_tds_deposited(
    entry_id: str,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_TAX.value))],
    challan_number: str = Query(..., min_length=1, max_length=60),
    deposited_date: date = Query(default_factory=date.today),
) -> TDSEntryOut:
    entry = db.get(TDSEntry, entry_id)
    if entry is None or entry.company_id != current.company_id:
        raise NotFoundError("TDS entry not found.")
    if entry.deposited:
        raise ConflictError("This TDS entry is already marked as deposited.")

    entry.deposited = True
    entry.deposited_date = deposited_date
    entry.challan_number = challan_number
    db.flush()

    from app.services.audit import record_event

    record_event(
        db,
        company_id=current.company_id,
        action=AuditAction.UPDATE,
        entity_type="TDSEntry",
        entity_id=entry.id,
        entity_label=f"{entry.section} {entry.tds_amount}",
        summary=f"TDS {entry.tds_amount} under {entry.section} recorded as deposited (challan {challan_number})",
        actor_type=ActorType.USER,
        actor_id=current.id,
        actor_label=current.email,
        amount=entry.tds_amount,
    )
    return TDSEntryOut.model_validate(entry)


# ------------------------------------------------- e-invoice / e-way bill --

@router.get("/providers")
def get_providers(current: CurrentUserDep) -> dict:
    """What is actually connected. Demo mode is stated plainly."""
    return provider_info()


@router.post("/einvoice/generate")
def generate_einvoice(
    payload: EInvoiceRequest,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_TAX.value))],
) -> dict:
    """Prepare and submit an e-invoice through the configured provider."""
    invoice = db.get(Invoice, payload.invoice_id)
    if invoice is None or invoice.company_id != current.company_id:
        raise NotFoundError("Invoice not found.")

    existing = db.execute(
        select(EInvoice).where(
            EInvoice.company_id == current.company_id, EInvoice.invoice_id == invoice.id
        )
    ).scalar_one_or_none()
    if existing is not None and existing.status == EDocStatus.ACKNOWLEDGED:
        raise ConflictError(
            f"An e-invoice already exists for {invoice.number} "
            f"({'demo' if existing.provider_mode == ProviderMode.MOCK else 'live'} mode)."
        )

    company = db.get(Company, current.company_id)
    request_payload = build_einvoice_payload(db, company, invoice)
    provider = get_einvoice_provider()
    response = provider.generate_irn(request_payload)

    record = existing or EInvoice(
        company_id=current.company_id, invoice_id=invoice.id
    )
    record.provider = provider.name
    record.provider_mode = provider.mode
    record.request_payload = request_payload
    record.response_payload = response.data
    record.status = response.status

    if response.success:
        record.irn = response.data.get("Irn", "")
        record.ack_no = response.data.get("AckNo", "")
        record.signed_qr_code = response.data.get("SignedQRCode", "")
        record.ack_date = utcnow()
        record.error_code = ""
        record.error_message = ""
        invoice.einvoice_status = EDocStatus.ACKNOWLEDGED
    else:
        record.error_code = response.error_code
        record.error_message = response.error_message
        invoice.einvoice_status = EDocStatus.FAILED

    if existing is None:
        db.add(record)
    db.flush()

    from app.services.audit import record_event

    record_event(
        db,
        company_id=current.company_id,
        action=AuditAction.CREATE,
        entity_type="EInvoice",
        entity_id=record.id,
        entity_label=invoice.number,
        summary=(
            f"e-Invoice {'generated' if response.success else 'failed'} for {invoice.number} "
            f"via the {provider.name} provider ({provider.mode} mode)"
        ),
        actor_type=ActorType.USER,
        actor_id=current.id,
        actor_label=current.email,
        amount=invoice.total,
        after={"mode": provider.mode, "status": record.status, "irn": record.irn},
    )

    return {
        **response.as_dict(),
        "einvoice_id": record.id,
        "invoice_number": invoice.number,
        "is_government_confirmed": record.is_government_confirmed,
    }


@router.post("/einvoice/validate")
def validate_einvoice(
    payload: EInvoiceRequest, db: DbDep, current: CurrentUserDep
) -> dict:
    """Run the IRP's validations locally, before submitting anything."""
    current.require(Permission.READ_TAX.value)
    invoice = db.get(Invoice, payload.invoice_id)
    if invoice is None or invoice.company_id != current.company_id:
        raise NotFoundError("Invoice not found.")

    company = db.get(Company, current.company_id)
    request_payload = build_einvoice_payload(db, company, invoice)
    errors = validate_einvoice_payload(request_payload)
    return {
        "valid": not errors,
        "errors": errors,
        "payload": request_payload,
    }


@router.get("/einvoice/{invoice_id}")
def get_einvoice(invoice_id: str, db: DbDep, current: CurrentUserDep) -> dict:
    current.require(Permission.READ_TAX.value)
    record = db.execute(
        select(EInvoice).where(
            EInvoice.company_id == current.company_id, EInvoice.invoice_id == invoice_id
        )
    ).scalar_one_or_none()
    if record is None:
        raise NotFoundError("No e-invoice exists for this invoice.")

    return {
        "id": record.id,
        "invoice_id": record.invoice_id,
        "status": record.status,
        "provider": record.provider,
        "provider_mode": record.provider_mode,
        "is_government_confirmed": record.is_government_confirmed,
        "irn": record.irn,
        "ack_no": record.ack_no,
        "ack_date": record.ack_date,
        "signed_qr_code": record.signed_qr_code,
        "error_message": record.error_message,
        "disclaimer": (
            "DEMO MODE — this IRN was generated locally and has no legal standing."
            if record.provider_mode == ProviderMode.MOCK
            else ""
        ),
    }


@router.post("/ewaybill/generate")
def generate_ewaybill(
    payload: EWayBillRequest,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_TAX.value))],
) -> dict:
    invoice = db.get(Invoice, payload.invoice_id)
    if invoice is None or invoice.company_id != current.company_id:
        raise NotFoundError("Invoice not found.")

    existing = db.execute(
        select(EWayBill).where(
            EWayBill.company_id == current.company_id, EWayBill.invoice_id == invoice.id
        )
    ).scalar_one_or_none()
    if existing is not None and existing.status == EDocStatus.ACKNOWLEDGED:
        raise ConflictError(f"An e-way bill already exists for {invoice.number}.")

    company = db.get(Company, current.company_id)
    request_payload = build_ewaybill_payload(
        db,
        company,
        invoice,
        distance_km=payload.distance_km,
        vehicle_number=payload.vehicle_number,
        transporter_id=payload.transporter_id,
        transporter_name=payload.transporter_name,
        transport_mode=payload.transport_mode,
        from_place=payload.from_place,
        to_place=payload.to_place,
    )
    provider = get_ewaybill_provider()
    response = provider.generate(request_payload)

    record = existing or EWayBill(company_id=current.company_id, invoice_id=invoice.id)
    record.provider = provider.name
    record.provider_mode = provider.mode
    record.request_payload = request_payload
    record.response_payload = response.data
    record.status = response.status
    record.transport_mode = payload.transport_mode
    record.vehicle_number = payload.vehicle_number
    record.transporter_id = payload.transporter_id
    record.transporter_name = payload.transporter_name
    record.distance_km = payload.distance_km
    record.from_place = payload.from_place or company.city
    record.to_place = payload.to_place

    if response.success:
        record.ewb_number = response.data.get("ewayBillNo", "")
        record.ewb_date = utcnow()
        record.error_message = ""
        invoice.ewaybill_status = EDocStatus.ACKNOWLEDGED
    else:
        record.error_message = response.error_message
        invoice.ewaybill_status = EDocStatus.FAILED

    if existing is None:
        db.add(record)
    db.flush()

    return {
        **response.as_dict(),
        "ewaybill_id": record.id,
        "invoice_number": invoice.number,
        "is_government_confirmed": record.is_government_confirmed,
    }


@router.get("/ewaybill/{invoice_id}")
def get_ewaybill(invoice_id: str, db: DbDep, current: CurrentUserDep) -> dict:
    current.require(Permission.READ_TAX.value)
    record = db.execute(
        select(EWayBill).where(
            EWayBill.company_id == current.company_id, EWayBill.invoice_id == invoice_id
        )
    ).scalar_one_or_none()
    if record is None:
        raise NotFoundError("No e-way bill exists for this invoice.")

    return {
        "id": record.id,
        "invoice_id": record.invoice_id,
        "status": record.status,
        "provider": record.provider,
        "provider_mode": record.provider_mode,
        "is_government_confirmed": record.is_government_confirmed,
        "ewb_number": record.ewb_number,
        "ewb_date": record.ewb_date,
        "valid_until": record.valid_until,
        "vehicle_number": record.vehicle_number,
        "distance_km": record.distance_km,
        "error_message": record.error_message,
        "disclaimer": (
            "DEMO MODE — this e-way bill number was generated locally and has no legal standing."
            if record.provider_mode == ProviderMode.MOCK
            else ""
        ),
    }
