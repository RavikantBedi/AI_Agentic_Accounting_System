"""Financial reporting.

Every figure here is aggregated from POSTED journal lines. There is no separate
reporting store to drift out of sync, and no report reads a cached balance.

The balance sheet closes by construction: current-period profit is carried into
equity as "Profit for the period", so Assets = Liabilities + Equity always
holds — and the report says so explicitly with a ``balanced`` flag.
"""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.accounting.ledger import AccountBalance, LedgerFilters, account_balances
from app.enums import AccountSubtype, AccountType, DocumentStatus, JournalStatus, NormalBalance
from app.models import (
    Account,
    Bill,
    BudgetLine,
    Company,
    Customer,
    Invoice,
    JournalEntry,
    JournalLine,
    Vendor,
)
from app.money import ZERO, D, money

# Aging buckets used across AR and AP.
AGING_BUCKETS = [
    ("current", 0, 0),
    ("1-30", 1, 30),
    ("31-60", 31, 60),
    ("61-90", 61, 90),
    ("90+", 91, 10_000),
]


def _period_label(value: date) -> str:
    return f"{value.year}-{value.month:02d}"


def _month_bounds(value: date) -> tuple[date, date]:
    return value.replace(day=1), value.replace(day=monthrange(value.year, value.month)[1])


# ------------------------------------------------------------------ P&L ----

def profit_and_loss(
    db: Session,
    company_id: str,
    *,
    start_date: date,
    end_date: date,
    filters: LedgerFilters | None = None,
    compare_start: date | None = None,
    compare_end: date | None = None,
) -> dict:
    """Profit & loss with optional comparative period."""
    filters = filters or LedgerFilters()
    filters.start_date = start_date
    filters.end_date = end_date

    balances = account_balances(db, company_id, filters=filters)
    current = _pl_from_balances(balances)

    comparative = None
    if compare_start and compare_end:
        compare_filters = LedgerFilters(
            start_date=compare_start,
            end_date=compare_end,
            branch_id=filters.branch_id,
            cost_center_id=filters.cost_center_id,
            department_id=filters.department_id,
        )
        comparative = _pl_from_balances(account_balances(db, company_id, filters=compare_filters))

    result = {
        "start_date": start_date,
        "end_date": end_date,
        **current,
        "comparative": comparative,
    }

    if comparative:
        result["variance"] = _variance_block(current, comparative)
    return result


def _pl_from_balances(balances: list[AccountBalance]) -> dict:
    """Build the P&L structure. Revenue is credit-positive, expenses debit-positive."""
    revenue_lines, cogs_lines, opex_lines, other_income_lines, other_expense_lines = [], [], [], [], []

    for balance in balances:
        amount = balance.balance  # positive in the account's normal direction
        if amount == ZERO:
            continue
        row = {
            "account_id": balance.account_id,
            "code": balance.code,
            "name": balance.name,
            "subtype": balance.subtype,
            "amount": amount,
        }
        if balance.type == AccountType.REVENUE:
            (other_income_lines if balance.subtype == AccountSubtype.OTHER_INCOME else revenue_lines).append(row)
        elif balance.type == AccountType.EXPENSE:
            if balance.subtype == AccountSubtype.COGS:
                cogs_lines.append(row)
            elif balance.subtype in (
                AccountSubtype.OTHER_EXPENSE,
                AccountSubtype.FINANCE_COST,
                AccountSubtype.TAX_EXPENSE,
            ):
                other_expense_lines.append(row)
            else:
                opex_lines.append(row)

    total = lambda rows: money(sum((row["amount"] for row in rows), ZERO))  # noqa: E731

    revenue = total(revenue_lines)
    cogs = total(cogs_lines)
    gross_profit = money(revenue - cogs)
    operating_expenses = total(opex_lines)
    operating_profit = money(gross_profit - operating_expenses)
    other_income = total(other_income_lines)
    other_expenses = total(other_expense_lines)
    net_profit = money(operating_profit + other_income - other_expenses)

    def margin(numerator: Decimal) -> Decimal:
        return money(numerator * 100 / revenue) if revenue > ZERO else ZERO

    return {
        "revenue": {"total": revenue, "lines": sorted(revenue_lines, key=lambda r: r["code"])},
        "cogs": {"total": cogs, "lines": sorted(cogs_lines, key=lambda r: r["code"])},
        "gross_profit": gross_profit,
        "gross_margin_percent": margin(gross_profit),
        "operating_expenses": {
            "total": operating_expenses,
            "lines": sorted(opex_lines, key=lambda r: r["amount"], reverse=True),
        },
        "operating_profit": operating_profit,
        "operating_margin_percent": margin(operating_profit),
        "other_income": {"total": other_income, "lines": other_income_lines},
        "other_expenses": {"total": other_expenses, "lines": other_expense_lines},
        "net_profit": net_profit,
        "net_margin_percent": margin(net_profit),
    }


def _variance_block(current: dict, previous: dict) -> dict:
    def compare(path: str, current_value: Decimal, previous_value: Decimal) -> dict:
        change = money(current_value - previous_value)
        percent = (
            money(change * 100 / abs(previous_value)) if previous_value != ZERO else None
        )
        return {
            "metric": path,
            "current": current_value,
            "previous": previous_value,
            "change": change,
            "change_percent": percent,
        }

    return {
        "revenue": compare("revenue", current["revenue"]["total"], previous["revenue"]["total"]),
        "gross_profit": compare("gross_profit", current["gross_profit"], previous["gross_profit"]),
        "operating_expenses": compare(
            "operating_expenses",
            current["operating_expenses"]["total"],
            previous["operating_expenses"]["total"],
        ),
        "net_profit": compare("net_profit", current["net_profit"], previous["net_profit"]),
    }


# -------------------------------------------------------- balance sheet ----

def balance_sheet(
    db: Session,
    company_id: str,
    *,
    as_of: date,
    filters: LedgerFilters | None = None,
) -> dict:
    """Balance sheet as at a date, including profit for the period in equity."""
    filters = filters or LedgerFilters()
    filters.start_date = None
    filters.end_date = as_of

    balances = account_balances(db, company_id, filters=filters)

    assets_current, assets_fixed, liabilities_current, liabilities_long, equity_lines = [], [], [], [], []
    revenue_total = ZERO
    expense_total = ZERO

    for balance in balances:
        amount = balance.balance
        row = {
            "account_id": balance.account_id,
            "code": balance.code,
            "name": balance.name,
            "subtype": balance.subtype,
            "amount": amount,
        }
        if balance.type == AccountType.ASSET:
            if amount == ZERO:
                continue
            if balance.subtype in (
                AccountSubtype.FIXED_ASSET,
                AccountSubtype.ACCUMULATED_DEPRECIATION,
                AccountSubtype.OTHER_ASSET,
            ):
                assets_fixed.append(row)
            else:
                assets_current.append(row)
        elif balance.type == AccountType.LIABILITY:
            if amount == ZERO:
                continue
            if balance.subtype in (AccountSubtype.LOAN, AccountSubtype.OTHER_LIABILITY):
                liabilities_long.append(row)
            else:
                liabilities_current.append(row)
        elif balance.type == AccountType.EQUITY:
            if amount != ZERO:
                equity_lines.append(row)
        elif balance.type == AccountType.REVENUE:
            revenue_total = money(revenue_total + amount)
        elif balance.type == AccountType.EXPENSE:
            expense_total = money(expense_total + amount)

    total = lambda rows: money(sum((row["amount"] for row in rows), ZERO))  # noqa: E731

    net_profit = money(revenue_total - expense_total)
    equity_lines.append(
        {
            "account_id": None,
            "code": "—",
            "name": "Profit for the period",
            "subtype": AccountSubtype.RETAINED_EARNINGS,
            "amount": net_profit,
            "is_computed": True,
        }
    )

    total_current_assets = total(assets_current)
    total_fixed_assets = total(assets_fixed)
    total_assets = money(total_current_assets + total_fixed_assets)
    total_current_liabilities = total(liabilities_current)
    total_long_liabilities = total(liabilities_long)
    total_liabilities = money(total_current_liabilities + total_long_liabilities)
    total_equity = total(equity_lines)
    difference = money(total_assets - total_liabilities - total_equity)

    return {
        "as_of": as_of,
        "assets": {
            "current": {"lines": sorted(assets_current, key=lambda r: r["code"]), "total": total_current_assets},
            "fixed": {"lines": sorted(assets_fixed, key=lambda r: r["code"]), "total": total_fixed_assets},
            "total": total_assets,
        },
        "liabilities": {
            "current": {
                "lines": sorted(liabilities_current, key=lambda r: r["code"]),
                "total": total_current_liabilities,
            },
            "long_term": {
                "lines": sorted(liabilities_long, key=lambda r: r["code"]),
                "total": total_long_liabilities,
            },
            "total": total_liabilities,
        },
        "equity": {"lines": equity_lines, "total": total_equity},
        "total_liabilities_and_equity": money(total_liabilities + total_equity),
        "difference": difference,
        "balanced": difference == ZERO,
        "net_profit": net_profit,
    }


# ------------------------------------------------------------ cash flow ----

def cash_flow(
    db: Session, company_id: str, *, start_date: date, end_date: date
) -> dict:
    """Cash flow, classified from the counterpart account of each cash movement.

    This is a direct-method statement built from actual cash/bank ledger lines,
    so opening + net movement always equals closing.
    """
    cash_accounts = list(
        db.execute(
            select(Account).where(
                Account.company_id == company_id,
                Account.subtype.in_([AccountSubtype.CASH, AccountSubtype.BANK]),
                Account.is_group.is_(False),
            )
        ).scalars()
    )
    cash_ids = [account.id for account in cash_accounts]
    if not cash_ids:
        return {
            "start_date": start_date,
            "end_date": end_date,
            "opening_cash": ZERO,
            "closing_cash": ZERO,
            "net_change": ZERO,
            "operating": {"inflows": ZERO, "outflows": ZERO, "net": ZERO, "lines": []},
            "investing": {"inflows": ZERO, "outflows": ZERO, "net": ZERO, "lines": []},
            "financing": {"inflows": ZERO, "outflows": ZERO, "net": ZERO, "lines": []},
            "reconciles": True,
        }

    opening = _cash_balance(db, company_id, cash_ids, before=start_date)
    closing = _cash_balance(db, company_id, cash_ids, before=end_date + timedelta(days=1))

    # For every entry touching cash, classify by the *other* side's account type.
    entry_ids = list(
        db.execute(
            select(JournalLine.entry_id)
            .where(
                JournalLine.company_id == company_id,
                JournalLine.status == JournalStatus.POSTED,
                JournalLine.account_id.in_(cash_ids),
                JournalLine.entry_date >= start_date,
                JournalLine.entry_date <= end_date,
            )
            .distinct()
        ).scalars()
    )

    categories: dict[str, dict[str, Decimal]] = {
        "operating": {},
        "investing": {},
        "financing": {},
    }

    for chunk_start in range(0, len(entry_ids), 500):
        chunk = entry_ids[chunk_start : chunk_start + 500]
        rows = db.execute(
            select(JournalLine, Account)
            .join(Account, Account.id == JournalLine.account_id)
            .where(
                JournalLine.entry_id.in_(chunk),
                JournalLine.status == JournalStatus.POSTED,
            )
        ).all()

        by_entry: dict[str, list] = {}
        for line, account in rows:
            by_entry.setdefault(line.entry_id, []).append((line, account))

        for lines in by_entry.values():
            cash_delta = money(
                sum(
                    (line.debit - line.credit for line, account in lines if account.id in cash_ids),
                    ZERO,
                )
            )
            if cash_delta == ZERO:
                continue

            counterparts = [(line, account) for line, account in lines if account.id not in cash_ids]
            if not counterparts:
                continue

            counterpart_total = money(
                sum((abs(line.debit - line.credit) for line, _ in counterparts), ZERO)
            )
            for line, account in counterparts:
                weight = abs(line.debit - line.credit)
                if weight == ZERO or counterpart_total == ZERO:
                    continue
                share = money(cash_delta * weight / counterpart_total)
                bucket = _classify_cash_flow(account)
                label = f"{account.code} {account.name}"
                categories[bucket][label] = money(categories[bucket].get(label, ZERO) + share)

    def build(bucket: str) -> dict:
        lines = [
            {"label": label, "amount": amount}
            for label, amount in sorted(
                categories[bucket].items(), key=lambda kv: abs(kv[1]), reverse=True
            )
            if amount != ZERO
        ]
        inflows = money(sum((line["amount"] for line in lines if line["amount"] > 0), ZERO))
        outflows = money(sum((-line["amount"] for line in lines if line["amount"] < 0), ZERO))
        return {
            "inflows": inflows,
            "outflows": outflows,
            "net": money(inflows - outflows),
            "lines": lines,
        }

    operating = build("operating")
    investing = build("investing")
    financing = build("financing")
    net_change = money(operating["net"] + investing["net"] + financing["net"])
    actual_change = money(closing - opening)

    return {
        "start_date": start_date,
        "end_date": end_date,
        "opening_cash": opening,
        "closing_cash": closing,
        "net_change": actual_change,
        "classified_change": net_change,
        "unclassified": money(actual_change - net_change),
        "operating": operating,
        "investing": investing,
        "financing": financing,
        "reconciles": money(actual_change - net_change) == ZERO,
    }


def _classify_cash_flow(account: Account) -> str:
    if account.subtype in (
        AccountSubtype.FIXED_ASSET,
        AccountSubtype.ACCUMULATED_DEPRECIATION,
        AccountSubtype.OTHER_ASSET,
    ):
        return "investing"
    if account.subtype in (
        AccountSubtype.LOAN,
        AccountSubtype.SHARE_CAPITAL,
        AccountSubtype.RETAINED_EARNINGS,
        AccountSubtype.DRAWINGS,
        AccountSubtype.RESERVES,
    ):
        return "financing"
    if account.type == AccountType.EQUITY:
        return "financing"
    return "operating"


def _cash_balance(db: Session, company_id: str, cash_ids: list[str], *, before: date) -> Decimal:
    row = db.execute(
        select(
            func.sum(JournalLine.debit).label("debit"),
            func.sum(JournalLine.credit).label("credit"),
        ).where(
            JournalLine.company_id == company_id,
            JournalLine.status == JournalStatus.POSTED,
            JournalLine.account_id.in_(cash_ids),
            JournalLine.entry_date < before,
        )
    ).one()
    return money(D(row.debit or 0) - D(row.credit or 0))


# ----------------------------------------------------------------- aging ---

def ar_aging(db: Session, company_id: str, *, as_of: date | None = None) -> dict:
    """Receivables aging by customer, from open invoice documents."""
    as_of = as_of or date.today()
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
                Invoice.doc_date <= as_of,
            )
        ).scalars()
    )
    return _build_aging(
        db,
        company_id,
        [
            {
                "id": invoice.id,
                "number": invoice.number,
                "party_id": invoice.customer_id,
                "date": invoice.doc_date,
                "due_date": invoice.due_date,
                "total": invoice.total,
                "balance": invoice.balance_due,
                "status": invoice.status,
            }
            for invoice in invoices
            if invoice.balance_due > ZERO
        ],
        as_of=as_of,
        party_model=Customer,
        label="customer",
    )


def ap_aging(db: Session, company_id: str, *, as_of: date | None = None) -> dict:
    """Payables aging by vendor, from open bills."""
    as_of = as_of or date.today()
    bills = list(
        db.execute(
            select(Bill).where(
                Bill.company_id == company_id,
                Bill.status.in_(
                    [DocumentStatus.APPROVED, DocumentStatus.PARTIALLY_PAID, DocumentStatus.OVERDUE]
                ),
                Bill.doc_date <= as_of,
            )
        ).scalars()
    )
    return _build_aging(
        db,
        company_id,
        [
            {
                "id": bill.id,
                "number": bill.number,
                "party_id": bill.vendor_id,
                "date": bill.doc_date,
                "due_date": bill.due_date,
                "total": bill.total,
                "balance": bill.balance_due,
                "status": bill.status,
                "vendor_invoice_number": bill.vendor_invoice_number,
            }
            for bill in bills
            if bill.balance_due > ZERO
        ],
        as_of=as_of,
        party_model=Vendor,
        label="vendor",
    )


def _build_aging(
    db: Session,
    company_id: str,
    documents: list[dict],
    *,
    as_of: date,
    party_model,
    label: str,
) -> dict:
    party_ids = {doc["party_id"] for doc in documents}
    parties = {
        party.id: party
        for party in db.execute(select(party_model).where(party_model.id.in_(party_ids))).scalars()
    } if party_ids else {}

    by_party: dict[str, dict] = {}
    bucket_totals = {name: ZERO for name, _, _ in AGING_BUCKETS}

    for doc in documents:
        days_overdue = (as_of - doc["due_date"]).days
        bucket = _bucket_for(days_overdue)
        balance = money(doc["balance"])

        party = parties.get(doc["party_id"])
        entry = by_party.setdefault(
            doc["party_id"],
            {
                f"{label}_id": doc["party_id"],
                "name": party.name if party else "(unknown)",
                "email": getattr(party, "email", "") if party else "",
                "phone": getattr(party, "phone", "") if party else "",
                "total": ZERO,
                "buckets": {name: ZERO for name, _, _ in AGING_BUCKETS},
                "documents": [],
                "oldest_days": 0,
            },
        )
        entry["total"] = money(entry["total"] + balance)
        entry["buckets"][bucket] = money(entry["buckets"][bucket] + balance)
        entry["oldest_days"] = max(entry["oldest_days"], max(0, days_overdue))
        entry["documents"].append({**doc, "days_overdue": max(0, days_overdue), "bucket": bucket})
        bucket_totals[bucket] = money(bucket_totals[bucket] + balance)

    rows = sorted(by_party.values(), key=lambda r: r["total"], reverse=True)
    total = money(sum((row["total"] for row in rows), ZERO))
    overdue = money(total - bucket_totals["current"])

    return {
        "as_of": as_of,
        "rows": rows,
        "bucket_totals": bucket_totals,
        "total_outstanding": total,
        "total_overdue": overdue,
        "overdue_percent": money(overdue * 100 / total) if total > ZERO else ZERO,
        "document_count": len(documents),
        "party_count": len(rows),
    }


def _bucket_for(days_overdue: int) -> str:
    if days_overdue <= 0:
        return "current"
    for name, low, high in AGING_BUCKETS[1:]:
        if low <= days_overdue <= high:
            return name
    return "90+"


# ---------------------------------------------------------------- ratios ---

def ratio_analysis(db: Session, company_id: str, *, as_of: date, start_date: date) -> dict:
    """Standard financial ratios, each with the inputs it was computed from."""
    bs = balance_sheet(db, company_id, as_of=as_of)
    pl = profit_and_loss(db, company_id, start_date=start_date, end_date=as_of)

    current_assets = bs["assets"]["current"]["total"]
    current_liabilities = bs["liabilities"]["current"]["total"]
    total_liabilities = bs["liabilities"]["total"]
    total_equity = bs["equity"]["total"]
    total_assets = bs["assets"]["total"]

    inventory = money(
        sum(
            (
                line["amount"]
                for line in bs["assets"]["current"]["lines"]
                if line["subtype"] == AccountSubtype.INVENTORY
            ),
            ZERO,
        )
    )
    receivables = money(
        sum(
            (
                line["amount"]
                for line in bs["assets"]["current"]["lines"]
                if line["subtype"] == AccountSubtype.ACCOUNTS_RECEIVABLE
            ),
            ZERO,
        )
    )
    payables = money(
        sum(
            (
                line["amount"]
                for line in bs["liabilities"]["current"]["lines"]
                if line["subtype"] == AccountSubtype.ACCOUNTS_PAYABLE
            ),
            ZERO,
        )
    )

    revenue = pl["revenue"]["total"]
    cogs = pl["cogs"]["total"]
    net_profit = pl["net_profit"]
    days = max(1, (as_of - start_date).days + 1)

    def ratio(numerator: Decimal, denominator: Decimal, *, percent: bool = False) -> Decimal | None:
        if denominator == ZERO:
            return None
        value = numerator / denominator
        return money(value * 100) if percent else money(value)

    return {
        "as_of": as_of,
        "period_start": start_date,
        "liquidity": {
            "current_ratio": {
                "value": ratio(current_assets, current_liabilities),
                "inputs": {"current_assets": current_assets, "current_liabilities": current_liabilities},
                "interpretation": "Current assets available per rupee of short-term obligation.",
            },
            "quick_ratio": {
                "value": ratio(money(current_assets - inventory), current_liabilities),
                "inputs": {
                    "current_assets": current_assets,
                    "inventory": inventory,
                    "current_liabilities": current_liabilities,
                },
                "interpretation": "Liquidity excluding inventory.",
            },
        },
        "leverage": {
            "debt_to_equity": {
                "value": ratio(total_liabilities, total_equity),
                "inputs": {"total_liabilities": total_liabilities, "total_equity": total_equity},
                "interpretation": "Borrowed funds per rupee of owners' funds.",
            },
        },
        "profitability": {
            "gross_margin_percent": {
                "value": pl["gross_margin_percent"],
                "inputs": {"revenue": revenue, "cogs": cogs},
                "interpretation": "Gross profit as a share of revenue.",
            },
            "operating_margin_percent": {
                "value": pl["operating_margin_percent"],
                "inputs": {"operating_profit": pl["operating_profit"], "revenue": revenue},
                "interpretation": "Operating profit as a share of revenue.",
            },
            "net_margin_percent": {
                "value": pl["net_margin_percent"],
                "inputs": {"net_profit": net_profit, "revenue": revenue},
                "interpretation": "Bottom line as a share of revenue.",
            },
            "return_on_equity_percent": {
                "value": ratio(net_profit, total_equity, percent=True),
                "inputs": {"net_profit": net_profit, "total_equity": total_equity},
                "interpretation": "Profit generated per rupee of equity.",
            },
            "return_on_assets_percent": {
                "value": ratio(net_profit, total_assets, percent=True),
                "inputs": {"net_profit": net_profit, "total_assets": total_assets},
                "interpretation": "Profit generated per rupee of assets.",
            },
        },
        "efficiency": {
            "inventory_turnover": {
                "value": ratio(cogs, inventory),
                "inputs": {"cogs": cogs, "inventory": inventory},
                "interpretation": "Times inventory was sold and replaced in the period.",
            },
            "receivables_turnover": {
                "value": ratio(revenue, receivables),
                "inputs": {"revenue": revenue, "receivables": receivables},
                "interpretation": "Times receivables were collected in the period.",
            },
            "payables_turnover": {
                "value": ratio(cogs, payables),
                "inputs": {"cogs": cogs, "payables": payables},
                "interpretation": "Times payables were settled in the period.",
            },
            "days_sales_outstanding": {
                "value": ratio(money(receivables * days), revenue),
                "inputs": {"receivables": receivables, "revenue": revenue, "days": Decimal(days)},
                "interpretation": "Average days to collect a rupee of revenue.",
            },
            "days_payable_outstanding": {
                "value": ratio(money(payables * days), cogs),
                "inputs": {"payables": payables, "cogs": cogs, "days": Decimal(days)},
                "interpretation": "Average days taken to pay suppliers.",
            },
        },
    }


# ------------------------------------------------------------- summaries ---

def monthly_trend(
    db: Session, company_id: str, *, months: int = 12, end_date: date | None = None
) -> list[dict]:
    """Revenue / expense / profit per month — the dashboard trend series."""
    end_date = end_date or date.today()
    series: list[dict] = []

    cursor = end_date.replace(day=1)
    periods: list[tuple[date, date]] = []
    for _ in range(months):
        start, finish = _month_bounds(cursor)
        periods.append((start, finish))
        cursor = (start - timedelta(days=1)).replace(day=1)
    periods.reverse()

    for start, finish in periods:
        rows = db.execute(
            select(
                Account.type,
                func.sum(JournalLine.debit).label("debit"),
                func.sum(JournalLine.credit).label("credit"),
            )
            .join(Account, Account.id == JournalLine.account_id)
            .where(
                JournalLine.company_id == company_id,
                JournalLine.status == JournalStatus.POSTED,
                JournalLine.entry_date >= start,
                JournalLine.entry_date <= finish,
                Account.type.in_([AccountType.REVENUE, AccountType.EXPENSE]),
            )
            .group_by(Account.type)
        ).all()

        revenue = expenses = ZERO
        for account_type, debit, credit in rows:
            debit, credit = D(debit or 0), D(credit or 0)
            if account_type == AccountType.REVENUE:
                revenue = money(credit - debit)
            else:
                expenses = money(debit - credit)

        series.append(
            {
                "period": _period_label(start),
                "label": start.strftime("%b %Y"),
                "start_date": start,
                "end_date": finish,
                "revenue": revenue,
                "expenses": expenses,
                "profit": money(revenue - expenses),
            }
        )
    return series


def expense_summary(
    db: Session, company_id: str, *, start_date: date, end_date: date, limit: int = 15
) -> dict:
    """Expenses by account, largest first — the "where did the money go" view."""
    rows = db.execute(
        select(
            Account.id,
            Account.code,
            Account.name,
            func.sum(JournalLine.debit - JournalLine.credit).label("amount"),
        )
        .join(Account, Account.id == JournalLine.account_id)
        .where(
            JournalLine.company_id == company_id,
            JournalLine.status == JournalStatus.POSTED,
            JournalLine.entry_date >= start_date,
            JournalLine.entry_date <= end_date,
            Account.type == AccountType.EXPENSE,
        )
        .group_by(Account.id, Account.code, Account.name)
    ).all()

    lines = [
        {"account_id": row[0], "code": row[1], "name": row[2], "amount": money(row[3] or 0)}
        for row in rows
        if money(row[3] or 0) != ZERO
    ]
    lines.sort(key=lambda r: r["amount"], reverse=True)
    total = money(sum((line["amount"] for line in lines), ZERO))
    for line in lines:
        line["percent"] = money(line["amount"] * 100 / total) if total > ZERO else ZERO

    return {
        "start_date": start_date,
        "end_date": end_date,
        "total": total,
        "lines": lines[:limit],
        "account_count": len(lines),
    }


def sales_summary(db: Session, company_id: str, *, start_date: date, end_date: date) -> dict:
    """Sales by customer and by item over a period."""
    invoices = list(
        db.execute(
            select(Invoice).where(
                Invoice.company_id == company_id,
                Invoice.doc_date >= start_date,
                Invoice.doc_date <= end_date,
                Invoice.status != DocumentStatus.CANCELLED,
                Invoice.status != DocumentStatus.DRAFT,
            )
        ).scalars()
    )

    by_customer: dict[str, dict] = {}
    for invoice in invoices:
        entry = by_customer.setdefault(
            invoice.customer_id,
            {"customer_id": invoice.customer_id, "name": invoice.customer.name, "amount": ZERO, "count": 0},
        )
        entry["amount"] = money(entry["amount"] + invoice.taxable_value)
        entry["count"] += 1

    rows = sorted(by_customer.values(), key=lambda r: r["amount"], reverse=True)
    total = money(sum((row["amount"] for row in rows), ZERO))

    return {
        "start_date": start_date,
        "end_date": end_date,
        "total_sales": total,
        "invoice_count": len(invoices),
        "average_invoice": money(total / len(invoices)) if invoices else ZERO,
        "by_customer": rows[:20],
        "customer_count": len(rows),
    }


def purchase_summary(db: Session, company_id: str, *, start_date: date, end_date: date) -> dict:
    bills = list(
        db.execute(
            select(Bill).where(
                Bill.company_id == company_id,
                Bill.doc_date >= start_date,
                Bill.doc_date <= end_date,
                Bill.status.notin_([DocumentStatus.CANCELLED, DocumentStatus.DRAFT]),
            )
        ).scalars()
    )

    by_vendor: dict[str, dict] = {}
    for bill in bills:
        entry = by_vendor.setdefault(
            bill.vendor_id,
            {"vendor_id": bill.vendor_id, "name": bill.vendor.name, "amount": ZERO, "count": 0},
        )
        entry["amount"] = money(entry["amount"] + bill.taxable_value)
        entry["count"] += 1

    rows = sorted(by_vendor.values(), key=lambda r: r["amount"], reverse=True)
    total = money(sum((row["amount"] for row in rows), ZERO))

    return {
        "start_date": start_date,
        "end_date": end_date,
        "total_purchases": total,
        "bill_count": len(bills),
        "by_vendor": rows[:20],
        "vendor_count": len(rows),
    }


def budget_variance(
    db: Session, company_id: str, *, period: str, cost_center_id: str | None = None
) -> dict:
    """Budget vs actual for one period, by account."""
    year, month = (int(part) for part in period.split("-"))
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])

    budget_rows = db.execute(
        select(BudgetLine.account_id, func.sum(BudgetLine.amount))
        .where(BudgetLine.company_id == company_id, BudgetLine.period == period)
        .group_by(BudgetLine.account_id)
    ).all()
    budgets = {row[0]: money(row[1] or 0) for row in budget_rows}

    stmt = (
        select(
            JournalLine.account_id,
            func.sum(JournalLine.debit - JournalLine.credit).label("amount"),
        )
        .where(
            JournalLine.company_id == company_id,
            JournalLine.status == JournalStatus.POSTED,
            JournalLine.entry_date >= start,
            JournalLine.entry_date <= end,
        )
        .group_by(JournalLine.account_id)
    )
    if cost_center_id:
        stmt = stmt.where(JournalLine.cost_center_id == cost_center_id)
    actuals = {row[0]: money(row[1] or 0) for row in db.execute(stmt).all()}

    account_ids = set(budgets) | set(actuals)
    if not account_ids:
        return {"period": period, "rows": [], "total_budget": ZERO, "total_actual": ZERO}

    accounts = {
        account.id: account
        for account in db.execute(select(Account).where(Account.id.in_(account_ids))).scalars()
    }

    rows = []
    for account_id in account_ids:
        account = accounts.get(account_id)
        if account is None or account.type not in (AccountType.EXPENSE, AccountType.REVENUE):
            continue
        budget = budgets.get(account_id, ZERO)
        actual = actuals.get(account_id, ZERO)
        if account.type == AccountType.REVENUE:
            actual = money(-actual)
        variance = money(actual - budget)
        rows.append(
            {
                "account_id": account_id,
                "code": account.code,
                "name": account.name,
                "type": account.type,
                "budget": budget,
                "actual": actual,
                "variance": variance,
                "variance_percent": money(variance * 100 / budget) if budget != ZERO else None,
                "over_budget": (
                    variance > ZERO if account.type == AccountType.EXPENSE else variance < ZERO
                ),
            }
        )

    rows.sort(key=lambda r: abs(r["variance"]), reverse=True)
    return {
        "period": period,
        "start_date": start,
        "end_date": end,
        "rows": rows,
        "total_budget": money(sum((row["budget"] for row in rows), ZERO)),
        "total_actual": money(sum((row["actual"] for row in rows), ZERO)),
    }
