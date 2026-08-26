"""The accounting engine's invariants — the tests that matter most.

If any test in this file fails, the product's core promise is broken.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.accounting.engine import AccountingEngine, EntryInput, LineInput
from app.accounting.ledger import trial_balance, verify_invariants
from app.accounting.periods import close_period, get_period, list_periods, reopen_period
from app.enums import JournalStatus, PeriodStatus
from app.errors import (
    AccountingError,
    ClosedPeriodError,
    ConflictError,
    ImmutableEntryError,
    UnbalancedJournalError,
    ValidationError,
)
from app.money import money


def simple_entry(entry_date: date, amount: str = "1000.00") -> EntryInput:
    return EntryInput(
        entry_date=entry_date,
        narration="Test entry",
        lines=[
            LineInput(account_code="1111", debit=Decimal(amount), description="Cash in"),
            LineInput(account_code="4110", credit=Decimal(amount), description="Sale"),
        ],
    )


# ------------------------------------------------------------ double entry --

def test_balanced_entry_posts(engine_for, today):
    entry = engine_for.create_and_post(simple_entry(today))
    assert entry.status == JournalStatus.POSTED
    assert entry.total_debit == entry.total_credit == money("1000.00")
    assert len(entry.lines) == 2
    assert entry.posted_at is not None


def test_unbalanced_entry_is_rejected(engine_for, today):
    with pytest.raises(UnbalancedJournalError) as exc:
        engine_for.create_and_post(
            EntryInput(
                entry_date=today,
                lines=[
                    LineInput(account_code="1111", debit=Decimal("100")),
                    LineInput(account_code="4110", credit=Decimal("90")),
                ],
            )
        )
    assert "out of balance by 10.00" in str(exc.value)


def test_single_line_entry_is_rejected(engine_for, today):
    with pytest.raises(AccountingError):
        engine_for.create_and_post(
            EntryInput(
                entry_date=today, lines=[LineInput(account_code="1111", debit=Decimal("100"))]
            )
        )


def test_line_cannot_be_both_debit_and_credit(engine_for, today):
    with pytest.raises(AccountingError) as exc:
        engine_for.create_and_post(
            EntryInput(
                entry_date=today,
                lines=[
                    LineInput(account_code="1111", debit=Decimal("100"), credit=Decimal("100")),
                    LineInput(account_code="4110", credit=Decimal("100")),
                ],
            )
        )
    assert "both a debit and a credit" in str(exc.value)


def test_negative_amounts_are_rejected(engine_for, today):
    with pytest.raises(AccountingError) as exc:
        engine_for.create_and_post(
            EntryInput(
                entry_date=today,
                lines=[
                    LineInput(account_code="1111", debit=Decimal("-100")),
                    LineInput(account_code="4110", credit=Decimal("-100")),
                ],
            )
        )
    assert "negative" in str(exc.value).lower()


def test_zero_amount_entry_is_rejected(engine_for, today):
    with pytest.raises(AccountingError):
        engine_for.create_and_post(
            EntryInput(
                entry_date=today,
                lines=[
                    LineInput(account_code="1111", debit=Decimal("0")),
                    LineInput(account_code="4110", credit=Decimal("0")),
                ],
            )
        )


def test_group_account_cannot_be_posted_to(engine_for, today):
    with pytest.raises(AccountingError) as exc:
        engine_for.create_and_post(
            EntryInput(
                entry_date=today,
                lines=[
                    LineInput(account_code="1000", debit=Decimal("100")),  # "Assets" group
                    LineInput(account_code="4110", credit=Decimal("100")),
                ],
            )
        )
    assert "group account" in str(exc.value)


def test_unknown_account_is_rejected(engine_for, today):
    with pytest.raises(AccountingError) as exc:
        engine_for.create_and_post(
            EntryInput(
                entry_date=today,
                lines=[
                    LineInput(account_code="9999", debit=Decimal("100")),
                    LineInput(account_code="4110", credit=Decimal("100")),
                ],
            )
        )
    assert "does not exist" in str(exc.value)


# ------------------------------------------------------------ immutability --

def test_posted_entry_cannot_be_posted_again(engine_for, today):
    entry = engine_for.create_and_post(simple_entry(today))
    with pytest.raises(ImmutableEntryError):
        engine_for.post(entry)


def test_posted_entry_cannot_be_voided(engine_for, today):
    entry = engine_for.create_and_post(simple_entry(today))
    with pytest.raises(ImmutableEntryError):
        engine_for.void_draft(entry, reason="changed my mind")


def test_guard_mutation_blocks_posted_entry(engine_for, today):
    entry = engine_for.create_and_post(simple_entry(today))
    with pytest.raises(ImmutableEntryError):
        engine_for.guard_mutation(entry)


def test_reversal_creates_mirror_and_links_both(engine_for, today):
    original = engine_for.create_and_post(simple_entry(today, "5000.00"))
    reversal = engine_for.reverse(original, reason="Booked to the wrong account")

    assert reversal.is_reversal is True
    assert reversal.reverses_entry_id == original.id
    assert original.reversed_by_entry_id == reversal.id
    assert original.status == JournalStatus.REVERSED

    # Every line is mirrored exactly.
    assert reversal.total_debit == original.total_debit
    original_pairs = {(line.account_id, line.debit, line.credit) for line in original.lines}
    reversal_pairs = {(line.account_id, line.credit, line.debit) for line in reversal.lines}
    assert original_pairs == reversal_pairs


def test_reversal_requires_a_reason(engine_for, today):
    entry = engine_for.create_and_post(simple_entry(today))
    with pytest.raises(ValidationError):
        engine_for.reverse(entry, reason="   ")


def test_entry_cannot_be_reversed_twice(engine_for, today):
    entry = engine_for.create_and_post(simple_entry(today))
    engine_for.reverse(entry, reason="First correction")
    with pytest.raises(ImmutableEntryError):
        engine_for.reverse(entry, reason="Second attempt")


def test_draft_entry_cannot_be_reversed(engine_for, today):
    draft = engine_for.create_draft(simple_entry(today))
    with pytest.raises(AccountingError):
        engine_for.reverse(draft, reason="Not posted yet")


def test_reversal_leaves_books_balanced(db, company, engine_for, today):
    engine_for.create_and_post(simple_entry(today, "7500.00"))
    entry = engine_for.create_and_post(simple_entry(today, "2500.00"))
    engine_for.reverse(entry, reason="Duplicate entry")

    result = trial_balance(db, company.id)
    assert result["balanced"] is True
    assert verify_invariants(db, company.id)["all_passed"] is True


# ----------------------------------------------------------------- drafts --

def test_draft_does_not_affect_the_ledger(db, company, engine_for, today):
    engine_for.create_draft(simple_entry(today, "9999.00"))
    result = trial_balance(db, company.id)
    # A draft contributes nothing: the trial balance is still empty.
    assert result["total_debit"] == money("0.00")


def test_draft_can_be_posted_later(engine_for, today):
    draft = engine_for.create_draft(simple_entry(today))
    assert draft.status == JournalStatus.DRAFT
    posted = engine_for.post(draft)
    assert posted.status == JournalStatus.POSTED
    assert all(line.status == JournalStatus.POSTED for line in posted.lines)


def test_void_draft_marks_it_void(engine_for, today):
    draft = engine_for.create_draft(simple_entry(today))
    voided = engine_for.void_draft(draft, reason="Entered by mistake")
    assert voided.status == JournalStatus.VOID


# ---------------------------------------------------------------- periods --

def test_cannot_post_into_a_closed_period(db, company, engine_for, today):
    period = get_period(db, company.id, today)
    close_period(db, company.id, period.id, user_id=None, force=True)

    with pytest.raises(ClosedPeriodError):
        engine_for.create_and_post(simple_entry(today))


def test_closed_period_blocks_but_reopening_restores(db, company, engine_for, today):
    period = get_period(db, company.id, today)
    close_period(db, company.id, period.id, user_id=None, force=True)
    reopen_period(db, company.id, period.id, user_id=None, reason="Late invoice received")

    entry = engine_for.create_and_post(simple_entry(today))
    assert entry.status == JournalStatus.POSTED


def test_reopen_requires_a_reason(db, company, today):
    period = get_period(db, company.id, today)
    close_period(db, company.id, period.id, user_id=None, force=True)
    with pytest.raises(ValidationError):
        reopen_period(db, company.id, period.id, user_id=None, reason="")


def test_close_is_blocked_by_unposted_entries(db, company, engine_for, today):
    engine_for.create_draft(simple_entry(today))
    period = get_period(db, company.id, today)

    with pytest.raises(ConflictError) as exc:
        close_period(db, company.id, period.id, user_id=None)
    assert "unposted" in str(exc.value).lower()


def test_period_lookup_covers_the_whole_month(db, company, today):
    period = get_period(db, company.id, today)
    assert period is not None
    assert period.start_date <= today <= period.end_date


# ------------------------------------------------------------- validation --

def test_validate_is_a_pure_dry_run(db, company, engine_for, today):
    from sqlalchemy import func, select

    from app.models import JournalEntry

    before = db.execute(
        select(func.count(JournalEntry.id)).where(JournalEntry.company_id == company.id)
    ).scalar_one()

    report = engine_for.validate(simple_entry(today))
    assert report.ok is True
    assert report.total_debit == report.total_credit

    after = db.execute(
        select(func.count(JournalEntry.id)).where(JournalEntry.company_id == company.id)
    ).scalar_one()
    assert before == after, "validate() must not write anything"


def test_validate_reports_every_problem_at_once(engine_for, today):
    report = engine_for.validate(
        EntryInput(
            entry_date=today,
            lines=[
                LineInput(account_code="1000", debit=Decimal("100")),  # group account
                LineInput(account_code="9999", credit=Decimal("90")),  # unknown + unbalanced
            ],
        )
    )
    assert report.ok is False
    assert len(report.errors) >= 3


def test_future_dated_entry_warns_but_posts(engine_for, today):
    from app.accounting.periods import ensure_fiscal_year

    future = today + timedelta(days=5)
    ensure_fiscal_year(engine_for.db, engine_for.company_id, future)
    report = engine_for.validate(simple_entry(future))
    assert report.ok is True
    assert any("future" in warning.lower() for warning in report.warnings)


# --------------------------------------------------------- entry numbering --

def test_entry_numbers_are_unique_and_sequential(engine_for, today):
    numbers = [engine_for.create_and_post(simple_entry(today)).entry_number for _ in range(5)]
    assert len(set(numbers)) == 5
    sequences = [int(number.rsplit("/", 1)[1]) for number in numbers]
    assert sequences == sorted(sequences)


def test_system_account_lookup_by_key(engine_for):
    account = engine_for.account_by_system_key("ACCOUNTS_RECEIVABLE")
    assert account.code == "1131"
    assert account.is_group is False


def test_missing_system_account_raises_clearly(engine_for):
    from app.errors import NotFoundError

    with pytest.raises(NotFoundError) as exc:
        engine_for.account_by_system_key("NOT_A_REAL_KEY")
    assert "not configured" in str(exc.value)
