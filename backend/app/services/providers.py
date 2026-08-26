"""External regulatory provider adapters: e-Invoice (IRP) and e-Way Bill.

**What this does and does not do.**

This module prepares the exact JSON payloads the government systems expect, and
defines the interface a real integration implements. It ships with a *mock*
provider so the workflow is testable end to end.

The mock never fabricates a government identifier. Every value it returns is
prefixed ``DEMO-`` and every record it produces is stamped ``provider_mode =
MOCK``, so a demo acknowledgement can never be mistaken for a real one — in the
database, in the API, or in the UI.

A real integration activates only when credentials are configured. Until then
the API says plainly that live filing is not configured.
"""
from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.enums import EDocStatus, ProviderMode
from app.errors import NotFoundError, ValidationError
from app.models import Company, Customer, EInvoice, EWayBill, Invoice
from app.money import ZERO, money


@dataclass
class ProviderResponse:
    """A provider's answer, always carrying which mode produced it."""

    success: bool
    mode: str
    status: str
    data: dict[str, Any] = field(default_factory=dict)
    error_code: str = ""
    error_message: str = ""
    is_government_confirmed: bool = False

    def as_dict(self) -> dict:
        return {
            "success": self.success,
            "mode": self.mode,
            "status": self.status,
            "data": self.data,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "is_government_confirmed": self.is_government_confirmed,
            "disclaimer": (
                "DEMO MODE — this identifier was generated locally and has no legal "
                "standing. It was not issued by the government portal."
                if self.mode == ProviderMode.MOCK
                else ""
            ),
        }


# --------------------------------------------------------- payload builders --

def build_einvoice_payload(db: Session, company: Company, invoice: Invoice) -> dict:
    """Build the IRP-shaped invoice payload (schema 1.1 field names)."""
    customer = db.get(Customer, invoice.customer_id)
    if customer is None:
        raise NotFoundError("Customer not found for this invoice.")

    return {
        "Version": "1.1",
        "TranDtls": {
            "TaxSch": "GST",
            "SupTyp": "B2B" if customer.gstin else "B2C",
            "RegRev": "Y" if invoice.is_reverse_charge else "N",
            "IgstOnIntra": "N",
        },
        "DocDtls": {
            "Typ": "INV",
            "No": invoice.number,
            "Dt": invoice.doc_date.strftime("%d/%m/%Y"),
        },
        "SellerDtls": {
            "Gstin": company.gstin,
            "LglNm": company.legal_name,
            "TrdNm": company.name,
            "Addr1": company.address_line1 or company.city,
            "Loc": company.city,
            "Pin": _pin(company.pincode),
            "Stcd": company.state_code,
            "Em": company.email,
            "Ph": company.phone[:12] if company.phone else "",
        },
        "BuyerDtls": {
            "Gstin": customer.gstin or "URP",
            "LglNm": customer.legal_name or customer.name,
            "TrdNm": customer.name,
            "Pos": invoice.place_of_supply or customer.state_code,
            "Addr1": customer.billing_address[:100] or customer.city,
            "Loc": customer.city,
            "Pin": _pin(customer.pincode),
            "Stcd": customer.state_code,
            "Em": customer.email,
        },
        "ItemList": [
            {
                "SlNo": str(line.line_number),
                "PrdDesc": (line.description or "")[:300],
                "IsServc": "N" if line.hsn_sac and not line.hsn_sac.startswith("99") else "Y",
                "HsnCd": line.hsn_sac or "",
                "Qty": float(line.quantity),
                "Unit": line.uom,
                "UnitPrice": float(line.rate),
                "TotAmt": float(line.gross_amount),
                "Discount": float(line.discount_amount),
                "AssAmt": float(line.taxable_value),
                "GstRt": float(line.gst_rate),
                "IgstAmt": float(line.igst),
                "CgstAmt": float(line.cgst),
                "SgstAmt": float(line.sgst),
                "CesRt": float(line.cess_rate),
                "CesAmt": float(line.cess),
                "TotItemVal": float(line.line_total),
            }
            for line in invoice.lines
        ],
        "ValDtls": {
            "AssVal": float(invoice.taxable_value),
            "CgstVal": float(invoice.cgst),
            "SgstVal": float(invoice.sgst),
            "IgstVal": float(invoice.igst),
            "CesVal": float(invoice.cess),
            "Discount": float(invoice.discount_total),
            "RndOffAmt": float(invoice.round_off),
            "TotInvVal": float(invoice.total),
        },
    }


def build_ewaybill_payload(
    db: Session,
    company: Company,
    invoice: Invoice,
    *,
    distance_km: int,
    vehicle_number: str = "",
    transporter_id: str = "",
    transporter_name: str = "",
    transport_mode: str = "ROAD",
    from_place: str = "",
    to_place: str = "",
) -> dict:
    customer = db.get(Customer, invoice.customer_id)
    mode_codes = {"ROAD": "1", "RAIL": "2", "AIR": "3", "SHIP": "4"}

    return {
        "supplyType": "O",
        "subSupplyType": "1",
        "docType": "INV",
        "docNo": invoice.number,
        "docDate": invoice.doc_date.strftime("%d/%m/%Y"),
        "fromGstin": company.gstin,
        "fromTrdName": company.name,
        "fromAddr1": company.address_line1 or company.city,
        "fromPlace": from_place or company.city,
        "fromPincode": _pin_int(company.pincode),
        "fromStateCode": _state_int(company.state_code),
        "toGstin": (customer.gstin if customer and customer.gstin else "URP"),
        "toTrdName": customer.name if customer else "",
        "toAddr1": (customer.shipping_address or customer.billing_address)[:100] if customer else "",
        "toPlace": to_place or (customer.city if customer else ""),
        "toPincode": _pin_int(customer.pincode if customer else ""),
        "toStateCode": _state_int(invoice.place_of_supply or (customer.state_code if customer else "")),
        "totalValue": float(invoice.taxable_value),
        "cgstValue": float(invoice.cgst),
        "sgstValue": float(invoice.sgst),
        "igstValue": float(invoice.igst),
        "cessValue": float(invoice.cess),
        "totInvValue": float(invoice.total),
        "transporterId": transporter_id,
        "transporterName": transporter_name,
        "transDocNo": "",
        "transMode": mode_codes.get(transport_mode.upper(), "1"),
        "transDistance": str(distance_km),
        "vehicleNo": vehicle_number.replace(" ", "").upper(),
        "vehicleType": "R",
        "itemList": [
            {
                "productName": (line.description or "")[:100],
                "hsnCode": line.hsn_sac or "",
                "quantity": float(line.quantity),
                "qtyUnit": line.uom,
                "taxableAmount": float(line.taxable_value),
                "sgstRate": float(line.gst_rate / 2) if line.sgst else 0.0,
                "cgstRate": float(line.gst_rate / 2) if line.cgst else 0.0,
                "igstRate": float(line.gst_rate) if line.igst else 0.0,
                "cessRate": float(line.cess_rate),
            }
            for line in invoice.lines
        ],
    }


def validate_einvoice_payload(payload: dict) -> list[str]:
    """The validations the real IRP performs, run locally first."""
    errors: list[str] = []

    seller = payload.get("SellerDtls", {})
    buyer = payload.get("BuyerDtls", {})

    if not seller.get("Gstin"):
        errors.append("The company GSTIN is required for e-invoicing.")
    if not seller.get("Stcd"):
        errors.append("The company state code is required.")
    if not buyer.get("Pos"):
        errors.append("Place of supply is required.")
    if buyer.get("Gstin") not in ("URP", None, "") and len(buyer.get("Gstin", "")) != 15:
        errors.append("The buyer GSTIN must be 15 characters, or 'URP' for unregistered.")

    items = payload.get("ItemList", [])
    if not items:
        errors.append("At least one line item is required.")
    for item in items:
        if not item.get("HsnCd"):
            errors.append(f"Line {item.get('SlNo')}: HSN/SAC code is mandatory for e-invoicing.")
        if float(item.get("AssAmt", 0)) <= 0:
            errors.append(f"Line {item.get('SlNo')}: taxable value must be greater than zero.")

    values = payload.get("ValDtls", {})
    computed = round(
        float(values.get("AssVal", 0))
        + float(values.get("CgstVal", 0))
        + float(values.get("SgstVal", 0))
        + float(values.get("IgstVal", 0))
        + float(values.get("CesVal", 0))
        + float(values.get("RndOffAmt", 0)),
        2,
    )
    stated = round(float(values.get("TotInvVal", 0)), 2)
    if abs(computed - stated) > 1.0:
        errors.append(
            f"Invoice total {stated} does not equal taxable value plus tax ({computed})."
        )

    return errors


# ------------------------------------------------------------- interfaces --

class EInvoiceProvider(ABC):
    """Interface a real IRP integration implements."""

    name = "base"
    mode = ProviderMode.MOCK

    @abstractmethod
    def generate_irn(self, payload: dict) -> ProviderResponse: ...

    @abstractmethod
    def cancel_irn(self, irn: str, reason: str, remark: str = "") -> ProviderResponse: ...

    def is_configured(self) -> bool:
        return False


class EWayBillProvider(ABC):
    name = "base"
    mode = ProviderMode.MOCK

    @abstractmethod
    def generate(self, payload: dict) -> ProviderResponse: ...

    @abstractmethod
    def cancel(self, ewb_number: str, reason: str) -> ProviderResponse: ...

    def is_configured(self) -> bool:
        return False


class MockEInvoiceProvider(EInvoiceProvider):
    """Demo IRP. Produces clearly-marked non-government identifiers."""

    name = "mock"
    mode = ProviderMode.MOCK

    def generate_irn(self, payload: dict) -> ProviderResponse:
        errors = validate_einvoice_payload(payload)
        if errors:
            return ProviderResponse(
                success=False,
                mode=self.mode,
                status=EDocStatus.FAILED,
                error_code="VALIDATION",
                error_message="; ".join(errors),
            )

        # A deterministic digest of the real payload, prefixed so it can never
        # be confused with a genuine 64-character IRN.
        seller = payload["SellerDtls"]["Gstin"]
        doc = payload["DocDtls"]
        digest = hashlib.sha256(
            f"{seller}|{doc['No']}|{doc['Dt']}".encode("utf-8")
        ).hexdigest()
        now = datetime.now(timezone.utc)

        return ProviderResponse(
            success=True,
            mode=self.mode,
            status=EDocStatus.ACKNOWLEDGED,
            is_government_confirmed=False,
            data={
                "Irn": f"DEMO-{digest[:59]}",
                "AckNo": f"DEMO{digest[:10].upper()}",
                "AckDt": now.strftime("%Y-%m-%d %H:%M:%S"),
                "SignedQRCode": f"DEMO-QR-{digest[:32]}",
                "SignedInvoice": "",
                "Status": "ACT",
                "_demo": True,
                "_notice": (
                    "Generated by the mock IRP adapter. Not registered with the "
                    "Invoice Registration Portal and has no legal validity."
                ),
            },
        )

    def cancel_irn(self, irn: str, reason: str, remark: str = "") -> ProviderResponse:
        if not irn.startswith("DEMO-"):
            return ProviderResponse(
                success=False,
                mode=self.mode,
                status=EDocStatus.FAILED,
                error_code="NOT_DEMO_IRN",
                error_message=(
                    "This IRN was not issued by the mock adapter and cannot be cancelled here."
                ),
            )
        return ProviderResponse(
            success=True,
            mode=self.mode,
            status=EDocStatus.CANCELLED,
            data={
                "Irn": irn,
                "CancelDate": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "Reason": reason,
                "Remark": remark,
                "_demo": True,
            },
        )

    def is_configured(self) -> bool:
        return True


class MockEWayBillProvider(EWayBillProvider):
    name = "mock"
    mode = ProviderMode.MOCK

    def generate(self, payload: dict) -> ProviderResponse:
        errors: list[str] = []
        if not payload.get("fromGstin"):
            errors.append("The company GSTIN is required.")
        if not payload.get("transDistance") or int(payload.get("transDistance", 0)) <= 0:
            errors.append("Transport distance in kilometres is required.")
        if not payload.get("vehicleNo") and not payload.get("transporterId"):
            errors.append("Either a vehicle number or a transporter ID is required.")
        if float(payload.get("totInvValue", 0)) <= 0:
            errors.append("Invoice value must be greater than zero.")

        if errors:
            return ProviderResponse(
                success=False,
                mode=self.mode,
                status=EDocStatus.FAILED,
                error_code="VALIDATION",
                error_message="; ".join(errors),
            )

        digest = hashlib.sha256(
            f"{payload['fromGstin']}|{payload['docNo']}|{payload['docDate']}".encode("utf-8")
        ).hexdigest()
        now = datetime.now(timezone.utc)
        distance = int(payload.get("transDistance", 0))
        # Real validity is one day per 200 km slab; mirrored here for realism.
        validity_days = max(1, (distance + 199) // 200)

        return ProviderResponse(
            success=True,
            mode=self.mode,
            status=EDocStatus.ACKNOWLEDGED,
            is_government_confirmed=False,
            data={
                "ewayBillNo": f"DEMO{int(digest[:9], 16) % 1_000_000_000:09d}",
                "ewayBillDate": now.strftime("%d/%m/%Y %I:%M:%S %p"),
                "validUpto": (now + timedelta(days=validity_days)).strftime("%d/%m/%Y %I:%M:%S %p"),
                "_demo": True,
                "_notice": (
                    "Generated by the mock e-way bill adapter. Not registered with the "
                    "NIC portal and has no legal validity."
                ),
            },
        )

    def cancel(self, ewb_number: str, reason: str) -> ProviderResponse:
        return ProviderResponse(
            success=True,
            mode=self.mode,
            status=EDocStatus.CANCELLED,
            data={"ewayBillNo": ewb_number, "reason": reason, "_demo": True},
        )

    def is_configured(self) -> bool:
        return True


class IRPEInvoiceProvider(EInvoiceProvider):
    """Real IRP integration.

    Intentionally refuses to run without credentials rather than degrading to a
    fake response — a silent fallback to mock would be exactly the kind of
    fabricated confirmation this product must never produce.
    """

    name = "irp"
    mode = ProviderMode.LIVE

    def is_configured(self) -> bool:
        return bool(
            settings.EINVOICE_API_BASE
            and settings.EINVOICE_USERNAME
            and settings.EINVOICE_PASSWORD
            and settings.EINVOICE_CLIENT_ID
            and settings.EINVOICE_CLIENT_SECRET
        )

    def generate_irn(self, payload: dict) -> ProviderResponse:
        if not self.is_configured():
            raise ValidationError(
                "Live e-invoicing is not configured. Set EINVOICE_API_BASE, "
                "EINVOICE_USERNAME, EINVOICE_PASSWORD, EINVOICE_CLIENT_ID and "
                "EINVOICE_CLIENT_SECRET to enable it. Until then, use the mock "
                "provider for testing.",
                code="einvoice_not_configured",
            )
        # A real implementation authenticates, encrypts the payload with the
        # session key and POSTs to the IRP. Deliberately not stubbed out with
        # a fake success path.
        raise ValidationError(
            "The live IRP client is not implemented in this build. Credentials are "
            "configured, but no request was sent and no IRN was generated.",
            code="einvoice_live_not_implemented",
        )

    def cancel_irn(self, irn: str, reason: str, remark: str = "") -> ProviderResponse:
        raise ValidationError(
            "The live IRP client is not implemented in this build.",
            code="einvoice_live_not_implemented",
        )


def get_einvoice_provider() -> EInvoiceProvider:
    if settings.EINVOICE_PROVIDER == "irp":
        return IRPEInvoiceProvider()
    return MockEInvoiceProvider()


def get_ewaybill_provider() -> EWayBillProvider:
    return MockEWayBillProvider()


def provider_info() -> dict:
    """What the UI shows about external integrations — honestly."""
    einvoice = get_einvoice_provider()
    ewaybill = get_ewaybill_provider()
    return {
        "einvoice": {
            "provider": einvoice.name,
            "mode": einvoice.mode,
            "configured": einvoice.is_configured(),
            "is_live": einvoice.mode == ProviderMode.LIVE,
            "notice": (
                "Running in DEMO mode. Identifiers are generated locally, prefixed "
                "'DEMO-', and have no legal validity."
                if einvoice.mode == ProviderMode.MOCK
                else "Live mode: credentials are configured."
            ),
        },
        "ewaybill": {
            "provider": ewaybill.name,
            "mode": ewaybill.mode,
            "configured": ewaybill.is_configured(),
            "is_live": ewaybill.mode == ProviderMode.LIVE,
            "notice": (
                "Running in DEMO mode. Numbers are generated locally, prefixed "
                "'DEMO', and have no legal validity."
                if ewaybill.mode == ProviderMode.MOCK
                else "Live mode: credentials are configured."
            ),
        },
        "gst_filing": {
            "supported": False,
            "notice": (
                "AI Ledger prepares GSTR-1 and GSTR-3B data for review and export. "
                "It does not file returns with the GST portal. File on the portal and "
                "record the ARN here."
            ),
        },
    }


def _pin(value: str) -> str:
    digits = "".join(c for c in (value or "") if c.isdigit())
    return digits[:6] if len(digits) >= 6 else ""


def _pin_int(value: str) -> int:
    digits = _pin(value)
    return int(digits) if digits else 0


def _state_int(value: str) -> int:
    digits = "".join(c for c in (value or "") if c.isdigit())
    return int(digits) if digits else 0
