"""Append-only audit trail with a tamper-evident hash chain.

Each event stores ``sha256(prev_hash + canonical_json(payload))``. Rewriting or
deleting history breaks the chain, and ``verify_chain`` reports exactly where.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.enums import ActorType, AuditAction
from app.models import AuditEvent
from app.models.base import utcnow

# Values that must never be written into an audit payload.
_REDACTED_KEYS = {
    "password",
    "password_hash",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "authorization",
    "secret_key",
}


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(value)
    return str(value)


def scrub(payload: Any) -> Any:
    """Recursively drop secret-looking values before they reach the audit log."""
    if isinstance(payload, dict):
        return {
            key: ("***redacted***" if str(key).lower() in _REDACTED_KEYS else scrub(value))
            for key, value in payload.items()
        }
    if isinstance(payload, (list, tuple)):
        return [scrub(item) for item in payload]
    return payload


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_json_default)


def compute_hash(prev_hash: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256((prev_hash + canonical_json(payload)).encode("utf-8")).hexdigest()


def diff_fields(before: dict | None, after: dict | None) -> list[str]:
    if not before or not after:
        return []
    changed = []
    for key in set(before) | set(after):
        if before.get(key) != after.get(key):
            changed.append(key)
    return sorted(changed)


def record_event(
    db: Session,
    *,
    company_id: str,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    entity_label: str = "",
    summary: str = "",
    reason: str = "",
    actor_type: str = ActorType.SYSTEM,
    actor_id: str | None = None,
    actor_label: str = "",
    agent_kind: str = "",
    before: dict | None = None,
    after: dict | None = None,
    amount: Decimal | None = None,
    request_id: str = "",
    ip_address: str = "",
    user_agent: str = "",
    proposal_id: str | None = None,
    agent_run_id: str | None = None,
    approval_id: str | None = None,
) -> AuditEvent:
    """Append one immutable audit event. Never updates an existing row."""
    before = scrub(before) if before else None
    after = scrub(after) if after else None

    last = db.execute(
        select(AuditEvent)
        .where(AuditEvent.company_id == company_id)
        .order_by(AuditEvent.sequence.desc())
        .limit(1)
    ).scalar_one_or_none()

    sequence = (last.sequence + 1) if last else 1
    prev_hash = last.hash if last else ""
    occurred_at = utcnow()

    payload = {
        "sequence": sequence,
        "company_id": company_id,
        "occurred_at": occurred_at.isoformat(),
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "summary": summary,
        "reason": reason,
        "before": before,
        "after": after,
        "amount": str(amount) if amount is not None else None,
    }

    event = AuditEvent(
        company_id=company_id,
        sequence=sequence,
        occurred_at=occurred_at,
        action=action,
        actor_type=actor_type,
        actor_id=actor_id,
        actor_label=actor_label,
        agent_kind=agent_kind,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_label=entity_label[:250],
        summary=summary[:500],
        reason=reason,
        before=before,
        after=after,
        changed_fields=diff_fields(before, after),
        amount=amount,
        request_id=request_id,
        ip_address=ip_address,
        user_agent=user_agent[:300],
        proposal_id=proposal_id,
        agent_run_id=agent_run_id,
        approval_id=approval_id,
        prev_hash=prev_hash,
        hash=compute_hash(prev_hash, payload),
        is_system=(actor_type == ActorType.SYSTEM),
    )
    db.add(event)
    db.flush()
    return event


def verify_chain(db: Session, company_id: str, *, limit: int | None = None) -> dict[str, Any]:
    """Recompute every hash and report the first break, if any."""
    stmt = (
        select(AuditEvent)
        .where(AuditEvent.company_id == company_id)
        .order_by(AuditEvent.sequence.asc())
    )
    if limit:
        stmt = stmt.limit(limit)
    events = list(db.execute(stmt).scalars())

    prev_hash = ""
    for event in events:
        payload = {
            "sequence": event.sequence,
            "company_id": event.company_id,
            "occurred_at": event.occurred_at.isoformat(),
            "action": event.action,
            "entity_type": event.entity_type,
            "entity_id": event.entity_id,
            "actor_type": event.actor_type,
            "actor_id": event.actor_id,
            "summary": event.summary,
            "reason": event.reason,
            "before": event.before,
            "after": event.after,
            "amount": str(event.amount) if event.amount is not None else None,
        }
        expected = compute_hash(prev_hash, payload)
        if event.prev_hash != prev_hash or event.hash != expected:
            return {
                "valid": False,
                "events_checked": len(events),
                "broken_at_sequence": event.sequence,
                "broken_event_id": event.id,
                "message": (
                    f"Audit chain broken at sequence {event.sequence}: "
                    "the stored hash does not match the recomputed value."
                ),
            }
        prev_hash = event.hash

    return {
        "valid": True,
        "events_checked": len(events),
        "broken_at_sequence": None,
        "message": f"Audit chain intact across {len(events)} events.",
    }


def entity_timeline(
    db: Session, company_id: str, entity_type: str, entity_id: str
) -> list[AuditEvent]:
    """Every recorded change to one record, oldest first."""
    return list(
        db.execute(
            select(AuditEvent)
            .where(
                AuditEvent.company_id == company_id,
                AuditEvent.entity_type == entity_type,
                AuditEvent.entity_id == entity_id,
            )
            .order_by(AuditEvent.sequence.asc())
        ).scalars()
    )


def count_events(db: Session, company_id: str) -> int:
    return int(
        db.execute(
            select(func.count(AuditEvent.id)).where(AuditEvent.company_id == company_id)
        ).scalar_one()
    )
