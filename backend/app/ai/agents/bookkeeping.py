"""Bookkeeping and expense agents.

The bookkeeping agent categorises bank transactions. Its cascade is:

1. **Learned rules** — deterministic patterns built from confirmed human
   decisions. Free, instant, and correct by construction for known payees.
2. **Historical precedent** — what this exact narration was categorised as
   before.
3. **The model** — only for genuinely new descriptions, and only choosing from
   a shortlist of *real* accounts supplied in the prompt.

Anything the model returns is checked against that shortlist, so it cannot
invent an account.
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
    AccountType,
    BankTxnDirection,
    DocumentStatus,
    ExceptionCategory,
    ProposalType,
    ReconStatus,
    Severity,
)
from app.models import Account, BankTransaction, ExpenseClaim, JournalLine
from app.money import ZERO, D, money, rate as to_rate
from app.services.ai_learning import retrieve_examples
from app.services.banking import apply_categorisation_rules

MAX_CANDIDATE_ACCOUNTS = 40


class BookkeepingAgent(BaseAgent):
    kind = AgentKind.BOOKKEEPING
    display_name = "Bookkeeping Agent"
    role_description = (
        "You categorise bank transactions into the correct ledger account. "
        "You choose ONLY from the candidate accounts supplied in the context. "
        "If none fits, say so rather than guessing."
    )

    async def run_agent(self, **params: Any) -> AgentResult:
        limit = int(params.get("limit", 100))
        bank_account_id = params.get("bank_account_id")

        stmt = select(BankTransaction).where(
            BankTransaction.company_id == self.company_id,
            BankTransaction.journal_entry_id.is_(None),
            BankTransaction.recon_status == ReconStatus.UNMATCHED,
        )
        if bank_account_id:
            stmt = stmt.where(BankTransaction.bank_account_id == bank_account_id)

        transactions = list(
            self.db.execute(stmt.order_by(BankTransaction.txn_date.desc()).limit(limit)).scalars()
        )

        if not transactions:
            return AgentResult(
                agent_kind=self.kind,
                summary="No uncategorised bank transactions were found.",
                metrics={"transactions_scanned": 0},
            )

        candidates = self._candidate_accounts()
        result = AgentResult(agent_kind=self.kind, items_processed=len(transactions))

        by_rule = by_history = by_model = unresolved = 0

        for transaction in transactions:
            suggestion = None

            # 1. Deterministic learned rules.
            rule = apply_categorisation_rules(self.db, self.company_id, transaction)
            if rule is not None:
                suggestion = {
                    "account_id": rule.account_id,
                    "confidence": D("0.98"),
                    "reasoning": f"Matched the saved rule '{rule.name}'.",
                    "method": "RULE",
                }
                by_rule += 1

            # 2. Historical precedent for this exact narration.
            if suggestion is None:
                historical = self._historical_account(transaction)
                if historical is not None:
                    account_id, times = historical
                    suggestion = {
                        "account_id": account_id,
                        "confidence": D("0.90") if times > 2 else D("0.75"),
                        "reasoning": (
                            f"Transactions with this description were posted to this account "
                            f"{times} time(s) before."
                        ),
                        "method": "HISTORY",
                    }
                    by_history += 1

            # 3. The model, constrained to the candidate list.
            if suggestion is None and self.uses_llm and candidates:
                suggestion = await self._ask_model(transaction, candidates)
                if suggestion is not None:
                    by_model += 1
                    result.used_llm = True

            if suggestion is None:
                unresolved += 1
                transaction.suggested_confidence = ZERO
                continue

            account = self.db.get(Account, suggestion["account_id"])
            if account is None or account.company_id != self.company_id or account.is_group:
                # The model named something that is not a usable account.
                unresolved += 1
                continue

            transaction.suggested_account_id = account.id
            transaction.suggested_category = account.name
            transaction.suggested_confidence = to_rate(suggestion["confidence"])
            transaction.categorised_by = suggestion["method"]

            confidence = to_rate(suggestion["confidence"])
            if confidence >= D("0.90"):
                result.high_confidence_count += 1
            else:
                result.needs_review_count += 1

            proposal = self.create_proposal(
                proposal_type=ProposalType.CATEGORISATION,
                title=(
                    f"Categorise {transaction.amount} "
                    f"{'received' if transaction.direction == BankTxnDirection.CREDIT else 'paid'} "
                    f"as {account.name}"
                ),
                summary=f"{transaction.description[:120]} -> {account.code} {account.name}",
                reasoning=suggestion["reasoning"],
                payload={
                    "bank_transaction_id": transaction.id,
                    "account_id": account.id,
                    "account_code": account.code,
                    "account_name": account.name,
                    "amount": str(transaction.amount),
                    "direction": transaction.direction,
                    "date": transaction.txn_date.isoformat(),
                    "method": suggestion["method"],
                },
                confidence=confidence,
                financial_impact=transaction.amount,
                target_type="BankTransaction",
                target_id=transaction.id,
                evidence=[
                    {
                        "type": "bank_transaction",
                        "id": transaction.id,
                        "label": transaction.description[:100],
                        "date": transaction.txn_date.isoformat(),
                        "amount": str(transaction.amount),
                    },
                    {
                        "type": "account",
                        "id": account.id,
                        "label": f"{account.code} {account.name}",
                    },
                ],
                source_records=[{"type": "bank_transaction", "id": transaction.id}],
            )
            result.proposals.append(proposal.id)

        self.db.flush()

        result.summary = (
            f"Reviewed {len(transactions)} uncategorised transaction(s): "
            f"{by_rule} matched a saved rule, {by_history} matched history, "
            f"{by_model} were classified by the model, {unresolved} need a human."
        )
        result.metrics = {
            "transactions_scanned": len(transactions),
            "by_rule": by_rule,
            "by_history": by_history,
            "by_model": by_model,
            "unresolved": unresolved,
        }
        result.findings = [
            {
                "label": "Needs manual categorisation",
                "count": unresolved,
                "detail": "No rule, history or confident model match was available.",
            }
        ] if unresolved else []
        result.status = (
            AgentRunStatus.NEEDS_REVIEW if result.needs_review_count or unresolved else AgentRunStatus.COMPLETED
        )
        return result

    # ---------------------------------------------------------- internals --

    def _candidate_accounts(self) -> list[dict]:
        """Postable P&L and cash-adjacent accounts the model may choose from."""
        accounts = list(
            self.db.execute(
                select(Account)
                .where(
                    Account.company_id == self.company_id,
                    Account.is_group.is_(False),
                    Account.is_active.is_(True),
                    Account.type.in_([AccountType.EXPENSE, AccountType.REVENUE]),
                )
                .order_by(Account.code)
                .limit(MAX_CANDIDATE_ACCOUNTS)
            ).scalars()
        )
        return [
            {"id": account.id, "code": account.code, "name": account.name, "type": account.type}
            for account in accounts
        ]

    def _historical_account(self, transaction: BankTransaction) -> tuple[str, int] | None:
        """Find how this narration was categorised before."""
        pattern = (transaction.normalised_description or "").strip()
        if len(pattern) < 4:
            return None

        rows = self.db.execute(
            select(
                BankTransaction.suggested_account_id,
                func.count(BankTransaction.id),
            )
            .where(
                BankTransaction.company_id == self.company_id,
                BankTransaction.normalised_description == pattern,
                BankTransaction.journal_entry_id.isnot(None),
                BankTransaction.suggested_account_id.isnot(None),
                BankTransaction.id != transaction.id,
            )
            .group_by(BankTransaction.suggested_account_id)
            .order_by(func.count(BankTransaction.id).desc())
            .limit(1)
        ).first()

        if rows and rows[0]:
            return str(rows[0]), int(rows[1])
        return None

    async def _ask_model(
        self, transaction: BankTransaction, candidates: list[dict]
    ) -> dict | None:
        examples = retrieve_examples(
            self.db,
            self.company_id,
            agent_kind=self.kind,
            query_text=transaction.normalised_description or "",
            limit=4,
        )

        parsed, meta = await self.ask_json(
            task="categorise",
            instruction=(
                "Choose the single best account for this bank transaction from "
                "candidate_accounts. Reply with JSON: "
                '{"account_id": "<id from the list>", "confidence": <0-1>, '
                '"reasoning": "<one sentence>"}. '
                'If no candidate is appropriate, reply {"insufficient_evidence": true, '
                '"reasoning": "..."}.'
            ),
            context={
                "description": transaction.description,
                "normalised_description": transaction.normalised_description,
                "amount": str(transaction.amount),
                "direction": transaction.direction,
                "date": transaction.txn_date.isoformat(),
                "candidate_accounts": candidates,
                "past_approved_examples": examples,
            },
            untrusted_text=transaction.description,
            untrusted_label="bank transaction narration",
        )

        if parsed is None or parsed.get("insufficient_evidence"):
            return None

        account_id = parsed.get("account_id")
        # Hard constraint: the answer must be one of the ids we supplied.
        valid_ids = {candidate["id"] for candidate in candidates}
        if account_id not in valid_ids:
            return None

        try:
            confidence = D(str(parsed.get("confidence", 0.5)))
        except Exception:  # noqa: BLE001
            confidence = D("0.5")

        # A model's self-reported confidence is capped: it has less evidence
        # than a saved rule or historical precedent.
        confidence = min(confidence, D("0.88"))

        return {
            "account_id": account_id,
            "confidence": confidence,
            "reasoning": str(parsed.get("reasoning", "Classified by the model."))[:400],
            "method": "AI",
        }


class ExpenseAgent(BaseAgent):
    kind = AgentKind.EXPENSE
    display_name = "Expense Agent"
    role_description = (
        "You review employee and vendor expense claims, suggest the right expense "
        "category and flag claims that need scrutiny."
    )

    async def run_agent(self, **params: Any) -> AgentResult:
        claims = list(
            self.db.execute(
                select(ExpenseClaim).where(
                    ExpenseClaim.company_id == self.company_id,
                    ExpenseClaim.status.in_([DocumentStatus.DRAFT, DocumentStatus.PENDING_APPROVAL]),
                )
                .order_by(ExpenseClaim.expense_date.desc())
                .limit(int(params.get("limit", 50)))
            ).scalars()
        )

        result = AgentResult(agent_kind=self.kind, items_processed=len(claims))
        if not claims:
            result.summary = "No expense claims are awaiting review."
            return result

        candidates = [
            {"id": account.id, "code": account.code, "name": account.name}
            for account in self.db.execute(
                select(Account)
                .where(
                    Account.company_id == self.company_id,
                    Account.type == AccountType.EXPENSE,
                    Account.is_group.is_(False),
                    Account.is_active.is_(True),
                )
                .order_by(Account.code)
                .limit(MAX_CANDIDATE_ACCOUNTS)
            ).scalars()
        ]

        categorised = 0
        flagged = 0
        for claim in claims:
            if claim.expense_account_id is None and candidates:
                parsed, meta = await self.ask_json(
                    task="categorise",
                    instruction=(
                        "Choose the best expense account for this claim from "
                        'candidate_accounts. Reply {"account_id": "...", "confidence": 0-1, '
                        '"reasoning": "..."} or {"insufficient_evidence": true}.'
                    ),
                    context={
                        "description": f"{claim.title} {claim.category}",
                        "amount": str(claim.total or claim.amount),
                        "candidate_accounts": candidates,
                    },
                    untrusted_text=f"{claim.title}\n{claim.notes}",
                    untrusted_label="expense claim text",
                )
                result.used_llm = result.used_llm or bool(meta.get("used_llm"))

                if parsed and not parsed.get("insufficient_evidence"):
                    account_id = parsed.get("account_id")
                    if account_id in {c["id"] for c in candidates}:
                        account = self.db.get(Account, account_id)
                        claim.ai_suggested_category = account.name if account else ""
                        claim.ai_category_confidence = to_rate(
                            min(D(str(parsed.get("confidence", 0.6))), D("0.88"))
                        )
                        categorised += 1

            # Claims without a receipt above a modest amount deserve a look.
            if claim.document_id is None and money(claim.total or claim.amount) >= money(5000):
                flagged += 1
                self.raise_exception_item(
                    category=ExceptionCategory.ACCOUNTING,
                    severity=Severity.MEDIUM,
                    title=f"Expense claim {claim.number} has no receipt",
                    reason=(
                        f"'{claim.title}' for {money(claim.total or claim.amount)} was submitted "
                        f"without an attached receipt."
                    ),
                    recommended_action="Ask the claimant to attach the receipt before approval.",
                    dedupe_key=f"expense_no_receipt:{claim.id}",
                    amount=money(claim.total or claim.amount),
                    entity_type="ExpenseClaim",
                    entity_id=claim.id,
                )

        self.db.flush()
        result.summary = (
            f"Reviewed {len(claims)} expense claim(s): {categorised} received a category "
            f"suggestion, {flagged} were flagged for a missing receipt."
        )
        result.metrics = {"claims": len(claims), "categorised": categorised, "flagged": flagged}
        result.needs_review_count = flagged
        return result
