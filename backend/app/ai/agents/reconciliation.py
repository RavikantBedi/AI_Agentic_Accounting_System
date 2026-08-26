"""Reconciliation agent.

The matching engine (``services.reconciliation``) does the work: eight scoring
strategies, evidence, and auto-confirmation only above the policy threshold.
This agent runs that engine, then asks the model to investigate *only* the
leftovers, choosing from candidates the engine already validated.

The model can never create a match: it names a candidate, and
``propose_ai_match`` re-verifies that candidate against the ledger before
recording a proposal that a human must still confirm.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.ai.agents.base import AgentResult, BaseAgent
from app.enums import (
    AgentKind,
    AgentRunStatus,
    ExceptionCategory,
    ReconStatus,
    Severity,
)
from app.models import BankAccount, BankTransaction
from app.money import ZERO, D, money
from app.services import reconciliation as recon_service


class ReconciliationAgent(BaseAgent):
    kind = AgentKind.RECONCILIATION
    display_name = "Reconciliation Agent"
    role_description = (
        "You investigate bank transactions the deterministic matcher could not "
        "resolve. You pick from the candidate ledger lines supplied — you never "
        "invent an id — and you explain the evidence for your choice."
    )

    async def run_agent(self, **params: Any) -> AgentResult:
        bank_account_id = params.get("bank_account_id")
        start_date = params.get("start_date")
        end_date = params.get("end_date")
        investigate = bool(params.get("investigate", True))

        accounts = self._bank_accounts(bank_account_id)
        if not accounts:
            return AgentResult(
                agent_kind=self.kind,
                summary="No active bank accounts are configured.",
            )

        result = AgentResult(agent_kind=self.kind)
        total_matched = total_auto = total_ambiguous = total_unmatched = 0
        sessions: list[dict] = []

        for account in accounts:
            session = recon_service.run_reconciliation(
                self.db,
                self.company_id,
                account.id,
                start_date=start_date,
                end_date=end_date,
                user_id=self.user_id,
            )
            total_matched += session.matched_count
            total_auto += session.auto_matched_count
            total_ambiguous += session.ambiguous_count
            total_unmatched += session.unmatched_count
            result.items_processed += session.total_transactions

            sessions.append(
                {
                    "bank_account": account.name,
                    "session_id": session.id,
                    "transactions": session.total_transactions,
                    "matched": session.matched_count,
                    "auto_matched": session.auto_matched_count,
                    "for_review": session.partial_count,
                    "ambiguous": session.ambiguous_count,
                    "unmatched": session.unmatched_count,
                    "book_balance": str(session.book_balance or ZERO),
                    "bank_balance": str(session.bank_balance) if session.bank_balance is not None else None,
                    "difference": str(session.difference) if session.difference is not None else None,
                }
            )

            if session.difference is not None and session.difference != ZERO:
                self.raise_exception_item(
                    category=ExceptionCategory.BANKING,
                    severity=Severity.HIGH if abs(session.difference) > money(10000) else Severity.MEDIUM,
                    title=f"{account.name} does not agree with the statement",
                    reason=(
                        f"The bank statement closing balance is {session.bank_balance} but the "
                        f"books show {session.book_balance} — a difference of {session.difference}."
                    ),
                    recommended_action=(
                        "Reconcile the outstanding items, or check for transactions the bank "
                        "has recorded that are not yet in the books."
                    ),
                    dedupe_key=f"bank_diff:{account.id}:{session.period_end}",
                    amount=abs(session.difference),
                    entity_type="BankAccount",
                    entity_id=account.id,
                    action_url=f"/banking/reconciliation?account={account.id}",
                )

        result.high_confidence_count = total_auto
        investigated = 0
        proposed = 0

        if investigate:
            investigated, proposed, used_llm = await self._investigate_leftovers(
                accounts, limit=int(params.get("investigate_limit", 15))
            )
            result.used_llm = used_llm
            result.proposals.extend([])  # proposals here are recon matches, tracked separately

        needs_attention = total_ambiguous + total_unmatched
        result.needs_review_count = needs_attention
        result.summary = (
            f"Reconciled {result.items_processed} transaction(s) across "
            f"{len(accounts)} bank account(s): {total_matched} matched "
            f"({total_auto} automatically at high confidence). "
            f"{needs_attention} still need a human"
            + (f", and I proposed matches for {proposed} of them." if proposed else ".")
        )
        result.metrics = {
            "sessions": sessions,
            "matched": total_matched,
            "auto_matched": total_auto,
            "ambiguous": total_ambiguous,
            "unmatched": total_unmatched,
            "ai_investigated": investigated,
            "ai_proposed": proposed,
        }
        result.findings = [
            {
                "label": "Unmatched transactions",
                "count": total_unmatched,
                "detail": "No ledger entry could be found for these bank lines.",
            },
            {
                "label": "Ambiguous matches",
                "count": total_ambiguous,
                "detail": "Several candidates scored too closely to choose between automatically.",
            },
        ]
        result.status = (
            AgentRunStatus.NEEDS_REVIEW if needs_attention else AgentRunStatus.COMPLETED
        )
        return result

    # ---------------------------------------------------------- internals --

    def _bank_accounts(self, bank_account_id: str | None) -> list[BankAccount]:
        stmt = select(BankAccount).where(
            BankAccount.company_id == self.company_id, BankAccount.is_active.is_(True)
        )
        if bank_account_id:
            stmt = stmt.where(BankAccount.id == bank_account_id)
        return list(self.db.execute(stmt).scalars())

    async def _investigate_leftovers(
        self, accounts: list[BankAccount], *, limit: int
    ) -> tuple[int, int, bool]:
        """Ask the model about transactions the engine could not resolve."""
        account_ids = [account.id for account in accounts]
        transactions = list(
            self.db.execute(
                select(BankTransaction)
                .where(
                    BankTransaction.company_id == self.company_id,
                    BankTransaction.bank_account_id.in_(account_ids),
                    BankTransaction.recon_status.in_(
                        [ReconStatus.UNMATCHED, ReconStatus.AMBIGUOUS]
                    ),
                )
                .order_by(BankTransaction.amount.desc())
                .limit(limit)
            ).scalars()
        )

        investigated = 0
        proposed = 0
        used_llm = False

        for transaction in transactions:
            # Re-score with a wider window than the automatic pass used.
            candidates = recon_service.find_candidates(
                self.db,
                self.company_id,
                transaction,
                tolerance_days=15,
                limit=5,
            )
            if not candidates:
                continue

            investigated += 1
            payload = [
                {
                    "journal_line_id": candidate.journal_line.id,
                    "entry_number": candidate.entry.entry_number,
                    "date": candidate.journal_line.entry_date.isoformat(),
                    "amount": str(
                        candidate.journal_line.debit or candidate.journal_line.credit
                    ),
                    "description": (
                        candidate.journal_line.description or candidate.entry.narration
                    )[:120],
                    "score": round(candidate.score, 1),
                    "reason": " ".join(candidate.reasons)[:200],
                }
                for candidate in candidates
            ]

            parsed, meta = await self.ask_json(
                task="reconcile",
                instruction=(
                    "Decide which candidate ledger line, if any, corresponds to this bank "
                    'transaction. Reply {"journal_line_id": "<id from candidates>", '
                    '"confidence": 0-1, "reasoning": "..."} or '
                    '{"insufficient_evidence": true, "reasoning": "..."}. '
                    "You must choose an id from the candidates list."
                ),
                context={
                    "bank_transaction": {
                        "date": transaction.txn_date.isoformat(),
                        "amount": str(transaction.amount),
                        "direction": transaction.direction,
                        "reference": transaction.reference,
                    },
                    "candidates": payload,
                },
                untrusted_text=transaction.description,
                untrusted_label="bank transaction narration",
            )
            used_llm = used_llm or bool(meta.get("used_llm"))

            if parsed is None or parsed.get("insufficient_evidence"):
                continue

            line_id = parsed.get("journal_line_id")
            valid_ids = {candidate.journal_line.id for candidate in candidates}
            if line_id not in valid_ids:
                # The model named a line we did not offer — reject silently and
                # leave the transaction for a human.
                continue

            try:
                confidence = D(str(parsed.get("confidence", 0.6)))
            except Exception:  # noqa: BLE001
                confidence = D("0.6")

            try:
                recon_service.propose_ai_match(
                    self.db,
                    self.company_id,
                    bank_transaction_id=transaction.id,
                    journal_line_id=line_id,
                    confidence=confidence,
                    reasoning=str(parsed.get("reasoning", ""))[:400],
                    agent_run_id=self.run.id if self.run else None,
                )
                proposed += 1
            except Exception:  # noqa: BLE001 — verification refused it; that is the system working
                continue

        return investigated, proposed, used_llm
