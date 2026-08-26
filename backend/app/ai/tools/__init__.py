"""Typed query tools — the only way an AI agent may read company data.

There is no natural-language-to-SQL anywhere in this product. An agent picks a
named tool from this registry; the tool runs a parameterised SQLAlchemy query
and returns typed, aggregated results.

Consequences that matter:

* SQL injection through the model is structurally impossible.
* Every tool declares the permission it needs, checked against the AI
  permission set — which cannot contain execute-class permissions.
* Results are aggregated and capped, so the whole ledger is never shipped to a
  model. Large questions are answered from summaries, not row dumps.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.enums import DocumentStatus, JournalStatus, Permission, ReconStatus
from app.errors import PolicyViolation, ValidationError
from app.models import (
    Account,
    Anomaly,
    BankTransaction,
    Bill,
    Customer,
    ExceptionItem,
    Invoice,
    Item,
    JournalEntry,
    JournalLine,
    TDSEntry,
    Vendor,
)
from app.money import ZERO, D, money
from app.services.policy import AI_ALLOWED_PERMISSIONS

MAX_ROWS = 50


@dataclass
class Tool:
    name: str
    description: str
    permission: str
    handler: Callable[..., dict]
    parameters: dict[str, str] = field(default_factory=dict)

    def spec(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "permission": self.permission,
        }


REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, permission: str, parameters: dict[str, str] | None = None):
    def decorator(func_: Callable[..., dict]) -> Callable[..., dict]:
        REGISTRY[name] = Tool(
            name=name,
            description=description,
            permission=permission,
            handler=func_,
            parameters=parameters or {},
        )
        return func_

    return decorator


def call_tool(
    db: Session, company_id: str, name: str, *, arguments: dict | None = None, actor: str = "AI"
) -> dict:
    """Execute a registered tool with permission enforcement."""
    registered = REGISTRY.get(name)
    if registered is None:
        raise ValidationError(
            f"'{name}' is not an available tool.",
            details={"available": sorted(REGISTRY)},
            code="unknown_tool",
        )

    if actor == "AI" and registered.permission not in AI_ALLOWED_PERMISSIONS:
        raise PolicyViolation(
            f"The tool '{name}' requires '{registered.permission}', which AI agents "
            "are never granted.",
            code="ai_tool_forbidden",
        )

    arguments = arguments or {}
    allowed = set(registered.parameters)
    unexpected = set(arguments) - allowed
    if unexpected:
        raise ValidationError(
            f"Unexpected argument(s) for '{name}': {', '.join(sorted(unexpected))}.",
            details={"accepted": sorted(allowed)},
        )

    return registered.handler(db, company_id, **arguments)


def tool_specs() -> list[dict]:
    return [t.spec() for t in REGISTRY.values()]


def _parse_date(value: Any, default: date) -> date:
    if value is None or value == "":
        return default
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ValidationError(f"'{value}' is not a valid ISO date (YYYY-MM-DD).") from exc


def _clamp(value: Any, default: int = 10) -> int:
    try:
        return max(1, min(MAX_ROWS, int(value)))
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------- position ----

@tool(
    "get_cash_position",
    "Current cash and bank balances per account, with the total.",
    Permission.READ_ACCOUNTS.value,
    {"as_of": "ISO date (optional, defaults to today)"},
)
def get_cash_position(db: Session, company_id: str, as_of: Any = None) -> dict:
    from app.accounting.ledger import account_balance
    from app.enums import AccountSubtype

    on_date = _parse_date(as_of, date.today())
    accounts = list(
        db.execute(
            select(Account).where(
                Account.company_id == company_id,
                Account.subtype.in_([AccountSubtype.CASH, AccountSubtype.BANK]),
                Account.is_group.is_(False),
                Account.is_active.is_(True),
            )
        ).scalars()
    )

    rows = []
    total = ZERO
    for account in accounts:
        balance = account_balance(db, company_id, account.id, as_of=on_date)
        total = money(total + balance)
        if balance != ZERO:
            rows.append(
                {"id": account.id, "code": account.code, "name": account.name, "balance": balance}
            )

    return {
        "as_of": on_date,
        "total_cash": total,
        "accounts": sorted(rows, key=lambda r: r["balance"], reverse=True),
        "account_count": len(rows),
    }


@tool(
    "get_profit_and_loss",
    "Profit & loss summary for a period, with the largest expense lines.",
    Permission.READ_REPORTS.value,
    {"start_date": "ISO date", "end_date": "ISO date"},
)
def get_profit_and_loss(db: Session, company_id: str, start_date: Any = None, end_date: Any = None) -> dict:
    from app.services.reports import profit_and_loss

    end = _parse_date(end_date, date.today())
    start = _parse_date(start_date, end.replace(day=1))
    report = profit_and_loss(db, company_id, start_date=start, end_date=end)

    return {
        "start_date": start,
        "end_date": end,
        "revenue": report["revenue"]["total"],
        "cogs": report["cogs"]["total"],
        "gross_profit": report["gross_profit"],
        "gross_margin_percent": report["gross_margin_percent"],
        "operating_expenses": report["operating_expenses"]["total"],
        "net_profit": report["net_profit"],
        "net_margin_percent": report["net_margin_percent"],
        "top_expenses": report["operating_expenses"]["lines"][:10],
    }


@tool(
    "get_balance_sheet",
    "Balance sheet totals as at a date, including whether it balances.",
    Permission.READ_REPORTS.value,
    {"as_of": "ISO date"},
)
def get_balance_sheet(db: Session, company_id: str, as_of: Any = None) -> dict:
    from app.services.reports import balance_sheet

    on_date = _parse_date(as_of, date.today())
    report = balance_sheet(db, company_id, as_of=on_date)
    return {
        "as_of": on_date,
        "total_assets": report["assets"]["total"],
        "current_assets": report["assets"]["current"]["total"],
        "fixed_assets": report["assets"]["fixed"]["total"],
        "total_liabilities": report["liabilities"]["total"],
        "current_liabilities": report["liabilities"]["current"]["total"],
        "total_equity": report["equity"]["total"],
        "balanced": report["balanced"],
        "difference": report["difference"],
    }


@tool(
    "compare_periods",
    "Compare P&L between two periods and rank the accounts that changed most.",
    Permission.READ_REPORTS.value,
    {
        "start_date": "ISO date (current period start)",
        "end_date": "ISO date (current period end)",
        "compare_start": "ISO date (prior period start)",
        "compare_end": "ISO date (prior period end)",
    },
)
def compare_periods(
    db: Session,
    company_id: str,
    start_date: Any = None,
    end_date: Any = None,
    compare_start: Any = None,
    compare_end: Any = None,
) -> dict:
    from app.services.reports import profit_and_loss

    end = _parse_date(end_date, date.today())
    start = _parse_date(start_date, end.replace(day=1))
    prior_end = _parse_date(compare_end, start - timedelta(days=1))
    prior_start = _parse_date(compare_start, prior_end.replace(day=1))

    current = profit_and_loss(db, company_id, start_date=start, end_date=end)
    previous = profit_and_loss(db, company_id, start_date=prior_start, end_date=prior_end)

    def index(report: dict) -> dict[str, dict]:
        rows: dict[str, dict] = {}
        for section in ("revenue", "cogs", "operating_expenses", "other_income", "other_expenses"):
            for line in report[section]["lines"]:
                rows[line["account_id"]] = line
        return rows

    current_rows, previous_rows = index(current), index(previous)
    drivers = []
    for account_id in set(current_rows) | set(previous_rows):
        now = current_rows.get(account_id, {})
        before = previous_rows.get(account_id, {})
        current_amount = money(now.get("amount", ZERO))
        previous_amount = money(before.get("amount", ZERO))
        change = money(current_amount - previous_amount)
        if change == ZERO:
            continue
        drivers.append(
            {
                "account_id": account_id,
                "code": now.get("code") or before.get("code"),
                "name": now.get("name") or before.get("name"),
                "current": current_amount,
                "previous": previous_amount,
                "change": change,
                "change_percent": (
                    money(change * 100 / abs(previous_amount)) if previous_amount != ZERO else None
                ),
            }
        )

    drivers.sort(key=lambda d: abs(d["change"]), reverse=True)
    profit_change = money(current["net_profit"] - previous["net_profit"])

    return {
        "current_period": {"start": start, "end": end, "net_profit": current["net_profit"], "revenue": current["revenue"]["total"]},
        "previous_period": {"start": prior_start, "end": prior_end, "net_profit": previous["net_profit"], "revenue": previous["revenue"]["total"]},
        "net_profit_change": profit_change,
        "net_profit_change_percent": (
            money(profit_change * 100 / abs(previous["net_profit"]))
            if previous["net_profit"] != ZERO
            else None
        ),
        "revenue_change": money(current["revenue"]["total"] - previous["revenue"]["total"]),
        "drivers": drivers[:12],
    }


# ------------------------------------------------------------------- AR ----

@tool(
    "list_outstanding_invoices",
    "Unpaid customer invoices, newest due first. Optionally only overdue ones.",
    Permission.READ_INVOICES.value,
    {"only_overdue": "true/false", "limit": "max rows", "customer_id": "optional filter"},
)
def list_outstanding_invoices(
    db: Session,
    company_id: str,
    only_overdue: Any = False,
    limit: Any = 10,
    customer_id: str | None = None,
) -> dict:
    limit = _clamp(limit)
    stmt = select(Invoice).where(
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
    if str(only_overdue).lower() in ("true", "1", "yes"):
        stmt = stmt.where(Invoice.due_date < date.today())
    if customer_id:
        stmt = stmt.where(Invoice.customer_id == customer_id)

    invoices = [
        inv
        for inv in db.execute(stmt.order_by(Invoice.due_date.asc()).limit(limit * 3)).scalars()
        if inv.balance_due > ZERO
    ][:limit]

    return {
        "count": len(invoices),
        "total_outstanding": money(sum((inv.balance_due for inv in invoices), ZERO)),
        "invoices": [
            {
                "id": inv.id,
                "number": inv.number,
                "customer_id": inv.customer_id,
                "customer_name": inv.customer.name,
                "date": inv.doc_date,
                "due_date": inv.due_date,
                "total": inv.total,
                "balance_due": inv.balance_due,
                "days_overdue": max(0, (date.today() - inv.due_date).days),
                "status": inv.status,
            }
            for inv in invoices
        ],
    }


@tool(
    "get_receivables_aging",
    "Receivables aging summary with per-customer buckets.",
    Permission.READ_INVOICES.value,
    {"as_of": "ISO date", "limit": "max customers"},
)
def get_receivables_aging(db: Session, company_id: str, as_of: Any = None, limit: Any = 10) -> dict:
    from app.services.reports import ar_aging

    report = ar_aging(db, company_id, as_of=_parse_date(as_of, date.today()))
    return {
        "as_of": report["as_of"],
        "total_outstanding": report["total_outstanding"],
        "total_overdue": report["total_overdue"],
        "overdue_percent": report["overdue_percent"],
        "bucket_totals": report["bucket_totals"],
        "customer_count": report["party_count"],
        "top_customers": [
            {
                "customer_id": row["customer_id"],
                "name": row["name"],
                "total": row["total"],
                "oldest_days": row["oldest_days"],
                "buckets": row["buckets"],
            }
            for row in report["rows"][: _clamp(limit)]
        ],
    }


# ------------------------------------------------------------------- AP ----

@tool(
    "list_outstanding_bills",
    "Unpaid vendor bills, soonest due first. Optionally only those due within N days.",
    Permission.READ_BILLS.value,
    {"due_within_days": "integer", "limit": "max rows", "vendor_id": "optional filter"},
)
def list_outstanding_bills(
    db: Session,
    company_id: str,
    due_within_days: Any = None,
    limit: Any = 10,
    vendor_id: str | None = None,
) -> dict:
    limit = _clamp(limit)
    stmt = select(Bill).where(
        Bill.company_id == company_id,
        Bill.status.in_(
            [DocumentStatus.APPROVED, DocumentStatus.PARTIALLY_PAID, DocumentStatus.OVERDUE]
        ),
    )
    if due_within_days is not None:
        try:
            horizon = date.today() + timedelta(days=int(due_within_days))
            stmt = stmt.where(Bill.due_date <= horizon)
        except (TypeError, ValueError):
            pass
    if vendor_id:
        stmt = stmt.where(Bill.vendor_id == vendor_id)

    bills = [
        bill
        for bill in db.execute(stmt.order_by(Bill.due_date.asc()).limit(limit * 3)).scalars()
        if bill.balance_due > ZERO
    ][:limit]

    return {
        "count": len(bills),
        "total_outstanding": money(sum((bill.balance_due for bill in bills), ZERO)),
        "bills": [
            {
                "id": bill.id,
                "number": bill.number,
                "vendor_invoice_number": bill.vendor_invoice_number,
                "vendor_id": bill.vendor_id,
                "vendor_name": bill.vendor.name,
                "date": bill.doc_date,
                "due_date": bill.due_date,
                "total": bill.total,
                "balance_due": bill.balance_due,
                "days_until_due": (bill.due_date - date.today()).days,
                "status": bill.status,
            }
            for bill in bills
        ],
    }


@tool(
    "get_payables_aging",
    "Payables aging summary with per-vendor buckets.",
    Permission.READ_BILLS.value,
    {"as_of": "ISO date", "limit": "max vendors"},
)
def get_payables_aging(db: Session, company_id: str, as_of: Any = None, limit: Any = 10) -> dict:
    from app.services.reports import ap_aging

    report = ap_aging(db, company_id, as_of=_parse_date(as_of, date.today()))
    return {
        "as_of": report["as_of"],
        "total_outstanding": report["total_outstanding"],
        "total_overdue": report["total_overdue"],
        "bucket_totals": report["bucket_totals"],
        "vendor_count": report["party_count"],
        "top_vendors": [
            {
                "vendor_id": row["vendor_id"],
                "name": row["name"],
                "total": row["total"],
                "oldest_days": row["oldest_days"],
                "buckets": row["buckets"],
            }
            for row in report["rows"][: _clamp(limit)]
        ],
    }


@tool(
    "find_duplicate_bills",
    "Find bills that look like duplicates (same vendor and amount, or repeated reference).",
    Permission.READ_BILLS.value,
    {"window_days": "lookback window", "limit": "max groups"},
)
def find_duplicate_bills(db: Session, company_id: str, window_days: Any = 90, limit: Any = 10) -> dict:
    try:
        window = int(window_days)
    except (TypeError, ValueError):
        window = 90
    cutoff = date.today() - timedelta(days=window)

    rows = db.execute(
        select(
            Bill.vendor_id,
            Bill.total,
            func.count(Bill.id).label("count"),
        )
        .where(
            Bill.company_id == company_id,
            Bill.doc_date >= cutoff,
            Bill.status != DocumentStatus.CANCELLED,
        )
        .group_by(Bill.vendor_id, Bill.total)
        .having(func.count(Bill.id) > 1)
        .limit(_clamp(limit))
    ).all()

    groups = []
    for vendor_id, total, count in rows:
        bills = list(
            db.execute(
                select(Bill).where(
                    Bill.company_id == company_id,
                    Bill.vendor_id == vendor_id,
                    Bill.total == total,
                    Bill.doc_date >= cutoff,
                    Bill.status != DocumentStatus.CANCELLED,
                )
            ).scalars()
        )
        if len(bills) < 2:
            continue
        vendor = db.get(Vendor, vendor_id)
        groups.append(
            {
                "vendor_id": vendor_id,
                "vendor_name": vendor.name if vendor else "(unknown)",
                "amount": money(total),
                "count": int(count),
                "potential_overpayment": money(money(total) * (int(count) - 1)),
                "bills": [
                    {
                        "id": bill.id,
                        "number": bill.number,
                        "vendor_invoice_number": bill.vendor_invoice_number,
                        "date": bill.doc_date,
                        "total": bill.total,
                        "status": bill.status,
                    }
                    for bill in bills
                ],
            }
        )

    return {
        "window_days": window,
        "group_count": len(groups),
        "groups": groups,
        "total_at_risk": money(sum((g["potential_overpayment"] for g in groups), ZERO)),
    }


# -------------------------------------------------------------- banking ----

@tool(
    "list_unreconciled_transactions",
    "Bank transactions that are not yet reconciled.",
    Permission.READ_TRANSACTIONS.value,
    {"limit": "max rows", "bank_account_id": "optional filter"},
)
def list_unreconciled_transactions(
    db: Session, company_id: str, limit: Any = 10, bank_account_id: str | None = None
) -> dict:
    limit = _clamp(limit)
    stmt = select(BankTransaction).where(
        BankTransaction.company_id == company_id,
        BankTransaction.recon_status.in_(
            [ReconStatus.UNMATCHED, ReconStatus.PARTIAL, ReconStatus.AMBIGUOUS, ReconStatus.EXCEPTION]
        ),
    )
    if bank_account_id:
        stmt = stmt.where(BankTransaction.bank_account_id == bank_account_id)

    total_count = int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    transactions = list(
        db.execute(stmt.order_by(BankTransaction.txn_date.desc()).limit(limit)).scalars()
    )

    return {
        "total_unreconciled": total_count,
        "showing": len(transactions),
        "total_amount": money(sum((t.amount for t in transactions), ZERO)),
        "transactions": [
            {
                "id": t.id,
                "date": t.txn_date,
                "description": t.description[:200],
                "reference": t.reference,
                "amount": t.amount,
                "direction": t.direction,
                "status": t.recon_status,
            }
            for t in transactions
        ],
    }


@tool(
    "get_reconciliation_status",
    "Reconciliation health: matched vs outstanding, and the match rate.",
    Permission.READ_TRANSACTIONS.value,
    {},
)
def get_reconciliation_status(db: Session, company_id: str) -> dict:
    from app.services.banking import unreconciled_summary

    return unreconciled_summary(db, company_id)


@tool(
    "search_transactions",
    "Search posted ledger lines by text, amount range and date range.",
    Permission.READ_TRANSACTIONS.value,
    {
        "query": "text to search in descriptions and references",
        "min_amount": "minimum amount",
        "max_amount": "maximum amount",
        "start_date": "ISO date",
        "end_date": "ISO date",
        "limit": "max rows",
    },
)
def search_transactions(
    db: Session,
    company_id: str,
    query: str = "",
    min_amount: Any = None,
    max_amount: Any = None,
    start_date: Any = None,
    end_date: Any = None,
    limit: Any = 20,
) -> dict:
    limit = _clamp(limit, 20)
    stmt = (
        select(JournalLine, JournalEntry, Account)
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Account, Account.id == JournalLine.account_id)
        .where(
            JournalLine.company_id == company_id,
            JournalLine.status == JournalStatus.POSTED,
        )
    )

    if query:
        pattern = f"%{str(query).lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(JournalLine.description).like(pattern),
                func.lower(JournalEntry.narration).like(pattern),
                func.lower(JournalEntry.reference).like(pattern),
                func.lower(Account.name).like(pattern),
            )
        )
    if min_amount is not None:
        amount = money(min_amount)
        stmt = stmt.where(or_(JournalLine.debit >= amount, JournalLine.credit >= amount))
    if max_amount is not None:
        amount = money(max_amount)
        stmt = stmt.where(JournalLine.debit <= amount, JournalLine.credit <= amount)
    if start_date:
        stmt = stmt.where(JournalLine.entry_date >= _parse_date(start_date, date.min))
    if end_date:
        stmt = stmt.where(JournalLine.entry_date <= _parse_date(end_date, date.max))

    total = int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    rows = db.execute(stmt.order_by(JournalLine.entry_date.desc()).limit(limit)).all()

    return {
        "total_matches": total,
        "showing": len(rows),
        "results": [
            {
                "id": line.id,
                "entry_id": entry.id,
                "entry_number": entry.entry_number,
                "date": line.entry_date,
                "account_code": account.code,
                "account_name": account.name,
                "description": (line.description or entry.narration)[:200],
                "debit": line.debit,
                "credit": line.credit,
                "voucher_type": entry.voucher_type,
            }
            for line, entry, account in rows
        ],
    }


@tool(
    "explain_journal_entry",
    "Full detail of one journal entry, including every line and its source document.",
    Permission.READ_TRANSACTIONS.value,
    {"entry_id": "journal entry id"},
)
def explain_journal_entry(db: Session, company_id: str, entry_id: str = "") -> dict:
    entry = db.get(JournalEntry, entry_id)
    if entry is None or entry.company_id != company_id:
        return {"found": False, "message": f"Journal entry '{entry_id}' does not exist."}

    lines = []
    for line in entry.lines:
        account = db.get(Account, line.account_id)
        lines.append(
            {
                "line_number": line.line_number,
                "account_code": account.code if account else "",
                "account_name": account.name if account else "",
                "account_type": account.type if account else "",
                "debit": line.debit,
                "credit": line.credit,
                "description": line.description,
            }
        )

    return {
        "found": True,
        "id": entry.id,
        "entry_number": entry.entry_number,
        "date": entry.entry_date,
        "voucher_type": entry.voucher_type,
        "narration": entry.narration,
        "reference": entry.reference,
        "status": entry.status,
        "source": entry.source,
        "source_type": entry.source_type,
        "source_id": entry.source_id,
        "total_debit": entry.total_debit,
        "total_credit": entry.total_credit,
        "balanced": entry.is_balanced,
        "posted_at": entry.posted_at,
        "is_reversal": entry.is_reversal,
        "reverses_entry_id": entry.reverses_entry_id,
        "reversed_by_entry_id": entry.reversed_by_entry_id,
        "lines": lines,
    }


# ------------------------------------------------------------------ tax ----

@tool(
    "get_gst_summary",
    "Output GST, input GST and the net position for a period.",
    Permission.READ_TAX.value,
    {"period": "YYYY-MM (optional)"},
)
def get_gst_summary(db: Session, company_id: str, period: str | None = None) -> dict:
    from app.services.gst import gst_summary

    return gst_summary(db, company_id, period=period)


@tool(
    "get_tds_summary",
    "TDS deducted, deposited and pending, by section.",
    Permission.READ_TAX.value,
    {"start_date": "ISO date", "end_date": "ISO date"},
)
def get_tds_summary(db: Session, company_id: str, start_date: Any = None, end_date: Any = None) -> dict:
    end = _parse_date(end_date, date.today())
    start = _parse_date(start_date, end.replace(month=4, day=1) if end.month >= 4 else end.replace(year=end.year - 1, month=4, day=1))

    entries = list(
        db.execute(
            select(TDSEntry).where(
                TDSEntry.company_id == company_id,
                TDSEntry.deduction_date >= start,
                TDSEntry.deduction_date <= end,
            )
        ).scalars()
    )

    by_section: dict[str, dict] = {}
    for entry in entries:
        bucket = by_section.setdefault(
            entry.section,
            {"section": entry.section, "deducted": ZERO, "deposited": ZERO, "pending": ZERO, "count": 0},
        )
        bucket["deducted"] = money(bucket["deducted"] + entry.tds_amount)
        bucket["count"] += 1
        if entry.deposited:
            bucket["deposited"] = money(bucket["deposited"] + entry.tds_amount)
        else:
            bucket["pending"] = money(bucket["pending"] + entry.tds_amount)

    return {
        "start_date": start,
        "end_date": end,
        "total_deducted": money(sum((s["deducted"] for s in by_section.values()), ZERO)),
        "total_deposited": money(sum((s["deposited"] for s in by_section.values()), ZERO)),
        "total_pending": money(sum((s["pending"] for s in by_section.values()), ZERO)),
        "entry_count": len(entries),
        "by_section": sorted(by_section.values(), key=lambda s: s["deducted"], reverse=True),
    }


# ------------------------------------------------------------ inventory ----

@tool(
    "get_inventory_summary",
    "Stock value, low-stock items and out-of-stock items.",
    Permission.READ_INVENTORY.value,
    {},
)
def get_inventory_summary(db: Session, company_id: str) -> dict:
    from app.services.inventory import stock_summary

    return stock_summary(db, company_id)


@tool(
    "get_stock_movement_analysis",
    "Fast-moving, slow-moving and dead stock, measured from actual movements.",
    Permission.READ_INVENTORY.value,
    {"as_of": "ISO date"},
)
def get_stock_movement_analysis(db: Session, company_id: str, as_of: Any = None) -> dict:
    from app.services.inventory import movement_analysis

    return movement_analysis(db, company_id, as_of=_parse_date(as_of, date.today()))


# ----------------------------------------------------------- exceptions ----

@tool(
    "list_open_exceptions",
    "Items in the exception centre that still need a human decision.",
    Permission.READ_TRANSACTIONS.value,
    {"category": "optional category filter", "limit": "max rows"},
)
def list_open_exceptions(
    db: Session, company_id: str, category: str | None = None, limit: Any = 15
) -> dict:
    from app.enums import ExceptionStatus

    stmt = select(ExceptionItem).where(
        ExceptionItem.company_id == company_id,
        ExceptionItem.status.in_([ExceptionStatus.OPEN, ExceptionStatus.IN_REVIEW]),
    )
    if category:
        stmt = stmt.where(ExceptionItem.category == str(category).upper())

    total = int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    items = list(
        db.execute(
            stmt.order_by(ExceptionItem.severity.desc(), ExceptionItem.created_at.desc()).limit(
                _clamp(limit, 15)
            )
        ).scalars()
    )

    return {
        "total_open": total,
        "showing": len(items),
        "exceptions": [
            {
                "id": item.id,
                "category": item.category,
                "severity": item.severity,
                "title": item.title,
                "reason": item.reason,
                "recommended_action": item.recommended_action,
                "amount": item.amount,
                "entity_type": item.entity_type,
                "entity_id": item.entity_id,
            }
            for item in items
        ],
    }


@tool(
    "list_anomalies",
    "Detected anomalies with severity, evidence and recommendation.",
    Permission.READ_TRANSACTIONS.value,
    {"severity": "optional minimum severity", "limit": "max rows"},
)
def list_anomalies(db: Session, company_id: str, severity: str | None = None, limit: Any = 15) -> dict:
    from app.enums import ExceptionStatus

    stmt = select(Anomaly).where(
        Anomaly.company_id == company_id, Anomaly.status == ExceptionStatus.OPEN
    )
    if severity:
        order = ["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"]
        wanted = str(severity).upper()
        if wanted in order:
            stmt = stmt.where(Anomaly.severity.in_(order[order.index(wanted) :]))

    total = int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    anomalies = list(
        db.execute(stmt.order_by(Anomaly.created_at.desc()).limit(_clamp(limit, 15))).scalars()
    )

    return {
        "total_open": total,
        "showing": len(anomalies),
        "anomalies": [
            {
                "id": a.id,
                "kind": a.kind,
                "severity": a.severity,
                "title": a.title,
                "reason": a.reason,
                "recommendation": a.recommendation,
                "amount": a.amount,
                "occurred_on": a.occurred_on,
                "entity_type": a.entity_type,
                "entity_id": a.entity_id,
            }
            for a in anomalies
        ],
    }


@tool(
    "get_top_customers_by_balance",
    "Customers owing the most, with contact details for follow-up.",
    Permission.READ_INVOICES.value,
    {"limit": "max rows"},
)
def get_top_customers_by_balance(db: Session, company_id: str, limit: Any = 10) -> dict:
    from app.services.reports import ar_aging

    report = ar_aging(db, company_id)
    rows = report["rows"][: _clamp(limit)]
    return {
        "total_receivable": report["total_outstanding"],
        "customers": [
            {
                "customer_id": row["customer_id"],
                "name": row["name"],
                "email": row["email"],
                "phone": row["phone"],
                "total_due": row["total"],
                "oldest_days": row["oldest_days"],
                "invoice_count": len(row["documents"]),
            }
            for row in rows
        ],
    }


@tool(
    "get_close_status",
    "Month-end close progress and any blocking tasks for a period.",
    Permission.READ_REPORTS.value,
    {"period": "YYYY-MM (optional)"},
)
def get_close_status(db: Session, company_id: str, period: str | None = None) -> dict:
    from app.services.close import close_status

    return close_status(db, company_id, period=period)


@tool(
    "get_cash_forecast",
    "Cash forecast built from known receivables, payables and recurring items.",
    Permission.READ_REPORTS.value,
    {"days": "horizon in days (7, 30, 60 or 90)"},
)
def get_cash_forecast(db: Session, company_id: str, days: Any = 30) -> dict:
    from app.services.forecast import cash_forecast

    try:
        horizon = int(days)
    except (TypeError, ValueError):
        horizon = 30
    return cash_forecast(db, company_id, horizon_days=horizon)


__all__ = ["REGISTRY", "Tool", "call_tool", "tool_specs", "tool"]
