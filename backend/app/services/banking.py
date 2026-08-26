"""Banking: accounts, statement import and transaction categorisation.

Statement lines are *external evidence* — they are stored verbatim and never
edited. Import is idempotent: every line gets a deterministic dedupe hash, so
re-importing an overlapping statement adds nothing.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.accounting.engine import AccountingEngine, EntryInput, LineInput
from app.enums import (
    ActorType,
    AuditAction,
    BankTxnDirection,
    EntrySource,
    JournalStatus,
    ReconStatus,
    VoucherType,
)
from app.errors import ConflictError, NotFoundError, ValidationError
from app.models import (
    Account,
    BankAccount,
    BankStatement,
    BankTransaction,
    CategorisationRule,
    JournalLine,
)
from app.money import ZERO, D, money
from app.services.audit import record_event

# Column aliases the importer understands, so most bank exports map themselves.
COLUMN_ALIASES: dict[str, list[str]] = {
    "date": ["date", "txn date", "transaction date", "value date", "posting date", "tran date", "date "],
    "description": ["description", "narration", "particulars", "details", "remarks", "transaction details", "narrative"],
    "reference": ["reference", "ref", "ref no", "cheque no", "chq no", "utr", "transaction id", "txn id", "reference number", "instrument no"],
    "debit": ["debit", "withdrawal", "withdrawal amt", "dr", "debit amount", "withdrawals", "paid out", "amount debit"],
    "credit": ["credit", "deposit", "deposit amt", "cr", "credit amount", "deposits", "paid in", "amount credit"],
    "amount": ["amount", "transaction amount", "amt", "value"],
    "balance": ["balance", "closing balance", "running balance", "balance amt", "available balance"],
    "type": ["type", "dr/cr", "transaction type", "txn type", "drcr"],
}

# Noise removed before comparing bank descriptions. Order matters: structured
# tokens (IFSC codes, reference numbers) are stripped while their delimiters are
# still present, because separators are what makes them recognisable.
_NOISE_PATTERNS = [
    # Payment rails and direction markers.
    r"\b(neft|rtgs|imps|upi|ach|ecs|nach|chq|cheque|clg|clearing|trf|transfer|pmt|payment)\b",
    # Reference tokens, with or without a separator: REF987654, UTR-123, TXN:99
    r"\b(ref|utr|txn|trn|rrn|id|no|nos|inb|mmt)[:\-\s]*[a-z0-9]{4,}\b",
    # IFSC codes: four letters, a literal '0', then six alphanumerics. The '0'
    # is required by the IFSC format and keeps ordinary 11-letter words
    # (like "maintenance") from being mistaken for a bank code.
    r"\b[a-z]{4}0[0-9a-z]{6}\b",
    # Standalone credit/debit markers left behind by the rail prefix.
    r"\b(cr|dr)\b",
    # Long digit runs: account numbers, reference ids.
    r"\b\d{5,}\b",
    # Anything else non-alphanumeric.
    r"[^a-z0-9\s]",
    # Short numeric fragments left over after the above.
    r"\b\d{1,4}\b",
]


def normalise_description(text: str) -> str:
    """Reduce a bank narration to comparable words.

    'NEFT-CR-HDFC0001234-GLOBEX LTD-REF987654' -> 'globex ltd'
    """
    value = (text or "").lower()
    for pattern in _NOISE_PATTERNS:
        value = re.sub(pattern, " ", value)
    return re.sub(r"\s+", " ", value).strip()


def extract_counterparty(text: str) -> str:
    """Best-effort counterparty extraction from a bank narration."""
    cleaned = normalise_description(text)
    if not cleaned:
        return ""
    words = [w for w in cleaned.split() if len(w) > 2]
    return " ".join(words[:4])


def compute_dedupe_hash(
    bank_account_id: str,
    txn_date: date,
    amount: Decimal,
    direction: str,
    description: str,
    reference: str,
    *,
    ordinal: int = 0,
) -> str:
    """Stable identity for a bank line.

    ``ordinal`` disambiguates genuinely repeated identical lines within one
    import (a real possibility: two identical 500.00 charges on one day).
    """
    payload = "|".join(
        [
            bank_account_id,
            txn_date.isoformat(),
            str(money(amount)),
            direction,
            normalise_description(description),
            (reference or "").strip().lower(),
            str(ordinal),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ------------------------------------------------------------- accounts ----

def create_bank_account(
    db: Session,
    company_id: str,
    *,
    name: str,
    bank_name: str,
    account_number: str,
    ifsc: str = "",
    account_type: str = "CURRENT",
    currency: str = "INR",
    opening_balance: Decimal = ZERO,
    opening_balance_date: date | None = None,
    gl_account_id: str | None = None,
    branch_name: str = "",
    user_id: str | None = None,
) -> BankAccount:
    """Create a bank account and, if needed, its dedicated GL account."""
    existing = db.execute(
        select(BankAccount).where(
            BankAccount.company_id == company_id,
            BankAccount.account_number == account_number,
        )
    ).scalar_one_or_none()
    if existing:
        raise ConflictError(f"A bank account ending {account_number[-4:]} already exists.")

    if gl_account_id is None:
        gl_account_id = _create_bank_gl_account(db, company_id, name, bank_name, account_number)

    account = BankAccount(
        company_id=company_id,
        name=name,
        bank_name=bank_name,
        account_number=account_number,
        ifsc=ifsc.upper(),
        branch_name=branch_name,
        account_type=account_type,
        currency=currency,
        gl_account_id=gl_account_id,
        opening_balance=money(opening_balance),
        opening_balance_date=opening_balance_date,
    )
    db.add(account)
    db.flush()

    record_event(
        db,
        company_id=company_id,
        action=AuditAction.CREATE,
        entity_type="BankAccount",
        entity_id=account.id,
        entity_label=f"{bank_name} ****{account_number[-4:]}",
        summary=f"Bank account '{name}' created",
        actor_type=ActorType.USER if user_id else ActorType.SYSTEM,
        actor_id=user_id,
        after={"name": name, "bank": bank_name, "masked": f"****{account_number[-4:]}"},
    )
    return account


def _create_bank_gl_account(
    db: Session, company_id: str, name: str, bank_name: str, account_number: str
) -> str:
    """Create a child GL account under the Bank Accounts group."""
    from app.accounting.coa import create_account
    from app.enums import AccountSubtype, AccountType

    parent = db.execute(
        select(Account).where(Account.company_id == company_id, Account.code == "1120")
    ).scalar_one_or_none()

    siblings = int(
        db.execute(
            select(func.count(Account.id)).where(
                Account.company_id == company_id, Account.parent_id == (parent.id if parent else None)
            )
        ).scalar_one()
    )
    code = str(1121 + siblings)
    while db.execute(
        select(Account).where(Account.company_id == company_id, Account.code == code)
    ).scalar_one_or_none():
        code = str(int(code) + 1)

    account = create_account(
        db,
        company_id,
        code=code,
        name=f"{bank_name} - {name}",
        type=AccountType.ASSET,
        subtype=AccountSubtype.BANK,
        parent_id=parent.id if parent else None,
        is_reconcilable=True,
        description=f"Bank account ****{account_number[-4:]}",
    )
    return account.id


def bank_book_balance(db: Session, company_id: str, bank_account_id: str, *, as_of: date | None = None) -> Decimal:
    """The balance *per the books* (GL), which reconciliation compares against."""
    from app.accounting.ledger import account_balance

    account = db.get(BankAccount, bank_account_id)
    if account is None or account.company_id != company_id:
        raise NotFoundError("Bank account not found.")
    return account_balance(db, company_id, account.gl_account_id, as_of=as_of)


# --------------------------------------------------------------- import ----

@dataclass
class ImportRow:
    txn_date: date
    description: str
    amount: Decimal
    direction: str
    reference: str = ""
    balance: Decimal | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class ImportResult:
    statement_id: str | None = None
    total: int = 0
    imported: int = 0
    duplicates: int = 0
    rejected: int = 0
    errors: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    opening_balance: Decimal | None = None
    closing_balance: Decimal | None = None

    def as_dict(self) -> dict:
        return {
            "statement_id": self.statement_id,
            "total": self.total,
            "imported": self.imported,
            "duplicates": self.duplicates,
            "rejected": self.rejected,
            "errors": self.errors[:100],
            "warnings": self.warnings,
            "opening_balance": self.opening_balance,
            "closing_balance": self.closing_balance,
        }


def detect_columns(headers: Iterable[str]) -> dict[str, str]:
    """Map file headers onto canonical field names."""
    mapping: dict[str, str] = {}
    normalised = {str(h).strip().lower(): str(h) for h in headers if h is not None}

    for field_name, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in normalised:
                mapping[field_name] = normalised[alias]
                break
        if field_name in mapping:
            continue
        # Fall back to a containment match ("Withdrawal Amount (INR)").
        for header_lower, original in normalised.items():
            if any(alias in header_lower for alias in aliases):
                mapping[field_name] = original
                break
    return mapping


def parse_date(value: Any) -> date | None:
    """Parse the date formats Indian bank exports actually use."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value).strip()
    formats = [
        "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y",
        "%m/%d/%Y", "%d %b %Y", "%d-%b-%Y", "%d %B %Y", "%Y/%m/%d",
        "%d.%m.%Y", "%Y-%m-%d %H:%M:%S", "%d-%b-%y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def rows_from_records(
    records: list[dict], mapping: dict[str, str]
) -> tuple[list[ImportRow], list[dict]]:
    """Convert mapped records into typed rows, collecting per-row errors."""
    rows: list[ImportRow] = []
    errors: list[dict] = []

    for index, record in enumerate(records, start=2):  # row 1 is the header
        try:
            raw_date = record.get(mapping.get("date", ""))
            txn_date = parse_date(raw_date)
            if txn_date is None:
                errors.append({"row": index, "error": f"Unrecognised date '{raw_date}'.", "data": record})
                continue

            description = str(record.get(mapping.get("description", ""), "") or "").strip()
            reference = str(record.get(mapping.get("reference", ""), "") or "").strip()

            debit = D(record.get(mapping.get("debit", ""), 0) or 0)
            credit = D(record.get(mapping.get("credit", ""), 0) or 0)

            if debit == ZERO and credit == ZERO and "amount" in mapping:
                signed = D(record.get(mapping["amount"], 0) or 0)
                type_hint = str(record.get(mapping.get("type", ""), "") or "").strip().upper()
                if type_hint in ("DR", "DEBIT", "W", "WITHDRAWAL"):
                    debit = abs(signed)
                elif type_hint in ("CR", "CREDIT", "D", "DEPOSIT"):
                    credit = abs(signed)
                elif signed < 0:
                    debit = abs(signed)
                else:
                    credit = signed

            if debit > ZERO and credit > ZERO:
                errors.append(
                    {"row": index, "error": "Row has both a debit and a credit amount.", "data": record}
                )
                continue
            if debit == ZERO and credit == ZERO:
                errors.append({"row": index, "error": "Row has no amount.", "data": record})
                continue

            direction = BankTxnDirection.DEBIT if debit > ZERO else BankTxnDirection.CREDIT
            amount = money(debit if debit > ZERO else credit)
            if amount <= ZERO:
                errors.append({"row": index, "error": "Amount must be positive.", "data": record})
                continue

            balance_value = record.get(mapping.get("balance", ""))
            rows.append(
                ImportRow(
                    txn_date=txn_date,
                    description=description,
                    amount=amount,
                    direction=direction,
                    reference=reference,
                    balance=money(balance_value) if balance_value not in (None, "") else None,
                    raw={str(k): (str(v) if v is not None else "") for k, v in record.items()},
                )
            )
        except Exception as exc:  # noqa: BLE001 - one bad row must not kill the import
            errors.append({"row": index, "error": str(exc), "data": record})

    return rows, errors


def import_transactions(
    db: Session,
    company_id: str,
    bank_account_id: str,
    rows: list[ImportRow],
    *,
    filename: str = "",
    file_hash: str = "",
    source: str = "CSV",
    provider: str = "file",
    provider_mode: str = "MOCK",
    errors: list[dict] | None = None,
    user_id: str | None = None,
) -> ImportResult:
    """Persist parsed rows, skipping anything already imported."""
    account = db.get(BankAccount, bank_account_id)
    if account is None or account.company_id != company_id:
        raise NotFoundError("Bank account not found.")

    result = ImportResult(total=len(rows) + len(errors or []), errors=list(errors or []))
    result.rejected = len(result.errors)

    if file_hash:
        previous = db.execute(
            select(BankStatement).where(
                BankStatement.company_id == company_id,
                BankStatement.bank_account_id == bank_account_id,
                BankStatement.file_hash == file_hash,
            )
        ).scalar_one_or_none()
        if previous:
            result.warnings.append(
                f"This exact file was already imported on "
                f"{previous.created_at.date()}. Only new lines will be added."
            )

    statement = BankStatement(
        company_id=company_id,
        bank_account_id=bank_account_id,
        file_name=filename,
        file_hash=file_hash,
        source=source,
        provider=provider,
        provider_mode=provider_mode,
        rows_total=len(rows),
        imported_by_user_id=user_id,
    )
    db.add(statement)
    db.flush()

    # Ordinal disambiguates identical lines inside one file.
    seen: dict[str, int] = {}
    dates: list[date] = []

    for row in rows:
        base_key = f"{row.txn_date}|{row.amount}|{row.direction}|{normalise_description(row.description)}|{row.reference}"
        ordinal = seen.get(base_key, 0)
        seen[base_key] = ordinal + 1

        dedupe = compute_dedupe_hash(
            bank_account_id,
            row.txn_date,
            row.amount,
            row.direction,
            row.description,
            row.reference,
            ordinal=ordinal,
        )

        exists = db.execute(
            select(BankTransaction.id).where(
                BankTransaction.bank_account_id == bank_account_id,
                BankTransaction.dedupe_hash == dedupe,
            )
        ).scalar_one_or_none()
        if exists:
            result.duplicates += 1
            continue

        db.add(
            BankTransaction(
                company_id=company_id,
                bank_account_id=bank_account_id,
                statement_id=statement.id,
                txn_date=row.txn_date,
                description=row.description,
                normalised_description=normalise_description(row.description),
                reference=row.reference,
                counterparty=extract_counterparty(row.description),
                direction=row.direction,
                amount=row.amount,
                running_balance=row.balance,
                currency=account.currency,
                dedupe_hash=dedupe,
                recon_status=ReconStatus.UNMATCHED,
                raw=row.raw,
            )
        )
        result.imported += 1
        dates.append(row.txn_date)

    if dates:
        statement.period_start = min(dates)
        statement.period_end = max(dates)
        closing_rows = [r for r in rows if r.balance is not None]
        if closing_rows:
            latest = max(closing_rows, key=lambda r: r.txn_date)
            statement.closing_balance = latest.balance
            result.closing_balance = latest.balance
            account.statement_balance = latest.balance
            account.statement_balance_date = latest.txn_date

    statement.rows_imported = result.imported
    statement.rows_duplicate = result.duplicates
    statement.rows_rejected = result.rejected
    statement.errors = result.errors[:200]
    result.statement_id = statement.id
    db.flush()

    record_event(
        db,
        company_id=company_id,
        action=AuditAction.IMPORT,
        entity_type="BankStatement",
        entity_id=statement.id,
        entity_label=filename or "bank statement",
        summary=(
            f"Imported {result.imported} bank transactions "
            f"({result.duplicates} duplicates skipped, {result.rejected} rejected)"
        ),
        actor_type=ActorType.USER if user_id else ActorType.SYSTEM,
        actor_id=user_id,
        after=result.as_dict(),
    )
    return result


# ------------------------------------------------------- categorisation ----

def apply_categorisation_rules(
    db: Session, company_id: str, transaction: BankTransaction
) -> CategorisationRule | None:
    """Deterministic rules first — most lines never need the LLM."""
    rules = list(
        db.execute(
            select(CategorisationRule)
            .where(
                CategorisationRule.company_id == company_id,
                CategorisationRule.is_active.is_(True),
            )
            .order_by(CategorisationRule.priority.asc())
        ).scalars()
    )

    haystacks = {
        "description": (transaction.description or "").lower(),
        "normalised_description": transaction.normalised_description or "",
        "reference": (transaction.reference or "").lower(),
        "counterparty": (transaction.counterparty or "").lower(),
    }

    for rule in rules:
        if rule.direction and rule.direction != transaction.direction:
            continue
        haystack = haystacks.get(rule.field, haystacks["description"])
        pattern = (rule.pattern or "").lower()
        if not pattern:
            continue

        matched = False
        if rule.match_type == "EXACT":
            matched = haystack.strip() == pattern
        elif rule.match_type == "PREFIX":
            matched = haystack.strip().startswith(pattern)
        elif rule.match_type == "REGEX":
            try:
                matched = bool(re.search(rule.pattern, haystack, flags=re.IGNORECASE))
            except re.error:
                matched = False
        else:
            matched = pattern in haystack

        if matched:
            transaction.suggested_account_id = rule.account_id
            transaction.suggested_confidence = D("1.0")
            transaction.categorised_by = "RULE"
            account = db.get(Account, rule.account_id)
            transaction.suggested_category = account.name if account else ""
            rule.times_applied += 1
            db.flush()
            return rule
    return None


def learn_rule_from_decision(
    db: Session,
    company_id: str,
    transaction: BankTransaction,
    account_id: str,
    *,
    user_id: str | None = None,
) -> CategorisationRule | None:
    """Turn a confirmed human categorisation into a reusable rule.

    Only creates a rule when the normalised description is distinctive enough
    to be safe, and never duplicates an existing rule.
    """
    pattern = (transaction.normalised_description or "").strip()
    if len(pattern) < 4:
        return None

    existing = db.execute(
        select(CategorisationRule).where(
            CategorisationRule.company_id == company_id,
            CategorisationRule.pattern == pattern,
            CategorisationRule.field == "normalised_description",
        )
    ).scalar_one_or_none()
    if existing:
        if existing.account_id != account_id:
            # The human disagreed with what the rule would have done.
            existing.times_corrected += 1
            existing.account_id = account_id
            db.flush()
        return existing

    account = db.get(Account, account_id)
    rule = CategorisationRule(
        company_id=company_id,
        name=f"Learned: {pattern[:60]}",
        match_type="CONTAINS",
        pattern=pattern,
        field="normalised_description",
        direction=transaction.direction,
        account_id=account_id,
        priority=200,
        created_from="LEARNED",
    )
    db.add(rule)
    db.flush()

    record_event(
        db,
        company_id=company_id,
        action=AuditAction.CREATE,
        entity_type="CategorisationRule",
        entity_id=rule.id,
        entity_label=rule.name,
        summary=(
            f"Learned categorisation rule: '{pattern[:60]}' -> "
            f"{account.code if account else account_id}"
        ),
        actor_type=ActorType.USER if user_id else ActorType.SYSTEM,
        actor_id=user_id,
        after={"pattern": pattern, "account_id": account_id},
    )
    return rule


def post_bank_transaction(
    db: Session,
    company_id: str,
    transaction_id: str,
    *,
    account_id: str,
    user_id: str | None = None,
    description: str | None = None,
    cost_center_id: str | None = None,
    learn: bool = True,
) -> BankTransaction:
    """Post an uncategorised bank line to the ledger against a chosen account.

    Used for genuinely new items (bank charges, interest, direct expenses) that
    have no matching invoice or bill.
    """
    transaction = db.get(BankTransaction, transaction_id)
    if transaction is None or transaction.company_id != company_id:
        raise NotFoundError("Bank transaction not found.")
    if transaction.journal_entry_id:
        raise ConflictError("This bank transaction is already posted.")

    bank_account = db.get(BankAccount, transaction.bank_account_id)
    engine = AccountingEngine(db, company_id, actor_user_id=user_id)
    target = engine.get_account(account_id=account_id)
    if target is None:
        raise NotFoundError("Target account not found.")

    narration = description or transaction.description or "Bank transaction"

    if transaction.direction == BankTxnDirection.CREDIT:
        lines = [
            LineInput(
                account_id=bank_account.gl_account_id,
                debit=transaction.amount,
                description=narration,
                bank_account_id=bank_account.id,
            ),
            LineInput(
                account_id=target.id,
                credit=transaction.amount,
                description=narration,
                cost_center_id=cost_center_id,
            ),
        ]
    else:
        lines = [
            LineInput(
                account_id=target.id,
                debit=transaction.amount,
                description=narration,
                cost_center_id=cost_center_id,
            ),
            LineInput(
                account_id=bank_account.gl_account_id,
                credit=transaction.amount,
                description=narration,
                bank_account_id=bank_account.id,
            ),
        ]

    journal = engine.create_and_post(
        EntryInput(
            entry_date=transaction.txn_date,
            voucher_type=VoucherType.PAYMENT
            if transaction.direction == BankTxnDirection.DEBIT
            else VoucherType.RECEIPT,
            narration=narration,
            reference=transaction.reference,
            lines=lines,
            source=EntrySource.RECONCILIATION,
            source_type="BankTransaction",
            source_id=transaction.id,
        )
    )

    transaction.journal_entry_id = journal.id
    transaction.recon_status = ReconStatus.MATCHED
    transaction.matched_amount = transaction.amount
    transaction.categorised_by = "HUMAN" if user_id else "SYSTEM"
    transaction.suggested_account_id = target.id

    # Mark the bank-side ledger line reconciled: it *is* this bank line.
    for line in journal.lines:
        if line.account_id == bank_account.gl_account_id:
            line.reconciled = True

    if learn and user_id:
        learn_rule_from_decision(db, company_id, transaction, target.id, user_id=user_id)

    db.flush()
    return transaction


def list_transactions(
    db: Session,
    company_id: str,
    *,
    bank_account_id: str | None = None,
    status: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    search: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    stmt = select(BankTransaction).where(BankTransaction.company_id == company_id)
    if bank_account_id:
        stmt = stmt.where(BankTransaction.bank_account_id == bank_account_id)
    if status:
        stmt = stmt.where(BankTransaction.recon_status == status)
    if start_date:
        stmt = stmt.where(BankTransaction.txn_date >= start_date)
    if end_date:
        stmt = stmt.where(BankTransaction.txn_date <= end_date)
    if search:
        pattern = f"%{search.lower()}%"
        stmt = stmt.where(
            func.lower(BankTransaction.description).like(pattern)
            | func.lower(BankTransaction.reference).like(pattern)
        )

    total = int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    transactions = list(
        db.execute(
            stmt.order_by(BankTransaction.txn_date.desc(), BankTransaction.created_at.desc())
            .limit(limit)
            .offset(offset)
        ).scalars()
    )
    return {"transactions": transactions, "total": total, "limit": limit, "offset": offset}


def unreconciled_summary(db: Session, company_id: str) -> dict:
    """Headline reconciliation numbers for the dashboard."""
    rows = db.execute(
        select(
            BankTransaction.recon_status,
            func.count(BankTransaction.id),
            func.sum(BankTransaction.amount),
        )
        .where(BankTransaction.company_id == company_id)
        .group_by(BankTransaction.recon_status)
    ).all()

    by_status = {row[0]: {"count": int(row[1]), "amount": money(row[2] or 0)} for row in rows}
    unmatched = by_status.get(ReconStatus.UNMATCHED, {"count": 0, "amount": ZERO})
    exceptions = by_status.get(ReconStatus.EXCEPTION, {"count": 0, "amount": ZERO})
    ambiguous = by_status.get(ReconStatus.AMBIGUOUS, {"count": 0, "amount": ZERO})
    partial = by_status.get(ReconStatus.PARTIAL, {"count": 0, "amount": ZERO})
    matched = by_status.get(ReconStatus.MATCHED, {"count": 0, "amount": ZERO})

    total_count = sum(v["count"] for v in by_status.values())
    needs_attention = unmatched["count"] + exceptions["count"] + ambiguous["count"] + partial["count"]

    return {
        "by_status": by_status,
        "total_transactions": total_count,
        "matched_count": matched["count"],
        "needs_attention_count": needs_attention,
        "unreconciled_amount": money(
            unmatched["amount"] + exceptions["amount"] + ambiguous["amount"]
        ),
        "match_rate": (
            money(Decimal(matched["count"]) * 100 / Decimal(total_count)) if total_count else ZERO
        ),
    }
