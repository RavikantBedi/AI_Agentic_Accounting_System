"""Document ingestion: storage, text extraction and OCR.

Text extraction degrades gracefully. PyMuPDF is used when available, then
pdfplumber, then Tesseract OCR for scanned pages. If none is installed the
document is still stored and flagged NEEDS_REVIEW — the product keeps working,
it just cannot read that file automatically.

Uploaded content is untrusted: it is hashed for duplicate detection, its type
is validated by content rather than by filename, and its text only ever reaches
a model inside a sanitised, fenced block.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.enums import ActorType, AuditAction, DocumentKind, ExtractionStatus
from app.errors import DuplicateDocumentError, NotFoundError, ValidationError
from app.models import StoredDocument
from app.models.base import new_id, utcnow
from app.money import ZERO, D, money

# Accepted types, identified by magic bytes rather than by extension.
MAGIC_SIGNATURES: list[tuple[bytes, str, str]] = [
    (b"%PDF-", "application/pdf", ".pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
]

MAX_TEXT_CHARS = 200_000


def detect_mime(content: bytes) -> tuple[str, str]:
    """Identify a file by its magic bytes. Returns ``(mime, extension)``."""
    for signature, mime, extension in MAGIC_SIGNATURES:
        if content.startswith(signature):
            return mime, extension
    # TIFF (both endiannesses)
    if content[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff", ".tiff"
    raise ValidationError(
        "Unsupported file type. Upload a PDF, PNG, JPEG, GIF or TIFF.",
        code="unsupported_file_type",
    )


def store_document(
    db: Session,
    company_id: str,
    *,
    content: bytes,
    filename: str,
    kind: str = DocumentKind.OTHER,
    user_id: str | None = None,
    party_type: str | None = None,
    party_id: str | None = None,
) -> StoredDocument:
    """Persist an uploaded file, rejecting duplicates by content hash."""
    if not content:
        raise ValidationError("The uploaded file is empty.")
    if len(content) > settings.MAX_UPLOAD_BYTES:
        raise ValidationError(
            f"The file is {len(content) // 1024 // 1024} MB, above the "
            f"{settings.MAX_UPLOAD_BYTES // 1024 // 1024} MB limit.",
            code="file_too_large",
        )

    mime, extension = detect_mime(content)
    content_hash = hashlib.sha256(content).hexdigest()

    existing = db.execute(
        select(StoredDocument).where(
            StoredDocument.company_id == company_id,
            StoredDocument.content_hash == content_hash,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise DuplicateDocumentError(
            f"This exact file was already uploaded as '{existing.filename}' on "
            f"{existing.created_at.date()}.",
            details={
                "document_id": existing.id,
                "filename": existing.filename,
                "uploaded_on": existing.created_at.date().isoformat(),
                "linked_type": existing.linked_type,
                "linked_id": existing.linked_id,
            },
        )

    # Never trust the client-supplied filename for the path on disk.
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename).name)[:120] or "document"
    company_dir = settings.storage_path / company_id
    company_dir.mkdir(parents=True, exist_ok=True)
    stored_path = company_dir / f"{content_hash[:16]}_{safe_name}"
    if not stored_path.suffix:
        stored_path = stored_path.with_suffix(extension)
    stored_path.write_bytes(content)

    document = StoredDocument(
        company_id=company_id,
        filename=safe_name,
        stored_path=str(stored_path),
        mime_type=mime,
        size_bytes=len(content),
        content_hash=content_hash,
        kind=kind,
        extraction_status=ExtractionStatus.UPLOADED,
        uploaded_by_user_id=user_id,
        party_type=party_type,
        party_id=party_id,
    )
    db.add(document)
    db.flush()

    from app.services.audit import record_event

    record_event(
        db,
        company_id=company_id,
        action=AuditAction.CREATE,
        entity_type="Document",
        entity_id=document.id,
        entity_label=safe_name,
        summary=f"Uploaded document '{safe_name}' ({len(content)} bytes, {mime})",
        actor_type=ActorType.USER if user_id else ActorType.SYSTEM,
        actor_id=user_id,
        after={"filename": safe_name, "mime": mime, "hash": content_hash[:16]},
    )
    return document


@dataclass
class ExtractionOutput:
    text: str = ""
    method: str = ""
    page_count: int = 0
    errors: list[str] = field(default_factory=list)


def extract_text(path: str, mime: str) -> ExtractionOutput:
    """Pull text out of a document, degrading gracefully across backends."""
    output = ExtractionOutput()
    file_path = Path(path)
    if not file_path.exists():
        output.errors.append("The stored file is missing from disk.")
        return output

    if mime == "application/pdf":
        # 1. PyMuPDF — fastest and handles most digital PDFs.
        try:
            import fitz  # type: ignore

            with fitz.open(path) as pdf:
                pages = [page.get_text() for page in pdf]
                output.page_count = len(pages)
            text = "\n".join(pages).strip()
            if text:
                output.text = text[:MAX_TEXT_CHARS]
                output.method = "PDF_TEXT"
                return output
        except ImportError:
            output.errors.append("PyMuPDF is not installed.")
        except Exception as exc:  # noqa: BLE001
            output.errors.append(f"PyMuPDF could not read the file: {exc}")

        # 2. pdfplumber — better on some table-heavy layouts.
        try:
            import pdfplumber  # type: ignore

            with pdfplumber.open(path) as pdf:
                pages = [page.extract_text() or "" for page in pdf.pages]
                output.page_count = output.page_count or len(pages)
            text = "\n".join(pages).strip()
            if text:
                output.text = text[:MAX_TEXT_CHARS]
                output.method = "PDF_TEXT"
                return output
        except ImportError:
            output.errors.append("pdfplumber is not installed.")
        except Exception as exc:  # noqa: BLE001
            output.errors.append(f"pdfplumber could not read the file: {exc}")

        output.errors.append(
            "No text layer was found — this looks like a scanned PDF. OCR is needed."
        )
        return output

    # Images -> OCR
    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore

        with Image.open(path) as image:
            text = pytesseract.image_to_string(image)
        output.text = (text or "").strip()[:MAX_TEXT_CHARS]
        output.method = "OCR"
        output.page_count = 1
        if not output.text:
            output.errors.append("OCR ran but found no readable text.")
        return output
    except ImportError:
        output.errors.append(
            "OCR is unavailable: install pytesseract and the Tesseract binary to read images."
        )
    except Exception as exc:  # noqa: BLE001
        output.errors.append(f"OCR failed: {exc}")

    return output


# --------------------------------------------------------- field parsing ---

# Deterministic patterns. These run before any model, and their results are
# what the model is asked to *confirm* rather than invent.
FIELD_PATTERNS: dict[str, list[str]] = {
    "invoice_number": [
        r"(?:tax\s+)?invoice\s*(?:no\.?|number|#)\s*[:\-]?\s*([A-Z0-9][A-Z0-9/\-_]{2,30})",
        r"\bbill\s*(?:no\.?|number|#)\s*[:\-]?\s*([A-Z0-9][A-Z0-9/\-_]{2,30})",
        r"\binv\s*[:\-#]\s*([A-Z0-9][A-Z0-9/\-_]{2,30})",
    ],
    "invoice_date": [
        r"(?:invoice|bill|document)\s*date\s*[:\-]?\s*(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}|\d{4}-\d{2}-\d{2}|\d{1,2}\s+\w{3,9}\s+\d{4})",
        r"\bdated?\s*[:\-]?\s*(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}|\d{4}-\d{2}-\d{2})",
    ],
    "due_date": [
        r"due\s*date\s*[:\-]?\s*(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}|\d{4}-\d{2}-\d{2})",
        r"payment\s*due\s*[:\-]?\s*(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4})",
    ],
    "gstin": [r"\b(\d{2}[A-Z]{5}\d{4}[A-Z]\d[Z][A-Z\d])\b"],
    "pan": [r"\bPAN\s*[:\-]?\s*([A-Z]{5}\d{4}[A-Z])\b"],
    "total": [
        r"(?:grand\s+total|total\s+amount(?:\s+payable)?|net\s+payable|amount\s+payable|invoice\s+total)\s*[:\-]?\s*(?:INR|Rs\.?|₹)?\s*([\d,]+\.?\d{0,2})",
        r"\btotal\s*[:\-]?\s*(?:INR|Rs\.?|₹)\s*([\d,]+\.?\d{0,2})",
    ],
    "taxable_value": [
        r"(?:taxable\s+(?:value|amount)|sub\s*-?\s*total|subtotal)\s*[:\-]?\s*(?:INR|Rs\.?|₹)?\s*([\d,]+\.?\d{0,2})"
    ],
    "cgst": [r"\bCGST\b.{0,30}?([\d,]+\.\d{2})"],
    "sgst": [r"\bSGST\b.{0,30}?([\d,]+\.\d{2})"],
    "igst": [r"\bIGST\b.{0,30}?([\d,]+\.\d{2})"],
    "hsn_sac": [r"\b(?:HSN|SAC)\s*(?:code)?\s*[:\-]?\s*(\d{4,8})\b"],
}


def parse_fields(text: str) -> tuple[dict[str, Any], dict[str, float]]:
    """Regex-extract known invoice fields. Missing fields stay missing."""
    fields: dict[str, Any] = {}
    confidence: dict[str, float] = {}
    if not text:
        return fields, confidence

    for name, patterns in FIELD_PATTERNS.items():
        for index, pattern in enumerate(patterns):
            match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if not match:
                continue
            value = match.group(1).strip()
            if name in ("total", "taxable_value", "cgst", "sgst", "igst"):
                try:
                    value = str(money(value.replace(",", "")))
                except Exception:  # noqa: BLE001
                    continue
            fields[name] = value
            # Earlier (more specific) patterns are trusted more.
            confidence[name] = round(0.9 - index * 0.12, 2)
            break

    # All GSTINs found — the supplier's and the recipient's.
    gstins = re.findall(FIELD_PATTERNS["gstin"][0], text)
    if len(gstins) > 1:
        fields["all_gstins"] = list(dict.fromkeys(gstins))

    return fields, confidence


def parse_date_value(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not value:
        return None
    from datetime import datetime

    text = str(value).strip()
    for fmt in (
        "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y",
        "%m/%d/%Y", "%d %b %Y", "%d %B %Y", "%d-%b-%Y",
    ):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def process_document(
    db: Session, company_id: str, document_id: str, *, user_id: str | None = None
) -> StoredDocument:
    """Extract text and fields. Never fabricates a missing value."""
    document = db.get(StoredDocument, document_id)
    if document is None or document.company_id != company_id:
        raise NotFoundError("Document not found.")

    document.extraction_status = ExtractionStatus.PROCESSING
    db.flush()

    extraction = extract_text(document.stored_path, document.mime_type)
    document.extracted_text = extraction.text
    document.extraction_method = extraction.method
    document.page_count = extraction.page_count
    document.extraction_errors = extraction.errors

    if not extraction.text:
        document.extraction_status = ExtractionStatus.NEEDS_REVIEW
        document.needs_review_reasons = [
            "No text could be extracted from this document.",
            *extraction.errors,
        ]
        document.overall_confidence = ZERO
        document.processed_at = utcnow()
        db.flush()
        return document

    fields, confidence = parse_fields(extraction.text)
    document.extracted_fields = fields
    document.field_confidence = confidence
    document.detected_kind = classify_document(extraction.text)
    if document.kind == DocumentKind.OTHER and document.detected_kind:
        document.kind = document.detected_kind

    # Confidence is the mean over the fields that actually matter.
    critical = ["invoice_number", "invoice_date", "total"]
    present = [name for name in critical if name in fields]
    missing = [name for name in critical if name not in fields]

    overall = (
        D(str(sum(confidence.get(name, 0) for name in present) / len(critical)))
        if present
        else ZERO
    )
    document.overall_confidence = overall

    reasons: list[str] = []
    if missing:
        reasons.append(
            "Could not find: " + ", ".join(name.replace("_", " ") for name in missing) + "."
        )
    if overall < D("0.7"):
        reasons.append("Extraction confidence is below the review threshold.")

    # Arithmetic cross-check: does taxable + tax equal the stated total?
    if all(name in fields for name in ("taxable_value", "total")):
        taxable = money(fields["taxable_value"])
        total = money(fields["total"])
        tax = money(
            sum(money(fields.get(component, 0)) for component in ("cgst", "sgst", "igst"))
        )
        if tax > ZERO:
            expected = money(taxable + tax)
            if abs(expected - total) > money("1.00"):
                reasons.append(
                    f"Arithmetic check failed: taxable {taxable} + tax {tax} = {expected}, "
                    f"but the document states {total}."
                )

    document.needs_review_reasons = reasons
    document.extraction_status = (
        ExtractionStatus.NEEDS_REVIEW if reasons else ExtractionStatus.EXTRACTED
    )
    document.processed_at = utcnow()
    db.flush()
    return document


def classify_document(text: str) -> str:
    """Classify by the vocabulary actually present in the document."""
    lowered = (text or "").lower()
    scores = {
        DocumentKind.INVOICE: sum(
            lowered.count(word) for word in ("tax invoice", "invoice no", "bill to", "gstin")
        ),
        DocumentKind.RECEIPT: sum(
            lowered.count(word) for word in ("receipt", "paid", "payment received", "thank you")
        ),
        DocumentKind.BANK_STATEMENT: sum(
            lowered.count(word)
            for word in ("statement of account", "closing balance", "withdrawal", "deposit", "ifsc")
        ),
        DocumentKind.PAYSLIP: sum(
            lowered.count(word) for word in ("payslip", "salary slip", "net pay", "basic pay")
        ),
        DocumentKind.CONTRACT: sum(
            lowered.count(word) for word in ("agreement", "hereby", "party of the first part")
        ),
    }
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else DocumentKind.OTHER


def link_document(
    db: Session,
    company_id: str,
    document_id: str,
    *,
    linked_type: str,
    linked_id: str,
    user_id: str | None = None,
) -> StoredDocument:
    document = db.get(StoredDocument, document_id)
    if document is None or document.company_id != company_id:
        raise NotFoundError("Document not found.")
    document.linked_type = linked_type
    document.linked_id = linked_id
    document.extraction_status = ExtractionStatus.APPROVED
    document.reviewed_by_user_id = user_id
    db.flush()
    return document
