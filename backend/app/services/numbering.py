"""Document numbering.

Numbers are allocated from a per-company, per-key, per-fiscal-year counter with
a row lock, so concurrent requests can never receive the same number.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Company, NumberSequence

DEFAULT_PREFIXES: dict[str, str] = {
    "SALES_INVOICE": "INV",
    "QUOTATION": "QT",
    "SALES_ORDER": "SO",
    "DELIVERY_NOTE": "DN",
    "CREDIT_NOTE": "CN",
    "RECEIPT": "RCP",
    "PURCHASE_ORDER": "PO",
    "GOODS_RECEIPT": "GRN",
    "BILL": "BILL",
    "DEBIT_NOTE": "DBN",
    "PAYMENT": "PAY",
    "EXPENSE": "EXP",
    "STOCK_TRANSFER": "STR",
    "STOCK_ADJUSTMENT": "STA",
    "JOURNAL_JOURNAL": "JV",
    "JOURNAL_SALES": "SV",
    "JOURNAL_PURCHASE": "PV",
    "JOURNAL_PAYMENT": "PMT",
    "JOURNAL_RECEIPT": "RV",
    "JOURNAL_CONTRA": "CV",
    "JOURNAL_EXPENSE": "EV",
    "JOURNAL_PAYROLL": "PRV",
    "JOURNAL_DEPRECIATION": "DEP",
    "JOURNAL_ACCRUAL": "ACR",
    "JOURNAL_PREPAID": "PPD",
    "JOURNAL_OPENING": "OPN",
    "JOURNAL_CLOSING": "CLS",
    "JOURNAL_CREDIT_NOTE": "CNV",
    "JOURNAL_DEBIT_NOTE": "DNV",
    "JOURNAL_STOCK_JOURNAL": "STK",
    "JOURNAL_TRANSFER": "TRF",
    "JOURNAL_DELIVERY_NOTE": "DNV",
    "JOURNAL_RECEIPT_NOTE": "RNV",
}


def fiscal_year_label(db: Session, company_id: str, on_date: date) -> str:
    """Return the Indian-style fiscal-year label, e.g. 2025-26 for April 2025."""
    company = db.get(Company, company_id)
    start_month = company.fiscal_year_start_month if company else 4
    start_year = on_date.year if on_date.month >= start_month else on_date.year - 1
    return f"{start_year}-{str((start_year + 1) % 100).zfill(2)}"


def next_number(db: Session, company_id: str, key: str, on_date: date | None = None) -> str:
    """Allocate the next document number for ``key``.

    Uses ``SELECT ... FOR UPDATE`` where the backend supports it; on SQLite the
    single-writer lock provides the same guarantee.
    """
    on_date = on_date or date.today()
    fy = fiscal_year_label(db, company_id, on_date)
    prefix = DEFAULT_PREFIXES.get(key, key[:4].upper())

    stmt = select(NumberSequence).where(
        NumberSequence.company_id == company_id,
        NumberSequence.key == key,
        NumberSequence.fiscal_year == fy,
    )
    if db.bind is not None and db.bind.dialect.name != "sqlite":
        stmt = stmt.with_for_update()

    sequence = db.execute(stmt).scalar_one_or_none()

    if sequence is None:
        sequence = NumberSequence(
            company_id=company_id, key=key, prefix=prefix, fiscal_year=fy, next_value=1, padding=4
        )
        db.add(sequence)
        try:
            db.flush()
        except IntegrityError:
            # Another writer created it first — re-read and continue.
            db.rollback()
            sequence = db.execute(stmt).scalar_one()

    value = sequence.next_value
    sequence.next_value = value + 1
    db.flush()

    return f"{sequence.prefix}/{sequence.fiscal_year}/{str(value).zfill(sequence.padding)}"


def peek_number(db: Session, company_id: str, key: str, on_date: date | None = None) -> str:
    """Preview the next number *without* consuming it (for UI drafts)."""
    on_date = on_date or date.today()
    fy = fiscal_year_label(db, company_id, on_date)
    prefix = DEFAULT_PREFIXES.get(key, key[:4].upper())
    sequence = db.execute(
        select(NumberSequence).where(
            NumberSequence.company_id == company_id,
            NumberSequence.key == key,
            NumberSequence.fiscal_year == fy,
        )
    ).scalar_one_or_none()
    value = sequence.next_value if sequence else 1
    padding = sequence.padding if sequence else 4
    used_prefix = sequence.prefix if sequence else prefix
    return f"{used_prefix}/{fy}/{str(value).zfill(padding)}"
