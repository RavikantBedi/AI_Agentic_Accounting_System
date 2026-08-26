"""Anomaly and fraud-control detection.

Every detector is deterministic and evidence-bearing: it computes a baseline
from the company's own history, compares the observation against it, and
records *why* the item was flagged along with the records that prove it.

No detector guesses. Where a statistical threshold is used, the baseline, the
observed value and the deviation are all stored so a reviewer can judge it.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.enums import (
    AnomalyKind,
    DocumentStatus,
    ExceptionCategory,
    ExceptionStatus,
    JournalStatus,
    Severity,
)
from app.models import (
    Account,
    Anomaly,
    BankTransaction,
    Bill,
    ExceptionItem,
    Invoice,
    JournalEntry,
    JournalLine,
    Payment,
    Vendor,
)
from app.money import ZERO, D, fmt_inr, money
from app.services.policy import get_policy


@dataclass
class Finding:
    kind: str
    severity: str
    title: str
    reason: str
    recommendation: str
    dedupe_key: str
    evidence: list[dict] = field(default_factory=list)
    amount: Decimal | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    related_entity_type: str | None = None
    related_entity_id: str | None = None
    occurred_on: date | None = None
    baseline_value: Decimal | None = None
    observed_value: Decimal | None = None
    deviation_score: Decimal | None = None
    detection_method: str = "RULE"


def detect_all(
    db: Session,
    company_id: str,
    *,
    lookback_days: int = 180,
    agent_run_id: str | None = None,
) -> dict:
    """Run every detector and persist new findings (deduplicated)."""
    policy = get_policy(db, company_id, "anomaly.detection")
    end = date.today()
    start = end - timedelta(days=lookback_days)

    findings: list[Finding] = []
    findings.extend(detect_duplicate_bills(db, company_id, policy, start, end))
    findings.extend(detect_duplicate_payments(db, company_id, policy, start, end))
    findings.extend(detect_duplicate_bank_postings(db, company_id, policy, start, end))
    findings.extend(detect_large_transactions(db, company_id, policy, start, end))
    findings.extend(detect_expense_spikes(db, company_id, policy, end))
    findings.extend(detect_round_numbers(db, company_id, policy, start, end))
    findings.extend(detect_new_vendor_activity(db, company_id, policy, start, end))
    findings.extend(detect_vendor_bank_changes(db, company_id, policy, end))
    findings.extend(detect_weekend_and_after_hours(db, company_id, policy, start, end))
    findings.extend(detect_unusual_journals(db, company_id, policy, start, end))
    findings.extend(detect_suspense_balance(db, company_id))
    findings.extend(detect_negative_stock(db, company_id))

    created = 0
    updated = 0
    for finding in findings:
        existing = db.execute(
            select(Anomaly).where(
                Anomaly.company_id == company_id, Anomaly.dedupe_key == finding.dedupe_key
            )
        ).scalar_one_or_none()

        if existing is not None:
            # Refresh the evidence but never resurrect a resolved item.
            if existing.status == ExceptionStatus.OPEN:
                existing.evidence = finding.evidence
                existing.reason = finding.reason
                updated += 1
            continue

        db.add(
            Anomaly(
                company_id=company_id,
                kind=finding.kind,
                severity=finding.severity,
                title=finding.title[:250],
                reason=finding.reason,
                recommendation=finding.recommendation,
                evidence=finding.evidence,
                detection_method=finding.detection_method,
                amount=finding.amount,
                entity_type=finding.entity_type,
                entity_id=finding.entity_id,
                related_entity_type=finding.related_entity_type,
                related_entity_id=finding.related_entity_id,
                occurred_on=finding.occurred_on,
                baseline_value=finding.baseline_value,
                observed_value=finding.observed_value,
                deviation_score=finding.deviation_score,
                dedupe_key=finding.dedupe_key[:250],
                agent_run_id=agent_run_id,
            )
        )
        created += 1

        if finding.severity in (Severity.HIGH, Severity.CRITICAL):
            _raise_exception(db, company_id, finding, agent_run_id)

    db.flush()

    by_severity: dict[str, int] = defaultdict(int)
    for finding in findings:
        by_severity[finding.severity] += 1

    return {
        "scanned_from": start,
        "scanned_to": end,
        "findings": len(findings),
        "created": created,
        "refreshed": updated,
        "by_severity": dict(by_severity),
        "total_amount_flagged": money(
            sum((f.amount for f in findings if f.amount is not None), ZERO)
        ),
    }


def _raise_exception(
    db: Session, company_id: str, finding: Finding, agent_run_id: str | None
) -> None:
    key = f"anomaly:{finding.dedupe_key}"[:250]
    existing = db.execute(
        select(ExceptionItem).where(
            ExceptionItem.company_id == company_id, ExceptionItem.dedupe_key == key
        )
    ).scalar_one_or_none()
    if existing is not None:
        return

    db.add(
        ExceptionItem(
            company_id=company_id,
            category=ExceptionCategory.ANOMALY,
            severity=finding.severity,
            status=ExceptionStatus.OPEN,
            title=finding.title[:250],
            reason=finding.reason,
            recommended_action=finding.recommendation,
            evidence=finding.evidence,
            amount=finding.amount,
            entity_type=finding.entity_type,
            entity_id=finding.entity_id,
            dedupe_key=key,
            detected_by="ENGINE",
            agent_run_id=agent_run_id,
        )
    )


# ------------------------------------------------------------ detectors ----

def detect_duplicate_bills(
    db: Session, company_id: str, policy: dict, start: date, end: date
) -> list[Finding]:
    """Same vendor + same amount within the window, or a repeated reference."""
    window = int(policy.get("duplicate_window_days", 90))
    cutoff = end - timedelta(days=window)

    rows = db.execute(
        select(Bill.vendor_id, Bill.total, func.count(Bill.id))
        .where(
            Bill.company_id == company_id,
            Bill.doc_date >= cutoff,
            Bill.status != DocumentStatus.CANCELLED,
        )
        .group_by(Bill.vendor_id, Bill.total)
        .having(func.count(Bill.id) > 1)
    ).all()

    findings: list[Finding] = []
    for vendor_id, total, count in rows:
        bills = list(
            db.execute(
                select(Bill)
                .where(
                    Bill.company_id == company_id,
                    Bill.vendor_id == vendor_id,
                    Bill.total == total,
                    Bill.doc_date >= cutoff,
                    Bill.status != DocumentStatus.CANCELLED,
                )
                .order_by(Bill.doc_date.asc())
            ).scalars()
        )
        if len(bills) < 2:
            continue

        vendor = db.get(Vendor, vendor_id)
        # Different vendor invoice numbers make a genuine repeat far more likely
        # (e.g. a monthly retainer), so severity is graded accordingly.
        references = {bill.vendor_invoice_number.strip().lower() for bill in bills}
        same_reference = len(references) == 1
        amount = money(total)

        findings.append(
            Finding(
                kind=AnomalyKind.DUPLICATE_BILL,
                severity=Severity.HIGH if same_reference else Severity.MEDIUM,
                title=(
                    f"{len(bills)} bills from {vendor.name if vendor else 'a vendor'} "
                    f"for exactly {fmt_inr(amount)}"
                ),
                reason=(
                    f"{len(bills)} bills of {fmt_inr(amount)} were booked against "
                    f"{vendor.name if vendor else 'this vendor'} between "
                    f"{bills[0].doc_date} and {bills[-1].doc_date}"
                    + (
                        f", all quoting the same vendor invoice number "
                        f"'{bills[0].vendor_invoice_number}'."
                        if same_reference
                        else " with different vendor invoice numbers, which may be legitimate."
                    )
                ),
                recommendation=(
                    "Compare the source documents. If it is a duplicate, cancel the later "
                    "bill and reverse its journal before any payment is released."
                    if same_reference
                    else "Confirm these are genuinely separate invoices (for example a recurring retainer)."
                ),
                dedupe_key=f"dup_bill:{vendor_id}:{amount}",
                amount=money(amount * (len(bills) - 1)),
                entity_type="Bill",
                entity_id=bills[-1].id,
                occurred_on=bills[-1].doc_date,
                evidence=[
                    {
                        "type": "bill",
                        "id": bill.id,
                        "label": f"{bill.number} ({bill.vendor_invoice_number})",
                        "date": bill.doc_date.isoformat(),
                        "amount": str(bill.total),
                        "status": bill.status,
                    }
                    for bill in bills
                ],
            )
        )
    return findings


def detect_duplicate_payments(
    db: Session, company_id: str, policy: dict, start: date, end: date
) -> list[Finding]:
    """The same vendor paid the same amount twice in a short window."""
    rows = db.execute(
        select(Payment.vendor_id, Payment.amount, func.count(Payment.id))
        .where(
            Payment.company_id == company_id,
            Payment.payment_date >= start,
            Payment.payment_date <= end,
            Payment.status != DocumentStatus.CANCELLED,
        )
        .group_by(Payment.vendor_id, Payment.amount)
        .having(func.count(Payment.id) > 1)
    ).all()

    findings: list[Finding] = []
    for vendor_id, amount, count in rows:
        payments = list(
            db.execute(
                select(Payment)
                .where(
                    Payment.company_id == company_id,
                    Payment.vendor_id == vendor_id,
                    Payment.amount == amount,
                    Payment.payment_date >= start,
                    Payment.payment_date <= end,
                    Payment.status != DocumentStatus.CANCELLED,
                )
                .order_by(Payment.payment_date.asc())
            ).scalars()
        )
        if len(payments) < 2:
            continue

        span = (payments[-1].payment_date - payments[0].payment_date).days
        if span > 45:
            continue  # a monthly pattern, not a duplicate

        vendor = db.get(Vendor, vendor_id)
        findings.append(
            Finding(
                kind=AnomalyKind.DUPLICATE_PAYMENT,
                severity=Severity.CRITICAL if span <= 7 else Severity.HIGH,
                title=f"{len(payments)} payments of {fmt_inr(amount)} to {vendor.name if vendor else 'a vendor'}",
                reason=(
                    f"{len(payments)} payments of exactly {fmt_inr(amount)} were released to "
                    f"{vendor.name if vendor else 'this vendor'} within {span} day(s) "
                    f"({payments[0].payment_date} to {payments[-1].payment_date})."
                ),
                recommendation=(
                    "Verify against the vendor statement. If it is a double payment, "
                    "request a refund or offset it against the next bill, and reverse "
                    "the duplicate entry."
                ),
                dedupe_key=f"dup_payment:{vendor_id}:{money(amount)}:{payments[0].payment_date}",
                amount=money(amount),
                entity_type="Payment",
                entity_id=payments[-1].id,
                occurred_on=payments[-1].payment_date,
                evidence=[
                    {
                        "type": "payment",
                        "id": payment.id,
                        "label": payment.number,
                        "date": payment.payment_date.isoformat(),
                        "amount": str(payment.amount),
                        "reference": payment.reference,
                    }
                    for payment in payments
                ],
            )
        )
    return findings


def detect_duplicate_bank_postings(
    db: Session, company_id: str, policy: dict, start: date, end: date
) -> list[Finding]:
    """The same bank line reconciled to more than one ledger entry."""
    rows = db.execute(
        select(
            BankTransaction.bank_account_id,
            BankTransaction.txn_date,
            BankTransaction.amount,
            BankTransaction.direction,
            func.count(BankTransaction.id),
        )
        .where(
            BankTransaction.company_id == company_id,
            BankTransaction.txn_date >= start,
            BankTransaction.txn_date <= end,
        )
        .group_by(
            BankTransaction.bank_account_id,
            BankTransaction.txn_date,
            BankTransaction.amount,
            BankTransaction.direction,
        )
        .having(func.count(BankTransaction.id) > 1)
        .limit(50)
    ).all()

    findings: list[Finding] = []
    for bank_account_id, txn_date, amount, direction, count in rows:
        transactions = list(
            db.execute(
                select(BankTransaction).where(
                    BankTransaction.company_id == company_id,
                    BankTransaction.bank_account_id == bank_account_id,
                    BankTransaction.txn_date == txn_date,
                    BankTransaction.amount == amount,
                    BankTransaction.direction == direction,
                )
            ).scalars()
        )
        # Identical same-day amounts are common and usually legitimate; only
        # flag when the descriptions also match.
        descriptions = {t.normalised_description for t in transactions}
        if len(descriptions) > 1:
            continue

        findings.append(
            Finding(
                kind=AnomalyKind.DUPLICATE_BANK_POSTING,
                severity=Severity.MEDIUM,
                title=f"{count} identical bank lines of {fmt_inr(amount)} on {txn_date}",
                reason=(
                    f"{count} bank transactions with the same date, amount ({money(amount)}), "
                    f"direction and description were imported. This can happen when "
                    f"overlapping statements are imported."
                ),
                recommendation=(
                    "Check the bank statement. If these are genuinely one transaction "
                    "imported twice, remove the extra line before reconciling."
                ),
                dedupe_key=f"dup_bank:{bank_account_id}:{txn_date}:{money(amount)}:{direction}",
                amount=money(amount),
                entity_type="BankTransaction",
                entity_id=transactions[0].id if transactions else None,
                occurred_on=txn_date,
                evidence=[
                    {
                        "type": "bank_transaction",
                        "id": t.id,
                        "label": t.description[:80],
                        "date": t.txn_date.isoformat(),
                        "amount": str(t.amount),
                        "status": t.recon_status,
                    }
                    for t in transactions
                ],
            )
        )
    return findings


def detect_large_transactions(
    db: Session, company_id: str, policy: dict, start: date, end: date
) -> list[Finding]:
    """Transactions far above the company's own normal size."""
    absolute_threshold = money(policy.get("large_transaction_amount", 500000))
    sigma_multiple = float(policy.get("large_transaction_sigma", 3.0))

    entries = list(
        db.execute(
            select(JournalEntry).where(
                JournalEntry.company_id == company_id,
                JournalEntry.status == JournalStatus.POSTED,
                JournalEntry.entry_date >= start,
                JournalEntry.entry_date <= end,
            )
        ).scalars()
    )
    if len(entries) < 20:
        return []

    amounts = [float(entry.total_debit) for entry in entries if entry.total_debit > ZERO]
    if len(amounts) < 20:
        return []

    mean = statistics.mean(amounts)
    stdev = statistics.pstdev(amounts) or 1.0
    statistical_threshold = mean + sigma_multiple * stdev

    findings: list[Finding] = []
    for entry in entries:
        value = float(entry.total_debit)
        exceeds_absolute = entry.total_debit >= absolute_threshold
        exceeds_statistical = value >= statistical_threshold
        if not (exceeds_absolute and exceeds_statistical):
            continue

        deviation = (value - mean) / stdev
        findings.append(
            Finding(
                kind=AnomalyKind.LARGE_TRANSACTION,
                severity=Severity.MEDIUM if deviation < 5 else Severity.HIGH,
                title=f"Unusually large {entry.voucher_type.lower()} of {fmt_inr(entry.total_debit)}",
                reason=(
                    f"{entry.entry_number} on {entry.entry_date} is {fmt_inr(entry.total_debit)}, "
                    f"{deviation:.1f} standard deviations above the {fmt_inr(mean)} average "
                    f"for the last {(end - start).days} days, and above the "
                    f"{fmt_inr(absolute_threshold)} review threshold."
                ),
                recommendation="Confirm the entry is supported by an approved document.",
                dedupe_key=f"large_txn:{entry.id}",
                amount=entry.total_debit,
                entity_type="JournalEntry",
                entity_id=entry.id,
                occurred_on=entry.entry_date,
                baseline_value=money(mean),
                observed_value=entry.total_debit,
                deviation_score=D(round(deviation, 4)),
                detection_method="STATISTICAL",
                evidence=[
                    {
                        "type": "journal_entry",
                        "id": entry.id,
                        "label": entry.entry_number,
                        "date": entry.entry_date.isoformat(),
                        "amount": str(entry.total_debit),
                        "narration": entry.narration[:120],
                    },
                    {
                        "type": "statistic",
                        "label": "Population mean",
                        "value": str(money(mean)),
                        "detail": f"Standard deviation {money(stdev)} over {len(amounts)} entries.",
                    },
                ],
            )
        )
    return findings[:20]


def detect_expense_spikes(
    db: Session, company_id: str, policy: dict, end: date
) -> list[Finding]:
    """A month-on-month jump in an expense account beyond the configured percent."""
    spike_percent = D(policy.get("expense_spike_percent", 40))
    current_start = end.replace(day=1)
    previous_end = current_start - timedelta(days=1)
    previous_start = previous_end.replace(day=1)

    def totals(start_date: date, end_date: date) -> dict[str, Decimal]:
        rows = db.execute(
            select(
                JournalLine.account_id,
                func.sum(JournalLine.debit - JournalLine.credit),
            )
            .join(Account, Account.id == JournalLine.account_id)
            .where(
                JournalLine.company_id == company_id,
                JournalLine.status == JournalStatus.POSTED,
                JournalLine.entry_date >= start_date,
                JournalLine.entry_date <= end_date,
                Account.type == "EXPENSE",
            )
            .group_by(JournalLine.account_id)
        ).all()
        return {row[0]: money(row[1] or 0) for row in rows}

    current = totals(current_start, end)
    previous = totals(previous_start, previous_end)

    findings: list[Finding] = []
    for account_id, current_amount in current.items():
        previous_amount = previous.get(account_id, ZERO)
        if previous_amount <= ZERO or current_amount <= previous_amount:
            continue
        change_percent = money((current_amount - previous_amount) * 100 / previous_amount)
        if change_percent < spike_percent:
            continue
        # Ignore trivial rupee movements that are large in percentage terms.
        if money(current_amount - previous_amount) < money(5000):
            continue

        account = db.get(Account, account_id)
        findings.append(
            Finding(
                kind=AnomalyKind.EXPENSE_SPIKE,
                severity=Severity.MEDIUM if change_percent < 100 else Severity.HIGH,
                title=f"{account.name if account else 'Expense'} up {change_percent:.0f}% this month",
                reason=(
                    f"{account.name if account else account_id} is {fmt_inr(current_amount)} so far in "
                    f"{current_start.strftime('%B %Y')}, against {fmt_inr(previous_amount)} in "
                    f"{previous_start.strftime('%B %Y')} — an increase of "
                    f"{fmt_inr(current_amount - previous_amount)} ({change_percent:.0f}%)."
                ),
                recommendation=(
                    "Review the transactions in this account for one-offs, "
                    "misclassification or duplicate bookings."
                ),
                dedupe_key=f"spike:{account_id}:{current_start}",
                amount=money(current_amount - previous_amount),
                entity_type="Account",
                entity_id=account_id,
                occurred_on=end,
                baseline_value=previous_amount,
                observed_value=current_amount,
                deviation_score=change_percent,
                detection_method="STATISTICAL",
                evidence=[
                    {
                        "type": "account",
                        "id": account_id,
                        "label": f"{account.code} {account.name}" if account else account_id,
                        "current_month": str(current_amount),
                        "previous_month": str(previous_amount),
                        "change_percent": str(change_percent),
                    }
                ],
            )
        )
    return findings


def detect_round_numbers(
    db: Session, company_id: str, policy: dict, start: date, end: date
) -> list[Finding]:
    """Repeated suspiciously round amounts — a classic manual-entry signal."""
    minimum = money(policy.get("round_number_min_amount", 50000))

    entries = list(
        db.execute(
            select(JournalEntry).where(
                JournalEntry.company_id == company_id,
                JournalEntry.status == JournalStatus.POSTED,
                JournalEntry.entry_date >= start,
                JournalEntry.entry_date <= end,
                JournalEntry.total_debit >= minimum,
                JournalEntry.source == "MANUAL",
            )
        ).scalars()
    )

    round_entries = [
        entry
        for entry in entries
        if entry.total_debit % money(10000) == ZERO and entry.total_debit > ZERO
    ]
    if len(round_entries) < 3:
        return []

    total = money(sum((entry.total_debit for entry in round_entries), ZERO))
    return [
        Finding(
            kind=AnomalyKind.ROUND_NUMBER,
            severity=Severity.LOW,
            title=f"{len(round_entries)} manual entries for exact round amounts",
            reason=(
                f"{len(round_entries)} manually-created entries totalling {fmt_inr(total)} are exact "
                f"multiples of 10,000. Real invoices rarely land on round figures, so this "
                f"can indicate estimates posted as actuals."
            ),
            recommendation="Confirm each has supporting documentation rather than being an estimate.",
            dedupe_key=f"round_numbers:{start}:{end}:{len(round_entries)}",
            amount=total,
            occurred_on=end,
            evidence=[
                {
                    "type": "journal_entry",
                    "id": entry.id,
                    "label": entry.entry_number,
                    "date": entry.entry_date.isoformat(),
                    "amount": str(entry.total_debit),
                    "narration": entry.narration[:100],
                }
                for entry in round_entries[:10]
            ],
        )
    ]


# Cap on individually-listed new-vendor findings; the rest are summarised.
MAX_NEW_VENDOR_FINDINGS = 10


def detect_new_vendor_activity(
    db: Session, company_id: str, policy: dict, start: date, end: date
) -> list[Finding]:
    """Large spend against a vendor created very recently."""
    new_vendor_days = int(policy.get("new_vendor_days", 30))
    threshold = money(policy.get("large_transaction_amount", 500000)) / Decimal(5)
    cutoff = end - timedelta(days=new_vendor_days)

    findings: list[Finding] = []
    vendors = list(
        db.execute(
            select(Vendor).where(
                Vendor.company_id == company_id, Vendor.created_at >= datetime.combine(cutoff, datetime.min.time())
            )
        ).scalars()
    )

    candidates: list[tuple[Decimal, int, Vendor]] = []
    for vendor in vendors:
        row = db.execute(
            select(func.sum(Bill.total), func.count(Bill.id)).where(
                Bill.company_id == company_id,
                Bill.vendor_id == vendor.id,
                Bill.status != DocumentStatus.CANCELLED,
            )
        ).one()
        total = money(row[0] or 0)
        count = int(row[1] or 0)
        if total < threshold or count == 0:
            continue
        candidates.append((total, count, vendor))

    # Largest exposure first, and capped.
    #
    # A detector that emits one finding per match is fine until a company
    # onboards a batch of vendors, at which point it buries every other signal
    # in the exception centre. Nothing is discarded: what does not fit as an
    # individual finding is rolled into one summary below, so the total spend
    # under review is still stated.
    candidates.sort(key=lambda row: row[0], reverse=True)
    detailed, remainder = candidates[:MAX_NEW_VENDOR_FINDINGS], candidates[MAX_NEW_VENDOR_FINDINGS:]

    for total, count, vendor in detailed:
        findings.append(
            Finding(
                kind=AnomalyKind.NEW_VENDOR,
                severity=Severity.MEDIUM,
                title=f"New vendor {vendor.name} already billed {fmt_inr(total)}",
                reason=(
                    f"{vendor.name} was added on {vendor.created_at.date()} "
                    f"({(end - vendor.created_at.date()).days} days ago) and already has "
                    f"{count} bill(s) totalling {fmt_inr(total)}."
                ),
                recommendation=(
                    "Verify the vendor's registration, PAN/GSTIN and bank details "
                    "independently before releasing payment."
                ),
                dedupe_key=f"new_vendor:{vendor.id}",
                amount=total,
                entity_type="Vendor",
                entity_id=vendor.id,
                occurred_on=vendor.created_at.date(),
                evidence=[
                    {
                        "type": "vendor",
                        "id": vendor.id,
                        "label": vendor.name,
                        "created": vendor.created_at.date().isoformat(),
                        "gstin": vendor.gstin or "(not provided)",
                        "pan": vendor.pan or "(not provided)",
                        "bill_count": str(count),
                        "total_billed": str(total),
                    }
                ],
            )
        )

    if remainder:
        rest_total = money(sum((row[0] for row in remainder), ZERO))
        findings.append(
            Finding(
                kind=AnomalyKind.NEW_VENDOR,
                severity=Severity.LOW,
                title=f"{len(remainder)} further new vendors billed {fmt_inr(rest_total)}",
                reason=(
                    f"{len(remainder)} vendors added in the last {new_vendor_days} days "
                    f"have bills totalling {fmt_inr(rest_total)}, each below the "
                    f"{fmt_inr(detailed[-1][0]) if detailed else fmt_inr(threshold)} "
                    "already listed individually."
                ),
                recommendation=(
                    "Review the vendor onboarding batch rather than each vendor "
                    "separately. Confirm the approvals came from the expected people."
                ),
                dedupe_key=f"new_vendor_batch:{end.isoformat()}",
                amount=rest_total,
                entity_type="Vendor",
                entity_id=None,
                occurred_on=end,
                evidence=[
                    {
                        "type": "vendor",
                        "id": vendor.id,
                        "label": vendor.name,
                        "created": vendor.created_at.date().isoformat(),
                        "total_billed": str(total),
                    }
                    for total, _count, vendor in remainder[:20]
                ],
            )
        )
    return findings


def detect_vendor_bank_changes(
    db: Session, company_id: str, policy: dict, end: date
) -> list[Finding]:
    """Bank details changed shortly before a payment — a classic fraud pattern."""
    hold_days = int(policy.get("block_payment_on_bank_change_days", 7) or 7)
    findings: list[Finding] = []

    vendors = list(
        db.execute(
            select(Vendor).where(
                Vendor.company_id == company_id,
                Vendor.bank_details_updated_at.isnot(None),
                Vendor.bank_details_updated_at >= end - timedelta(days=60),
            )
        ).scalars()
    )

    for vendor in vendors:
        changed_on = vendor.bank_details_updated_at
        payments = list(
            db.execute(
                select(Payment).where(
                    Payment.company_id == company_id,
                    Payment.vendor_id == vendor.id,
                    Payment.payment_date >= changed_on,
                    Payment.payment_date <= changed_on + timedelta(days=hold_days),
                    Payment.status != DocumentStatus.CANCELLED,
                )
            ).scalars()
        )
        if not payments:
            continue

        total = money(sum((payment.amount for payment in payments), ZERO))
        findings.append(
            Finding(
                kind=AnomalyKind.VENDOR_BANK_CHANGE,
                severity=Severity.CRITICAL,
                title=f"{vendor.name} paid {fmt_inr(total)} soon after a bank-detail change",
                reason=(
                    f"{vendor.name}'s bank details changed on {changed_on}, and "
                    f"{len(payments)} payment(s) totalling {fmt_inr(total)} were released within "
                    f"{hold_days} days. Bank-detail changes immediately before payment are "
                    f"a common invoice-redirection fraud pattern."
                ),
                recommendation=(
                    "Confirm the new bank details with the vendor using a phone number "
                    "from your own records — not one taken from the change request."
                ),
                dedupe_key=f"vendor_bank_change:{vendor.id}:{changed_on}",
                amount=total,
                entity_type="Vendor",
                entity_id=vendor.id,
                occurred_on=changed_on,
                evidence=[
                    {
                        "type": "vendor",
                        "id": vendor.id,
                        "label": vendor.name,
                        "bank_changed_on": changed_on.isoformat(),
                    }
                ]
                + [
                    {
                        "type": "payment",
                        "id": payment.id,
                        "label": payment.number,
                        "date": payment.payment_date.isoformat(),
                        "amount": str(payment.amount),
                    }
                    for payment in payments
                ],
            )
        )
    return findings


def detect_weekend_and_after_hours(
    db: Session, company_id: str, policy: dict, start: date, end: date
) -> list[Finding]:
    """Manual journals created at unusual times."""
    if not policy.get("flag_weekend_transactions", True):
        return []

    after_start = int(policy.get("after_hours_start", 21))
    after_end = int(policy.get("after_hours_end", 6))
    threshold = money(policy.get("large_transaction_amount", 500000)) / Decimal(10)

    entries = list(
        db.execute(
            select(JournalEntry).where(
                JournalEntry.company_id == company_id,
                JournalEntry.status == JournalStatus.POSTED,
                JournalEntry.entry_date >= start,
                JournalEntry.entry_date <= end,
                JournalEntry.source == "MANUAL",
                JournalEntry.total_debit >= threshold,
            )
        ).scalars()
    )

    weekend, after_hours = [], []
    for entry in entries:
        created = entry.created_at
        if entry.entry_date.weekday() >= 5:
            weekend.append(entry)
        hour = created.hour
        if hour >= after_start or hour < after_end:
            after_hours.append(entry)

    findings: list[Finding] = []
    if weekend:
        findings.append(
            Finding(
                kind=AnomalyKind.WEEKEND_TRANSACTION,
                severity=Severity.LOW,
                title=f"{len(weekend)} manual entries dated on a weekend",
                reason=(
                    f"{len(weekend)} manual journal(s) above {fmt_inr(threshold)} are dated on a "
                    f"Saturday or Sunday, outside normal business days."
                ),
                recommendation="Confirm the dates are intentional and supported.",
                dedupe_key=f"weekend:{start}:{end}:{len(weekend)}",
                amount=money(sum((entry.total_debit for entry in weekend), ZERO)),
                occurred_on=end,
                evidence=[
                    {
                        "type": "journal_entry",
                        "id": entry.id,
                        "label": entry.entry_number,
                        "date": entry.entry_date.isoformat(),
                        "weekday": entry.entry_date.strftime("%A"),
                        "amount": str(entry.total_debit),
                    }
                    for entry in weekend[:10]
                ],
            )
        )
    if after_hours:
        findings.append(
            Finding(
                kind=AnomalyKind.AFTER_HOURS_JOURNAL,
                severity=Severity.MEDIUM,
                title=f"{len(after_hours)} manual entries created outside working hours",
                reason=(
                    f"{len(after_hours)} manual journal(s) above {fmt_inr(threshold)} were created "
                    f"between {after_start}:00 and {after_end}:00."
                ),
                recommendation="Confirm who created these entries and why they were posted then.",
                dedupe_key=f"after_hours:{start}:{end}:{len(after_hours)}",
                amount=money(sum((entry.total_debit for entry in after_hours), ZERO)),
                occurred_on=end,
                evidence=[
                    {
                        "type": "journal_entry",
                        "id": entry.id,
                        "label": entry.entry_number,
                        "created_at": entry.created_at.isoformat(),
                        "amount": str(entry.total_debit),
                    }
                    for entry in after_hours[:10]
                ],
            )
        )
    return findings


def detect_unusual_journals(
    db: Session, company_id: str, policy: dict, start: date, end: date
) -> list[Finding]:
    """Manual journals that touch revenue or cash directly — worth a look."""
    entries = list(
        db.execute(
            select(JournalEntry).where(
                JournalEntry.company_id == company_id,
                JournalEntry.status == JournalStatus.POSTED,
                JournalEntry.entry_date >= start,
                JournalEntry.entry_date <= end,
                JournalEntry.source == "MANUAL",
                JournalEntry.voucher_type == "JOURNAL",
            )
        ).scalars()
    )

    findings: list[Finding] = []
    for entry in entries:
        touches_revenue = False
        for line in entry.lines:
            account = db.get(Account, line.account_id)
            if account and account.type == "REVENUE":
                touches_revenue = True
                break
        if not touches_revenue:
            continue

        findings.append(
            Finding(
                kind=AnomalyKind.UNUSUAL_JOURNAL,
                severity=Severity.MEDIUM,
                title=f"Manual journal {entry.entry_number} adjusts revenue",
                reason=(
                    f"{entry.entry_number} on {entry.entry_date} is a manual journal that "
                    f"posts directly to a revenue account for {fmt_inr(entry.total_debit)}. Revenue "
                    f"normally arrives through invoices."
                ),
                recommendation="Confirm this adjustment is supported and correctly documented.",
                dedupe_key=f"manual_revenue:{entry.id}",
                amount=entry.total_debit,
                entity_type="JournalEntry",
                entity_id=entry.id,
                occurred_on=entry.entry_date,
                evidence=[
                    {
                        "type": "journal_entry",
                        "id": entry.id,
                        "label": entry.entry_number,
                        "date": entry.entry_date.isoformat(),
                        "amount": str(entry.total_debit),
                        "narration": entry.narration[:150],
                    }
                ],
            )
        )
    return findings[:10]


def detect_suspense_balance(db: Session, company_id: str) -> list[Finding]:
    """Anything sitting in suspense is unfinished accounting."""
    from app.accounting.ledger import account_balance

    account = db.execute(
        select(Account).where(Account.company_id == company_id, Account.system_key == "SUSPENSE")
    ).scalar_one_or_none()
    if account is None:
        return []

    balance = account_balance(db, company_id, account.id)
    if balance == ZERO:
        return []

    return [
        Finding(
            kind=AnomalyKind.UNUSUAL_JOURNAL,
            severity=Severity.HIGH,
            title=f"Suspense account holds {fmt_inr(balance)}",
            reason=(
                f"The suspense account has a balance of {fmt_inr(balance)}. Amounts in suspense are "
                f"unclassified and will distort the financial statements until cleared."
            ),
            recommendation="Identify the underlying transactions and reclassify them before closing the period.",
            dedupe_key=f"suspense:{account.id}:{date.today().replace(day=1)}",
            amount=balance,
            entity_type="Account",
            entity_id=account.id,
            occurred_on=date.today(),
            evidence=[
                {
                    "type": "account",
                    "id": account.id,
                    "label": f"{account.code} {account.name}",
                    "balance": str(balance),
                }
            ],
        )
    ]


def detect_negative_stock(db: Session, company_id: str) -> list[Finding]:
    """Negative stock means goods were issued that were never received."""
    from app.models import Item

    items = list(
        db.execute(
            select(Item).where(
                Item.company_id == company_id,
                Item.track_inventory.is_(True),
                Item.current_qty < 0,
            )
        ).scalars()
    )
    if not items:
        return []

    return [
        Finding(
            kind=AnomalyKind.INVENTORY_ANOMALY,
            severity=Severity.HIGH,
            title=f"{len(items)} item(s) show negative stock",
            reason=(
                f"{len(items)} item(s) have a negative quantity on hand, which means goods "
                f"were issued without a corresponding receipt. Inventory valuation and COGS "
                f"are both unreliable until this is fixed."
            ),
            recommendation=(
                "Book the missing purchases or goods receipts, then re-run the stock valuation."
            ),
            dedupe_key=f"negative_stock:{date.today()}",
            amount=money(sum((item.stock_value for item in items), ZERO)),
            entity_type="Item",
            entity_id=items[0].id,
            occurred_on=date.today(),
            evidence=[
                {
                    "type": "item",
                    "id": item.id,
                    "label": f"{item.sku} — {item.name}",
                    "quantity": str(item.current_qty),
                    "value": str(item.stock_value),
                }
                for item in items[:10]
            ],
        )
    ]
