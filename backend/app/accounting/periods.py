"""Fiscal calendar: year/period creation, guards and the closing process.

Closing a period is a *controlled* operation: it re-verifies the accounting
invariants, refuses to close over blocking issues, and records an audit event.
Re-opening is possible but always audited and permission-gated.
"""
from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.enums import ActorType, AuditAction, JournalStatus, PeriodStatus
from app.errors import ClosedPeriodError, ConflictError, NotFoundError, ValidationError
from app.models import AccountingPeriod, Company, FiscalYear, JournalEntry, JournalLine
from app.models.base import utcnow
from app.money import ZERO, money

MONTH_ABBR = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def fiscal_year_bounds(company: Company, on_date: date) -> tuple[date, date, str]:
    """Return (start, end, label) of the fiscal year containing ``on_date``."""
    start_month = company.fiscal_year_start_month or 4
    start_day = company.fiscal_year_start_day or 1

    start_year = on_date.year
    if (on_date.month, on_date.day) < (start_month, start_day):
        start_year -= 1

    start = date(start_year, start_month, start_day)
    if start_month == 1 and start_day == 1:
        end = date(start_year, 12, 31)
        label = str(start_year)
    else:
        end_year = start_year + 1
        end_month = start_month - 1 if start_month > 1 else 12
        end_day = calendar.monthrange(end_year, end_month)[1]
        end = date(end_year, end_month, end_day)
        label = f"{start_year}-{str(end_year % 100).zfill(2)}"
    return start, end, label


def ensure_fiscal_year(db: Session, company_id: str, on_date: date) -> FiscalYear:
    """Create the fiscal year containing ``on_date`` (with its months). Idempotent."""
    company = db.get(Company, company_id)
    if company is None:
        raise NotFoundError("Company not found.")

    start, end, label = fiscal_year_bounds(company, on_date)

    existing = db.execute(
        select(FiscalYear).where(FiscalYear.company_id == company_id, FiscalYear.name == label)
    ).scalar_one_or_none()
    if existing:
        _ensure_periods(db, company_id, existing)
        return existing

    fiscal_year = FiscalYear(
        company_id=company_id, name=label, start_date=start, end_date=end, status=PeriodStatus.OPEN
    )
    db.add(fiscal_year)
    db.flush()
    _ensure_periods(db, company_id, fiscal_year)
    return fiscal_year


def _ensure_periods(db: Session, company_id: str, fiscal_year: FiscalYear) -> list[AccountingPeriod]:
    """Create one AccountingPeriod per calendar month inside the fiscal year."""
    existing = {
        (period.year, period.month): period
        for period in db.execute(
            select(AccountingPeriod).where(
                AccountingPeriod.company_id == company_id,
                AccountingPeriod.fiscal_year_id == fiscal_year.id,
            )
        ).scalars()
    }

    periods: list[AccountingPeriod] = []
    cursor = fiscal_year.start_date.replace(day=1)
    while cursor <= fiscal_year.end_date:
        year, month = cursor.year, cursor.month
        if (year, month) in existing:
            periods.append(existing[(year, month)])
        else:
            last_day = calendar.monthrange(year, month)[1]
            start = max(date(year, month, 1), fiscal_year.start_date)
            end = min(date(year, month, last_day), fiscal_year.end_date)
            period = AccountingPeriod(
                company_id=company_id,
                fiscal_year_id=fiscal_year.id,
                name=f"{MONTH_ABBR[month]} {year}",
                year=year,
                month=month,
                start_date=start,
                end_date=end,
                status=PeriodStatus.OPEN,
            )
            db.add(period)
            periods.append(period)

        cursor = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)

    db.flush()
    return periods


def get_period(db: Session, company_id: str, on_date: date) -> AccountingPeriod | None:
    return db.execute(
        select(AccountingPeriod).where(
            AccountingPeriod.company_id == company_id,
            AccountingPeriod.start_date <= on_date,
            AccountingPeriod.end_date >= on_date,
        )
    ).scalar_one_or_none()


def require_open_period(db: Session, company_id: str, on_date: date) -> AccountingPeriod:
    """Guard used by every service that posts to the ledger."""
    period = get_period(db, company_id, on_date)
    if period is None:
        raise ValidationError(
            f"No accounting period covers {on_date.isoformat()}.", code="period_missing"
        )
    if period.status != PeriodStatus.OPEN:
        raise ClosedPeriodError(
            f"Period '{period.name}' is {period.status}; it cannot receive new entries.",
            details={"period_id": period.id, "period": period.name, "status": period.status},
        )
    return period


def period_checks(db: Session, company_id: str, period: AccountingPeriod) -> dict:
    """Pre-close verification: everything that must be true to close cleanly."""
    draft_count = int(
        db.execute(
            select(func.count(JournalEntry.id)).where(
                JournalEntry.company_id == company_id,
                JournalEntry.period_id == period.id,
                JournalEntry.status.in_([JournalStatus.DRAFT, JournalStatus.PENDING_APPROVAL]),
            )
        ).scalar_one()
    )

    totals = db.execute(
        select(func.count(JournalEntry.id)).where(
            JournalEntry.company_id == company_id,
            JournalEntry.period_id == period.id,
            JournalEntry.status == JournalStatus.POSTED,
        )
    ).scalar_one()

    lines = list(
        db.execute(
            select(JournalLine.debit, JournalLine.credit).where(
                JournalLine.company_id == company_id,
                JournalLine.status == JournalStatus.POSTED,
                JournalLine.entry_date >= period.start_date,
                JournalLine.entry_date <= period.end_date,
            )
        )
    )
    total_debit = money(sum((row[0] for row in lines), ZERO))
    total_credit = money(sum((row[1] for row in lines), ZERO))
    difference = money(total_debit - total_credit)

    blocking: list[str] = []
    if draft_count:
        blocking.append(
            f"{draft_count} unposted entr{'y' if draft_count == 1 else 'ies'} in this period."
        )
    if difference != ZERO:
        blocking.append(f"Period debits and credits differ by {difference}.")

    return {
        "period_id": period.id,
        "period": period.name,
        "posted_entries": int(totals),
        "draft_entries": draft_count,
        "total_debit": total_debit,
        "total_credit": total_credit,
        "difference": difference,
        "balanced": difference == ZERO,
        "blocking_issues": blocking,
        "can_close": not blocking,
    }


def begin_close(db: Session, company_id: str, period_id: str, *, user_id: str | None) -> AccountingPeriod:
    """Move a period into CLOSING — a soft freeze while the checklist runs."""
    period = _get(db, company_id, period_id)
    if period.status == PeriodStatus.CLOSED:
        raise ConflictError(f"Period '{period.name}' is already closed.")
    period.status = PeriodStatus.CLOSING
    db.flush()
    _audit(db, company_id, period, AuditAction.UPDATE, f"Started close for {period.name}", user_id)
    return period


def close_period(
    db: Session,
    company_id: str,
    period_id: str,
    *,
    user_id: str | None,
    notes: str = "",
    force: bool = False,
) -> AccountingPeriod:
    """Close a period after verifying it. ``force`` still records why."""
    period = _get(db, company_id, period_id)
    if period.status == PeriodStatus.CLOSED:
        raise ConflictError(f"Period '{period.name}' is already closed.")

    checks = period_checks(db, company_id, period)
    if not checks["can_close"] and not force:
        # State the reasons in the message itself: an error a user can act on
        # without having to inspect a details payload.
        raise ConflictError(
            f"Period '{period.name}' cannot be closed yet. "
            + " ".join(checks["blocking_issues"]),
            details={"blocking_issues": checks["blocking_issues"]},
            code="period_close_blocked",
        )

    # Earlier periods must be closed first — otherwise history stays mutable.
    earlier_open = db.execute(
        select(AccountingPeriod).where(
            AccountingPeriod.company_id == company_id,
            AccountingPeriod.end_date < period.start_date,
            AccountingPeriod.status != PeriodStatus.CLOSED,
        )
    ).scalars().first()
    if earlier_open and not force:
        raise ConflictError(
            f"Close '{earlier_open.name}' before closing '{period.name}'.",
            code="earlier_period_open",
        )

    period.status = PeriodStatus.CLOSED
    period.closed_at = utcnow()
    period.closed_by_user_id = user_id
    period.close_notes = notes
    db.flush()

    _audit(
        db,
        company_id,
        period,
        AuditAction.CLOSE_PERIOD,
        f"Closed {period.name}"
        + (" (forced)" if force and not checks["can_close"] else ""),
        user_id,
        extra={
            "checks": {k: str(v) for k, v in checks.items() if not isinstance(v, list)},
            "blocking_issues": checks["blocking_issues"],
            "forced": force,
        },
    )
    return period


def reopen_period(
    db: Session, company_id: str, period_id: str, *, user_id: str | None, reason: str
) -> AccountingPeriod:
    """Re-open a closed period. Requires a reason and is always audited."""
    if not reason or not reason.strip():
        raise ValidationError("Re-opening a closed period requires a reason.")

    period = _get(db, company_id, period_id)
    if period.status != PeriodStatus.CLOSED:
        raise ConflictError(f"Period '{period.name}' is not closed.")

    later_closed = db.execute(
        select(AccountingPeriod).where(
            AccountingPeriod.company_id == company_id,
            AccountingPeriod.start_date > period.end_date,
            AccountingPeriod.status == PeriodStatus.CLOSED,
        )
    ).scalars().first()
    if later_closed:
        raise ConflictError(
            f"Re-open '{later_closed.name}' first — periods must re-open newest to oldest.",
            code="later_period_closed",
        )

    period.status = PeriodStatus.OPEN
    period.closed_at = None
    period.closed_by_user_id = None
    db.flush()
    _audit(db, company_id, period, AuditAction.REOPEN_PERIOD, f"Re-opened {period.name}", user_id, reason=reason)
    return period


def list_periods(db: Session, company_id: str) -> list[AccountingPeriod]:
    return list(
        db.execute(
            select(AccountingPeriod)
            .where(AccountingPeriod.company_id == company_id)
            .order_by(AccountingPeriod.start_date.asc())
        ).scalars()
    )


def _get(db: Session, company_id: str, period_id: str) -> AccountingPeriod:
    period = db.get(AccountingPeriod, period_id)
    if period is None or period.company_id != company_id:
        raise NotFoundError("Accounting period not found.")
    return period


def _audit(
    db: Session,
    company_id: str,
    period: AccountingPeriod,
    action: str,
    summary: str,
    user_id: str | None,
    reason: str = "",
    extra: dict | None = None,
) -> None:
    from app.services.audit import record_event

    record_event(
        db,
        company_id=company_id,
        action=action,
        entity_type="AccountingPeriod",
        entity_id=period.id,
        entity_label=period.name,
        summary=summary,
        reason=reason,
        actor_type=ActorType.USER if user_id else ActorType.SYSTEM,
        actor_id=user_id,
        after={"status": period.status, **(extra or {})},
    )
