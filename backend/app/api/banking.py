"""Banking endpoints: accounts, statement import, categorisation, reconciliation."""
from __future__ import annotations

import hashlib
import io
import json
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Query, UploadFile
from sqlalchemy import select

from app.config import settings
from app.deps import CurrentUserDep, DbDep, require_permission
from app.enums import Permission
from app.errors import NotFoundError, ValidationError
from app.models import BankAccount
from app.schemas import (
    BankAccountCreate,
    BankAccountOut,
    BankTransactionOut,
    CategoriseRequest,
    ImportResultOut,
    MatchActionRequest,
    MessageResponse,
    Page,
    ReconcileRequest,
)
from app.services import banking as banking_service
from app.services import reconciliation as recon_service

router = APIRouter(prefix="/banking", tags=["Banking"])


# ---------------------------------------------------------------- accounts --

@router.get("/accounts", response_model=list[BankAccountOut])
def list_bank_accounts(db: DbDep, current: CurrentUserDep) -> list[BankAccountOut]:
    current.require(Permission.READ_TRANSACTIONS.value)

    accounts = list(
        db.execute(
            select(BankAccount).where(
                BankAccount.company_id == current.company_id, BankAccount.is_active.is_(True)
            )
        ).scalars()
    )

    results = []
    for account in accounts:
        book_balance = banking_service.bank_book_balance(db, current.company_id, account.id)
        out = BankAccountOut.model_validate(account)
        out.masked_number = f"****{account.account_number[-4:]}"
        out.book_balance = book_balance
        if account.statement_balance is not None:
            from app.money import money

            out.difference = money(account.statement_balance - book_balance)
        results.append(out)
    return results


@router.post("/accounts", response_model=BankAccountOut, status_code=201)
def create_bank_account(
    payload: BankAccountCreate,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_SETTINGS.value))],
) -> BankAccountOut:
    account = banking_service.create_bank_account(
        db,
        current.company_id,
        name=payload.name,
        bank_name=payload.bank_name,
        account_number=payload.account_number,
        ifsc=payload.ifsc,
        branch_name=payload.branch_name,
        account_type=payload.account_type.value,
        currency=payload.currency,
        opening_balance=payload.opening_balance,
        opening_balance_date=payload.opening_balance_date,
        user_id=current.id,
    )
    out = BankAccountOut.model_validate(account)
    out.masked_number = f"****{account.account_number[-4:]}"
    out.book_balance = banking_service.bank_book_balance(db, current.company_id, account.id)
    return out


# ------------------------------------------------------------ transactions --

@router.get("/transactions", response_model=Page[BankTransactionOut])
def list_transactions(
    db: DbDep,
    current: CurrentUserDep,
    bank_account_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    search: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[BankTransactionOut]:
    current.require(Permission.READ_TRANSACTIONS.value)
    result = banking_service.list_transactions(
        db,
        current.company_id,
        bank_account_id=bank_account_id,
        status=status.upper() if status else None,
        start_date=start_date,
        end_date=end_date,
        search=search,
        limit=limit,
        offset=offset,
    )
    return Page(
        items=[BankTransactionOut.model_validate(t) for t in result["transactions"]],
        total=result["total"],
        limit=limit,
        offset=offset,
    )


@router.post("/transactions/{transaction_id}/categorise", response_model=BankTransactionOut)
def categorise_transaction(
    transaction_id: str,
    payload: CategoriseRequest,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.POST_JOURNAL.value))],
) -> BankTransactionOut:
    """Post an uncategorised bank line to a chosen account and learn from it."""
    transaction = banking_service.post_bank_transaction(
        db,
        current.company_id,
        transaction_id,
        account_id=payload.account_id,
        description=payload.description,
        cost_center_id=payload.cost_center_id,
        learn=payload.learn,
        user_id=current.id,
    )
    return BankTransactionOut.model_validate(transaction)


@router.get("/summary")
def reconciliation_summary(db: DbDep, current: CurrentUserDep) -> dict:
    current.require(Permission.READ_TRANSACTIONS.value)
    return banking_service.unreconciled_summary(db, current.company_id)


# ----------------------------------------------------------------- import --

@router.post("/accounts/{bank_account_id}/import/preview")
async def preview_import(
    bank_account_id: str,
    db: DbDep,
    current: CurrentUserDep,
    file: UploadFile = File(...),
) -> dict:
    """Parse an uploaded statement and show the detected column mapping."""
    current.require(Permission.READ_TRANSACTIONS.value)
    _assert_bank_account(db, current.company_id, bank_account_id)

    content = await file.read()
    records, columns = _parse_tabular(content, file.filename or "")
    mapping = banking_service.detect_columns(columns)

    missing = [field for field in ("date",) if field not in mapping]
    has_amount = any(field in mapping for field in ("debit", "credit", "amount"))
    warnings: list[str] = []
    if missing:
        warnings.append(f"Could not detect a column for: {', '.join(missing)}.")
    if not has_amount:
        warnings.append("Could not detect a debit, credit or amount column.")

    rows, errors = banking_service.rows_from_records(records[:20], mapping)

    return {
        "columns": columns,
        "detected_mapping": mapping,
        "sample_rows": [
            {
                "date": row.txn_date.isoformat(),
                "description": row.description,
                "reference": row.reference,
                "direction": row.direction,
                "amount": str(row.amount),
                "balance": str(row.balance) if row.balance is not None else None,
            }
            for row in rows[:10]
        ],
        "total_rows": len(records),
        "warnings": warnings,
        "sample_errors": errors[:5],
    }


@router.post("/accounts/{bank_account_id}/import", response_model=ImportResultOut)
async def import_statement(
    bank_account_id: str,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.MANAGE_SETTINGS.value))],
    file: UploadFile = File(...),
    mapping: str | None = Query(default=None, description="JSON column mapping override"),
) -> ImportResultOut:
    """Import a bank statement. Re-importing the same lines is a no-op."""
    _assert_bank_account(db, current.company_id, bank_account_id)

    content = await file.read()
    if len(content) > settings.MAX_UPLOAD_BYTES:
        raise ValidationError("The file exceeds the upload size limit.", code="file_too_large")

    records, columns = _parse_tabular(content, file.filename or "")
    column_mapping = banking_service.detect_columns(columns)
    if mapping:
        try:
            column_mapping.update(json.loads(mapping))
        except json.JSONDecodeError as exc:
            raise ValidationError("The mapping override is not valid JSON.") from exc

    rows, errors = banking_service.rows_from_records(records, column_mapping)
    result = banking_service.import_transactions(
        db,
        current.company_id,
        bank_account_id,
        rows,
        filename=file.filename or "",
        file_hash=hashlib.sha256(content).hexdigest(),
        source=(file.filename or "").rsplit(".", 1)[-1].upper() or "CSV",
        errors=errors,
        user_id=current.id,
    )

    return ImportResultOut(
        total=result.total,
        imported=result.imported,
        duplicates=result.duplicates,
        rejected=result.rejected,
        warnings=result.warnings,
        errors=result.errors[:50],
        batch_id=result.statement_id,
    )


# --------------------------------------------------------- reconciliation --

@router.post("/reconcile")
def run_reconciliation(
    payload: ReconcileRequest,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.PROPOSE_RECONCILIATION.value))],
) -> dict:
    """Run the matching engine over unreconciled transactions."""
    accounts = (
        [payload.bank_account_id]
        if payload.bank_account_id
        else [
            account.id
            for account in db.execute(
                select(BankAccount).where(
                    BankAccount.company_id == current.company_id,
                    BankAccount.is_active.is_(True),
                )
            ).scalars()
        ]
    )
    if not accounts:
        raise NotFoundError("No bank accounts are configured.")

    sessions = []
    for account_id in accounts:
        session = recon_service.run_reconciliation(
            db,
            current.company_id,
            account_id,
            start_date=payload.start_date,
            end_date=payload.end_date,
            auto_confirm=payload.auto_confirm,
            user_id=current.id,
        )
        sessions.append(
            {
                "session_id": session.id,
                "bank_account_id": account_id,
                "total_transactions": session.total_transactions,
                "matched": session.matched_count,
                "auto_matched": session.auto_matched_count,
                "for_review": session.partial_count,
                "ambiguous": session.ambiguous_count,
                "unmatched": session.unmatched_count,
                "book_balance": str(session.book_balance) if session.book_balance is not None else None,
                "bank_balance": str(session.bank_balance) if session.bank_balance is not None else None,
                "difference": str(session.difference) if session.difference is not None else None,
            }
        )

    return {
        "sessions": sessions,
        "total_matched": sum(s["matched"] for s in sessions),
        "total_for_review": sum(s["for_review"] + s["ambiguous"] for s in sessions),
    }


@router.get("/reconciliation/{bank_account_id}")
def reconciliation_view(
    bank_account_id: str,
    db: DbDep,
    current: CurrentUserDep,
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """The split bank-vs-books view with proposed matches and their evidence."""
    current.require(Permission.READ_TRANSACTIONS.value)
    return recon_service.reconciliation_view(
        db,
        current.company_id,
        bank_account_id,
        status=status.upper() if status else None,
        limit=limit,
        offset=offset,
    )


@router.get("/transactions/{transaction_id}/candidates")
def match_candidates(
    transaction_id: str,
    db: DbDep,
    current: CurrentUserDep,
    tolerance_days: int = Query(default=15, ge=0, le=90),
) -> dict:
    """Scored candidate ledger lines for one bank transaction, with evidence."""
    current.require(Permission.READ_TRANSACTIONS.value)

    from app.models import BankTransaction

    transaction = db.get(BankTransaction, transaction_id)
    if transaction is None or transaction.company_id != current.company_id:
        raise NotFoundError("Bank transaction not found.")

    candidates = recon_service.find_candidates(
        db, current.company_id, transaction, tolerance_days=tolerance_days
    )
    return {
        "transaction": {
            "id": transaction.id,
            "date": transaction.txn_date,
            "description": transaction.description,
            "amount": str(transaction.amount),
            "direction": transaction.direction,
        },
        "candidates": [
            {
                "journal_line_id": candidate.journal_line.id,
                "entry_id": candidate.entry.id,
                "entry_number": candidate.entry.entry_number,
                "date": candidate.journal_line.entry_date,
                "amount": str(candidate.journal_line.debit or candidate.journal_line.credit),
                "description": (
                    candidate.journal_line.description or candidate.entry.narration
                )[:150],
                "score": round(candidate.score, 1),
                "confidence": str(candidate.confidence),
                "strategies": candidate.strategies,
                "reasons": candidate.reasons,
                "evidence": candidate.evidence,
            }
            for candidate in candidates
        ],
    }


@router.post("/matches/{match_id}/confirm")
def confirm_match(
    match_id: str,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.POST_JOURNAL.value))],
) -> dict:
    match = recon_service.confirm_match(db, current.company_id, match_id, user_id=current.id)
    return {"id": match.id, "status": match.status, "confirmed": match.is_confirmed}


@router.post("/matches/{match_id}/reject")
def reject_match(
    match_id: str,
    payload: MatchActionRequest,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.PROPOSE_RECONCILIATION.value))],
) -> dict:
    match = recon_service.reject_match(
        db, current.company_id, match_id, user_id=current.id, reason=payload.reason
    )
    return {"id": match.id, "status": match.status}


@router.post("/transactions/{transaction_id}/unmatch")
def unmatch_transaction(
    transaction_id: str,
    payload: MatchActionRequest,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.POST_JOURNAL.value))],
) -> dict:
    transaction = recon_service.unmatch(
        db, current.company_id, transaction_id, user_id=current.id, reason=payload.reason
    )
    return {"id": transaction.id, "status": transaction.recon_status}


@router.post("/matches/manual")
def create_manual_match(
    bank_transaction_id: Annotated[str, Query()],
    journal_line_id: Annotated[str, Query()],
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.POST_JOURNAL.value))],
) -> dict:
    """Human-created match, verified by the same checks as an AI proposal."""
    match = recon_service.propose_ai_match(
        db,
        current.company_id,
        bank_transaction_id=bank_transaction_id,
        journal_line_id=journal_line_id,
        confidence=1,
        reasoning="Matched manually by a user.",
    )
    match.proposed_by = "HUMAN"
    match.strategy = "MANUAL"
    confirmed = recon_service.confirm_match(db, current.company_id, match.id, user_id=current.id)
    return {"id": confirmed.id, "status": confirmed.status, "confirmed": confirmed.is_confirmed}


# -------------------------------------------------------------- helpers ---

def _assert_bank_account(db, company_id: str, bank_account_id: str) -> BankAccount:
    account = db.get(BankAccount, bank_account_id)
    if account is None or account.company_id != company_id:
        raise NotFoundError("Bank account not found.")
    return account


def _parse_tabular(content: bytes, filename: str) -> tuple[list[dict], list[str]]:
    """Parse CSV, XLSX or JSON into records plus the header list."""
    name = (filename or "").lower()

    if name.endswith(".json"):
        try:
            payload = json.loads(content.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValidationError("The file is not valid JSON.", code="invalid_json") from exc
        records = payload if isinstance(payload, list) else payload.get("transactions", [])
        if not isinstance(records, list):
            raise ValidationError("JSON must be an array of rows, or {\"transactions\": [...]}.")
        columns = list(records[0].keys()) if records and isinstance(records[0], dict) else []
        return records, columns

    if name.endswith((".xlsx", ".xls")):
        try:
            import pandas as pd

            frame = pd.read_excel(io.BytesIO(content), dtype=object)
        except ImportError as exc:  # pragma: no cover
            raise ValidationError(
                "Excel support requires pandas and openpyxl.", code="xlsx_unsupported"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise ValidationError(f"Could not read the spreadsheet: {exc}") from exc
        frame = frame.where(frame.notna(), None)
        return frame.to_dict(orient="records"), [str(column) for column in frame.columns]

    # Default: CSV, tolerating the usual encodings and a preamble.
    text = None
    for encoding in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValidationError("Could not decode the file as text.", code="encoding_error")

    import csv

    lines = text.splitlines()
    # Some bank exports prepend account details before the real header row.
    header_index = 0
    best_columns = 0
    for index, line in enumerate(lines[:25]):
        count = max(line.count(","), line.count(";"), line.count("\t"))
        if count > best_columns:
            best_columns, header_index = count, index

    body = "\n".join(lines[header_index:])
    dialect = csv.Sniffer().sniff(body[:4000], delimiters=",;\t|") if body[:4000] else csv.excel
    reader = csv.DictReader(io.StringIO(body), dialect=dialect)
    records = [
        {(key or "").strip(): value for key, value in row.items() if key}
        for row in reader
    ]
    columns = [str(name).strip() for name in (reader.fieldnames or [])]
    return records, columns
