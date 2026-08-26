"""The double-entry accounting engine.

This is the *only* code path that may write to ``journal_entries`` /
``journal_lines``. Everything else — the API, the importers, every AI agent —
must come through ``AccountingEngine``.

Invariants enforced on every posting, without exception:

1.  Total debits equal total credits, to the paisa.
2.  A line carries a debit or a credit, never both, never negative.
3.  Accounts exist, are active, belong to this company, and are postable
    (group/header accounts are rejected).
4.  The target period exists and is OPEN.
5.  A POSTED entry is immutable — corrections happen by reversal.
6.  An entry has at least two lines and a non-zero total.

``validate()`` is a pure dry-run used by the AI layer to prove a proposal would
post cleanly *before* a human is asked to approve it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import (
    ActorType,
    AuditAction,
    EntrySource,
    JournalStatus,
    PeriodStatus,
    VoucherType,
)
from app.errors import (
    AccountingError,
    ClosedPeriodError,
    ImmutableEntryError,
    NotFoundError,
    UnbalancedJournalError,
    ValidationError,
)
from app.models import Account, AccountingPeriod, JournalEntry, JournalLine
from app.models.base import utcnow
from app.money import ZERO, money

MAX_LINES_PER_ENTRY = 500


@dataclass
class LineInput:
    """One proposed ledger line. Accepts an account id *or* a code."""

    account_id: str | None = None
    account_code: str | None = None
    debit: Decimal = ZERO
    credit: Decimal = ZERO
    description: str = ""

    cost_center_id: str | None = None
    department_id: str | None = None
    branch_id: str | None = None
    project: str = ""

    party_type: str | None = None
    party_id: str | None = None
    bank_account_id: str | None = None
    item_id: str | None = None

    tax_component: str | None = None
    tax_rate: Decimal | None = None
    taxable_value: Decimal | None = None

    currency: str = "INR"
    fx_rate: Decimal = Decimal("1.00")
    fc_amount: Decimal | None = None

    def __post_init__(self) -> None:
        self.debit = money(self.debit)
        self.credit = money(self.credit)


@dataclass
class EntryInput:
    """A proposed journal entry, before validation."""

    entry_date: date
    lines: list[LineInput]
    voucher_type: str = VoucherType.JOURNAL
    narration: str = ""
    reference: str = ""
    currency: str = "INR"
    branch_id: str | None = None
    source: str = EntrySource.MANUAL
    source_type: str | None = None
    source_id: str | None = None
    ai_proposal_id: str | None = None
    ai_confidence: Decimal | None = None
    is_adjustment: bool = False
    is_opening: bool = False
    entry_number: str | None = None


@dataclass
class ValidationReport:
    """Result of a dry-run. ``ok`` means posting is guaranteed to succeed."""

    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    total_debit: Decimal = ZERO
    total_credit: Decimal = ZERO
    difference: Decimal = ZERO
    period_id: str | None = None
    resolved_accounts: dict[str, str] = field(default_factory=dict)

    def raise_if_invalid(self) -> None:
        """Raise the most specific error the failures justify.

        An out-of-balance entry is a different problem from a bad account or a
        closed period, and callers (and the UI) act on the distinction.
        """
        if self.ok:
            return

        details = {
            "errors": self.errors,
            "warnings": self.warnings,
            "total_debit": str(self.total_debit),
            "total_credit": str(self.total_credit),
            "difference": str(self.difference),
        }
        message = "Journal entry failed validation: " + "; ".join(self.errors)

        if self.difference != ZERO:
            raise UnbalancedJournalError(message, details=details)
        if any("is CLOSED" in e or "is CLOSING" in e for e in self.errors):
            raise ClosedPeriodError(message, details=details)
        raise AccountingError(message, details=details)


class AccountingEngine:
    """Transactional posting engine scoped to one company."""

    def __init__(self, db: Session, company_id: str, *, actor_user_id: str | None = None):
        self.db = db
        self.company_id = company_id
        self.actor_user_id = actor_user_id
        self._account_cache: dict[str, Account] = {}

    # ------------------------------------------------------------ lookups --

    def get_account(self, *, account_id: str | None = None, code: str | None = None) -> Account | None:
        cache_key = account_id or f"code:{code}"
        if cache_key in self._account_cache:
            return self._account_cache[cache_key]

        stmt = select(Account).where(Account.company_id == self.company_id)
        if account_id:
            stmt = stmt.where(Account.id == account_id)
        elif code:
            stmt = stmt.where(Account.code == str(code))
        else:
            return None

        account = self.db.execute(stmt).scalar_one_or_none()
        if account is not None:
            self._account_cache[cache_key] = account
        return account

    def account_by_system_key(self, system_key: str) -> Account:
        account = self.db.execute(
            select(Account).where(
                Account.company_id == self.company_id, Account.system_key == system_key
            )
        ).scalar_one_or_none()
        if account is None:
            raise NotFoundError(
                f"System account '{system_key}' is not configured for this company.",
                code="system_account_missing",
            )
        return account

    def find_period(self, on_date: date) -> AccountingPeriod | None:
        return self.db.execute(
            select(AccountingPeriod).where(
                AccountingPeriod.company_id == self.company_id,
                AccountingPeriod.start_date <= on_date,
                AccountingPeriod.end_date >= on_date,
            )
        ).scalar_one_or_none()

    # --------------------------------------------------------- validation --

    def validate(self, entry: EntryInput, *, allow_closed_period: bool = False) -> ValidationReport:
        """Dry-run. Never touches the database beyond reads."""
        errors: list[str] = []
        warnings: list[str] = []
        resolved: dict[str, str] = {}

        if not entry.lines:
            errors.append("Entry has no lines.")
        elif len(entry.lines) < 2:
            errors.append("A double-entry journal needs at least two lines.")
        elif len(entry.lines) > MAX_LINES_PER_ENTRY:
            errors.append(f"Entry exceeds the {MAX_LINES_PER_ENTRY}-line limit.")

        total_debit = ZERO
        total_credit = ZERO

        for index, line in enumerate(entry.lines, start=1):
            label = f"Line {index}"
            debit = money(line.debit)
            credit = money(line.credit)

            if debit < 0 or credit < 0:
                errors.append(f"{label}: negative amounts are not allowed (use the opposite side).")
            if debit > 0 and credit > 0:
                errors.append(f"{label}: a line cannot be both a debit and a credit.")
            if debit == 0 and credit == 0:
                errors.append(f"{label}: amount is zero.")

            account = self.get_account(account_id=line.account_id, code=line.account_code)
            if account is None:
                errors.append(
                    f"{label}: account "
                    f"{line.account_code or line.account_id or '(missing)'} does not exist."
                )
            else:
                if account.company_id != self.company_id:
                    errors.append(f"{label}: account {account.code} belongs to another company.")
                if account.is_group:
                    errors.append(
                        f"{label}: '{account.code} {account.name}' is a group account and "
                        "cannot be posted to. Choose a leaf account."
                    )
                if not account.is_active:
                    errors.append(f"{label}: account '{account.code} {account.name}' is inactive.")
                resolved[label] = account.id

            total_debit += debit
            total_credit += credit

        total_debit = money(total_debit)
        total_credit = money(total_credit)
        difference = money(total_debit - total_credit)

        if difference != ZERO:
            errors.append(
                f"Entry is out of balance by {difference}: "
                f"debits {total_debit} vs credits {total_credit}."
            )
        if total_debit == ZERO and total_credit == ZERO and entry.lines:
            errors.append("Entry total is zero.")

        # Period checks
        period = self.find_period(entry.entry_date)
        period_id = period.id if period else None
        if period is None:
            errors.append(
                f"No accounting period covers {entry.entry_date.isoformat()}. "
                "Create the fiscal year before posting."
            )
        elif period.status != PeriodStatus.OPEN and not allow_closed_period:
            errors.append(
                f"Period '{period.name}' is {period.status}. "
                "Post an adjustment into an open period instead."
            )
        elif period.status == PeriodStatus.CLOSING:
            warnings.append(f"Period '{period.name}' is being closed — expect extra review.")

        if entry.entry_date > date.today():
            warnings.append("Entry is dated in the future.")

        return ValidationReport(
            ok=not errors,
            errors=errors,
            warnings=warnings,
            total_debit=total_debit,
            total_credit=total_credit,
            difference=difference,
            period_id=period_id,
            resolved_accounts=resolved,
        )

    # ------------------------------------------------------------ writing --

    def create_draft(self, entry: EntryInput) -> JournalEntry:
        """Persist a DRAFT entry after validating it. Drafts do not affect books."""
        report = self.validate(entry)
        report.raise_if_invalid()
        return self._materialise(entry, report, status=JournalStatus.DRAFT)

    def create_and_post(self, entry: EntryInput, *, allow_closed_period: bool = False) -> JournalEntry:
        """Validate, persist and post in one transaction — the common path."""
        report = self.validate(entry, allow_closed_period=allow_closed_period)
        report.raise_if_invalid()
        journal = self._materialise(entry, report, status=JournalStatus.DRAFT)
        return self.post(journal, allow_closed_period=allow_closed_period)

    def _materialise(
        self, entry: EntryInput, report: ValidationReport, *, status: str
    ) -> JournalEntry:
        from app.services.numbering import next_number  # local import: avoids cycle

        number = entry.entry_number or next_number(
            self.db, self.company_id, f"JOURNAL_{entry.voucher_type}", entry.entry_date
        )

        journal = JournalEntry(
            company_id=self.company_id,
            entry_number=number,
            voucher_type=entry.voucher_type,
            entry_date=entry.entry_date,
            period_id=report.period_id,
            branch_id=entry.branch_id,
            narration=entry.narration,
            reference=entry.reference,
            currency=entry.currency,
            total_debit=report.total_debit,
            total_credit=report.total_credit,
            status=status,
            source=entry.source,
            source_type=entry.source_type,
            source_id=entry.source_id,
            ai_proposal_id=entry.ai_proposal_id,
            ai_confidence=entry.ai_confidence,
            created_by_user_id=self.actor_user_id,
            is_adjustment=entry.is_adjustment,
            is_opening=entry.is_opening,
        )
        self.db.add(journal)
        self.db.flush()

        for index, line in enumerate(entry.lines, start=1):
            account = self.get_account(account_id=line.account_id, code=line.account_code)
            assert account is not None  # guaranteed by validate()
            self.db.add(
                JournalLine(
                    company_id=self.company_id,
                    entry_id=journal.id,
                    line_number=index,
                    account_id=account.id,
                    debit=money(line.debit),
                    credit=money(line.credit),
                    description=line.description or entry.narration,
                    entry_date=entry.entry_date,
                    status=status,
                    branch_id=line.branch_id or entry.branch_id,
                    cost_center_id=line.cost_center_id,
                    department_id=line.department_id,
                    project=line.project,
                    party_type=line.party_type,
                    party_id=line.party_id,
                    bank_account_id=line.bank_account_id,
                    item_id=line.item_id,
                    tax_component=line.tax_component,
                    tax_rate=line.tax_rate,
                    taxable_value=line.taxable_value,
                    currency=line.currency or entry.currency,
                    fx_rate=line.fx_rate,
                    fc_amount=line.fc_amount,
                )
            )
        self.db.flush()
        return journal

    def post(self, journal: JournalEntry | str, *, allow_closed_period: bool = False) -> JournalEntry:
        """Move a DRAFT entry to POSTED. Re-verifies every invariant."""
        journal = self._resolve_entry(journal)

        if journal.status == JournalStatus.POSTED:
            raise ImmutableEntryError(f"Entry {journal.entry_number} is already posted.")
        if journal.status in (JournalStatus.REVERSED, JournalStatus.VOID):
            raise ImmutableEntryError(
                f"Entry {journal.entry_number} is {journal.status} and cannot be posted."
            )

        lines = list(journal.lines)
        if len(lines) < 2:
            raise AccountingError("A journal must have at least two lines to post.")

        total_debit = money(sum((line.debit for line in lines), ZERO))
        total_credit = money(sum((line.credit for line in lines), ZERO))
        if total_debit != total_credit:
            raise UnbalancedJournalError(
                f"Cannot post {journal.entry_number}: debits {total_debit} != credits {total_credit}.",
                details={"difference": str(money(total_debit - total_credit))},
            )
        if total_debit == ZERO:
            raise AccountingError(f"Cannot post {journal.entry_number}: total is zero.")

        period = self.db.get(AccountingPeriod, journal.period_id)
        if period is None:
            raise AccountingError("Entry is not linked to an accounting period.")
        if period.status != PeriodStatus.OPEN and not allow_closed_period:
            raise ClosedPeriodError(
                f"Period '{period.name}' is {period.status}; entries cannot be posted into it.",
                details={"period": period.name, "status": period.status},
            )

        for line in lines:
            account = self.db.get(Account, line.account_id)
            if account is None or account.company_id != self.company_id:
                raise AccountingError(f"Line {line.line_number}: invalid account.")
            if account.is_group:
                raise AccountingError(
                    f"Line {line.line_number}: '{account.code}' is a group account."
                )
            if not account.is_active:
                raise AccountingError(f"Line {line.line_number}: account '{account.code}' is inactive.")

        journal.total_debit = total_debit
        journal.total_credit = total_credit
        journal.status = JournalStatus.POSTED
        journal.posted_at = utcnow()
        journal.posted_by_user_id = self.actor_user_id
        for line in lines:
            line.status = JournalStatus.POSTED
            line.entry_date = journal.entry_date

        self.db.flush()
        self._audit(
            journal,
            AuditAction.POST,
            summary=f"Posted {journal.voucher_type} {journal.entry_number} for {total_debit}",
        )
        return journal

    def reverse(
        self,
        journal: JournalEntry | str,
        *,
        reversal_date: date | None = None,
        reason: str,
        allow_closed_period: bool = False,
    ) -> JournalEntry:
        """Create and post the mirror image of a posted entry.

        This is the *only* way to correct posted books. The original row is
        never edited; both entries stay visible and linked forever.
        """
        original = self._resolve_entry(journal)

        # Check "already reversed" first: it is the more specific situation and
        # produces a far more useful message than "entry is REVERSED".
        if original.reversed_by_entry_id:
            reversal = self.db.get(JournalEntry, original.reversed_by_entry_id)
            raise ImmutableEntryError(
                f"Entry {original.entry_number} was already reversed by "
                f"{reversal.entry_number if reversal else original.reversed_by_entry_id}."
            )
        if original.status != JournalStatus.POSTED:
            raise AccountingError(
                f"Only posted entries can be reversed (entry {original.entry_number} "
                f"is {original.status})."
            )
        if not reason or not reason.strip():
            raise ValidationError("A reversal requires a reason — it becomes part of the audit trail.")

        target_date = reversal_date or date.today()
        period = self.find_period(target_date)
        if period is None:
            raise AccountingError(f"No accounting period covers {target_date.isoformat()}.")
        if period.status != PeriodStatus.OPEN and not allow_closed_period:
            raise ClosedPeriodError(
                f"Cannot reverse into '{period.name}' because it is {period.status}."
            )

        mirrored = [
            LineInput(
                account_id=line.account_id,
                debit=line.credit,
                credit=line.debit,
                description=f"Reversal: {line.description}"[:500],
                cost_center_id=line.cost_center_id,
                department_id=line.department_id,
                branch_id=line.branch_id,
                project=line.project,
                party_type=line.party_type,
                party_id=line.party_id,
                bank_account_id=line.bank_account_id,
                item_id=line.item_id,
                tax_component=line.tax_component,
                tax_rate=line.tax_rate,
                taxable_value=(-line.taxable_value if line.taxable_value is not None else None),
                currency=line.currency,
                fx_rate=line.fx_rate,
            )
            for line in original.lines
        ]

        reversal_input = EntryInput(
            entry_date=target_date,
            lines=mirrored,
            voucher_type=original.voucher_type,
            narration=f"Reversal of {original.entry_number}: {reason}",
            reference=original.reference,
            currency=original.currency,
            branch_id=original.branch_id,
            source=EntrySource.SYSTEM,
            source_type=original.source_type,
            source_id=original.source_id,
            is_adjustment=True,
        )
        report = self.validate(reversal_input, allow_closed_period=allow_closed_period)
        report.raise_if_invalid()

        reversal = self._materialise(reversal_input, report, status=JournalStatus.DRAFT)
        reversal.is_reversal = True
        reversal.reverses_entry_id = original.id
        self.db.flush()

        self.post(reversal, allow_closed_period=allow_closed_period)

        original.reversed_by_entry_id = reversal.id
        # The *header* is marked REVERSED so the UI and workflows know its
        # business state. Its LINES deliberately stay POSTED: a reversal works
        # by adding an equal and opposite entry, so both must remain in the
        # ledger and cancel each other out. Un-posting the original would
        # remove one side and leave the reversal standing alone — the balance
        # would swing to the opposite sign instead of returning to zero.
        original.status = JournalStatus.REVERSED
        self.db.flush()

        self._audit(
            original,
            AuditAction.REVERSE,
            summary=f"Reversed {original.entry_number} via {reversal.entry_number}",
            reason=reason,
        )
        return reversal

    def void_draft(self, journal: JournalEntry | str, *, reason: str) -> JournalEntry:
        """Void a DRAFT entry. Posted entries can never be voided."""
        entry = self._resolve_entry(journal)
        if entry.status == JournalStatus.POSTED:
            raise ImmutableEntryError(
                f"Entry {entry.entry_number} is posted and cannot be voided. Reverse it instead."
            )
        entry.status = JournalStatus.VOID
        entry.narration = f"{entry.narration} [VOID: {reason}]".strip()
        for line in entry.lines:
            line.status = JournalStatus.VOID
        self.db.flush()
        self._audit(entry, AuditAction.DELETE, summary=f"Voided draft {entry.entry_number}", reason=reason)
        return entry

    def guard_mutation(self, journal: JournalEntry) -> None:
        """Raise if caller is about to mutate a posted entry."""
        if journal.status == JournalStatus.POSTED:
            raise ImmutableEntryError(
                f"Entry {journal.entry_number} is posted and immutable. "
                "Create a reversal and a corrected entry instead."
            )

    # ----------------------------------------------------------- helpers --

    def _resolve_entry(self, journal: JournalEntry | str) -> JournalEntry:
        if isinstance(journal, str):
            found = self.db.get(JournalEntry, journal)
            if found is None or found.company_id != self.company_id:
                raise NotFoundError(f"Journal entry {journal} not found.")
            return found
        return journal

    def _audit(
        self, journal: JournalEntry, action: str, *, summary: str, reason: str = ""
    ) -> None:
        from app.services.audit import record_event  # local import: avoids cycle

        record_event(
            self.db,
            company_id=self.company_id,
            action=action,
            entity_type="JournalEntry",
            entity_id=journal.id,
            entity_label=journal.entry_number,
            summary=summary,
            reason=reason,
            actor_type=ActorType.USER if self.actor_user_id else ActorType.SYSTEM,
            actor_id=self.actor_user_id,
            amount=journal.total_debit,
            after={
                "entry_number": journal.entry_number,
                "status": journal.status,
                "entry_date": journal.entry_date.isoformat(),
                "total_debit": str(journal.total_debit),
                "total_credit": str(journal.total_credit),
                "voucher_type": journal.voucher_type,
            },
            proposal_id=journal.ai_proposal_id,
        )


def balanced(lines: Iterable[LineInput]) -> bool:
    """Convenience predicate used by tests and the AI validation layer."""
    debit = money(sum((money(line.debit) for line in lines), ZERO))
    credit = money(sum((money(line.credit) for line in lines), ZERO))
    return debit == credit and debit > ZERO


def sum_sides(lines: Sequence[LineInput]) -> tuple[Decimal, Decimal]:
    return (
        money(sum((money(line.debit) for line in lines), ZERO)),
        money(sum((money(line.credit) for line in lines), ZERO)),
    )
