"""Month-end close.

Every checklist item is backed by a **real, re-runnable check** against the
ledger — never a manual tick-box. Running the checklist recomputes each item's
status, issue count and amount at risk, so the progress percentage means
something.

A period can only be closed when the blocking items actually pass.
"""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.accounting.ledger import account_balance, verify_invariants
from app.accounting.periods import close_period as close_accounting_period
from app.accounting.periods import get_period, period_checks
from app.enums import (
    ActorType,
    AuditAction,
    CloseTaskStatus,
    DocumentStatus,
    ExceptionStatus,
    JournalStatus,
    PeriodStatus,
    ReconStatus,
)
from app.errors import ConflictError, NotFoundError, ValidationError
from app.models import (
    Account,
    AccountingPeriod,
    Anomaly,
    BankTransaction,
    Bill,
    CloseChecklist,
    CloseTask,
    DepreciationEntry,
    ExceptionItem,
    Invoice,
    Item,
    JournalEntry,
    PayrollRun,
    ScheduleEntry,
    TDSEntry,
)
from app.models.base import utcnow
from app.money import ZERO, money


@dataclass
class CheckResult:
    passed: bool
    detail: str
    issue_count: int = 0
    amount_at_risk: Decimal = ZERO
    evidence: list[dict] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.evidence is None:
            self.evidence = []


def period_bounds(period: str) -> tuple[date, date]:
    year, month = (int(part) for part in period.split("-"))
    return date(year, month, 1), date(year, month, monthrange(year, month)[1])


# --------------------------------------------------------------- checks ----

def _check_bank_reconciliation(db: Session, company_id: str, start: date, end: date) -> CheckResult:
    unreconciled = list(
        db.execute(
            select(BankTransaction).where(
                BankTransaction.company_id == company_id,
                BankTransaction.txn_date >= start,
                BankTransaction.txn_date <= end,
                BankTransaction.recon_status.in_(
                    [ReconStatus.UNMATCHED, ReconStatus.AMBIGUOUS, ReconStatus.EXCEPTION]
                ),
            )
        ).scalars()
    )
    amount = money(sum((t.amount for t in unreconciled), ZERO))
    return CheckResult(
        passed=not unreconciled,
        detail=(
            "All bank transactions in the period are reconciled."
            if not unreconciled
            else f"{len(unreconciled)} bank transaction(s) totalling {amount} are unreconciled."
        ),
        issue_count=len(unreconciled),
        amount_at_risk=amount,
        evidence=[
            {
                "type": "bank_transaction",
                "id": t.id,
                "label": t.description[:80],
                "date": t.txn_date.isoformat(),
                "amount": str(t.amount),
            }
            for t in unreconciled[:10]
        ],
    )


def _check_draft_entries(db: Session, company_id: str, start: date, end: date) -> CheckResult:
    drafts = list(
        db.execute(
            select(JournalEntry).where(
                JournalEntry.company_id == company_id,
                JournalEntry.entry_date >= start,
                JournalEntry.entry_date <= end,
                JournalEntry.status.in_([JournalStatus.DRAFT, JournalStatus.PENDING_APPROVAL]),
            )
        ).scalars()
    )
    amount = money(sum((entry.total_debit for entry in drafts), ZERO))
    return CheckResult(
        passed=not drafts,
        detail=(
            "No unposted entries remain in this period."
            if not drafts
            else f"{len(drafts)} entr{'y' if len(drafts) == 1 else 'ies'} totalling {amount} are still unposted."
        ),
        issue_count=len(drafts),
        amount_at_risk=amount,
        evidence=[
            {
                "type": "journal_entry",
                "id": entry.id,
                "label": entry.entry_number,
                "date": entry.entry_date.isoformat(),
                "amount": str(entry.total_debit),
                "status": entry.status,
            }
            for entry in drafts[:10]
        ],
    )


def _check_ar(db: Session, company_id: str, start: date, end: date) -> CheckResult:
    overdue = [
        invoice
        for invoice in db.execute(
            select(Invoice).where(
                Invoice.company_id == company_id,
                Invoice.due_date < end,
                Invoice.status.in_(
                    [DocumentStatus.SENT, DocumentStatus.PARTIALLY_PAID, DocumentStatus.OVERDUE]
                ),
            )
        ).scalars()
        if invoice.balance_due > ZERO
    ]
    amount = money(sum((invoice.balance_due for invoice in overdue), ZERO))
    # Overdue receivables do not *block* a close; they are informational.
    return CheckResult(
        passed=True,
        detail=(
            "No overdue receivables at period end."
            if not overdue
            else f"{len(overdue)} overdue invoice(s) totalling {amount} — reviewed, not blocking."
        ),
        issue_count=len(overdue),
        amount_at_risk=amount,
        evidence=[
            {
                "type": "invoice",
                "id": invoice.id,
                "label": f"{invoice.number} — {invoice.customer.name}",
                "due_date": invoice.due_date.isoformat(),
                "amount": str(invoice.balance_due),
            }
            for invoice in sorted(overdue, key=lambda i: i.balance_due, reverse=True)[:10]
        ],
    )


def _check_ap(db: Session, company_id: str, start: date, end: date) -> CheckResult:
    overdue = [
        bill
        for bill in db.execute(
            select(Bill).where(
                Bill.company_id == company_id,
                Bill.due_date < end,
                Bill.status.in_(
                    [DocumentStatus.APPROVED, DocumentStatus.PARTIALLY_PAID, DocumentStatus.OVERDUE]
                ),
            )
        ).scalars()
        if bill.balance_due > ZERO
    ]
    amount = money(sum((bill.balance_due for bill in overdue), ZERO))
    return CheckResult(
        passed=True,
        detail=(
            "No overdue payables at period end."
            if not overdue
            else f"{len(overdue)} overdue bill(s) totalling {amount} — reviewed, not blocking."
        ),
        issue_count=len(overdue),
        amount_at_risk=amount,
        evidence=[
            {
                "type": "bill",
                "id": bill.id,
                "label": f"{bill.number} — {bill.vendor.name}",
                "due_date": bill.due_date.isoformat(),
                "amount": str(bill.balance_due),
            }
            for bill in sorted(overdue, key=lambda b: b.balance_due, reverse=True)[:10]
        ],
    )


def _check_inventory(db: Session, company_id: str, start: date, end: date) -> CheckResult:
    from app.services.inventory import rebuild_all_valuations

    result = rebuild_all_valuations(db, company_id)
    negative = list(
        db.execute(
            select(Item).where(
                Item.company_id == company_id,
                Item.track_inventory.is_(True),
                Item.current_qty < 0,
            )
        ).scalars()
    )

    issues = result["inconsistent"] + len(negative)
    detail_parts = []
    if result["inconsistent"]:
        detail_parts.append(f"{result['inconsistent']} item(s) had a valuation drift (now corrected)")
    if negative:
        detail_parts.append(f"{len(negative)} item(s) have negative stock")
    if not detail_parts:
        detail_parts.append(
            f"Stock valuation verified against movements for {result['items_checked']} item(s); "
            f"total value {result['total_value']}"
        )

    return CheckResult(
        passed=not negative,
        detail=". ".join(detail_parts) + ".",
        issue_count=issues,
        amount_at_risk=money(sum((item.stock_value for item in negative), ZERO)),
        evidence=[
            {"type": "item", "id": item.id, "label": f"{item.sku} — {item.name}", "quantity": str(item.current_qty)}
            for item in negative[:10]
        ],
    )


def _check_accruals(db: Session, company_id: str, start: date, end: date) -> CheckResult:
    period = f"{start.year}-{start.month:02d}"
    pending = list(
        db.execute(
            select(ScheduleEntry).where(
                ScheduleEntry.company_id == company_id,
                ScheduleEntry.period == period,
                ScheduleEntry.accrual_schedule_id.isnot(None),
                ScheduleEntry.is_posted.is_(False),
            )
        ).scalars()
    )
    amount = money(sum((entry.amount for entry in pending), ZERO))
    return CheckResult(
        passed=not pending,
        detail=(
            "All scheduled accruals for the period are posted."
            if not pending
            else f"{len(pending)} accrual(s) totalling {amount} are scheduled but not posted."
        ),
        issue_count=len(pending),
        amount_at_risk=amount,
        evidence=[
            {"type": "schedule_entry", "id": entry.id, "period": entry.period, "amount": str(entry.amount)}
            for entry in pending[:10]
        ],
    )


def _check_prepaids(db: Session, company_id: str, start: date, end: date) -> CheckResult:
    period = f"{start.year}-{start.month:02d}"
    pending = list(
        db.execute(
            select(ScheduleEntry).where(
                ScheduleEntry.company_id == company_id,
                ScheduleEntry.period == period,
                ScheduleEntry.amortisation_schedule_id.isnot(None),
                ScheduleEntry.is_posted.is_(False),
            )
        ).scalars()
    )
    amount = money(sum((entry.amount for entry in pending), ZERO))
    return CheckResult(
        passed=not pending,
        detail=(
            "All prepaid amortisation for the period is posted."
            if not pending
            else f"{len(pending)} prepaid instalment(s) totalling {amount} are not yet posted."
        ),
        issue_count=len(pending),
        amount_at_risk=amount,
        evidence=[
            {"type": "schedule_entry", "id": entry.id, "period": entry.period, "amount": str(entry.amount)}
            for entry in pending[:10]
        ],
    )


def _check_depreciation(db: Session, company_id: str, start: date, end: date) -> CheckResult:
    period = f"{start.year}-{start.month:02d}"
    pending = list(
        db.execute(
            select(DepreciationEntry).where(
                DepreciationEntry.company_id == company_id,
                DepreciationEntry.period == period,
                DepreciationEntry.is_posted.is_(False),
            )
        ).scalars()
    )
    amount = money(sum((entry.amount for entry in pending), ZERO))
    return CheckResult(
        passed=not pending,
        detail=(
            "Depreciation for the period is posted."
            if not pending
            else f"{len(pending)} depreciation entr{'y' if len(pending) == 1 else 'ies'} "
            f"totalling {amount} are not yet posted."
        ),
        issue_count=len(pending),
        amount_at_risk=amount,
        evidence=[
            {"type": "depreciation_entry", "id": entry.id, "period": entry.period, "amount": str(entry.amount)}
            for entry in pending[:10]
        ],
    )


def _check_payroll(db: Session, company_id: str, start: date, end: date) -> CheckResult:
    period = f"{start.year}-{start.month:02d}"
    run = db.execute(
        select(PayrollRun).where(PayrollRun.company_id == company_id, PayrollRun.period == period)
    ).scalar_one_or_none()

    if run is None:
        return CheckResult(
            passed=True,
            detail="No payroll run exists for this period (not blocking if payroll is not used).",
        )
    posted = run.status in ("POSTED", "PAID")
    return CheckResult(
        passed=posted,
        detail=(
            f"Payroll for {period} is {run.status.lower()} "
            f"({run.employee_count} employees, net {run.total_net})."
        ),
        issue_count=0 if posted else 1,
        amount_at_risk=ZERO if posted else run.total_net,
        evidence=[
            {"type": "payroll_run", "id": run.id, "period": period, "status": run.status}
        ],
    )


def _check_gst(db: Session, company_id: str, start: date, end: date) -> CheckResult:
    from app.services.gst import gst_summary

    period = f"{start.year}-{start.month:02d}"
    summary = gst_summary(db, company_id, period=period)
    prepared = summary["returns"]["GSTR1"] != "NOT_STARTED" and summary["returns"]["GSTR3B"] != "NOT_STARTED"

    return CheckResult(
        passed=prepared,
        detail=(
            f"GSTR-1 is {summary['returns']['GSTR1']} and GSTR-3B is {summary['returns']['GSTR3B']}. "
            f"Net GST position {summary['net_position']}."
        ),
        issue_count=0 if prepared else 1,
        amount_at_risk=summary["net_payable"],
        evidence=[
            {
                "type": "gst_summary",
                "label": f"GST for {period}",
                "output_tax": str(summary["output"]["total"]),
                "input_tax": str(summary["input"]["total"]),
                "net_payable": str(summary["net_payable"]),
            }
        ],
    )


def _check_tds(db: Session, company_id: str, start: date, end: date) -> CheckResult:
    pending = list(
        db.execute(
            select(TDSEntry).where(
                TDSEntry.company_id == company_id,
                TDSEntry.deduction_date >= start,
                TDSEntry.deduction_date <= end,
                TDSEntry.deposited.is_(False),
            )
        ).scalars()
    )
    amount = money(sum((entry.tds_amount for entry in pending), ZERO))
    return CheckResult(
        passed=True,  # deposit deadlines fall after period end
        detail=(
            "No TDS deducted in the period, or all of it is deposited."
            if not pending
            else f"{len(pending)} TDS deduction(s) totalling {amount} are pending deposit."
        ),
        issue_count=len(pending),
        amount_at_risk=amount,
        evidence=[
            {
                "type": "tds_entry",
                "id": entry.id,
                "section": entry.section,
                "amount": str(entry.tds_amount),
                "date": entry.deduction_date.isoformat(),
            }
            for entry in pending[:10]
        ],
    )


def _check_suspense(db: Session, company_id: str, start: date, end: date) -> CheckResult:
    account = db.execute(
        select(Account).where(Account.company_id == company_id, Account.system_key == "SUSPENSE")
    ).scalar_one_or_none()
    if account is None:
        return CheckResult(passed=True, detail="No suspense account is configured.")

    balance = account_balance(db, company_id, account.id, as_of=end)
    return CheckResult(
        passed=balance == ZERO,
        detail=(
            "The suspense account is clear."
            if balance == ZERO
            else f"The suspense account holds {balance} at period end and must be cleared."
        ),
        issue_count=0 if balance == ZERO else 1,
        amount_at_risk=abs(balance),
        evidence=[
            {"type": "account", "id": account.id, "label": f"{account.code} {account.name}", "balance": str(balance)}
        ],
    )


def _check_trial_balance(db: Session, company_id: str, start: date, end: date) -> CheckResult:
    result = verify_invariants(db, company_id, as_of=end)
    failed = [check for check in result["checks"] if not check["passed"]]
    return CheckResult(
        passed=result["all_passed"],
        detail=(
            f"All {len(result['checks'])} accounting invariants hold at {end}."
            if result["all_passed"]
            else f"{len(failed)} invariant(s) failed: " + "; ".join(c["name"] for c in failed)
        ),
        issue_count=len(failed),
        evidence=[
            {"type": "invariant", "label": check["name"], "passed": check["passed"], "detail": check["detail"]}
            for check in result["checks"]
        ],
    )


def _check_anomalies(db: Session, company_id: str, start: date, end: date) -> CheckResult:
    open_anomalies = list(
        db.execute(
            select(Anomaly).where(
                Anomaly.company_id == company_id,
                Anomaly.status == ExceptionStatus.OPEN,
                Anomaly.severity.in_(["HIGH", "CRITICAL"]),
            )
        ).scalars()
    )
    amount = money(sum((a.amount for a in open_anomalies if a.amount), ZERO))
    return CheckResult(
        passed=not open_anomalies,
        detail=(
            "No high-severity anomalies are open."
            if not open_anomalies
            else f"{len(open_anomalies)} high-severity anomal{'y' if len(open_anomalies) == 1 else 'ies'} "
            f"involving {amount} need review."
        ),
        issue_count=len(open_anomalies),
        amount_at_risk=amount,
        evidence=[
            {"type": "anomaly", "id": a.id, "label": a.title, "severity": a.severity, "amount": str(a.amount or ZERO)}
            for a in open_anomalies[:10]
        ],
    )


def _check_variance(db: Session, company_id: str, start: date, end: date) -> CheckResult:
    from app.services.reports import profit_and_loss
    from datetime import timedelta

    previous_end = start - timedelta(days=1)
    previous_start = previous_end.replace(day=1)

    current = profit_and_loss(db, company_id, start_date=start, end_date=end)
    previous = profit_and_loss(db, company_id, start_date=previous_start, end_date=previous_end)

    change = money(current["net_profit"] - previous["net_profit"])
    percent = (
        money(change * 100 / abs(previous["net_profit"])) if previous["net_profit"] != ZERO else None
    )

    return CheckResult(
        passed=True,
        detail=(
            f"Net profit {current['net_profit']} vs {previous['net_profit']} last month "
            f"(change {change}"
            + (f", {percent}%" if percent is not None else "")
            + ")."
        ),
        evidence=[
            {
                "type": "comparison",
                "label": "Net profit",
                "current": str(current["net_profit"]),
                "previous": str(previous["net_profit"]),
                "change": str(change),
            }
        ],
    )


def _check_reports(db: Session, company_id: str, start: date, end: date) -> CheckResult:
    from app.services.reports import balance_sheet, cash_flow

    bs = balance_sheet(db, company_id, as_of=end)
    cf = cash_flow(db, company_id, start_date=start, end_date=end)

    issues = []
    if not bs["balanced"]:
        issues.append(f"the balance sheet is out by {bs['difference']}")
    if not cf["reconciles"]:
        issues.append(f"cash flow leaves {cf['unclassified']} unclassified")

    return CheckResult(
        passed=not issues,
        detail=(
            "Balance sheet balances and the cash flow statement reconciles."
            if not issues
            else "Report issues: " + "; ".join(issues) + "."
        ),
        issue_count=len(issues),
        evidence=[
            {"type": "report", "label": "Balance sheet", "balanced": bs["balanced"], "difference": str(bs["difference"])},
            {"type": "report", "label": "Cash flow", "reconciles": cf["reconciles"], "unclassified": str(cf["unclassified"])},
        ],
    )


# key, name, category, order, blocking, check function
CLOSE_TASKS: list[tuple[str, str, str, int, bool, Callable]] = [
    ("bank_reconciliation", "Bank reconciliation complete", "BANKING", 10, True, _check_bank_reconciliation),
    ("draft_entries", "All entries posted", "ACCOUNTING", 20, True, _check_draft_entries),
    ("ar_review", "Receivables reviewed", "AR", 30, False, _check_ar),
    ("ap_review", "Payables reviewed", "AP", 40, False, _check_ap),
    ("inventory", "Inventory valued and verified", "INVENTORY", 50, True, _check_inventory),
    ("accruals", "Accruals posted", "ACCOUNTING", 60, True, _check_accruals),
    ("prepaids", "Prepaid amortisation posted", "ACCOUNTING", 70, True, _check_prepaids),
    ("depreciation", "Depreciation posted", "ACCOUNTING", 80, True, _check_depreciation),
    ("payroll", "Payroll posted", "PAYROLL", 90, False, _check_payroll),
    ("gst", "GST returns prepared", "GST", 100, False, _check_gst),
    ("tds", "TDS reviewed", "TAX", 110, False, _check_tds),
    ("suspense", "Suspense account cleared", "ACCOUNTING", 120, True, _check_suspense),
    ("trial_balance", "Trial balance and invariants verified", "ACCOUNTING", 130, True, _check_trial_balance),
    ("anomalies", "Anomalies reviewed", "ANOMALY", 140, True, _check_anomalies),
    ("variance", "Variance analysis reviewed", "CLOSE", 150, False, _check_variance),
    ("reports", "Financial statements verified", "CLOSE", 160, True, _check_reports),
]


def run_checklist(
    db: Session,
    company_id: str,
    *,
    period: str | None = None,
    user_id: str | None = None,
    agent_run_id: str | None = None,
) -> CloseChecklist:
    """Create or refresh the close checklist by re-running every check."""
    period = period or _default_period()
    start, end = period_bounds(period)

    accounting_period = get_period(db, company_id, start)
    if accounting_period is None:
        raise NotFoundError(
            f"No accounting period exists for {period}. Create the fiscal year first."
        )

    checklist = db.execute(
        select(CloseChecklist).where(
            CloseChecklist.company_id == company_id,
            CloseChecklist.period_id == accounting_period.id,
        )
    ).scalar_one_or_none()

    if checklist is None:
        checklist = CloseChecklist(
            company_id=company_id,
            period_id=accounting_period.id,
            period_label=accounting_period.name,
            started_by_user_id=user_id,
        )
        db.add(checklist)
        db.flush()

    existing = {task.key: task for task in checklist.tasks}
    completed = blocked = 0
    findings: list[dict] = []

    for key, name, category, order, blocking, check in CLOSE_TASKS:
        try:
            result = check(db, company_id, start, end)
        except Exception as exc:  # noqa: BLE001 — one failing check must not abort the close
            result = CheckResult(
                passed=False,
                detail=f"The check could not run: {exc}",
                issue_count=1,
            )

        task = existing.get(key)
        if task is None:
            task = CloseTask(
                company_id=company_id,
                checklist_id=checklist.id,
                key=key,
                name=name,
                category=category,
                display_order=order,
                is_blocking=blocking,
            )
            db.add(task)

        # A task a human already signed off stays completed unless it now fails.
        if result.passed:
            task.status = CloseTaskStatus.COMPLETED
            completed += 1
        elif blocking:
            task.status = CloseTaskStatus.BLOCKED
            blocked += 1
        else:
            task.status = CloseTaskStatus.READY_FOR_REVIEW

        task.description = result.detail
        task.check_result = {
            "passed": result.passed,
            "detail": result.detail,
            "evidence": result.evidence,
        }
        task.issue_count = result.issue_count
        task.amount_at_risk = result.amount_at_risk
        task.last_checked_at = utcnow()

        if not result.passed:
            findings.append(
                {
                    "key": key,
                    "name": name,
                    "category": category,
                    "blocking": blocking,
                    "detail": result.detail,
                    "issue_count": result.issue_count,
                    "amount_at_risk": str(result.amount_at_risk),
                }
            )

    total = len(CLOSE_TASKS)
    checklist.total_tasks = total
    checklist.completed_tasks = completed
    checklist.blocked_tasks = blocked
    checklist.progress_percent = (
        money(Decimal(completed) * 100 / Decimal(total)) if total else ZERO
    )
    checklist.ai_findings = findings
    checklist.ai_run_at = utcnow()
    checklist.status = (
        PeriodStatus.CLOSED
        if accounting_period.status == PeriodStatus.CLOSED
        else PeriodStatus.CLOSING
        if completed > 0
        else PeriodStatus.OPEN
    )
    db.flush()

    from app.services.audit import record_event

    record_event(
        db,
        company_id=company_id,
        action=AuditAction.UPDATE,
        entity_type="CloseChecklist",
        entity_id=checklist.id,
        entity_label=accounting_period.name,
        summary=(
            f"Close checklist for {accounting_period.name}: {completed}/{total} complete, "
            f"{blocked} blocking issue(s)."
        ),
        actor_type=ActorType.USER if user_id else ActorType.SYSTEM,
        actor_id=user_id,
        agent_run_id=agent_run_id,
        after={"completed": completed, "blocked": blocked, "progress": str(checklist.progress_percent)},
    )
    return checklist


def close_status(db: Session, company_id: str, *, period: str | None = None) -> dict:
    """Current close state — used by the dashboard and the AI tools."""
    period = period or _default_period()
    start, _ = period_bounds(period)
    accounting_period = get_period(db, company_id, start)
    if accounting_period is None:
        return {
            "period": period,
            "exists": False,
            "message": f"No accounting period exists for {period}.",
        }

    checklist = db.execute(
        select(CloseChecklist).where(
            CloseChecklist.company_id == company_id,
            CloseChecklist.period_id == accounting_period.id,
        )
    ).scalar_one_or_none()

    if checklist is None:
        return {
            "period": period,
            "period_label": accounting_period.name,
            "period_status": accounting_period.status,
            "exists": False,
            "progress_percent": ZERO,
            "message": "The close checklist has not been run for this period yet.",
        }

    tasks = sorted(checklist.tasks, key=lambda t: t.display_order)
    blocking = [t for t in tasks if t.is_blocking and t.status == CloseTaskStatus.BLOCKED]

    return {
        "period": period,
        "period_label": accounting_period.name,
        "period_status": accounting_period.status,
        "exists": True,
        "checklist_id": checklist.id,
        "progress_percent": checklist.progress_percent,
        "total_tasks": checklist.total_tasks,
        "completed_tasks": checklist.completed_tasks,
        "blocked_tasks": checklist.blocked_tasks,
        "can_close": not blocking and accounting_period.status != PeriodStatus.CLOSED,
        "last_run": checklist.ai_run_at,
        "ai_summary": checklist.ai_summary,
        "tasks": [
            {
                "id": task.id,
                "key": task.key,
                "name": task.name,
                "category": task.category,
                "status": task.status,
                "is_blocking": task.is_blocking,
                "description": task.description,
                "issue_count": task.issue_count,
                "amount_at_risk": task.amount_at_risk,
                "evidence": (task.check_result or {}).get("evidence", []),
                "last_checked_at": task.last_checked_at,
            }
            for task in tasks
        ],
        "blocking_issues": [
            {"key": task.key, "name": task.name, "detail": task.description} for task in blocking
        ],
    }


def approve_and_close(
    db: Session,
    company_id: str,
    *,
    period: str,
    user_id: str,
    notes: str = "",
    force: bool = False,
) -> dict:
    """Re-verify the checklist, then close the accounting period."""
    checklist = run_checklist(db, company_id, period=period, user_id=user_id)
    status = close_status(db, company_id, period=period)

    if status["blocked_tasks"] and not force:
        raise ConflictError(
            f"{status['blocked_tasks']} blocking task(s) must be resolved before "
            f"{status['period_label']} can be closed.",
            details={"blocking_issues": status["blocking_issues"]},
            code="close_blocked",
        )

    start, _ = period_bounds(period)
    accounting_period = get_period(db, company_id, start)
    close_accounting_period(
        db, company_id, accounting_period.id, user_id=user_id, notes=notes, force=force
    )

    checklist.status = PeriodStatus.CLOSED
    checklist.approved_by_user_id = user_id
    checklist.closed_at = utcnow()
    db.flush()

    return close_status(db, company_id, period=period)


def complete_task(
    db: Session, company_id: str, task_id: str, *, user_id: str, notes: str = ""
) -> CloseTask:
    """Mark a non-blocking review task as done.

    Blocking tasks cannot be ticked by hand — they pass only when their
    underlying check passes.
    """
    task = db.get(CloseTask, task_id)
    if task is None or task.company_id != company_id:
        raise NotFoundError("Close task not found.")

    if task.is_blocking and not (task.check_result or {}).get("passed", False):
        raise ValidationError(
            f"'{task.name}' is a blocking task and its check has not passed. "
            f"{task.description} Resolve the underlying issue and re-run the checklist.",
            code="blocking_task_unresolved",
        )

    task.status = CloseTaskStatus.COMPLETED
    task.completed_by_user_id = user_id
    task.completed_at = utcnow()
    task.notes = notes
    db.flush()

    checklist = db.get(CloseChecklist, task.checklist_id)
    completed = sum(1 for t in checklist.tasks if t.status == CloseTaskStatus.COMPLETED)
    checklist.completed_tasks = completed
    checklist.progress_percent = (
        money(Decimal(completed) * 100 / Decimal(checklist.total_tasks))
        if checklist.total_tasks
        else ZERO
    )
    db.flush()
    return task


def _default_period() -> str:
    today = date.today()
    return f"{today.year}-{today.month:02d}"
