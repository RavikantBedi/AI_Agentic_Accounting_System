"""Financial reporting endpoints, plus dashboard aggregates and exports."""
from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func, select

from app.accounting.ledger import LedgerFilters, trial_balance, verify_invariants
from app.deps import CurrentUserDep, DbDep, require_permission
from app.enums import ExceptionStatus, Permission, ReconStatus
from app.errors import ValidationError
from app.money import ZERO, money
from app.services import reports as report_service
from app.services.forecast import cash_forecast, current_cash, multi_horizon_forecast

router = APIRouter(prefix="/reports", tags=["Reports"])


def _default_range(start: date | None, end: date | None) -> tuple[date, date]:
    end = end or date.today()
    start = start or end.replace(day=1)
    if start > end:
        raise ValidationError("start_date cannot be after end_date.")
    return start, end


@router.get("/profit-and-loss")
def profit_and_loss(
    db: DbDep,
    current: CurrentUserDep,
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    compare: bool = Query(default=False),
    branch_id: str | None = Query(default=None),
    cost_center_id: str | None = Query(default=None),
) -> dict:
    current.require(Permission.READ_REPORTS.value)
    start, end = _default_range(start_date, end_date)

    compare_start = compare_end = None
    if compare:
        compare_end = start - timedelta(days=1)
        compare_start = compare_end.replace(day=1)

    return report_service.profit_and_loss(
        db,
        current.company_id,
        start_date=start,
        end_date=end,
        filters=LedgerFilters(branch_id=branch_id, cost_center_id=cost_center_id),
        compare_start=compare_start,
        compare_end=compare_end,
    )


@router.get("/balance-sheet")
def balance_sheet(
    db: DbDep,
    current: CurrentUserDep,
    as_of: date | None = Query(default=None),
    branch_id: str | None = Query(default=None),
) -> dict:
    current.require(Permission.READ_REPORTS.value)
    return report_service.balance_sheet(
        db,
        current.company_id,
        as_of=as_of or date.today(),
        filters=LedgerFilters(branch_id=branch_id),
    )


@router.get("/cash-flow")
def cash_flow(
    db: DbDep,
    current: CurrentUserDep,
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
) -> dict:
    current.require(Permission.READ_REPORTS.value)
    start, end = _default_range(start_date, end_date)
    return report_service.cash_flow(db, current.company_id, start_date=start, end_date=end)


@router.get("/ar-aging")
def ar_aging(
    db: DbDep, current: CurrentUserDep, as_of: date | None = Query(default=None)
) -> dict:
    current.require(Permission.READ_INVOICES.value)
    return report_service.ar_aging(db, current.company_id, as_of=as_of)


@router.get("/ap-aging")
def ap_aging(
    db: DbDep, current: CurrentUserDep, as_of: date | None = Query(default=None)
) -> dict:
    current.require(Permission.READ_BILLS.value)
    return report_service.ap_aging(db, current.company_id, as_of=as_of)


@router.get("/ratios")
def ratios(
    db: DbDep,
    current: CurrentUserDep,
    as_of: date | None = Query(default=None),
    start_date: date | None = Query(default=None),
) -> dict:
    current.require(Permission.READ_REPORTS.value)
    end = as_of or date.today()
    start = start_date or (
        date(end.year, 4, 1) if end.month >= 4 else date(end.year - 1, 4, 1)
    )
    return report_service.ratio_analysis(db, current.company_id, as_of=end, start_date=start)


@router.get("/trend")
def trend(
    db: DbDep, current: CurrentUserDep, months: int = Query(default=12, ge=1, le=36)
) -> dict:
    current.require(Permission.READ_REPORTS.value)
    return {"series": report_service.monthly_trend(db, current.company_id, months=months)}


@router.get("/expense-summary")
def expense_summary(
    db: DbDep,
    current: CurrentUserDep,
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    limit: int = Query(default=15, ge=1, le=50),
) -> dict:
    current.require(Permission.READ_REPORTS.value)
    start, end = _default_range(start_date, end_date)
    return report_service.expense_summary(
        db, current.company_id, start_date=start, end_date=end, limit=limit
    )


@router.get("/sales-summary")
def sales_summary(
    db: DbDep,
    current: CurrentUserDep,
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
) -> dict:
    current.require(Permission.READ_INVOICES.value)
    start, end = _default_range(start_date, end_date)
    return report_service.sales_summary(db, current.company_id, start_date=start, end_date=end)


@router.get("/purchase-summary")
def purchase_summary(
    db: DbDep,
    current: CurrentUserDep,
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
) -> dict:
    current.require(Permission.READ_BILLS.value)
    start, end = _default_range(start_date, end_date)
    return report_service.purchase_summary(db, current.company_id, start_date=start, end_date=end)


@router.get("/budget-variance")
def budget_variance(
    db: DbDep,
    current: CurrentUserDep,
    period: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    cost_center_id: str | None = Query(default=None),
) -> dict:
    current.require(Permission.READ_REPORTS.value)
    return report_service.budget_variance(
        db, current.company_id, period=period, cost_center_id=cost_center_id
    )


@router.get("/cash-forecast")
def forecast(
    db: DbDep, current: CurrentUserDep, days: int = Query(default=30, ge=1, le=365)
) -> dict:
    current.require(Permission.READ_REPORTS.value)
    return cash_forecast(db, current.company_id, horizon_days=days)


@router.get("/cash-forecast/all")
def forecast_all(db: DbDep, current: CurrentUserDep) -> dict:
    current.require(Permission.READ_REPORTS.value)
    return multi_horizon_forecast(db, current.company_id)


# ------------------------------------------------------------- dashboard --

@router.get("/dashboard")
def dashboard(
    db: DbDep,
    current: CurrentUserDep,
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
) -> dict:
    """Everything the overview screen needs, in one round trip."""
    current.require(Permission.READ_REPORTS.value)

    from app.models import Anomaly, BankTransaction, ExceptionItem
    from app.services.banking import unreconciled_summary
    from app.services.close import close_status
    from app.services.inventory import stock_summary

    end = end_date or date.today()
    start = start_date or (date(end.year, 4, 1) if end.month >= 4 else date(end.year - 1, 4, 1))

    pl = report_service.profit_and_loss(db, current.company_id, start_date=start, end_date=end)
    bs = report_service.balance_sheet(db, current.company_id, as_of=end)
    ar = report_service.ar_aging(db, current.company_id, as_of=end)
    ap = report_service.ap_aging(db, current.company_id, as_of=end)
    recon = unreconciled_summary(db, current.company_id)
    stock = stock_summary(db, current.company_id)
    cash = current_cash(db, current.company_id, as_of=end)
    trend_series = report_service.monthly_trend(db, current.company_id, months=12, end_date=end)
    expenses = report_service.expense_summary(
        db, current.company_id, start_date=start, end_date=end, limit=8
    )

    open_exceptions = int(
        db.execute(
            select(func.count(ExceptionItem.id)).where(
                ExceptionItem.company_id == current.company_id,
                ExceptionItem.status.in_([ExceptionStatus.OPEN, ExceptionStatus.IN_REVIEW]),
            )
        ).scalar_one()
    )
    open_anomalies = int(
        db.execute(
            select(func.count(Anomaly.id)).where(
                Anomaly.company_id == current.company_id,
                Anomaly.status == ExceptionStatus.OPEN,
            )
        ).scalar_one()
    )

    invariants = verify_invariants(db, current.company_id, as_of=end)

    return {
        "period": {"start_date": start, "end_date": end},
        "kpis": {
            "cash": cash,
            "revenue": pl["revenue"]["total"],
            "expenses": money(
                pl["cogs"]["total"]
                + pl["operating_expenses"]["total"]
                + pl["other_expenses"]["total"]
            ),
            "net_profit": pl["net_profit"],
            "net_margin_percent": pl["net_margin_percent"],
            "receivable": ar["total_outstanding"],
            "receivable_overdue": ar["total_overdue"],
            "payable": ap["total_outstanding"],
            "payable_overdue": ap["total_overdue"],
            "unreconciled_count": recon["needs_attention_count"],
            "unreconciled_amount": recon["unreconciled_amount"],
            "inventory_value": stock["total_stock_value"],
            "low_stock_count": stock["low_stock_count"],
        },
        "trend": trend_series,
        "expense_breakdown": expenses["lines"],
        "balance_sheet": {
            "total_assets": bs["assets"]["total"],
            "total_liabilities": bs["liabilities"]["total"],
            "total_equity": bs["equity"]["total"],
            "balanced": bs["balanced"],
        },
        "attention": {
            "open_exceptions": open_exceptions,
            "open_anomalies": open_anomalies,
            "unreconciled": recon["needs_attention_count"],
            "overdue_invoices": ar["document_count"],
            "overdue_bills": ap["document_count"],
        },
        "close": close_status(db, current.company_id),
        "health": {
            "invariants_passed": invariants["all_passed"],
            "checks": [
                {"name": check["name"], "passed": check["passed"], "detail": check["detail"]}
                for check in invariants["checks"]
            ],
        },
    }


# ---------------------------------------------------------------- export --

EXPORTABLE = {
    "trial-balance",
    "profit-and-loss",
    "balance-sheet",
    "cash-flow",
    "ar-aging",
    "ap-aging",
    "expense-summary",
    "general-ledger",
}


@router.get("/export/{report}.csv")
def export_csv(
    report: str,
    db: DbDep,
    current: CurrentUserDep,
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    as_of: date | None = Query(default=None),
    account_id: str | None = Query(default=None),
) -> Response:
    """Download a report as CSV."""
    current.require(Permission.READ_REPORTS.value)
    if report not in EXPORTABLE:
        raise ValidationError(
            f"'{report}' cannot be exported.", details={"available": sorted(EXPORTABLE)}
        )

    start, end = _default_range(start_date, end_date)
    buffer = io.StringIO()
    writer = csv.writer(buffer)

    if report == "trial-balance":
        data = trial_balance(db, current.company_id, as_of=as_of or end)
        writer.writerow(["Code", "Account", "Type", "Debit", "Credit"])
        for row in data["rows"]:
            writer.writerow([row["code"], row["name"], row["type"], row["debit"], row["credit"]])
        writer.writerow([])
        writer.writerow(["", "TOTAL", "", data["total_debit"], data["total_credit"]])

    elif report == "profit-and-loss":
        data = report_service.profit_and_loss(
            db, current.company_id, start_date=start, end_date=end
        )
        writer.writerow(["Section", "Code", "Account", "Amount"])
        for section in ("revenue", "cogs", "operating_expenses", "other_income", "other_expenses"):
            for line in data[section]["lines"]:
                writer.writerow(
                    [section.replace("_", " ").title(), line["code"], line["name"], line["amount"]]
                )
            writer.writerow([section.replace("_", " ").title() + " total", "", "", data[section]["total"]])
        writer.writerow([])
        writer.writerow(["Gross profit", "", "", data["gross_profit"]])
        writer.writerow(["Operating profit", "", "", data["operating_profit"]])
        writer.writerow(["Net profit", "", "", data["net_profit"]])

    elif report == "balance-sheet":
        data = report_service.balance_sheet(db, current.company_id, as_of=as_of or end)
        writer.writerow(["Section", "Code", "Account", "Amount"])
        for section, group in (
            ("Current assets", data["assets"]["current"]),
            ("Fixed assets", data["assets"]["fixed"]),
            ("Current liabilities", data["liabilities"]["current"]),
            ("Long-term liabilities", data["liabilities"]["long_term"]),
            ("Equity", data["equity"]),
        ):
            for line in group["lines"]:
                writer.writerow([section, line["code"], line["name"], line["amount"]])
            writer.writerow([f"{section} total", "", "", group["total"]])
        writer.writerow([])
        writer.writerow(["Total assets", "", "", data["assets"]["total"]])
        writer.writerow(["Total liabilities and equity", "", "", data["total_liabilities_and_equity"]])
        writer.writerow(["Balanced", "", "", "Yes" if data["balanced"] else "No"])

    elif report == "cash-flow":
        data = report_service.cash_flow(db, current.company_id, start_date=start, end_date=end)
        writer.writerow(["Category", "Line", "Amount"])
        for category in ("operating", "investing", "financing"):
            for line in data[category]["lines"]:
                writer.writerow([category.title(), line["label"], line["amount"]])
            writer.writerow([f"{category.title()} net", "", data[category]["net"]])
        writer.writerow([])
        writer.writerow(["Opening cash", "", data["opening_cash"]])
        writer.writerow(["Closing cash", "", data["closing_cash"]])

    elif report in ("ar-aging", "ap-aging"):
        data = (
            report_service.ar_aging(db, current.company_id, as_of=as_of or end)
            if report == "ar-aging"
            else report_service.ap_aging(db, current.company_id, as_of=as_of or end)
        )
        writer.writerow(["Name", "Current", "1-30", "31-60", "61-90", "90+", "Total", "Oldest days"])
        for row in data["rows"]:
            buckets = row["buckets"]
            writer.writerow(
                [
                    row["name"],
                    buckets["current"],
                    buckets["1-30"],
                    buckets["31-60"],
                    buckets["61-90"],
                    buckets["90+"],
                    row["total"],
                    row["oldest_days"],
                ]
            )
        writer.writerow([])
        writer.writerow(["TOTAL", "", "", "", "", "", data["total_outstanding"], ""])

    elif report == "expense-summary":
        data = report_service.expense_summary(
            db, current.company_id, start_date=start, end_date=end, limit=1000
        )
        writer.writerow(["Code", "Account", "Amount", "Percent"])
        for line in data["lines"]:
            writer.writerow([line["code"], line["name"], line["amount"], line["percent"]])

    elif report == "general-ledger":
        if not account_id:
            raise ValidationError("account_id is required for a general ledger export.")
        from app.accounting.ledger import general_ledger

        data = general_ledger(
            db, current.company_id, account_id, start_date=start, end_date=end, limit=10000
        )
        writer.writerow(["Date", "Entry", "Description", "Debit", "Credit", "Balance"])
        for row in data["rows"]:
            writer.writerow(
                [
                    row["date"],
                    row["entry_number"],
                    row["description"],
                    row["debit"],
                    row["credit"],
                    row["balance"],
                ]
            )

    filename = f"{report}-{(as_of or end).isoformat()}.csv"
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
