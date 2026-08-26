"""Accounts receivable agent.

Prioritises collections using each customer's *measured* payment behaviour,
drafts reminder messages for human approval, and estimates expected receipts.
Drafts are never sent automatically.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from app.ai.agents.base import AgentResult, BaseAgent
from app.enums import (
    AgentKind,
    AgentRunStatus,
    DocumentStatus,
    ExceptionCategory,
    ProposalType,
    Severity,
)
from app.models import Customer, Invoice, Receipt, ReceiptAllocation
from app.money import ZERO, D, money
from app.services.policy import get_policy
from app.services.reports import ar_aging


class ARAgent(BaseAgent):
    kind = AgentKind.AR
    display_name = "Accounts Receivable Agent"
    role_description = (
        "You prioritise collections and draft polite, factual payment reminders. "
        "Only reference invoice numbers and amounts supplied in the context. "
        "Drafts are for human review — you never send anything."
    )

    async def run_agent(self, **params: Any) -> AgentResult:
        today = date.today()
        policy = get_policy(self.db, self.company_id, "ar.collections")
        escalate_after = int(policy.get("escalate_after_days", 45))
        high_risk_days = int(policy.get("high_risk_days", 60))
        min_amount = money(policy.get("min_amount_to_chase", 1000))
        draft_limit = int(params.get("draft_limit", 10))

        aging = ar_aging(self.db, self.company_id, as_of=today)
        rows = [row for row in aging["rows"] if row["total"] >= min_amount]

        result = AgentResult(agent_kind=self.kind, items_processed=len(rows))
        if not rows:
            result.summary = "No receivables are outstanding above the follow-up threshold."
            result.metrics = {"total_receivable": str(aging["total_outstanding"])}
            return result

        prioritised: list[dict] = []
        for row in rows:
            behaviour = self._payment_behaviour(row["customer_id"])
            oldest = row["oldest_days"]

            risk_score = 0
            risk_reasons: list[str] = []

            if oldest > high_risk_days:
                risk_score += 40
                risk_reasons.append(f"oldest invoice is {oldest} days overdue")
            elif oldest > escalate_after:
                risk_score += 25
                risk_reasons.append(f"oldest invoice is {oldest} days overdue")
            elif oldest > 0:
                risk_score += 10
                risk_reasons.append(f"{oldest} days overdue")

            if behaviour["invoices_paid"] >= 3:
                if behaviour["average_days_to_pay"] > 45:
                    risk_score += 20
                    risk_reasons.append(
                        f"historically pays in {behaviour['average_days_to_pay']} days on average"
                    )
                elif behaviour["average_days_to_pay"] <= 15:
                    risk_score -= 10
                    risk_reasons.append(
                        f"usually pays quickly ({behaviour['average_days_to_pay']} days on average)"
                    )
            else:
                risk_reasons.append("limited payment history")

            if row["total"] >= money(500000):
                risk_score += 15
                risk_reasons.append("large balance")

            risk = "HIGH" if risk_score >= 50 else "MEDIUM" if risk_score >= 25 else "LOW"
            expected_days = (
                behaviour["average_days_to_pay"] if behaviour["invoices_paid"] >= 3 else 30
            )

            prioritised.append(
                {
                    "customer_id": row["customer_id"],
                    "name": row["name"],
                    "email": row["email"],
                    "phone": row["phone"],
                    "total_due": str(row["total"]),
                    "oldest_days": oldest,
                    "buckets": {k: str(v) for k, v in row["buckets"].items()},
                    "invoice_count": len(row["documents"]),
                    "risk": risk,
                    "risk_score": risk_score,
                    "risk_reasons": risk_reasons,
                    "payment_behaviour": behaviour,
                    "expected_within_days": expected_days,
                    "invoices": [
                        {
                            "id": doc["id"],
                            "number": doc["number"],
                            "due_date": doc["due_date"].isoformat(),
                            "balance": str(doc["balance"]),
                            "days_overdue": doc["days_overdue"],
                        }
                        for doc in sorted(
                            row["documents"], key=lambda d: d["days_overdue"], reverse=True
                        )[:5]
                    ],
                }
            )

        prioritised.sort(key=lambda r: (-r["risk_score"], -float(r["total_due"])))

        # Expected collections over the next 30 days, weighted by behaviour.
        expected_30 = ZERO
        for entry in prioritised:
            if entry["expected_within_days"] <= 30:
                weight = D("0.9") if entry["risk"] == "LOW" else D("0.6") if entry["risk"] == "MEDIUM" else D("0.3")
                expected_30 = money(expected_30 + money(D(entry["total_due"]) * weight))

        # Draft reminders for the highest-risk accounts.
        drafts: list[dict] = []
        for entry in prioritised[:draft_limit]:
            if entry["oldest_days"] <= 0:
                continue
            draft = await self._draft_reminder(entry)
            if draft:
                drafts.append(draft)
                result.used_llm = True

        high_risk = [entry for entry in prioritised if entry["risk"] == "HIGH"]

        proposal = self.create_proposal(
            proposal_type=ProposalType.COLLECTION_ACTION,
            title=f"Collection plan: {len(prioritised)} customer(s), {aging['total_outstanding']} outstanding",
            summary=(
                f"{len(high_risk)} high-risk account(s). Expected collections over the next "
                f"30 days: {expected_30}."
            ),
            reasoning=(
                "Ranked by how overdue each balance is, the customer's measured average "
                "days-to-pay, and the size of the balance. Reminder drafts are included "
                "for review — nothing has been sent."
            ),
            payload={
                "as_of": today.isoformat(),
                "total_outstanding": str(aging["total_outstanding"]),
                "total_overdue": str(aging["total_overdue"]),
                "expected_30_days": str(expected_30),
                "customers": prioritised[:30],
                "reminder_drafts": drafts,
            },
            confidence=D("0.9"),
            financial_impact=aging["total_overdue"],
            severity=Severity.HIGH if high_risk else Severity.INFO,
            evidence=[
                {
                    "type": "customer",
                    "id": entry["customer_id"],
                    "label": entry["name"],
                    "amount": entry["total_due"],
                    "oldest_days": entry["oldest_days"],
                }
                for entry in prioritised[:15]
            ],
            source_records=[
                {"type": "customer", "id": entry["customer_id"]} for entry in prioritised
            ],
        )
        result.proposals.append(proposal.id)

        for entry in high_risk[:10]:
            self.raise_exception_item(
                category=ExceptionCategory.AR,
                severity=Severity.HIGH,
                title=f"{entry['name']} owes {entry['total_due']} — collection risk",
                reason=(
                    f"{entry['name']} has {entry['total_due']} outstanding across "
                    f"{entry['invoice_count']} invoice(s); " + ", ".join(entry["risk_reasons"]) + "."
                ),
                recommended_action=(
                    "Contact the customer, agree a payment date, and escalate if no "
                    "commitment is obtained."
                ),
                dedupe_key=f"ar_risk:{entry['customer_id']}:{today.replace(day=1)}",
                amount=money(D(entry["total_due"])),
                entity_type="Customer",
                entity_id=entry["customer_id"],
                action_url=f"/sales/customers/{entry['customer_id']}",
            )

        result.summary = (
            f"{aging['total_outstanding']} is outstanding from {len(prioritised)} customer(s), "
            f"of which {aging['total_overdue']} is overdue. {len(high_risk)} account(s) are "
            f"high risk. Expected collections in 30 days: {expected_30}. "
            f"{len(drafts)} reminder(s) drafted for approval."
        )
        result.metrics = {
            "total_receivable": str(aging["total_outstanding"]),
            "total_overdue": str(aging["total_overdue"]),
            "high_risk_customers": len(high_risk),
            "expected_30_days": str(expected_30),
            "drafts": len(drafts),
        }
        result.needs_review_count = len(drafts)
        result.findings = [
            {
                "label": "High-risk accounts",
                "count": len(high_risk),
                "detail": "Overdue balances with a poor or unknown payment record.",
            }
        ]
        result.status = AgentRunStatus.NEEDS_REVIEW if drafts else AgentRunStatus.COMPLETED
        return result

    # ---------------------------------------------------------- internals --

    def _payment_behaviour(self, customer_id: str) -> dict:
        """Measured days-to-pay from this customer's settled invoices."""
        rows = self.db.execute(
            select(Invoice, Receipt.receipt_date)
            .join(ReceiptAllocation, ReceiptAllocation.invoice_id == Invoice.id)
            .join(Receipt, Receipt.id == ReceiptAllocation.receipt_id)
            .where(
                Invoice.company_id == self.company_id,
                Invoice.customer_id == customer_id,
                Invoice.status == DocumentStatus.PAID,
            )
            .limit(50)
        ).all()

        if not rows:
            return {
                "invoices_paid": 0,
                "average_days_to_pay": 0,
                "on_time_rate": None,
                "note": "No settled invoices yet, so no payment pattern can be measured.",
            }

        gaps = []
        on_time = 0
        for invoice, receipt_date in rows:
            gap = (receipt_date - invoice.doc_date).days
            gaps.append(max(0, gap))
            if receipt_date <= invoice.due_date:
                on_time += 1

        average = int(sum(gaps) / len(gaps))
        return {
            "invoices_paid": len(gaps),
            "average_days_to_pay": average,
            "on_time_rate": round(on_time / len(gaps), 2),
            "note": f"Based on {len(gaps)} settled invoice(s).",
        }

    async def _draft_reminder(self, entry: dict) -> dict | None:
        parsed, meta = await self.ask_json(
            task="summarise",
            instruction=(
                "Draft a short, polite payment reminder email. Use ONLY the invoice "
                "numbers and amounts in the context. Do not invent bank details, "
                "threats or legal language. Reply "
                '{"subject": "...", "body": "...", "tone": "friendly|firm"}.'
            ),
            context={
                "customer_name": entry["name"],
                "total_due": entry["total_due"],
                "oldest_days": entry["oldest_days"],
                "invoices": entry["invoices"],
                "facts": {
                    "total_due": entry["total_due"],
                    "invoice_count": entry["invoice_count"],
                    "oldest_days_overdue": entry["oldest_days"],
                },
            },
        )

        tone = "firm" if entry["oldest_days"] > 45 else "friendly"
        if parsed and parsed.get("body"):
            return {
                "customer_id": entry["customer_id"],
                "customer_name": entry["name"],
                "email": entry["email"],
                "subject": str(parsed.get("subject", f"Payment reminder — {entry['total_due']} outstanding"))[:200],
                "body": str(parsed["body"])[:2000],
                "tone": str(parsed.get("tone", tone)),
                "generated_by": meta.get("provider", "mock"),
                "status": "DRAFT — requires human approval before sending",
            }

        # Deterministic fallback so the feature works with no model at all.
        invoice_lines = "\n".join(
            f"  • {inv['number']} — {inv['balance']} (due {inv['due_date']}, "
            f"{inv['days_overdue']} days overdue)"
            for inv in entry["invoices"]
        )
        return {
            "customer_id": entry["customer_id"],
            "customer_name": entry["name"],
            "email": entry["email"],
            "subject": f"Payment reminder — {entry['total_due']} outstanding",
            "body": (
                f"Dear {entry['name']},\n\n"
                f"Our records show {entry['total_due']} outstanding across "
                f"{entry['invoice_count']} invoice(s):\n\n{invoice_lines}\n\n"
                f"We would appreciate an update on the expected payment date. "
                f"If any of these have already been settled, please share the payment "
                f"reference so we can reconcile our records.\n\n"
                f"Thank you."
            ),
            "tone": tone,
            "generated_by": "deterministic template",
            "status": "DRAFT — requires human approval before sending",
        }
