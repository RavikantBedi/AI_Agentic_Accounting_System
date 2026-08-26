"""Bank reconciliation: a layered, explainable matching engine.

Candidates are scored by eight strategies, applied strongest-first:

1. Exact reference (UTR / cheque number)     2. Exact amount + date
3. Date tolerance                             4. Normalised description
5. Counterparty (party name)                  6. Historical pattern
7. Fuzzy score                                8. AI investigation (last resort)

Every match carries its **evidence**: which signals agreed, by how much, and
the financial impact. High-confidence matches auto-confirm only when policy
allows; everything else lands in the review queue rather than being guessed at.

The AI layer never writes a match directly — it calls ``propose_ai_match``,
which re-verifies the candidate against the ledger before recording anything.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.enums import (
    ActorType,
    AuditAction,
    BankTxnDirection,
    JournalStatus,
    MatchStrategy,
    ReconStatus,
)
from app.errors import ConflictError, NotFoundError, ValidationError
from app.models import (
    Account,
    BankAccount,
    BankTransaction,
    Customer,
    Invoice,
    JournalEntry,
    JournalLine,
    ReconciliationMatch,
    ReconciliationSession,
    Vendor,
)
from app.models.base import utcnow
from app.money import ZERO, D, money
from app.services.policy import get_policy

try:  # rapidfuzz is optional; difflib is the always-available fallback
    from rapidfuzz import fuzz

    def _similarity(a: str, b: str) -> float:
        return float(fuzz.token_set_ratio(a, b))

except ImportError:  # pragma: no cover - exercised only without rapidfuzz
    from difflib import SequenceMatcher

    def _similarity(a: str, b: str) -> float:
        return SequenceMatcher(None, a, b).ratio() * 100


@dataclass
class Candidate:
    """A possible counterpart for a bank line, with its accumulated evidence."""

    journal_line: JournalLine
    entry: JournalEntry
    score: float = 0.0
    strategies: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    @property
    def confidence(self) -> Decimal:
        return D(min(0.99, max(0.0, self.score / 100)))


# Weights are tuned so that a single strong signal (an exact UTR match) can
# stand alone, while weak signals must corroborate each other.
WEIGHTS = {
    MatchStrategy.EXACT_REFERENCE: 45.0,
    MatchStrategy.EXACT_AMOUNT: 30.0,
    MatchStrategy.DATE_TOLERANCE: 15.0,
    MatchStrategy.DESCRIPTION_NORMALISED: 12.0,
    MatchStrategy.COUNTERPARTY: 18.0,
    MatchStrategy.HISTORICAL_PATTERN: 10.0,
    MatchStrategy.FUZZY_SCORE: 10.0,
}


def find_candidates(
    db: Session,
    company_id: str,
    transaction: BankTransaction,
    *,
    tolerance_days: int | None = None,
    amount_tolerance: Decimal | None = None,
    limit: int = 10,
) -> list[Candidate]:
    """Score unreconciled ledger lines against one bank transaction."""
    policy = get_policy(db, company_id, "reconciliation.matching")
    tolerance_days = tolerance_days if tolerance_days is not None else int(policy.get("date_tolerance_days", 5))
    amount_tolerance = (
        amount_tolerance if amount_tolerance is not None else money(policy.get("amount_tolerance", 1))
    )
    percent_tolerance = D(policy.get("amount_tolerance_percent", 0.5))
    fuzzy_threshold = float(policy.get("fuzzy_name_threshold", 82))

    bank_account = db.get(BankAccount, transaction.bank_account_id)
    if bank_account is None:
        raise NotFoundError("Bank account not found.")

    # A bank *credit* (money in) corresponds to a debit on the bank GL account.
    want_debit = transaction.direction == BankTxnDirection.CREDIT

    window_start = transaction.txn_date - timedelta(days=tolerance_days)
    window_end = transaction.txn_date + timedelta(days=tolerance_days)

    # Widen the amount window by the larger of the absolute and percentage
    # tolerances, so both small and large transactions behave sensibly.
    percent_amount = money(transaction.amount * percent_tolerance / Decimal(100))
    effective_tolerance = max(amount_tolerance, percent_amount)
    low = money(transaction.amount - effective_tolerance)
    high = money(transaction.amount + effective_tolerance)

    amount_column = JournalLine.debit if want_debit else JournalLine.credit

    stmt = (
        select(JournalLine, JournalEntry)
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .where(
            JournalLine.company_id == company_id,
            JournalLine.account_id == bank_account.gl_account_id,
            JournalLine.status == JournalStatus.POSTED,
            JournalLine.reconciled.is_(False),
            JournalLine.entry_date >= window_start,
            JournalLine.entry_date <= window_end,
            amount_column >= low,
            amount_column <= high,
        )
        .limit(200)
    )

    candidates: list[Candidate] = []
    bank_reference = (transaction.reference or "").strip().lower()
    bank_description = transaction.normalised_description or ""

    for line, entry in db.execute(stmt):
        line_amount = money(line.debit if want_debit else line.credit)
        if line_amount <= ZERO:
            continue

        candidate = Candidate(journal_line=line, entry=entry)
        difference = money(abs(line_amount - transaction.amount))
        day_gap = abs((line.entry_date - transaction.txn_date).days)

        # 1. Exact reference — the strongest available signal.
        entry_reference = (entry.reference or "").strip().lower()
        if bank_reference and entry_reference:
            if bank_reference == entry_reference:
                candidate.score += WEIGHTS[MatchStrategy.EXACT_REFERENCE]
                candidate.strategies.append(MatchStrategy.EXACT_REFERENCE)
                candidate.evidence["reference"] = {
                    "matched": True,
                    "bank": transaction.reference,
                    "book": entry.reference,
                }
                candidate.reasons.append(f"Reference '{entry.reference}' matches exactly.")
            elif bank_reference in entry_reference or entry_reference in bank_reference:
                candidate.score += WEIGHTS[MatchStrategy.EXACT_REFERENCE] * 0.6
                candidate.strategies.append(MatchStrategy.EXACT_REFERENCE)
                candidate.evidence["reference"] = {
                    "matched": "partial",
                    "bank": transaction.reference,
                    "book": entry.reference,
                }
                candidate.reasons.append("Reference partially matches.")

        # 2. Amount.
        if difference == ZERO:
            candidate.score += WEIGHTS[MatchStrategy.EXACT_AMOUNT]
            candidate.strategies.append(MatchStrategy.EXACT_AMOUNT)
            candidate.evidence["amount"] = {
                "matched": True,
                "bank": transaction.amount,
                "book": line_amount,
                "difference": ZERO,
            }
            candidate.reasons.append(f"Amount {transaction.amount} matches exactly.")
        else:
            proportion = float(difference / transaction.amount) if transaction.amount else 1.0
            candidate.score += WEIGHTS[MatchStrategy.EXACT_AMOUNT] * max(0.0, 1 - proportion * 20)
            candidate.evidence["amount"] = {
                "matched": False,
                "bank": transaction.amount,
                "book": line_amount,
                "difference": difference,
            }
            candidate.reasons.append(f"Amount differs by {difference}.")

        # 3. Date proximity.
        if day_gap == 0:
            candidate.score += WEIGHTS[MatchStrategy.DATE_TOLERANCE]
            candidate.reasons.append("Same date.")
        else:
            decay = max(0.0, 1 - (day_gap / max(tolerance_days, 1)) * 0.7)
            candidate.score += WEIGHTS[MatchStrategy.DATE_TOLERANCE] * decay
            candidate.reasons.append(f"{day_gap} day(s) apart.")
        candidate.strategies.append(MatchStrategy.DATE_TOLERANCE)
        candidate.evidence["date"] = {
            "bank": transaction.txn_date,
            "book": line.entry_date,
            "days_apart": day_gap,
        }

        # 4/5. Description and counterparty.
        book_text = " ".join(
            filter(None, [line.description or "", entry.narration or "", entry.reference or ""])
        ).lower()
        party_name = _party_name(db, line)

        if bank_description and book_text:
            similarity = _similarity(bank_description, book_text)
            if similarity >= fuzzy_threshold:
                candidate.score += WEIGHTS[MatchStrategy.DESCRIPTION_NORMALISED]
                candidate.strategies.append(MatchStrategy.DESCRIPTION_NORMALISED)
                candidate.reasons.append(f"Description similarity {similarity:.0f}%.")
            elif similarity >= 50:
                candidate.score += WEIGHTS[MatchStrategy.FUZZY_SCORE] * (similarity / 100)
                candidate.strategies.append(MatchStrategy.FUZZY_SCORE)
            candidate.evidence["description"] = {
                "bank": transaction.description,
                "book": (line.description or entry.narration),
                "similarity": round(similarity, 1),
            }

        if party_name:
            party_similarity = _similarity(
                transaction.counterparty or bank_description, party_name.lower()
            )
            if party_similarity >= fuzzy_threshold:
                candidate.score += WEIGHTS[MatchStrategy.COUNTERPARTY]
                candidate.strategies.append(MatchStrategy.COUNTERPARTY)
                candidate.reasons.append(f"Counterparty '{party_name}' matches.")
            candidate.evidence["counterparty"] = {
                "bank": transaction.counterparty,
                "book": party_name,
                "similarity": round(party_similarity, 1),
            }

        # 6. Historical pattern: this party was matched to similar lines before.
        if line.party_id and _has_historical_pattern(db, company_id, transaction, line.party_id):
            candidate.score += WEIGHTS[MatchStrategy.HISTORICAL_PATTERN]
            candidate.strategies.append(MatchStrategy.HISTORICAL_PATTERN)
            candidate.reasons.append("Similar transactions from this party matched here before.")
            candidate.evidence["historical_pattern"] = True

        candidate.evidence["financial_impact"] = line_amount
        candidate.evidence["entry_number"] = entry.entry_number
        candidate.evidence["voucher_type"] = entry.voucher_type
        candidates.append(candidate)

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:limit]


def _party_name(db: Session, line: JournalLine) -> str:
    if not line.party_id:
        return ""
    if line.party_type == "CUSTOMER":
        customer = db.get(Customer, line.party_id)
        return customer.name if customer else ""
    if line.party_type == "VENDOR":
        vendor = db.get(Vendor, line.party_id)
        return vendor.name if vendor else ""
    return ""


def _has_historical_pattern(
    db: Session, company_id: str, transaction: BankTransaction, party_id: str
) -> bool:
    """Has a similar bank narration been confirmed against this party before?"""
    if not transaction.normalised_description:
        return False
    rows = db.execute(
        select(BankTransaction.normalised_description)
        .join(ReconciliationMatch, ReconciliationMatch.bank_transaction_id == BankTransaction.id)
        .join(JournalLine, JournalLine.id == ReconciliationMatch.journal_line_id)
        .where(
            BankTransaction.company_id == company_id,
            JournalLine.party_id == party_id,
            ReconciliationMatch.is_confirmed.is_(True),
            BankTransaction.id != transaction.id,
        )
        .limit(20)
    ).scalars()
    return any(
        _similarity(transaction.normalised_description, text or "") >= 85 for text in rows
    )


def run_reconciliation(
    db: Session,
    company_id: str,
    bank_account_id: str,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    auto_confirm: bool | None = None,
    user_id: str | None = None,
    limit: int = 1000,
) -> ReconciliationSession:
    """Match every unreconciled bank line in the window and record the outcome."""
    bank_account = db.get(BankAccount, bank_account_id)
    if bank_account is None or bank_account.company_id != company_id:
        raise NotFoundError("Bank account not found.")

    policy = get_policy(db, company_id, "reconciliation.matching")
    auto_threshold = D(policy.get("auto_confirm_min_confidence", 0.95))
    review_threshold = D(policy.get("review_min_confidence", 0.60))
    if auto_confirm is None:
        auto_confirm = True

    stmt = select(BankTransaction).where(
        BankTransaction.company_id == company_id,
        BankTransaction.bank_account_id == bank_account_id,
        BankTransaction.recon_status.in_([ReconStatus.UNMATCHED, ReconStatus.AMBIGUOUS]),
    )
    if start_date:
        stmt = stmt.where(BankTransaction.txn_date >= start_date)
    if end_date:
        stmt = stmt.where(BankTransaction.txn_date <= end_date)

    transactions = list(
        db.execute(stmt.order_by(BankTransaction.txn_date.asc()).limit(limit)).scalars()
    )

    session = ReconciliationSession(
        company_id=company_id,
        bank_account_id=bank_account_id,
        period_start=start_date or (transactions[0].txn_date if transactions else date.today()),
        period_end=end_date or (transactions[-1].txn_date if transactions else date.today()),
        total_transactions=len(transactions),
        run_by_user_id=user_id,
    )
    db.add(session)
    db.flush()

    matched = auto_matched = ambiguous = unmatched = 0
    claimed_lines: set[str] = set()

    for transaction in transactions:
        candidates = [
            c
            for c in find_candidates(db, company_id, transaction)
            if c.journal_line.id not in claimed_lines
        ]

        if not candidates:
            transaction.recon_status = ReconStatus.UNMATCHED
            unmatched += 1
            continue

        best = candidates[0]
        runner_up = candidates[1] if len(candidates) > 1 else None

        # Two candidates that are too close to separate is *ambiguous*, not a
        # match. Guessing here is how reconciliation quietly corrupts books.
        if runner_up and (best.score - runner_up.score) < 8 and best.confidence < auto_threshold:
            transaction.recon_status = ReconStatus.AMBIGUOUS
            ambiguous += 1
            _record_match(
                db,
                company_id,
                session,
                transaction,
                best,
                status=ReconStatus.AMBIGUOUS,
                confirmed=False,
                note=(
                    f"Ambiguous: {len(candidates)} similar candidates "
                    f"(top scores {best.score:.0f} vs {runner_up.score:.0f})."
                ),
            )
            continue

        confidence = best.confidence
        if confidence >= auto_threshold and auto_confirm:
            _record_match(
                db, company_id, session, transaction, best, status=ReconStatus.MATCHED, confirmed=True
            )
            claimed_lines.add(best.journal_line.id)
            matched += 1
            auto_matched += 1
        elif confidence >= review_threshold:
            _record_match(
                db,
                company_id,
                session,
                transaction,
                best,
                status=ReconStatus.PARTIAL,
                confirmed=False,
                note="Needs review: confidence is below the auto-confirm threshold.",
            )
            transaction.recon_status = ReconStatus.PARTIAL
        else:
            transaction.recon_status = ReconStatus.UNMATCHED
            unmatched += 1

    session.matched_count = matched
    session.auto_matched_count = auto_matched
    session.ambiguous_count = ambiguous
    session.unmatched_count = unmatched
    session.partial_count = len(transactions) - matched - ambiguous - unmatched
    session.completed_at = utcnow()

    from app.services.banking import bank_book_balance

    session.book_balance = bank_book_balance(db, company_id, bank_account_id, as_of=session.period_end)
    session.bank_balance = bank_account.statement_balance
    if session.bank_balance is not None:
        session.difference = money(session.bank_balance - session.book_balance)

    db.flush()

    from app.services.audit import record_event

    record_event(
        db,
        company_id=company_id,
        action=AuditAction.UPDATE,
        entity_type="ReconciliationSession",
        entity_id=session.id,
        entity_label=bank_account.name,
        summary=(
            f"Reconciliation over {len(transactions)} transactions: {matched} matched "
            f"({auto_matched} automatically), {session.partial_count} for review, "
            f"{ambiguous} ambiguous, {unmatched} unmatched."
        ),
        actor_type=ActorType.USER if user_id else ActorType.SYSTEM,
        actor_id=user_id,
        after={
            "matched": matched,
            "auto_matched": auto_matched,
            "ambiguous": ambiguous,
            "unmatched": unmatched,
            "book_balance": str(session.book_balance),
        },
    )
    return session


def _record_match(
    db: Session,
    company_id: str,
    session: ReconciliationSession,
    transaction: BankTransaction,
    candidate: Candidate,
    *,
    status: str,
    confirmed: bool,
    note: str = "",
    proposed_by: str = "ENGINE",
) -> ReconciliationMatch:
    match = ReconciliationMatch(
        company_id=company_id,
        session_id=session.id if session else None,
        bank_transaction_id=transaction.id,
        journal_line_id=candidate.journal_line.id,
        amount=money(
            candidate.journal_line.debit
            if transaction.direction == BankTxnDirection.CREDIT
            else candidate.journal_line.credit
        ),
        strategy=candidate.strategies[0] if candidate.strategies else MatchStrategy.FUZZY_SCORE,
        confidence=candidate.confidence,
        status=status,
        evidence={
            **candidate.evidence,
            "strategies": candidate.strategies,
            "score": round(candidate.score, 1),
        },
        reason=note or " ".join(candidate.reasons),
        is_confirmed=confirmed,
        proposed_by=proposed_by,
        document_type=candidate.entry.source_type,
        document_id=candidate.entry.source_id,
    )
    db.add(match)

    if confirmed:
        match.confirmed_at = utcnow()
        candidate.journal_line.reconciled = True
        candidate.journal_line.reconciled_at = utcnow()
        transaction.recon_status = ReconStatus.MATCHED
        transaction.matched_amount = match.amount
        transaction.journal_entry_id = candidate.entry.id

    db.flush()
    return match


def confirm_match(
    db: Session, company_id: str, match_id: str, *, user_id: str, learn: bool = True
) -> ReconciliationMatch:
    """A human confirms a proposed match. This is what makes it real."""
    match = db.get(ReconciliationMatch, match_id)
    if match is None or match.company_id != company_id:
        raise NotFoundError("Match not found.")
    if match.is_confirmed:
        raise ConflictError("This match is already confirmed.")

    line = db.get(JournalLine, match.journal_line_id)
    if line is None:
        raise NotFoundError("The matched ledger line no longer exists.")
    if line.reconciled:
        raise ConflictError(
            "That ledger line has already been reconciled against another bank transaction."
        )

    transaction = db.get(BankTransaction, match.bank_transaction_id)

    match.is_confirmed = True
    match.status = ReconStatus.MATCHED
    match.confirmed_by_user_id = user_id
    match.confirmed_at = utcnow()

    line.reconciled = True
    line.reconciled_at = utcnow()

    transaction.recon_status = ReconStatus.MATCHED
    transaction.matched_amount = match.amount
    transaction.journal_entry_id = line.entry_id

    # Any competing proposals for the same bank line are now superseded.
    for other in db.execute(
        select(ReconciliationMatch).where(
            ReconciliationMatch.bank_transaction_id == transaction.id,
            ReconciliationMatch.id != match.id,
            ReconciliationMatch.is_confirmed.is_(False),
        )
    ).scalars():
        other.status = ReconStatus.EXCEPTION
        other.reason = f"Superseded — {match.id} was confirmed instead."

    db.flush()

    from app.services.ai_learning import log_decision

    log_decision(
        db,
        company_id,
        agent_kind="RECONCILIATION",
        proposal_type="RECONCILIATION_MATCH",
        prediction={
            "journal_line_id": match.journal_line_id,
            "confidence": str(match.confidence),
            "strategy": match.strategy,
        },
        human_decision={"journal_line_id": match.journal_line_id, "action": "confirmed"},
        decision="APPROVED",
        was_correct=True,
        confidence=match.confidence,
        user_id=user_id,
        context={"bank_transaction_id": transaction.id},
    )

    from app.services.audit import record_event

    record_event(
        db,
        company_id=company_id,
        action=AuditAction.APPROVE,
        entity_type="ReconciliationMatch",
        entity_id=match.id,
        entity_label=f"{transaction.description[:60]}",
        summary=f"Confirmed reconciliation match for {match.amount}",
        actor_type=ActorType.USER,
        actor_id=user_id,
        amount=match.amount,
        after={"confidence": str(match.confidence), "strategy": match.strategy},
    )
    return match


def reject_match(
    db: Session, company_id: str, match_id: str, *, user_id: str, reason: str = ""
) -> ReconciliationMatch:
    """Reject a proposed match and feed the correction back into learning."""
    match = db.get(ReconciliationMatch, match_id)
    if match is None or match.company_id != company_id:
        raise NotFoundError("Match not found.")
    if match.is_confirmed:
        raise ConflictError(
            "This match is already confirmed. Unmatch it instead of rejecting it."
        )

    match.status = ReconStatus.EXCEPTION
    match.reason = f"{match.reason} | Rejected: {reason}".strip(" |")
    transaction = db.get(BankTransaction, match.bank_transaction_id)
    transaction.recon_status = ReconStatus.UNMATCHED
    db.flush()

    from app.services.ai_learning import log_decision

    log_decision(
        db,
        company_id,
        agent_kind="RECONCILIATION",
        proposal_type="RECONCILIATION_MATCH",
        prediction={"journal_line_id": match.journal_line_id, "confidence": str(match.confidence)},
        human_decision={"action": "rejected", "reason": reason},
        decision="REJECTED",
        was_correct=False,
        confidence=match.confidence,
        user_id=user_id,
        context={"bank_transaction_id": transaction.id},
    )
    return match


def unmatch(
    db: Session, company_id: str, transaction_id: str, *, user_id: str, reason: str = ""
) -> BankTransaction:
    """Undo a confirmed reconciliation, releasing the ledger line."""
    transaction = db.get(BankTransaction, transaction_id)
    if transaction is None or transaction.company_id != company_id:
        raise NotFoundError("Bank transaction not found.")

    matches = list(
        db.execute(
            select(ReconciliationMatch).where(
                ReconciliationMatch.bank_transaction_id == transaction_id,
                ReconciliationMatch.is_confirmed.is_(True),
            )
        ).scalars()
    )
    for match in matches:
        line = db.get(JournalLine, match.journal_line_id) if match.journal_line_id else None
        if line is not None:
            line.reconciled = False
            line.reconciled_at = None
        match.is_confirmed = False
        match.status = ReconStatus.EXCEPTION
        match.reason = f"{match.reason} | Unmatched: {reason}".strip(" |")

    transaction.recon_status = ReconStatus.UNMATCHED
    transaction.matched_amount = ZERO
    transaction.journal_entry_id = None
    db.flush()

    from app.services.audit import record_event

    record_event(
        db,
        company_id=company_id,
        action=AuditAction.UPDATE,
        entity_type="BankTransaction",
        entity_id=transaction.id,
        entity_label=transaction.description[:60],
        summary=f"Unmatched bank transaction of {transaction.amount}",
        reason=reason,
        actor_type=ActorType.USER,
        actor_id=user_id,
        amount=transaction.amount,
    )
    return transaction


def propose_ai_match(
    db: Session,
    company_id: str,
    *,
    bank_transaction_id: str,
    journal_line_id: str,
    confidence: Decimal,
    reasoning: str,
    agent_run_id: str | None = None,
) -> ReconciliationMatch:
    """Record an AI-suggested match after re-verifying it against the ledger.

    The AI supplies only *ids and a rationale*. This function independently
    re-reads both records and refuses anything that does not hold up, so a
    hallucinated or malicious id cannot create a match.
    """
    transaction = db.get(BankTransaction, bank_transaction_id)
    if transaction is None or transaction.company_id != company_id:
        raise NotFoundError("The referenced bank transaction does not exist.")

    line = db.get(JournalLine, journal_line_id)
    if line is None or line.company_id != company_id:
        raise NotFoundError("The referenced ledger line does not exist.")
    if line.status != JournalStatus.POSTED:
        raise ValidationError("Only posted ledger lines can be reconciled.")
    if line.reconciled:
        raise ConflictError("That ledger line is already reconciled.")

    bank_account = db.get(BankAccount, transaction.bank_account_id)
    if line.account_id != bank_account.gl_account_id:
        raise ValidationError(
            "The proposed ledger line does not belong to this bank account's GL account."
        )

    want_debit = transaction.direction == BankTxnDirection.CREDIT
    line_amount = money(line.debit if want_debit else line.credit)
    if line_amount <= ZERO:
        raise ValidationError(
            "The proposed ledger line is on the wrong side for this bank transaction."
        )

    entry = db.get(JournalEntry, line.entry_id)
    difference = money(abs(line_amount - transaction.amount))

    match = ReconciliationMatch(
        company_id=company_id,
        bank_transaction_id=transaction.id,
        journal_line_id=line.id,
        amount=line_amount,
        strategy=MatchStrategy.AI_INVESTIGATION,
        confidence=D(min(D(confidence), D("0.94"))),  # AI proposals never auto-confirm
        status=ReconStatus.PARTIAL,
        evidence={
            "amount": {
                "bank": transaction.amount,
                "book": line_amount,
                "difference": difference,
            },
            "date": {
                "bank": transaction.txn_date,
                "book": line.entry_date,
                "days_apart": abs((line.entry_date - transaction.txn_date).days),
            },
            "entry_number": entry.entry_number if entry else "",
            "verified_by_engine": True,
        },
        reason=f"AI investigation: {reasoning}",
        is_confirmed=False,
        proposed_by="AI",
    )
    db.add(match)
    transaction.recon_status = ReconStatus.PARTIAL
    db.flush()
    return match


def reconciliation_view(
    db: Session,
    company_id: str,
    bank_account_id: str,
    *,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """The split bank-vs-books view with proposed matches and their evidence."""
    bank_account = db.get(BankAccount, bank_account_id)
    if bank_account is None or bank_account.company_id != company_id:
        raise NotFoundError("Bank account not found.")

    stmt = select(BankTransaction).where(
        BankTransaction.company_id == company_id,
        BankTransaction.bank_account_id == bank_account_id,
    )
    if status:
        stmt = stmt.where(BankTransaction.recon_status == status)
    else:
        stmt = stmt.where(
            BankTransaction.recon_status.in_(
                [ReconStatus.UNMATCHED, ReconStatus.PARTIAL, ReconStatus.AMBIGUOUS, ReconStatus.EXCEPTION]
            )
        )

    total = int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    transactions = list(
        db.execute(
            stmt.order_by(BankTransaction.txn_date.desc()).limit(limit).offset(offset)
        ).scalars()
    )

    items = []
    for transaction in transactions:
        proposals = list(
            db.execute(
                select(ReconciliationMatch)
                .where(
                    ReconciliationMatch.bank_transaction_id == transaction.id,
                    ReconciliationMatch.is_confirmed.is_(False),
                    ReconciliationMatch.status != ReconStatus.EXCEPTION,
                )
                .order_by(ReconciliationMatch.confidence.desc())
            ).scalars()
        )
        items.append(
            {
                "transaction": {
                    "id": transaction.id,
                    "date": transaction.txn_date,
                    "description": transaction.description,
                    "reference": transaction.reference,
                    "counterparty": transaction.counterparty,
                    "amount": transaction.amount,
                    "direction": transaction.direction,
                    "status": transaction.recon_status,
                    "suggested_account_id": transaction.suggested_account_id,
                    "suggested_category": transaction.suggested_category,
                },
                "proposals": [
                    {
                        "id": proposal.id,
                        "journal_line_id": proposal.journal_line_id,
                        "amount": proposal.amount,
                        "confidence": proposal.confidence,
                        "strategy": proposal.strategy,
                        "reason": proposal.reason,
                        "evidence": proposal.evidence,
                        "proposed_by": proposal.proposed_by,
                        "document_type": proposal.document_type,
                        "document_id": proposal.document_id,
                    }
                    for proposal in proposals
                ],
            }
        )

    from app.services.banking import bank_book_balance

    book_balance = bank_book_balance(db, company_id, bank_account_id)
    return {
        "bank_account": {
            "id": bank_account.id,
            "name": bank_account.name,
            "bank_name": bank_account.bank_name,
            "masked_number": f"****{bank_account.account_number[-4:]}",
            "book_balance": book_balance,
            "statement_balance": bank_account.statement_balance,
            "difference": (
                money(bank_account.statement_balance - book_balance)
                if bank_account.statement_balance is not None
                else None
            ),
        },
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
    }
