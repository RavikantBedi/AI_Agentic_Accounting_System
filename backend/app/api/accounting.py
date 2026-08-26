"""Accounting endpoints: chart of accounts, journals, periods and reports feed."""
from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select

from app.accounting.coa import create_account, deactivate_account, seed_chart_of_accounts
from app.accounting.engine import AccountingEngine, EntryInput, LineInput
from app.accounting.ledger import (
    LedgerFilters,
    account_balances,
    day_book,
    general_ledger,
    trial_balance,
    verify_invariants,
)
from app.accounting.periods import (
    close_period,
    ensure_fiscal_year,
    list_periods,
    period_checks,
    reopen_period,
)
from app.deps import CurrentUserDep, DbDep, audit_context, require_permission
from app.enums import JournalStatus, Permission
from app.errors import NotFoundError
from app.models import Account, JournalEntry, JournalLine
from app.money import ZERO, money
from app.schemas import (
    AccountCreate,
    AccountOut,
    AccountWithBalance,
    ClosePeriodRequest,
    JournalEntryCreate,
    JournalEntryOut,
    JournalLineOut,
    MessageResponse,
    Page,
    PeriodOut,
    ReopenPeriodRequest,
    ReverseEntryRequest,
)

router = APIRouter(prefix="/accounting", tags=["Accounting"])


# ------------------------------------------------------ chart of accounts --

@router.get("/accounts", response_model=list[AccountWithBalance])
def list_accounts(
    db: DbDep,
    current: CurrentUserDep,
    include_balances: bool = Query(default=True),
    include_inactive: bool = Query(default=False),
    account_type: str | None = Query(default=None),
    as_of: date | None = Query(default=None),
) -> list[AccountWithBalance]:
    """The chart of accounts as a tree, optionally with balances."""
    current.require(Permission.READ_ACCOUNTS.value)

    stmt = select(Account).where(Account.company_id == current.company_id)
    if not include_inactive:
        stmt = stmt.where(Account.is_active.is_(True))
    if account_type:
        stmt = stmt.where(Account.type == account_type.upper())

    accounts = list(db.execute(stmt.order_by(Account.code)).scalars())

    balances: dict[str, tuple] = {}
    if include_balances:
        for balance in account_balances(
            db,
            current.company_id,
            filters=LedgerFilters(end_date=as_of),
            include_zero=True,
        ):
            balances[balance.account_id] = (balance.balance, balance.debit, balance.credit)

    nodes: dict[str, AccountWithBalance] = {}
    for account in accounts:
        balance, debit, credit = balances.get(account.id, (ZERO, ZERO, ZERO))
        node = AccountWithBalance.model_validate(account)
        node.balance, node.debit, node.credit = balance, debit, credit
        node.children = []
        nodes[account.id] = node

    # Group accounts roll up the balances of their descendants.
    roots: list[AccountWithBalance] = []
    for account in accounts:
        node = nodes[account.id]
        if account.parent_id and account.parent_id in nodes:
            nodes[account.parent_id].children.append(node)
        else:
            roots.append(node)

    def rollup(node: AccountWithBalance) -> tuple:
        total, debit, credit = node.balance, node.debit, node.credit
        for child in node.children:
            child_total, child_debit, child_credit = rollup(child)
            total = money(total + child_total)
            debit = money(debit + child_debit)
            credit = money(credit + child_credit)
        node.balance, node.debit, node.credit = total, debit, credit
        return total, debit, credit

    for root in roots:
        rollup(root)

    return roots


@router.post("/accounts", response_model=AccountOut, status_code=201)
def create_account_endpoint(
    payload: AccountCreate,
    request: Request,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_SETTINGS.value))],
) -> AccountOut:
    account = create_account(
        db,
        current.company_id,
        code=payload.code,
        name=payload.name,
        type=payload.type.value,
        subtype=payload.subtype,
        parent_id=payload.parent_id,
        is_group=payload.is_group,
        tax_category=payload.tax_category,
        hsn_sac=payload.hsn_sac,
        description=payload.description,
        is_reconcilable=payload.is_reconcilable,
    )

    from app.enums import ActorType, AuditAction
    from app.services.audit import record_event

    record_event(
        db,
        company_id=current.company_id,
        action=AuditAction.CREATE,
        entity_type="Account",
        entity_id=account.id,
        entity_label=f"{account.code} {account.name}",
        summary=f"Created account {account.code} {account.name}",
        actor_type=ActorType.USER,
        actor_id=current.id,
        actor_label=current.email,
        after={"code": account.code, "name": account.name, "type": account.type},
        **audit_context(request),
    )
    return AccountOut.model_validate(account)


@router.delete("/accounts/{account_id}", response_model=MessageResponse)
def deactivate_account_endpoint(
    account_id: str,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_SETTINGS.value))],
) -> MessageResponse:
    account = deactivate_account(db, current.company_id, account_id)
    return MessageResponse(message=f"Account {account.code} deactivated.")


@router.post("/accounts/seed-defaults", response_model=MessageResponse)
def seed_defaults(
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_SETTINGS.value))],
) -> MessageResponse:
    accounts = seed_chart_of_accounts(db, current.company_id)
    return MessageResponse(message=f"Chart of accounts ready with {len(accounts)} accounts.")


# --------------------------------------------------------------- journals --

@router.get("/journals", response_model=Page[JournalEntryOut])
def list_journals(
    db: DbDep,
    current: CurrentUserDep,
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    voucher_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    search: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[JournalEntryOut]:
    current.require(Permission.READ_TRANSACTIONS.value)

    stmt = select(JournalEntry).where(JournalEntry.company_id == current.company_id)
    if start_date:
        stmt = stmt.where(JournalEntry.entry_date >= start_date)
    if end_date:
        stmt = stmt.where(JournalEntry.entry_date <= end_date)
    if voucher_type:
        stmt = stmt.where(JournalEntry.voucher_type == voucher_type.upper())
    if status:
        stmt = stmt.where(JournalEntry.status == status.upper())
    if search:
        pattern = f"%{search.lower()}%"
        stmt = stmt.where(
            func.lower(JournalEntry.narration).like(pattern)
            | func.lower(JournalEntry.entry_number).like(pattern)
            | func.lower(JournalEntry.reference).like(pattern)
        )

    total = int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    entries = list(
        db.execute(
            stmt.order_by(JournalEntry.entry_date.desc(), JournalEntry.created_at.desc())
            .limit(limit)
            .offset(offset)
        ).scalars()
    )

    return Page(items=[_entry_out(db, entry) for entry in entries], total=total, limit=limit, offset=offset)


@router.get("/journals/{entry_id}", response_model=JournalEntryOut)
def get_journal(entry_id: str, db: DbDep, current: CurrentUserDep) -> JournalEntryOut:
    current.require(Permission.READ_TRANSACTIONS.value)
    entry = db.get(JournalEntry, entry_id)
    if entry is None or entry.company_id != current.company_id:
        raise NotFoundError("Journal entry not found.")
    return _entry_out(db, entry)


@router.post("/journals", response_model=JournalEntryOut, status_code=201)
def create_journal(
    payload: JournalEntryCreate,
    request: Request,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.PROPOSE_JOURNAL.value))],
) -> JournalEntryOut:
    """Create a journal entry, posting it when the caller may post."""
    engine = AccountingEngine(db, current.company_id, actor_user_id=current.id)
    ensure_fiscal_year(db, current.company_id, payload.entry_date)

    entry_input = EntryInput(
        entry_date=payload.entry_date,
        voucher_type=payload.voucher_type.value,
        narration=payload.narration,
        reference=payload.reference,
        branch_id=payload.branch_id,
        lines=[
            LineInput(
                account_id=line.account_id,
                account_code=line.account_code,
                debit=line.debit,
                credit=line.credit,
                description=line.description,
                cost_center_id=line.cost_center_id,
                party_type=line.party_type,
                party_id=line.party_id,
                branch_id=line.branch_id,
                project=line.project,
            )
            for line in payload.lines
        ],
    )

    should_post = payload.post and current.has(Permission.POST_JOURNAL.value)
    entry = (
        engine.create_and_post(entry_input) if should_post else engine.create_draft(entry_input)
    )
    return _entry_out(db, entry)


@router.post("/journals/{entry_id}/post", response_model=JournalEntryOut)
def post_journal(
    entry_id: str,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.POST_JOURNAL.value))],
) -> JournalEntryOut:
    engine = AccountingEngine(db, current.company_id, actor_user_id=current.id)
    entry = engine.post(entry_id)
    return _entry_out(db, entry)


@router.post("/journals/{entry_id}/reverse", response_model=JournalEntryOut)
def reverse_journal(
    entry_id: str,
    payload: ReverseEntryRequest,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.POST_JOURNAL.value))],
) -> JournalEntryOut:
    """Reverse a posted entry. This is the only way to correct posted books."""
    engine = AccountingEngine(db, current.company_id, actor_user_id=current.id)
    reversal = engine.reverse(
        entry_id, reversal_date=payload.reversal_date, reason=payload.reason
    )
    return _entry_out(db, reversal)


# ---------------------------------------------------------------- ledger --

@router.get("/trial-balance")
def get_trial_balance(
    db: DbDep,
    current: CurrentUserDep,
    as_of: date | None = Query(default=None),
    start_date: date | None = Query(default=None),
    include_zero: bool = Query(default=False),
) -> dict:
    current.require(Permission.READ_REPORTS.value)
    return trial_balance(
        db, current.company_id, as_of=as_of, start_date=start_date, include_zero=include_zero
    )


@router.get("/ledger/{account_id}")
def get_ledger(
    account_id: str,
    db: DbDep,
    current: CurrentUserDep,
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    current.require(Permission.READ_TRANSACTIONS.value)
    return general_ledger(
        db,
        current.company_id,
        account_id,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        offset=offset,
    )


@router.get("/day-book")
def get_day_book(
    db: DbDep,
    current: CurrentUserDep,
    start_date: date = Query(...),
    end_date: date = Query(...),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    current.require(Permission.READ_TRANSACTIONS.value)
    return day_book(
        db, current.company_id, start_date=start_date, end_date=end_date, limit=limit, offset=offset
    )


@router.get("/invariants")
def check_invariants(
    db: DbDep, current: CurrentUserDep, as_of: date | None = Query(default=None)
) -> dict:
    """Run every structural accounting invariant and report pass/fail."""
    current.require(Permission.READ_REPORTS.value)
    return verify_invariants(db, current.company_id, as_of=as_of)


# --------------------------------------------------------------- periods --

@router.get("/periods", response_model=list[PeriodOut])
def get_periods(db: DbDep, current: CurrentUserDep) -> list[PeriodOut]:
    current.require(Permission.READ_ACCOUNTS.value)
    return [PeriodOut.model_validate(period) for period in list_periods(db, current.company_id)]


@router.get("/periods/{period_id}/checks")
def get_period_checks(period_id: str, db: DbDep, current: CurrentUserDep) -> dict:
    current.require(Permission.READ_REPORTS.value)
    from app.models import AccountingPeriod

    period = db.get(AccountingPeriod, period_id)
    if period is None or period.company_id != current.company_id:
        raise NotFoundError("Accounting period not found.")
    return period_checks(db, current.company_id, period)


@router.post("/periods/{period_id}/close", response_model=PeriodOut)
def close_accounting_period(
    period_id: str,
    payload: ClosePeriodRequest,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.CLOSE_PERIOD.value))],
) -> PeriodOut:
    period = close_period(
        db,
        current.company_id,
        period_id,
        user_id=current.id,
        notes=payload.notes,
        force=payload.force,
    )
    return PeriodOut.model_validate(period)


@router.post("/periods/{period_id}/reopen", response_model=PeriodOut)
def reopen_accounting_period(
    period_id: str,
    payload: ReopenPeriodRequest,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.CLOSE_PERIOD.value))],
) -> PeriodOut:
    period = reopen_period(
        db, current.company_id, period_id, user_id=current.id, reason=payload.reason
    )
    return PeriodOut.model_validate(period)


@router.post("/fiscal-years/ensure", response_model=MessageResponse)
def ensure_year(
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_SETTINGS.value))],
    on_date: date = Query(default_factory=date.today),
) -> MessageResponse:
    fiscal_year = ensure_fiscal_year(db, current.company_id, on_date)
    return MessageResponse(message=f"Fiscal year {fiscal_year.name} is ready.")


# -------------------------------------------------------------- helpers ---

def _entry_out(db, entry: JournalEntry) -> JournalEntryOut:
    account_ids = [line.account_id for line in entry.lines]
    accounts = (
        {
            account.id: account
            for account in db.execute(select(Account).where(Account.id.in_(account_ids))).scalars()
        }
        if account_ids
        else {}
    )

    return JournalEntryOut(
        id=entry.id,
        entry_number=entry.entry_number,
        voucher_type=entry.voucher_type,
        entry_date=entry.entry_date,
        narration=entry.narration,
        reference=entry.reference,
        total_debit=entry.total_debit,
        total_credit=entry.total_credit,
        status=entry.status,
        source=entry.source,
        source_type=entry.source_type,
        source_id=entry.source_id,
        is_reversal=entry.is_reversal,
        reverses_entry_id=entry.reverses_entry_id,
        reversed_by_entry_id=entry.reversed_by_entry_id,
        posted_at=entry.posted_at,
        created_at=entry.created_at,
        lines=[
            JournalLineOut(
                id=line.id,
                line_number=line.line_number,
                account_id=line.account_id,
                account_code=accounts[line.account_id].code if line.account_id in accounts else "",
                account_name=accounts[line.account_id].name if line.account_id in accounts else "",
                debit=line.debit,
                credit=line.credit,
                description=line.description,
                cost_center_id=line.cost_center_id,
                party_type=line.party_type,
                party_id=line.party_id,
                reconciled=line.reconciled,
            )
            for line in entry.lines
        ],
    )
