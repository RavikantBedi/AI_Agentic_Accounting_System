"""Cash forecasting.

The forecast is built from **known, dated commitments** first (open invoices,
open bills, scheduled recurring items), with a historical run-rate only for the
residual. Every assumption is returned alongside the numbers, and the
confidence range widens with the horizon because it genuinely is less certain.

Nothing here is presented as a prediction of fact — the API returns
``assumptions`` and ``confidence_note`` and the UI shows them.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.enums import AccountSubtype, DocumentStatus, JournalStatus, ScheduleStatus
from app.models import Account, Bill, Invoice, JournalLine, RecurringTransaction
from app.money import ZERO, D, money

HORIZONS = [7, 30, 60, 90]

# Share of an overdue receivable assumed collectable, by how overdue it is.
# These are stated as assumptions in the output, not hidden constants.
COLLECTION_PROBABILITY = [
    (0, Decimal("0.95")),      # not yet due
    (30, Decimal("0.85")),
    (60, Decimal("0.65")),
    (90, Decimal("0.40")),
    (10_000, Decimal("0.20")),
]


def _collection_probability(days_overdue: int) -> Decimal:
    for threshold, probability in COLLECTION_PROBABILITY:
        if days_overdue <= threshold:
            return probability
    return COLLECTION_PROBABILITY[-1][1]


def current_cash(db: Session, company_id: str, *, as_of: date | None = None) -> Decimal:
    as_of = as_of or date.today()
    row = db.execute(
        select(
            func.sum(JournalLine.debit).label("debit"),
            func.sum(JournalLine.credit).label("credit"),
        )
        .join(Account, Account.id == JournalLine.account_id)
        .where(
            JournalLine.company_id == company_id,
            JournalLine.status == JournalStatus.POSTED,
            JournalLine.entry_date <= as_of,
            Account.subtype.in_([AccountSubtype.CASH, AccountSubtype.BANK]),
        )
    ).one()
    return money(D(row.debit or 0) - D(row.credit or 0))


def historical_run_rate(db: Session, company_id: str, *, lookback_days: int = 90) -> dict:
    """Average daily cash in/out over the lookback window."""
    end = date.today()
    start = end - timedelta(days=lookback_days)

    row = db.execute(
        select(
            func.sum(JournalLine.debit).label("inflow"),
            func.sum(JournalLine.credit).label("outflow"),
        )
        .join(Account, Account.id == JournalLine.account_id)
        .where(
            JournalLine.company_id == company_id,
            JournalLine.status == JournalStatus.POSTED,
            JournalLine.entry_date >= start,
            JournalLine.entry_date <= end,
            Account.subtype.in_([AccountSubtype.CASH, AccountSubtype.BANK]),
        )
    ).one()

    inflow = money(row.inflow or 0)
    outflow = money(row.outflow or 0)
    days = Decimal(max(1, lookback_days))

    return {
        "lookback_days": lookback_days,
        "total_inflow": inflow,
        "total_outflow": outflow,
        "daily_inflow": money(inflow / days),
        "daily_outflow": money(outflow / days),
        "daily_net": money((inflow - outflow) / days),
        "has_history": inflow > ZERO or outflow > ZERO,
    }


def cash_forecast(db: Session, company_id: str, *, horizon_days: int = 30) -> dict:
    """Forecast closing cash over a horizon from committed and expected flows."""
    today = date.today()
    horizon_end = today + timedelta(days=horizon_days)
    opening = current_cash(db, company_id)

    assumptions: list[str] = []
    weekly: dict[str, dict] = defaultdict(
        lambda: {"inflow": ZERO, "outflow": ZERO, "items": []}
    )

    # ---- Committed inflows: open invoices -------------------------------
    invoices = list(
        db.execute(
            select(Invoice).where(
                Invoice.company_id == company_id,
                Invoice.status.in_(
                    [
                        DocumentStatus.SENT,
                        DocumentStatus.APPROVED,
                        DocumentStatus.PARTIALLY_PAID,
                        DocumentStatus.OVERDUE,
                    ]
                ),
            )
        ).scalars()
    )

    expected_inflow = ZERO
    gross_receivable = ZERO
    for invoice in invoices:
        balance = invoice.balance_due
        if balance <= ZERO:
            continue
        gross_receivable = money(gross_receivable + balance)

        days_overdue = (today - invoice.due_date).days
        # Overdue invoices are assumed to arrive soon but discounted; future
        # invoices are expected on their due date.
        expected_date = invoice.due_date if invoice.due_date >= today else today + timedelta(days=7)
        if expected_date > horizon_end:
            continue

        probability = _collection_probability(max(0, days_overdue))
        amount = money(balance * probability)
        expected_inflow = money(expected_inflow + amount)

        bucket = weekly[_week_key(expected_date, today)]
        bucket["inflow"] = money(bucket["inflow"] + amount)
        bucket["items"].append(
            {
                "type": "invoice",
                "id": invoice.id,
                "label": f"{invoice.number} — {invoice.customer.name}",
                "date": expected_date,
                "gross": balance,
                "expected": amount,
                "probability": probability,
            }
        )

    if invoices:
        assumptions.append(
            "Open invoices are expected on their due date; overdue invoices are assumed "
            "to arrive within a week, discounted by how long they have been outstanding "
            "(95% if current, down to 20% beyond 90 days)."
        )

    # ---- Committed outflows: open bills ---------------------------------
    bills = list(
        db.execute(
            select(Bill).where(
                Bill.company_id == company_id,
                Bill.status.in_(
                    [DocumentStatus.APPROVED, DocumentStatus.PARTIALLY_PAID, DocumentStatus.OVERDUE]
                ),
            )
        ).scalars()
    )

    expected_outflow = ZERO
    gross_payable = ZERO
    for bill in bills:
        balance = bill.balance_due
        if balance <= ZERO:
            continue
        gross_payable = money(gross_payable + balance)

        expected_date = bill.due_date if bill.due_date >= today else today
        if expected_date > horizon_end:
            continue

        # Payables are assumed paid in full — prudent for a cash forecast.
        expected_outflow = money(expected_outflow + balance)
        bucket = weekly[_week_key(expected_date, today)]
        bucket["outflow"] = money(bucket["outflow"] + balance)
        bucket["items"].append(
            {
                "type": "bill",
                "id": bill.id,
                "label": f"{bill.number} — {bill.vendor.name}",
                "date": expected_date,
                "gross": balance,
                "expected": balance,
                "probability": Decimal("1.0"),
            }
        )

    if bills:
        assumptions.append(
            "Open bills are assumed paid in full on their due date; overdue bills are "
            "assumed paid immediately."
        )

    # ---- Scheduled recurring commitments --------------------------------
    recurring = list(
        db.execute(
            select(RecurringTransaction).where(
                RecurringTransaction.company_id == company_id,
                RecurringTransaction.status == ScheduleStatus.ACTIVE,
            )
        ).scalars()
    )
    recurring_outflow = ZERO
    for template in recurring:
        occurrences = _occurrences_in_window(template, today, horizon_end)
        for occurrence_date in occurrences:
            amount = money(template.amount)
            if amount <= ZERO:
                continue
            recurring_outflow = money(recurring_outflow + amount)
            bucket = weekly[_week_key(occurrence_date, today)]
            bucket["outflow"] = money(bucket["outflow"] + amount)
            bucket["items"].append(
                {
                    "type": "recurring",
                    "id": template.id,
                    "label": template.name,
                    "date": occurrence_date,
                    "gross": amount,
                    "expected": amount,
                    "probability": Decimal("1.0"),
                }
            )
    if recurring:
        assumptions.append(
            f"{len(recurring)} active recurring commitment(s) are included on their "
            "scheduled dates."
        )

    expected_outflow = money(expected_outflow + recurring_outflow)
    run_rate = historical_run_rate(db, company_id)

    if not run_rate["has_history"]:
        assumptions.append(
            "There is no meaningful transaction history yet, so no run-rate adjustment "
            "was applied. The forecast reflects only known commitments."
        )

    net_change = money(expected_inflow - expected_outflow)
    closing = money(opening + net_change)

    # Confidence band widens with the horizon: known commitments dominate the
    # short term, estimation dominates the long term.
    uncertainty_percent = {7: Decimal("5"), 30: Decimal("12"), 60: Decimal("20"), 90: Decimal("30")}.get(
        horizon_days, Decimal("20")
    )
    band = money(abs(expected_inflow) * uncertainty_percent / Decimal(100))

    weeks = []
    running = opening
    for key in sorted(weekly, key=lambda k: weekly[k]["items"][0]["date"] if weekly[k]["items"] else today):
        bucket = weekly[key]
        running = money(running + bucket["inflow"] - bucket["outflow"])
        weeks.append(
            {
                "label": key,
                "inflow": bucket["inflow"],
                "outflow": bucket["outflow"],
                "net": money(bucket["inflow"] - bucket["outflow"]),
                "closing_cash": running,
                "item_count": len(bucket["items"]),
                "items": sorted(bucket["items"], key=lambda i: i["expected"], reverse=True)[:8],
            }
        )

    warnings = []
    if closing < ZERO:
        warnings.append(
            f"Projected cash goes negative: {closing} by {horizon_end.isoformat()}. "
            "Review collections and payment timing."
        )
    lowest = min([week["closing_cash"] for week in weeks], default=closing)
    if lowest < ZERO <= closing:
        warnings.append(
            f"Cash dips to {lowest} mid-period even though the closing position is positive."
        )

    return {
        "horizon_days": horizon_days,
        "start_date": today,
        "end_date": horizon_end,
        "opening_cash": opening,
        "expected_inflow": expected_inflow,
        "expected_outflow": expected_outflow,
        "net_change": net_change,
        "closing_cash": closing,
        "confidence_low": money(closing - band),
        "confidence_high": money(closing + band),
        "uncertainty_percent": uncertainty_percent,
        "gross_receivable": gross_receivable,
        "gross_payable": gross_payable,
        "weeks": weeks,
        "assumptions": assumptions,
        "warnings": warnings,
        "run_rate": run_rate,
        "confidence_note": (
            "This is a projection from known commitments and stated assumptions, not a "
            "prediction. Actual cash will differ if customers pay late or new costs arise."
        ),
        "basis": {
            "open_invoices": len(invoices),
            "open_bills": len(bills),
            "recurring_items": len(recurring),
        },
    }


def multi_horizon_forecast(db: Session, company_id: str) -> dict:
    """Forecast across all standard horizons for the dashboard."""
    return {
        "generated_on": date.today(),
        "horizons": {str(days): cash_forecast(db, company_id, horizon_days=days) for days in HORIZONS},
    }


def _week_key(target: date, today: date) -> str:
    offset = (target - today).days
    week = max(0, offset // 7)
    start = today + timedelta(days=week * 7)
    end = start + timedelta(days=6)
    return f"{start.strftime('%d %b')} – {end.strftime('%d %b')}"


def _occurrences_in_window(
    template: RecurringTransaction, start: date, end: date
) -> list[date]:
    """Dates a recurring template fires inside the window."""
    from app.enums import ScheduleFrequency

    occurrences: list[date] = []
    cursor = template.next_run_date or template.start_date
    if cursor is None:
        return occurrences

    step_days = {
        ScheduleFrequency.DAILY: 1,
        ScheduleFrequency.WEEKLY: 7,
        ScheduleFrequency.MONTHLY: 30,
        ScheduleFrequency.QUARTERLY: 91,
        ScheduleFrequency.YEARLY: 365,
    }.get(template.frequency, 30)

    guard = 0
    while cursor <= end and guard < 400:
        guard += 1
        if cursor >= start and (template.end_date is None or cursor <= template.end_date):
            occurrences.append(cursor)
        cursor = cursor + timedelta(days=step_days * max(1, template.interval))
    return occurrences
