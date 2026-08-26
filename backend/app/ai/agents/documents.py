"""Document processing agent and the inventory agent.

The document agent turns an uploaded invoice into a *proposed bill*. It:

1. Extracts fields deterministically (regex over the document text).
2. Asks the model only to fill gaps the patterns missed — from the document
   text alone, in a sanitised fence.
3. Matches the vendor against real vendor records by GSTIN then by name.
4. Recomputes every total with the tax engine rather than trusting the document.
5. Checks for duplicates before proposing anything.

If a required field cannot be found, it is left empty and the document is
marked NEEDS_REVIEW. It is never guessed.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select

from app.ai.agents.base import AgentResult, BaseAgent
from app.enums import (
    AgentKind,
    AgentRunStatus,
    ExceptionCategory,
    ExtractionStatus,
    ProposalType,
    Severity,
)
from app.models import Item, StoredDocument, Vendor
from app.money import ZERO, D, money, quantity as to_qty
from app.services.documents import parse_date_value, process_document
from app.services.inventory import movement_analysis, stock_summary
from app.services.purchases import check_duplicate_bill

try:
    from rapidfuzz import fuzz

    def _similarity(a: str, b: str) -> float:
        return float(fuzz.token_set_ratio(a, b))

except ImportError:  # pragma: no cover
    from difflib import SequenceMatcher

    def _similarity(a: str, b: str) -> float:
        return SequenceMatcher(None, a, b).ratio() * 100


class DocumentAgent(BaseAgent):
    kind = AgentKind.DOCUMENT
    display_name = "Document Processing Agent"
    role_description = (
        "You read invoice text and report the fields you can actually find. "
        "If a field is not present in the text, leave it null — never guess a "
        "number, date or invoice reference."
    )

    async def run_agent(self, **params: Any) -> AgentResult:
        document_id = params.get("document_id")
        limit = int(params.get("limit", 10))

        stmt = select(StoredDocument).where(StoredDocument.company_id == self.company_id)
        if document_id:
            stmt = stmt.where(StoredDocument.id == document_id)
        else:
            stmt = stmt.where(
                StoredDocument.extraction_status.in_(
                    [ExtractionStatus.UPLOADED, ExtractionStatus.PROCESSING]
                )
            )

        documents = list(
            self.db.execute(stmt.order_by(StoredDocument.created_at.asc()).limit(limit)).scalars()
        )

        result = AgentResult(agent_kind=self.kind, items_processed=len(documents))
        if not documents:
            result.summary = "No documents are waiting to be processed."
            return result

        extracted = needs_review = proposed = 0

        for document in documents:
            process_document(self.db, self.company_id, document.id, user_id=self.user_id)

            if not document.extracted_text:
                needs_review += 1
                result.findings.append(
                    {
                        "label": document.filename,
                        "detail": "No readable text — this document needs manual entry.",
                        "document_id": document.id,
                    }
                )
                continue

            # Let the model fill only the gaps regex could not.
            fields = dict(document.extracted_fields or {})
            confidence = dict(document.field_confidence or {})
            missing = [
                name
                for name in ("invoice_number", "invoice_date", "total", "taxable_value")
                if name not in fields
            ]

            if missing:
                enriched, meta = await self._ask_for_fields(document, missing)
                result.used_llm = result.used_llm or bool(meta.get("used_llm"))
                if meta.get("injection_detected"):
                    self.raise_exception_item(
                        category=ExceptionCategory.ACCOUNTING,
                        severity=Severity.HIGH,
                        title=f"Suspicious instructions found in '{document.filename}'",
                        reason=(
                            "The uploaded document contains text that attempts to give the AI "
                            "instructions. It was treated as data and ignored, but the "
                            "document should be inspected."
                        ),
                        recommended_action="Review the document before accepting any data from it.",
                        dedupe_key=f"doc_injection:{document.id}",
                        entity_type="Document",
                        entity_id=document.id,
                    )
                for name, value in (enriched or {}).items():
                    if name in missing and value:
                        fields[name] = value
                        confidence[name] = 0.6  # model-derived: lower than a regex hit

                document.extracted_fields = fields
                document.field_confidence = confidence

            vendor, vendor_confidence, vendor_reason = self._match_vendor(fields, document)
            proposal = self._build_bill_proposal(document, fields, confidence, vendor, vendor_reason)

            if proposal is not None:
                result.proposals.append(proposal.id)
                proposed += 1

            if document.extraction_status == ExtractionStatus.NEEDS_REVIEW:
                needs_review += 1
            else:
                extracted += 1

        self.db.flush()

        result.summary = (
            f"Processed {len(documents)} document(s): {extracted} extracted cleanly, "
            f"{needs_review} need review, {proposed} bill proposal(s) created for approval."
        )
        result.metrics = {
            "documents": len(documents),
            "extracted": extracted,
            "needs_review": needs_review,
            "proposals": proposed,
        }
        result.needs_review_count = needs_review
        result.high_confidence_count = extracted
        result.status = AgentRunStatus.NEEDS_REVIEW if needs_review else AgentRunStatus.COMPLETED
        return result

    # ---------------------------------------------------------- internals --

    async def _ask_for_fields(
        self, document: StoredDocument, missing: list[str]
    ) -> tuple[dict, dict]:
        parsed, meta = await self.ask_json(
            task="extract_document",
            instruction=(
                f"Find these fields in the document text: {', '.join(missing)}. "
                'Reply {"fields": {"<name>": "<value or null>"}}. '
                "Use null for anything not clearly present. Do NOT infer, calculate "
                "or guess a value that is not written in the document."
            ),
            context={"requested_fields": missing, "document_text": document.extracted_text[:8000]},
            untrusted_text=document.extracted_text,
            untrusted_label=f"uploaded document '{document.filename}'",
        )
        if parsed is None:
            return {}, meta

        fields = parsed.get("fields") if isinstance(parsed.get("fields"), dict) else parsed
        cleaned: dict[str, Any] = {}
        for name in missing:
            value = (fields or {}).get(name)
            if value in (None, "", "null", "N/A", "unknown"):
                continue
            cleaned[name] = str(value).strip()
        return cleaned, meta

    def _match_vendor(
        self, fields: dict, document: StoredDocument
    ) -> tuple[Vendor | None, float, str]:
        """Match against real vendors: GSTIN first, then fuzzy name."""
        gstins = fields.get("all_gstins") or ([fields["gstin"]] if fields.get("gstin") else [])

        from app.models import Company

        company = self.db.get(Company, self.company_id)
        own_gstin = (company.gstin or "").upper() if company else ""

        for gstin in gstins:
            candidate = str(gstin).upper()
            if candidate == own_gstin:
                continue  # that is us, not the supplier
            vendor = self.db.execute(
                select(Vendor).where(
                    Vendor.company_id == self.company_id, func.upper(Vendor.gstin) == candidate
                )
            ).scalar_one_or_none()
            if vendor is not None:
                return vendor, 0.98, f"Matched vendor by GSTIN {candidate}."

        # Fall back to a name match against the first lines of the document.
        header = "\n".join((document.extracted_text or "").splitlines()[:15]).lower()
        vendors = list(
            self.db.execute(
                select(Vendor).where(
                    Vendor.company_id == self.company_id, Vendor.is_active.is_(True)
                )
            ).scalars()
        )
        best, best_score = None, 0.0
        for vendor in vendors:
            if vendor.name.lower() in header:
                score = 95.0
            else:
                score = _similarity(vendor.name.lower(), header)
            if score > best_score:
                best, best_score = vendor, score

        if best is not None and best_score >= 85:
            return best, round(best_score / 100, 2), (
                f"Matched vendor '{best.name}' by name (similarity {best_score:.0f}%)."
            )
        return None, 0.0, "No existing vendor matched this document."

    def _build_bill_proposal(
        self,
        document: StoredDocument,
        fields: dict,
        confidence: dict,
        vendor: Vendor | None,
        vendor_reason: str,
    ):
        """Create a bill proposal only when the essentials are present."""
        invoice_number = fields.get("invoice_number")
        total = fields.get("total")
        invoice_date = parse_date_value(fields.get("invoice_date"))

        blockers: list[str] = []
        if not invoice_number:
            blockers.append("the vendor's invoice number could not be found")
        if not total:
            blockers.append("the invoice total could not be found")
        if invoice_date is None:
            blockers.append("the invoice date could not be found or parsed")
        if vendor is None:
            blockers.append(vendor_reason)

        if blockers:
            document.extraction_status = ExtractionStatus.NEEDS_REVIEW
            document.needs_review_reasons = list(document.needs_review_reasons or []) + blockers
            self.db.flush()
            return None

        total_amount = money(total)
        taxable = money(fields.get("taxable_value", ZERO)) if fields.get("taxable_value") else ZERO
        tax_total = money(
            sum(money(fields.get(part, 0)) for part in ("cgst", "sgst", "igst"))
        )
        if taxable == ZERO:
            taxable = money(total_amount - tax_total) if tax_total > ZERO else total_amount

        duplicate = check_duplicate_bill(
            self.db,
            self.company_id,
            vendor_id=vendor.id,
            vendor_invoice_number=str(invoice_number),
            amount=total_amount,
            doc_date=invoice_date,
        )

        overall = document.overall_confidence or ZERO
        severity = Severity.HIGH if duplicate["is_duplicate"] else Severity.INFO

        if duplicate["is_duplicate"]:
            self.raise_exception_item(
                category=ExceptionCategory.AP,
                severity=Severity.HIGH,
                title=f"Uploaded invoice looks like a duplicate ({invoice_number})",
                reason=duplicate["reason"],
                recommended_action="Do not book this bill again. Verify against the existing one.",
                dedupe_key=f"doc_duplicate:{document.id}",
                amount=total_amount,
                entity_type="Document",
                entity_id=document.id,
            )

        proposal = self.create_proposal(
            proposal_type=ProposalType.BILL,
            title=f"Book bill {invoice_number} from {vendor.name} for {total_amount}",
            summary=(
                f"Extracted from '{document.filename}' with "
                f"{float(overall) * 100:.0f}% field confidence. {vendor_reason}"
            ),
            reasoning=(
                "Fields were read from the document text. Totals will be recomputed by the "
                "tax engine when the bill is created — the document's own arithmetic is not "
                "trusted."
                + (f" WARNING: {duplicate['reason']}" if duplicate["reason"] else "")
            ),
            payload={
                "document_id": document.id,
                "vendor_id": vendor.id,
                "vendor_name": vendor.name,
                "vendor_invoice_number": str(invoice_number),
                "doc_date": invoice_date.isoformat(),
                "due_date": (
                    parse_date_value(fields.get("due_date"))
                    or (invoice_date + timedelta(days=vendor.payment_terms_days or 30))
                ).isoformat(),
                "taxable_value": str(taxable),
                "tax_total": str(tax_total),
                "total": str(total_amount),
                "extracted_fields": fields,
                "field_confidence": confidence,
                "duplicate_check": {
                    "is_duplicate": duplicate["is_duplicate"],
                    "reason": duplicate["reason"],
                    "similar_count": len(duplicate["similar"]),
                },
                "lines": [
                    {
                        "description": f"As per invoice {invoice_number}",
                        "quantity": "1",
                        "rate": str(taxable),
                        "hsn_sac": fields.get("hsn_sac", ""),
                    }
                ],
            },
            confidence=overall,
            financial_impact=total_amount,
            severity=severity,
            target_type="Document",
            target_id=document.id,
            evidence=[
                {
                    "type": "document",
                    "id": document.id,
                    "label": document.filename,
                    "extraction_method": document.extraction_method,
                    "confidence": str(overall),
                },
                {"type": "vendor", "id": vendor.id, "label": vendor.name, "detail": vendor_reason},
            ]
            + [
                {
                    "type": "field",
                    "label": name.replace("_", " ").title(),
                    "value": str(value),
                    "confidence": str(confidence.get(name, "")),
                }
                for name, value in fields.items()
                if name != "all_gstins"
            ],
            source_records=[{"type": "document", "id": document.id}],
            validation_errors=[duplicate["reason"]] if duplicate["is_duplicate"] else [],
        )
        return proposal


class InventoryAgent(BaseAgent):
    kind = AgentKind.INVENTORY
    display_name = "Inventory Agent"
    role_description = (
        "You review stock levels and movement and recommend reorders or reviews. "
        "You cannot change stock — only recommend."
    )
    uses_llm = False

    async def run_agent(self, **params: Any) -> AgentResult:
        summary = stock_summary(self.db, self.company_id)
        analysis = movement_analysis(self.db, self.company_id)

        result = AgentResult(agent_kind=self.kind, items_processed=summary["tracked_items"])
        recommendations: list[dict] = []

        for item in summary["low_stock_items"]:
            suggested = (
                to_qty(item["reorder_quantity"])
                if D(item["reorder_quantity"]) > 0
                else to_qty(D(item["reorder_level"]) * 2)
            )
            recommendations.append(
                {
                    "action": "REORDER",
                    "item_id": item["id"],
                    "sku": item["sku"],
                    "name": item["name"],
                    "current_qty": str(item["current_qty"]),
                    "reorder_level": str(item["reorder_level"]),
                    "suggested_quantity": str(suggested),
                    "reason": (
                        f"Stock is {item['current_qty']} {item['uom']}, at or below the "
                        f"reorder level of {item['reorder_level']}."
                    ),
                }
            )

        for item in summary["out_of_stock_items"]:
            recommendations.append(
                {
                    "action": "URGENT_REORDER",
                    "item_id": item["id"],
                    "sku": item["sku"],
                    "name": item["name"],
                    "reason": "Out of stock — sales of this item cannot be fulfilled.",
                }
            )

        for item in analysis["dead_stock"]:
            recommendations.append(
                {
                    "action": "REVIEW_DEAD_STOCK",
                    "item_id": item["id"],
                    "sku": item["sku"],
                    "name": item["name"],
                    "stock_value": str(item["stock_value"]),
                    "reason": item.get("reason", "No recent movement."),
                }
            )

        if recommendations:
            proposal = self.create_proposal(
                proposal_type=ProposalType.INVENTORY_ACTION,
                title=f"{len(recommendations)} inventory action(s) recommended",
                summary=(
                    f"{summary['low_stock_count']} item(s) low, "
                    f"{summary['out_of_stock_count']} out of stock, "
                    f"{len(analysis['dead_stock'])} showing no movement."
                ),
                reasoning=(
                    "Derived from current stock levels against configured reorder points, "
                    "and from measured outward movement over the analysis window."
                ),
                payload={
                    "recommendations": recommendations,
                    "stock_value": str(summary["total_stock_value"]),
                    "dead_stock_value": str(analysis["dead_stock_value"]),
                },
                confidence=D("0.95"),
                financial_impact=analysis["dead_stock_value"],
                evidence=[
                    {
                        "type": "item",
                        "id": rec["item_id"],
                        "label": f"{rec['sku']} — {rec['name']}",
                        "detail": rec["reason"],
                    }
                    for rec in recommendations[:15]
                ],
            )
            result.proposals.append(proposal.id)

        if summary["out_of_stock_count"]:
            self.raise_exception_item(
                category=ExceptionCategory.INVENTORY,
                severity=Severity.HIGH,
                title=f"{summary['out_of_stock_count']} item(s) are out of stock",
                reason="These items cannot be sold or delivered until stock is received.",
                recommended_action="Raise purchase orders for the affected items.",
                dedupe_key=f"out_of_stock:{date.today()}",
                evidence=summary["out_of_stock_items"][:10],
            )

        result.summary = (
            f"Stock value {summary['total_stock_value']} across {summary['tracked_items']} "
            f"tracked item(s). {summary['low_stock_count']} low, "
            f"{summary['out_of_stock_count']} out of stock, "
            f"{len(analysis['dead_stock'])} dead stock worth {analysis['dead_stock_value']}."
        )
        result.metrics = {
            "stock_value": str(summary["total_stock_value"]),
            "tracked_items": summary["tracked_items"],
            "low_stock": summary["low_stock_count"],
            "out_of_stock": summary["out_of_stock_count"],
            "dead_stock_count": len(analysis["dead_stock"]),
            "dead_stock_value": str(analysis["dead_stock_value"]),
            "fast_moving_count": len(analysis["fast_moving"]),
        }
        result.findings = [
            {"label": rec["name"], "detail": rec["reason"], "action": rec["action"]}
            for rec in recommendations[:20]
        ]
        result.needs_review_count = len(recommendations)
        result.status = (
            AgentRunStatus.NEEDS_REVIEW if recommendations else AgentRunStatus.COMPLETED
        )
        return result
