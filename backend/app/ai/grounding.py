"""Grounding: proving an AI output refers to records that actually exist.

The rule the whole product rests on: **an AI proposal is a claim, not a fact.**
Before any proposal reaches a human, every id it references is re-read from the
database and every number is recomputed. Anything the model invented is
rejected here, with an explicit reason.

This is what makes "no AI fabrication" an enforced property rather than a
prompt instruction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import JournalStatus
from app.errors import AIGroundingError
from app.models import (
    Account,
    BankTransaction,
    Bill,
    Customer,
    Invoice,
    Item,
    JournalEntry,
    JournalLine,
    Vendor,
)
from app.money import ZERO, D, money

# Entity name -> (model, human label)
ENTITY_MODELS = {
    "account": (Account, "account"),
    "invoice": (Invoice, "invoice"),
    "bill": (Bill, "bill"),
    "customer": (Customer, "customer"),
    "vendor": (Vendor, "vendor"),
    "item": (Item, "item"),
    "journal_entry": (JournalEntry, "journal entry"),
    "journal_line": (JournalLine, "journal line"),
    "bank_transaction": (BankTransaction, "bank transaction"),
}


@dataclass
class GroundingResult:
    """Outcome of verifying one AI output against the database."""

    grounded: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    verified: dict[str, Any] = field(default_factory=dict)
    hallucinated_ids: list[dict] = field(default_factory=list)

    def fail(self, message: str) -> None:
        self.grounded = False
        self.errors.append(message)

    def raise_if_ungrounded(self) -> None:
        if not self.grounded:
            raise AIGroundingError(
                "The AI output referenced records that do not exist or do not match: "
                + "; ".join(self.errors),
                details={
                    "errors": self.errors,
                    "hallucinated_ids": self.hallucinated_ids,
                },
            )

    def as_dict(self) -> dict:
        return {
            "grounded": self.grounded,
            "errors": self.errors,
            "warnings": self.warnings,
            "hallucinated_ids": self.hallucinated_ids,
        }


def verify_entity(
    db: Session, company_id: str, entity_type: str, entity_id: str
) -> tuple[bool, Any, str]:
    """Check one id really exists and belongs to this company."""
    spec = ENTITY_MODELS.get(entity_type)
    if spec is None:
        return False, None, f"Unknown entity type '{entity_type}'."

    model, label = spec
    if not entity_id or not isinstance(entity_id, str):
        return False, None, f"No {label} id was supplied."

    record = db.get(model, entity_id)
    if record is None:
        return False, None, f"The {label} id '{entity_id}' does not exist."
    if getattr(record, "company_id", company_id) != company_id:
        return False, None, f"The {label} id '{entity_id}' belongs to a different company."
    return True, record, ""


def verify_ids(
    db: Session, company_id: str, references: Iterable[dict]
) -> GroundingResult:
    """Verify a batch of ``{"type": ..., "id": ...}`` references."""
    result = GroundingResult()
    for reference in references:
        entity_type = str(reference.get("type", "")).lower()
        entity_id = reference.get("id")
        ok, record, message = verify_entity(db, company_id, entity_type, str(entity_id or ""))
        if not ok:
            result.fail(message)
            result.hallucinated_ids.append({"type": entity_type, "id": entity_id, "reason": message})
        else:
            result.verified.setdefault(entity_type, []).append(entity_id)
    return result


def ground_journal_proposal(
    db: Session, company_id: str, payload: dict
) -> tuple[GroundingResult, list[dict]]:
    """Verify a proposed journal entry line by line.

    Returns the grounding result plus the *resolved* lines, where every account
    reference has been replaced by a real account id read from the database.
    """
    result = GroundingResult()
    resolved: list[dict] = []

    lines = payload.get("lines")
    if not isinstance(lines, list) or not lines:
        result.fail("The proposal contains no journal lines.")
        return result, resolved

    total_debit = ZERO
    total_credit = ZERO

    for index, line in enumerate(lines, start=1):
        if not isinstance(line, dict):
            result.fail(f"Line {index} is not an object.")
            continue

        account = None
        account_id = line.get("account_id")
        account_code = line.get("account_code")

        if account_id:
            ok, account, message = verify_entity(db, company_id, "account", str(account_id))
            if not ok:
                result.fail(f"Line {index}: {message}")
                result.hallucinated_ids.append(
                    {"type": "account", "id": account_id, "reason": message}
                )
        elif account_code:
            account = db.execute(
                select(Account).where(
                    Account.company_id == company_id, Account.code == str(account_code)
                )
            ).scalar_one_or_none()
            if account is None:
                message = f"Account code '{account_code}' does not exist in this chart of accounts."
                result.fail(f"Line {index}: {message}")
                result.hallucinated_ids.append(
                    {"type": "account_code", "id": account_code, "reason": message}
                )
        else:
            result.fail(f"Line {index}: no account was specified.")

        if account is not None:
            if account.is_group:
                result.fail(
                    f"Line {index}: '{account.code} {account.name}' is a group account "
                    "and cannot be posted to."
                )
            if not account.is_active:
                result.fail(f"Line {index}: account '{account.code}' is inactive.")

        try:
            debit = money(line.get("debit", 0) or 0)
            credit = money(line.get("credit", 0) or 0)
        except Exception:  # noqa: BLE001
            result.fail(f"Line {index}: amounts are not valid numbers.")
            continue

        if debit < 0 or credit < 0:
            result.fail(f"Line {index}: negative amounts are not allowed.")
        if debit > 0 and credit > 0:
            result.fail(f"Line {index}: a line cannot be both a debit and a credit.")
        if debit == 0 and credit == 0:
            result.fail(f"Line {index}: the amount is zero.")

        total_debit += debit
        total_credit += credit

        # Verify optional party references too — a hallucinated vendor id on an
        # otherwise valid entry would corrupt the sub-ledger.
        party_type = (line.get("party_type") or "").upper()
        party_id = line.get("party_id")
        if party_id and party_type in ("CUSTOMER", "VENDOR"):
            ok, _, message = verify_entity(
                db, company_id, party_type.lower(), str(party_id)
            )
            if not ok:
                result.fail(f"Line {index}: {message}")
                result.hallucinated_ids.append(
                    {"type": party_type.lower(), "id": party_id, "reason": message}
                )
                party_id = None

        if account is not None:
            resolved.append(
                {
                    "account_id": account.id,
                    "account_code": account.code,
                    "account_name": account.name,
                    "debit": debit,
                    "credit": credit,
                    "description": str(line.get("description", ""))[:500],
                    "party_type": party_type or None,
                    "party_id": party_id,
                    "cost_center_id": line.get("cost_center_id"),
                }
            )

    difference = money(total_debit - total_credit)
    if difference != ZERO:
        result.fail(
            f"The proposed entry is out of balance by {difference} "
            f"(debits {money(total_debit)} vs credits {money(total_credit)})."
        )

    result.verified["total_debit"] = money(total_debit)
    result.verified["total_credit"] = money(total_credit)
    return result, resolved


def verify_amount_claim(
    claimed: Any, actual: Decimal, *, label: str, tolerance: Decimal = ZERO
) -> tuple[bool, str]:
    """Check a number the model asserted against the number the books hold."""
    try:
        claimed_value = money(claimed)
    except Exception:  # noqa: BLE001
        return False, f"{label}: '{claimed}' is not a valid amount."

    difference = money(abs(claimed_value - actual))
    if difference > tolerance:
        return False, (
            f"{label}: the model stated {claimed_value} but the ledger holds {actual} "
            f"(difference {difference}). The ledger value is authoritative."
        )
    return True, ""


def ground_answer(
    db: Session, company_id: str, answer: dict, *, allowed_ids: set[str] | None = None
) -> GroundingResult:
    """Verify the source records a copilot answer cites.

    ``allowed_ids`` is the set of ids actually handed to the model. Citing
    anything outside that set means the model invented a reference.
    """
    result = GroundingResult()
    sources = answer.get("source_records") or answer.get("sources") or []

    if not isinstance(sources, list):
        result.warnings.append("The answer's source list was not an array; it was ignored.")
        return result

    for source in sources:
        if not isinstance(source, dict):
            continue
        entity_type = str(source.get("type", "")).lower()
        entity_id = str(source.get("id", ""))
        if not entity_id:
            continue

        if allowed_ids is not None and entity_id not in allowed_ids:
            message = (
                f"The answer cites {entity_type} '{entity_id}', which was not among the "
                "records supplied to the model."
            )
            result.fail(message)
            result.hallucinated_ids.append(
                {"type": entity_type, "id": entity_id, "reason": message}
            )
            continue

        if entity_type in ENTITY_MODELS:
            ok, _, message = verify_entity(db, company_id, entity_type, entity_id)
            if not ok:
                result.fail(message)
                result.hallucinated_ids.append(
                    {"type": entity_type, "id": entity_id, "reason": message}
                )

    return result


def collect_allowed_ids(*collections: Any) -> set[str]:
    """Build the id whitelist from the context handed to the model.

    Walks the supplied structures to any depth. This has to mirror *exactly*
    what the model was shown: an id nested inside an anomaly's evidence list or
    an invoice's lines is on screen for the model just as much as a top-level
    one, and a whitelist built only from the outermost keys rejects a perfectly
    honest citation.

    That failure is not harmless. The check discards the model's wording when it
    trips, so an under-collecting whitelist silently replaces good answers with
    template text — the system looks like it is not using a model at all.

    The safety property is unchanged: a cited id still has to have been present
    in the data supplied. Only keys named ``id`` or ending in ``_id`` are
    harvested, so this cannot admit an arbitrary string.
    """
    allowed: set[str] = set()

    def walk(node: Any, under_id_key: bool = False) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                is_id_key = key == "id" or key.endswith("_id")
                if is_id_key and isinstance(value, str) and value:
                    allowed.add(value)
                else:
                    walk(value, is_id_key)
        elif isinstance(node, (list, tuple, set)):
            for item in node:
                if under_id_key and isinstance(item, str) and item:
                    allowed.add(item)
                else:
                    walk(item, under_id_key)

    for collection in collections:
        walk(collection)
    return allowed


NO_EVIDENCE_ANSWER = (
    "I don't have enough evidence to determine this. "
    "The records I can see don't contain the information needed to answer reliably."
)


def insufficient_evidence(reason: str = "") -> dict:
    """The canonical honest non-answer."""
    return {
        "answer": NO_EVIDENCE_ANSWER,
        "insufficient_evidence": True,
        "reason": reason,
        "confidence": ZERO,
        "evidence": [],
        "source_records": [],
    }
