"""Accounts payable agent.

Prepares the payment run: which bills are due, which are duplicates, which are
blocked, and in what order they should be paid. It **never** releases a
payment — it produces a prioritised schedule that a human authorises.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.ai.agents.base import AgentResult, BaseAgent
from app.enums import (
    AgentKind,
    AgentRunStatus,
    DocumentStatus,
    ExceptionCategory,
    ProposalType,
    Severity,
)
from app.models import Bill, Vendor
from app.money import ZERO, D, money
from app.services.forecast import current_cash
from app.services.policy import get_policy
from app.services.purchases import check_duplicate_bill


class APAgent(BaseAgent):
    kind = AgentKind.AP
    display_name = "Accounts Payable Agent"
    role_description = (
        "You prepare the vendor payment run: identify what is due, spot duplicates "
        "and rank payments by urgency. You never authorise or release a payment."
    )

    async def run_agent(self, **params: Any) -> AgentResult:
        horizon_days = int(params.get("horizon_days", 30))
        today = date.today()
        horizon = today + timedelta(days=horizon_days)
        policy = get_policy(self.db, self.company_id, "ap.payments")
        warn_days = int(policy.get("warn_days_before_due", 5))
        msme_days = int(policy.get("msme_payment_days", 45))

        bills = [
            bill
            for bill in self.db.execute(
                select(Bill)
                .where(
                    Bill.company_id == self.company_id,
                    Bill.status.in_(
                        [
                            DocumentStatus.APPROVED,
                            DocumentStatus.PARTIALLY_PAID,
                            DocumentStatus.OVERDUE,
                        ]
                    ),
                    Bill.due_date <= horizon,
                )
                .order_by(Bill.due_date.asc())
            ).scalars()
            if bill.balance_due > ZERO
        ]

        result = AgentResult(agent_kind=self.kind, items_processed=len(bills))
        if not bills:
            result.summary = f"No bills fall due in the next {horizon_days} days."
            return result

        cash = current_cash(self.db, self.company_id)
        schedule: list[dict] = []
        duplicates_found = 0
        blocked = 0
        total_due = ZERO

        for bill in bills:
            vendor = self.db.get(Vendor, bill.vendor_id)
            days_until_due = (bill.due_date - today).days
            balance = bill.balance_due
            total_due = money(total_due + balance)

            reasons: list[str] = []
            urgency = 50

            if days_until_due < 0:
                urgency += min(40, abs(days_until_due))
                reasons.append(f"overdue by {abs(days_until_due)} day(s)")
            elif days_until_due <= warn_days:
                urgency += 20
                reasons.append(f"due in {days_until_due} day(s)")
            else:
                reasons.append(f"due in {days_until_due} day(s)")

            if vendor and vendor.is_msme:
                urgency += 25
                reasons.append(
                    f"MSME vendor — statutory expectation is payment within {msme_days} days"
                )

            if balance >= money(100000):
                urgency += 10
                reasons.append("large amount")

            # Duplicate check before anything is scheduled for payment.
            duplicate = check_duplicate_bill(
                self.db,
                self.company_id,
                vendor_id=bill.vendor_id,
                vendor_invoice_number=bill.vendor_invoice_number,
                amount=bill.total,
                doc_date=bill.doc_date,
                exclude_bill_id=bill.id,
            )
            is_blocked = False
            block_reason = ""

            if duplicate["is_duplicate"] or duplicate["similar"]:
                duplicates_found += 1
                is_blocked = bool(policy.get("block_payment_on_duplicate", True))
                block_reason = duplicate["reason"]
                self.raise_exception_item(
                    category=ExceptionCategory.AP,
                    severity=Severity.HIGH if duplicate["is_duplicate"] else Severity.MEDIUM,
                    title=f"Possible duplicate: bill {bill.number} from {vendor.name if vendor else ''}",
                    reason=duplicate["reason"],
                    recommended_action=(
                        "Compare the source documents before paying. If it is a duplicate, "
                        "cancel this bill and reverse its journal."
                    ),
                    dedupe_key=f"ap_duplicate:{bill.id}",
                    amount=bill.total,
                    entity_type="Bill",
                    entity_id=bill.id,
                    evidence=[
                        {
                            "type": "bill",
                            "id": bill.id,
                            "label": f"{bill.number} ({bill.vendor_invoice_number})",
                            "amount": str(bill.total),
                        }
                    ]
                    + [
                        {
                            "type": "bill",
                            "id": match.get("id"),
                            "label": f"{match.get('number')} ({match.get('vendor_invoice_number')})",
                            "amount": str(match.get("total")),
                            "date": str(match.get("date")),
                        }
                        for match in ([duplicate["exact_match"]] if duplicate["exact_match"] else duplicate["similar"])
                    ],
                )

            # Bank-detail change hold.
            if vendor and vendor.bank_details_updated_at:
                hold_days = int(policy.get("block_payment_on_bank_change_days", 7))
                days_since = (today - vendor.bank_details_updated_at).days
                if 0 <= days_since < hold_days:
                    is_blocked = True
                    block_reason = (
                        f"{vendor.name}'s bank details changed {days_since} day(s) ago; "
                        f"payments are held for {hold_days} days."
                    )

            if is_blocked:
                blocked += 1

            schedule.append(
                {
                    "bill_id": bill.id,
                    "bill_number": bill.number,
                    "vendor_invoice_number": bill.vendor_invoice_number,
                    "vendor_id": bill.vendor_id,
                    "vendor_name": vendor.name if vendor else "",
                    "due_date": bill.due_date.isoformat(),
                    "days_until_due": days_until_due,
                    "amount": str(balance),
                    "urgency_score": urgency,
                    "reasons": reasons,
                    "blocked": is_blocked,
                    "block_reason": block_reason,
                    "is_msme": bool(vendor and vendor.is_msme),
                }
            )

        schedule.sort(key=lambda row: (-row["urgency_score"], row["due_date"]))
        payable_now = money(
            sum((D(row["amount"]) for row in schedule if not row["blocked"]), ZERO)
        )
        shortfall = money(payable_now - cash) if payable_now > cash else ZERO

        proposal = self.create_proposal(
            proposal_type=ProposalType.PAYMENT_PRIORITY,
            title=f"Payment run: {len(schedule)} bill(s) totalling {total_due}",
            summary=(
                f"{len(schedule)} bill(s) due within {horizon_days} days. "
                f"{blocked} are blocked pending review."
            ),
            reasoning=(
                "Ranked by how overdue each bill is, MSME status and amount. "
                "Blocked bills are excluded from the payable total. "
                "This is a schedule for approval — no payment has been made."
            ),
            payload={
                "horizon_days": horizon_days,
                "schedule": schedule,
                "total_due": str(total_due),
                "payable_now": str(payable_now),
                "blocked_count": blocked,
                "available_cash": str(cash),
                "shortfall": str(shortfall),
            },
            confidence=D("0.95"),  # deterministic ranking, not a model guess
            financial_impact=total_due,
            severity=Severity.HIGH if shortfall > ZERO else Severity.INFO,
            evidence=[
                {
                    "type": "bill",
                    "id": row["bill_id"],
                    "label": f"{row['bill_number']} — {row['vendor_name']}",
                    "amount": row["amount"],
                    "due_date": row["due_date"],
                }
                for row in schedule[:15]
            ],
            source_records=[{"type": "bill", "id": row["bill_id"]} for row in schedule],
        )
        result.proposals.append(proposal.id)

        if shortfall > ZERO:
            self.raise_exception_item(
                category=ExceptionCategory.AP,
                severity=Severity.HIGH,
                title=f"Cash shortfall of {shortfall} against upcoming payments",
                reason=(
                    f"{payable_now} is payable within {horizon_days} days but only {cash} "
                    f"is available in cash and bank."
                ),
                recommended_action=(
                    "Accelerate collections, or agree revised payment dates with vendors."
                ),
                dedupe_key=f"ap_shortfall:{today}",
                amount=shortfall,
            )

        overdue_count = sum(1 for row in schedule if row["days_until_due"] < 0)
        result.summary = (
            f"{len(schedule)} bill(s) totalling {total_due} fall due within {horizon_days} days "
            f"({overdue_count} already overdue). {duplicates_found} possible duplicate(s) found; "
            f"{blocked} payment(s) blocked pending review. "
            f"Payable now {payable_now} against {cash} available."
        )
        result.metrics = {
            "bills": len(schedule),
            "total_due": str(total_due),
            "payable_now": str(payable_now),
            "available_cash": str(cash),
            "shortfall": str(shortfall),
            "duplicates": duplicates_found,
            "blocked": blocked,
            "overdue": overdue_count,
        }
        result.needs_review_count = blocked + duplicates_found
        result.high_confidence_count = len(schedule) - blocked
        result.findings = [
            {"label": "Overdue bills", "count": overdue_count, "detail": "Past their due date."},
            {"label": "Possible duplicates", "count": duplicates_found, "detail": "Blocked from payment."},
        ]
        result.status = (
            AgentRunStatus.NEEDS_REVIEW if (blocked or duplicates_found) else AgentRunStatus.COMPLETED
        )
        return result
