"""Demo company endpoints — first-run experience."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.database import get_db
from app.deps import CurrentUserDep, DbDep, require_permission
from app.enums import Permission
from app.errors import ConflictError
from app.models import Company, User
from app.schemas import MessageResponse

router = APIRouter(prefix="/demo", tags=["Demo"])


@router.get("/status")
def demo_status(db: DbDep) -> dict:
    """Is a demo company loaded? Used by the first-run screen (no auth needed)."""
    from app.demo.generator import demo_company_exists

    company = demo_company_exists(db)
    has_users = int(db.execute(select(func.count(User.id))).scalar_one()) > 0

    return {
        "demo_loaded": company is not None,
        "company_id": company.id if company else None,
        "company_name": company.legal_name if company else None,
        "has_users": has_users,
        "login_email": "demo@ailedger.local" if company else None,
        "login_password": "demo1234" if company else None,
        "notice": (
            "This is generated demo data for evaluation. Every figure comes from "
            "transactions posted through the real accounting engine."
        ),
    }


@router.post("/load")
def load_demo(
    db: DbDep,
    months: int = Query(default=12, ge=1, le=24),
    force: bool = Query(default=False),
) -> dict:
    """Generate the NovaTech demo company.

    Open by design: this is the first-run path, before any account exists. It
    refuses to run when a demo already exists unless ``force`` is passed.
    """
    from app.demo.generator import demo_company_exists, generate_demo_company, reset_demo

    existing = demo_company_exists(db)
    if existing is not None:
        if not force:
            raise ConflictError(
                f"A demo company ('{existing.legal_name}') is already loaded. "
                "Pass force=true to rebuild it from scratch.",
                details={"company_id": existing.id},
                code="demo_exists",
            )
        reset_demo(db)

    stats = generate_demo_company(db, months=months)
    return {
        **stats.as_dict(),
        "message": (
            f"Demo company created with {stats.invoices} invoices, {stats.bills} bills and "
            f"{stats.journal_entries} journal entries across {months} months. "
            f"Sign in as {stats.login_email} / {stats.login_password}."
        ),
        "notice": "DEMO DATA — generated locally for evaluation.",
    }


@router.post("/reset", response_model=MessageResponse)
def reset(
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_SETTINGS.value))],
) -> MessageResponse:
    """Delete every demo company. Real companies are untouched."""
    from app.demo.generator import reset_demo

    count = reset_demo(db)
    return MessageResponse(message=f"{count} demo company/companies removed.")


@router.get("/verify")
def verify_demo(db: DbDep, current: CurrentUserDep) -> dict:
    """Prove the demo books are internally consistent."""
    current.require(Permission.READ_REPORTS.value)

    from app.accounting.ledger import trial_balance, verify_invariants
    from app.services.audit import verify_chain
    from app.services.inventory import rebuild_all_valuations
    from app.services.reports import balance_sheet, cash_flow
    from datetime import date

    today = date.today()
    invariants = verify_invariants(db, current.company_id)
    tb = trial_balance(db, current.company_id)
    bs = balance_sheet(db, current.company_id, as_of=today)
    start = date(today.year, 4, 1) if today.month >= 4 else date(today.year - 1, 4, 1)
    cf = cash_flow(db, current.company_id, start_date=start, end_date=today)
    chain = verify_chain(db, current.company_id)
    stock = rebuild_all_valuations(db, current.company_id)

    checks = [
        {"name": "Accounting invariants", "passed": invariants["all_passed"]},
        {"name": "Trial balance balances", "passed": tb["balanced"]},
        {"name": "Balance sheet balances", "passed": bs["balanced"]},
        {"name": "Cash flow reconciles", "passed": cf["reconciles"]},
        {"name": "Audit chain intact", "passed": chain["valid"]},
        {"name": "Stock valuation consistent", "passed": stock["inconsistent"] == 0},
    ]

    return {
        "all_passed": all(check["passed"] for check in checks),
        "checks": checks,
        "trial_balance_total": str(tb["total_debit"]),
        "balance_sheet_difference": str(bs["difference"]),
        "cash_flow_unclassified": str(cf["unclassified"]),
        "audit_events": chain["events_checked"],
        "stock_value": str(stock["total_value"]),
    }
