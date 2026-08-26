"""Ledger queries: account balances, trial balance, general ledger, day book.

Every number the product displays is aggregated from POSTED journal lines here.
No report ever reads a cached "current balance" column on an account.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Iterable, Sequence

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import Session

from app.enums import AccountType, JournalStatus, NormalBalance
from app.models import Account, JournalEntry, JournalLine
from app.money import ZERO, money


@dataclass
class AccountBalance:
    account_id: str
    code: str
    name: str
    type: str
    subtype: str
    normal_balance: str
    debit: Decimal = ZERO
    credit: Decimal = ZERO

    @property
    def net_debit(self) -> Decimal:
        return money(self.debit - self.credit)

    @property
    def balance(self) -> Decimal:
        """Signed by the account's own normal balance (always non-negative for
        a normally-behaving account)."""
        if self.normal_balance == NormalBalance.DEBIT:
            return money(self.debit - self.credit)
        return money(self.credit - self.debit)

    def as_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "code": self.code,
            "name": self.name,
            "type": self.type,
            "subtype": self.subtype,
            "normal_balance": self.normal_balance,
            "debit": self.debit,
            "credit": self.credit,
            "balance": self.balance,
            "net_debit": self.net_debit,
        }


@dataclass
class LedgerFilters:
    """Dimension filters applied consistently across every ledger query."""

    start_date: date | None = None
    end_date: date | None = None
    branch_id: str | None = None
    cost_center_id: str | None = None
    department_id: str | None = None
    project: str | None = None
    account_ids: Sequence[str] | None = None
    account_types: Sequence[str] | None = None

    def apply(self, stmt: Select, *, include_dates: bool = True) -> Select:
        if include_dates and self.start_date:
            stmt = stmt.where(JournalLine.entry_date >= self.start_date)
        if include_dates and self.end_date:
            stmt = stmt.where(JournalLine.entry_date <= self.end_date)
        if self.branch_id:
            stmt = stmt.where(JournalLine.branch_id == self.branch_id)
        if self.cost_center_id:
            stmt = stmt.where(JournalLine.cost_center_id == self.cost_center_id)
        if self.department_id:
            stmt = stmt.where(JournalLine.department_id == self.department_id)
        if self.project:
            stmt = stmt.where(JournalLine.project == self.project)
        if self.account_ids:
            stmt = stmt.where(JournalLine.account_id.in_(list(self.account_ids)))
        return stmt


def _posted(stmt: Select, company_id: str) -> Select:
    """Restrict to POSTED lines of one company — the only ledger truth."""
    return stmt.where(
        JournalLine.company_id == company_id,
        JournalLine.status == JournalStatus.POSTED,
    )


def account_balances(
    db: Session,
    company_id: str,
    *,
    filters: LedgerFilters | None = None,
    include_zero: bool = False,
) -> list[AccountBalance]:
    """Aggregate debits/credits per account over the filter window."""
    filters = filters or LedgerFilters()

    stmt = select(
        JournalLine.account_id,
        func.sum(JournalLine.debit).label("debit"),
        func.sum(JournalLine.credit).label("credit"),
    ).group_by(JournalLine.account_id)
    stmt = filters.apply(_posted(stmt, company_id))

    sums = {
        row.account_id: (money(row.debit or 0), money(row.credit or 0))
        for row in db.execute(stmt)
    }

    account_stmt = select(Account).where(Account.company_id == company_id)
    if filters.account_types:
        account_stmt = account_stmt.where(Account.type.in_(list(filters.account_types)))
    if filters.account_ids:
        account_stmt = account_stmt.where(Account.id.in_(list(filters.account_ids)))
    accounts = list(db.execute(account_stmt.order_by(Account.code)).scalars())

    balances: list[AccountBalance] = []
    for account in accounts:
        debit, credit = sums.get(account.id, (ZERO, ZERO))
        if not include_zero and debit == ZERO and credit == ZERO:
            continue
        balances.append(
            AccountBalance(
                account_id=account.id,
                code=account.code,
                name=account.name,
                type=account.type,
                subtype=account.subtype,
                normal_balance=account.normal_balance,
                debit=debit,
                credit=credit,
            )
        )
    return balances


def account_balance(
    db: Session,
    company_id: str,
    account_id: str,
    *,
    as_of: date | None = None,
    start_date: date | None = None,
) -> Decimal:
    """Signed balance of one account (positive in its own normal direction)."""
    account = db.get(Account, account_id)
    if account is None or account.company_id != company_id:
        return ZERO

    stmt = select(
        func.sum(JournalLine.debit).label("debit"), func.sum(JournalLine.credit).label("credit")
    ).where(JournalLine.account_id == account_id)
    stmt = _posted(stmt, company_id)
    if start_date:
        stmt = stmt.where(JournalLine.entry_date >= start_date)
    if as_of:
        stmt = stmt.where(JournalLine.entry_date <= as_of)

    row = db.execute(stmt).one()
    debit, credit = money(row.debit or 0), money(row.credit or 0)
    return (
        money(debit - credit)
        if account.normal_balance == NormalBalance.DEBIT
        else money(credit - debit)
    )


def balances_by_system_key(
    db: Session, company_id: str, system_keys: Iterable[str], *, as_of: date | None = None
) -> dict[str, Decimal]:
    """Balances for system accounts (AR control, GST output, …)."""
    keys = list(system_keys)
    accounts = list(
        db.execute(
            select(Account).where(
                Account.company_id == company_id, Account.system_key.in_(keys)
            )
        ).scalars()
    )
    return {
        account.system_key: account_balance(db, company_id, account.id, as_of=as_of)
        for account in accounts
        if account.system_key
    }


def trial_balance(
    db: Session,
    company_id: str,
    *,
    as_of: date | None = None,
    start_date: date | None = None,
    filters: LedgerFilters | None = None,
    include_zero: bool = False,
) -> dict:
    """The trial balance. ``balanced`` must always be True on healthy books."""
    filters = filters or LedgerFilters()
    filters.start_date = start_date or filters.start_date
    filters.end_date = as_of or filters.end_date

    balances = account_balances(db, company_id, filters=filters, include_zero=include_zero)

    rows = []
    total_debit = ZERO
    total_credit = ZERO
    for balance in balances:
        net = balance.net_debit
        debit_column = net if net > 0 else ZERO
        credit_column = -net if net < 0 else ZERO
        total_debit += debit_column
        total_credit += credit_column
        rows.append(
            {
                "account_id": balance.account_id,
                "code": balance.code,
                "name": balance.name,
                "type": balance.type,
                "subtype": balance.subtype,
                "normal_balance": balance.normal_balance,
                "period_debit": balance.debit,
                "period_credit": balance.credit,
                "debit": debit_column,
                "credit": credit_column,
            }
        )

    total_debit = money(total_debit)
    total_credit = money(total_credit)
    difference = money(total_debit - total_credit)

    return {
        "as_of": as_of,
        "start_date": start_date,
        "rows": rows,
        "total_debit": total_debit,
        "total_credit": total_credit,
        "difference": difference,
        "balanced": difference == ZERO,
        "account_count": len(rows),
    }


def general_ledger(
    db: Session,
    company_id: str,
    account_id: str,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    filters: LedgerFilters | None = None,
    limit: int = 500,
    offset: int = 0,
) -> dict:
    """Running-balance ledger for one account, with an accurate opening balance."""
    account = db.get(Account, account_id)
    if account is None or account.company_id != company_id:
        return {"account": None, "opening_balance": ZERO, "rows": [], "total": 0}

    opening = ZERO
    if start_date:
        opening_stmt = select(
            func.sum(JournalLine.debit).label("debit"),
            func.sum(JournalLine.credit).label("credit"),
        ).where(JournalLine.account_id == account_id, JournalLine.entry_date < start_date)
        row = db.execute(_posted(opening_stmt, company_id)).one()
        debit, credit = money(row.debit or 0), money(row.credit or 0)
        opening = (
            money(debit - credit)
            if account.normal_balance == NormalBalance.DEBIT
            else money(credit - debit)
        )

    stmt = (
        select(JournalLine, JournalEntry)
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .where(JournalLine.account_id == account_id)
    )
    stmt = _posted(stmt, company_id)
    if start_date:
        stmt = stmt.where(JournalLine.entry_date >= start_date)
    if end_date:
        stmt = stmt.where(JournalLine.entry_date <= end_date)
    if filters:
        stmt = filters.apply(stmt, include_dates=False)

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = int(db.execute(count_stmt).scalar_one())

    stmt = stmt.order_by(
        JournalLine.entry_date.asc(), JournalEntry.entry_number.asc(), JournalLine.line_number.asc()
    ).limit(limit).offset(offset)

    running = opening
    rows = []
    for line, entry in db.execute(stmt):
        delta = (
            money(line.debit - line.credit)
            if account.normal_balance == NormalBalance.DEBIT
            else money(line.credit - line.debit)
        )
        running = money(running + delta)
        rows.append(
            {
                "line_id": line.id,
                "entry_id": entry.id,
                "entry_number": entry.entry_number,
                "date": line.entry_date,
                "voucher_type": entry.voucher_type,
                "reference": entry.reference,
                "description": line.description or entry.narration,
                "debit": line.debit,
                "credit": line.credit,
                "balance": running,
                "party_type": line.party_type,
                "party_id": line.party_id,
                "cost_center_id": line.cost_center_id,
                "source_type": entry.source_type,
                "source_id": entry.source_id,
                "reconciled": line.reconciled,
            }
        )

    return {
        "account": {
            "id": account.id,
            "code": account.code,
            "name": account.name,
            "type": account.type,
            "normal_balance": account.normal_balance,
        },
        "opening_balance": opening,
        "closing_balance": running,
        "rows": rows,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def day_book(
    db: Session,
    company_id: str,
    *,
    start_date: date,
    end_date: date,
    voucher_types: Sequence[str] | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict:
    """All vouchers in a date range, newest first."""
    stmt = select(JournalEntry).where(
        JournalEntry.company_id == company_id,
        JournalEntry.status == JournalStatus.POSTED,
        JournalEntry.entry_date >= start_date,
        JournalEntry.entry_date <= end_date,
    )
    if voucher_types:
        stmt = stmt.where(JournalEntry.voucher_type.in_(list(voucher_types)))

    total = int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    entries = list(
        db.execute(
            stmt.order_by(JournalEntry.entry_date.desc(), JournalEntry.entry_number.desc())
            .limit(limit)
            .offset(offset)
        ).scalars()
    )

    return {
        "start_date": start_date,
        "end_date": end_date,
        "entries": [
            {
                "id": entry.id,
                "entry_number": entry.entry_number,
                "date": entry.entry_date,
                "voucher_type": entry.voucher_type,
                "narration": entry.narration,
                "reference": entry.reference,
                "amount": entry.total_debit,
                "source": entry.source,
                "status": entry.status,
            }
            for entry in entries
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def verify_invariants(db: Session, company_id: str, *, as_of: date | None = None) -> dict:
    """Run every structural accounting invariant and report pass/fail.

    Exposed through the API and used by the close checklist and the test suite.
    """
    results: list[dict] = []

    # 1. Every posted entry balances internally.
    unbalanced = list(
        db.execute(
            select(JournalEntry.id, JournalEntry.entry_number, JournalEntry.total_debit, JournalEntry.total_credit)
            .where(
                JournalEntry.company_id == company_id,
                JournalEntry.status == JournalStatus.POSTED,
                JournalEntry.total_debit != JournalEntry.total_credit,
            )
            .limit(20)
        )
    )
    results.append(
        {
            "name": "Every posted entry balances",
            "passed": not unbalanced,
            "detail": (
                "All posted entries have equal debits and credits."
                if not unbalanced
                else f"{len(unbalanced)} unbalanced entries found."
            ),
            "offenders": [
                {"id": row[0], "entry_number": row[1], "debit": str(row[2]), "credit": str(row[3])}
                for row in unbalanced
            ],
        }
    )

    # 2. Ledger-wide debits equal credits.
    stmt = select(
        func.sum(JournalLine.debit).label("debit"), func.sum(JournalLine.credit).label("credit")
    )
    stmt = _posted(stmt, company_id)
    if as_of:
        stmt = stmt.where(JournalLine.entry_date <= as_of)
    row = db.execute(stmt).one()
    total_debit, total_credit = money(row.debit or 0), money(row.credit or 0)
    results.append(
        {
            "name": "Trial balance balances",
            "passed": total_debit == total_credit,
            "detail": f"Debits {total_debit} vs credits {total_credit}.",
            "offenders": [],
        }
    )

    # 3. Assets = Liabilities + Equity + (Revenue - Expenses)
    balances = account_balances(
        db, company_id, filters=LedgerFilters(end_date=as_of), include_zero=False
    )
    by_type: dict[str, Decimal] = {}
    for balance in balances:
        by_type[balance.type] = money(by_type.get(balance.type, ZERO) + balance.net_debit)

    assets = by_type.get(AccountType.ASSET, ZERO)
    liabilities = -by_type.get(AccountType.LIABILITY, ZERO)
    equity = -by_type.get(AccountType.EQUITY, ZERO)
    revenue = -by_type.get(AccountType.REVENUE, ZERO)
    expenses = by_type.get(AccountType.EXPENSE, ZERO)
    net_profit = money(revenue - expenses)
    accounting_equation_diff = money(assets - (liabilities + equity + net_profit))

    results.append(
        {
            "name": "Assets = Liabilities + Equity + Net Profit",
            "passed": accounting_equation_diff == ZERO,
            "detail": (
                f"Assets {assets} vs liabilities {liabilities} + equity {equity} "
                f"+ net profit {net_profit} (difference {accounting_equation_diff})."
            ),
            "offenders": [],
        }
    )

    # 4. No posted line sits on a group account.
    group_lines = list(
        db.execute(
            select(func.count(JournalLine.id))
            .join(Account, Account.id == JournalLine.account_id)
            .where(
                JournalLine.company_id == company_id,
                JournalLine.status == JournalStatus.POSTED,
                Account.is_group.is_(True),
            )
        ).scalars()
    )
    group_count = int(group_lines[0]) if group_lines else 0
    results.append(
        {
            "name": "No postings to group accounts",
            "passed": group_count == 0,
            "detail": f"{group_count} posted lines on group accounts.",
            "offenders": [],
        }
    )

    # 5. Each line has exactly one side.
    bad_sides = int(
        db.execute(
            select(func.count(JournalLine.id)).where(
                JournalLine.company_id == company_id,
                JournalLine.status == JournalStatus.POSTED,
                or_(
                    and_(JournalLine.debit > 0, JournalLine.credit > 0),
                    and_(JournalLine.debit == 0, JournalLine.credit == 0),
                ),
            )
        ).scalar_one()
    )
    results.append(
        {
            "name": "Each line has exactly one side",
            "passed": bad_sides == 0,
            "detail": f"{bad_sides} lines carry both or neither of debit/credit.",
            "offenders": [],
        }
    )

    return {
        "checked_at": as_of,
        "all_passed": all(item["passed"] for item in results),
        "checks": results,
        "totals": {
            "assets": assets,
            "liabilities": liabilities,
            "equity": equity,
            "revenue": revenue,
            "expenses": expenses,
            "net_profit": net_profit,
            "total_debit": total_debit,
            "total_credit": total_credit,
        },
    }
