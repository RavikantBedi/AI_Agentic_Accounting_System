"""AI learning and *measured* accuracy.

Two numbers are kept strictly apart:

* **Confidence** — what the model claimed. Never presented as accuracy.
* **Measured accuracy** — the share of decided proposals a human accepted
  unchanged. Computed from ``AIDecisionLog`` rows, and simply absent until
  enough decisions exist to mean anything.

Learning is retrieval-based: approved decisions become examples supplied to
later prompts. No model weights are ever modified.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.orm import Session

from app.models import AgentMetric, AIDecisionLog, AIProposal
from app.money import ZERO, D, money, rate as to_rate

# Below this many decided items, an accuracy percentage is noise.
MIN_SAMPLES_FOR_ACCURACY = 10


def log_decision(
    db: Session,
    company_id: str,
    *,
    agent_kind: str,
    proposal_type: str,
    prediction: dict[str, Any],
    human_decision: dict[str, Any],
    decision: str,
    was_correct: bool | None,
    confidence: Decimal,
    user_id: str | None = None,
    proposal_id: str | None = None,
    context: dict[str, Any] | None = None,
    reason: str = "",
) -> AIDecisionLog:
    """Record one prediction/decision pair — the raw material for accuracy."""
    log = AIDecisionLog(
        company_id=company_id,
        proposal_id=proposal_id,
        agent_kind=agent_kind,
        proposal_type=proposal_type,
        prediction=prediction,
        human_decision=human_decision,
        decision=decision,
        was_correct=was_correct,
        confidence=to_rate(confidence),
        context=context or {},
        reason=reason,
        decided_by_user_id=user_id,
        # Only clean approvals become future examples.
        use_as_example=(decision == "APPROVED" and was_correct is True),
        example_key=_example_key(agent_kind, prediction, context or {}),
    )
    db.add(log)
    db.flush()
    return log


def _example_key(agent_kind: str, prediction: dict, context: dict) -> str:
    """A retrieval key that groups similar past decisions together."""
    parts = [agent_kind]
    for field in ("normalised_description", "vendor_name", "counterparty", "category"):
        value = context.get(field) or prediction.get(field)
        if value:
            parts.append(str(value)[:80])
            break
    return "|".join(parts).lower()


def retrieve_examples(
    db: Session,
    company_id: str,
    *,
    agent_kind: str,
    query_text: str = "",
    limit: int = 5,
) -> list[dict]:
    """Fetch past approved decisions to ground a new prompt.

    This is the entire learning mechanism: the model sees what this company's
    accountants actually approved for similar inputs.
    """
    stmt = (
        select(AIDecisionLog)
        .where(
            AIDecisionLog.company_id == company_id,
            AIDecisionLog.agent_kind == agent_kind,
            AIDecisionLog.use_as_example.is_(True),
        )
        .order_by(AIDecisionLog.created_at.desc())
        .limit(200)
    )
    logs = list(db.execute(stmt).scalars())
    if not logs:
        return []

    if query_text:
        needle = query_text.lower()
        scored = []
        for log in logs:
            haystack = " ".join(
                str(v) for v in (list(log.context.values()) + list(log.prediction.values()))
            ).lower()
            overlap = len(set(needle.split()) & set(haystack.split()))
            scored.append((overlap, log))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        logs = [log for score, log in scored if score > 0][:limit] or logs[:limit]
    else:
        logs = logs[:limit]

    return [
        {
            "input": log.context,
            "approved_output": log.human_decision or log.prediction,
            "decided_on": log.created_at.date().isoformat(),
        }
        for log in logs
    ]


def measured_accuracy(
    db: Session,
    company_id: str,
    *,
    agent_kind: str | None = None,
    since: date | None = None,
) -> dict:
    """Accuracy computed from real outcomes, or an honest "not enough data"."""
    # SUM over a Boolean column: SQLAlchemy would otherwise coerce the result
    # back to the column's Boolean type, turning a count of 9 into True (=1).
    # Casting to Integer keeps it a genuine count.
    stmt = select(
        func.count(AIDecisionLog.id),
        func.sum(cast(func.coalesce(AIDecisionLog.was_correct, 0), Integer)),
        func.avg(AIDecisionLog.confidence),
    ).where(
        AIDecisionLog.company_id == company_id,
        AIDecisionLog.was_correct.isnot(None),
    )
    if agent_kind:
        stmt = stmt.where(AIDecisionLog.agent_kind == agent_kind)
    if since:
        stmt = stmt.where(AIDecisionLog.created_at >= since)

    row = db.execute(stmt).one()
    total = int(row[0] or 0)
    correct = int(row[1] or 0)

    # avg() over the string-backed decimal type can come back as text.
    avg_confidence = to_rate(D(row[2])) if row[2] is not None else None

    if total < MIN_SAMPLES_FOR_ACCURACY:
        return {
            "sample_size": total,
            "accuracy": None,
            "avg_confidence": avg_confidence,
            "sufficient_data": False,
            "message": (
                f"Only {total} decided item(s) so far — at least "
                f"{MIN_SAMPLES_FOR_ACCURACY} are needed before an accuracy figure is meaningful."
            ),
        }

    accuracy = to_rate(Decimal(correct) / Decimal(total))
    return {
        "sample_size": total,
        "accuracy": accuracy,
        "correct": correct,
        "incorrect": total - correct,
        "avg_confidence": avg_confidence,
        "sufficient_data": True,
        "message": (
            f"{correct} of {total} decided proposals were accepted unchanged "
            f"({float(accuracy) * 100:.1f}%)."
        ),
    }


def calibration_report(db: Session, company_id: str, *, agent_kind: str | None = None) -> dict:
    """Is claimed confidence trustworthy? Compare it against real outcomes.

    Well-calibrated means: of items claimed at 90-95% confidence, roughly 90-95%
    should actually have been correct.
    """
    stmt = select(AIDecisionLog).where(
        AIDecisionLog.company_id == company_id, AIDecisionLog.was_correct.isnot(None)
    )
    if agent_kind:
        stmt = stmt.where(AIDecisionLog.agent_kind == agent_kind)

    logs = list(db.execute(stmt).scalars())
    buckets = [
        (D("0.0"), D("0.5"), "below 50%"),
        (D("0.5"), D("0.7"), "50-70%"),
        (D("0.7"), D("0.85"), "70-85%"),
        (D("0.85"), D("0.95"), "85-95%"),
        (D("0.95"), D("1.01"), "95-100%"),
    ]

    rows = []
    for low, high, label in buckets:
        in_bucket = [log for log in logs if low <= D(log.confidence) < high]
        if not in_bucket:
            rows.append({"band": label, "count": 0, "actual_accuracy": None, "claimed_avg": None})
            continue
        correct = sum(1 for log in in_bucket if log.was_correct)
        rows.append(
            {
                "band": label,
                "count": len(in_bucket),
                "actual_accuracy": to_rate(Decimal(correct) / Decimal(len(in_bucket))),
                "claimed_avg": to_rate(
                    sum((D(log.confidence) for log in in_bucket), ZERO) / Decimal(len(in_bucket))
                ),
            }
        )

    return {
        "total_decided": len(logs),
        "bands": rows,
        "sufficient_data": len(logs) >= MIN_SAMPLES_FOR_ACCURACY,
        "note": (
            "Confidence is the model's own claim. 'Actual accuracy' is what humans "
            "confirmed. A large gap means confidence should not be trusted."
        ),
    }


def refresh_agent_metrics(db: Session, company_id: str, *, on_date: date | None = None) -> int:
    """Recompute per-agent daily metrics from proposals and decisions."""
    on_date = on_date or date.today()
    start = on_date
    end = on_date + timedelta(days=1)

    rows = db.execute(
        select(
            AIProposal.agent_kind,
            func.count(AIProposal.id),
            func.avg(AIProposal.confidence),
        )
        .where(
            AIProposal.company_id == company_id,
            AIProposal.created_at >= start,
            AIProposal.created_at < end,
        )
        .group_by(AIProposal.agent_kind)
    ).all()

    updated = 0
    for agent_kind, proposal_count, avg_confidence in rows:
        decided = db.execute(
            select(AIProposal.status, func.count(AIProposal.id))
            .where(
                AIProposal.company_id == company_id,
                AIProposal.agent_kind == agent_kind,
                AIProposal.created_at >= start,
                AIProposal.created_at < end,
            )
            .group_by(AIProposal.status)
        ).all()
        by_status = {status: int(count) for status, count in decided}

        approved = by_status.get("APPROVED", 0) + by_status.get("AUTO_APPROVED", 0) + by_status.get("APPLIED", 0)
        rejected = by_status.get("REJECTED", 0)
        decided_count = approved + rejected

        metric = db.execute(
            select(AgentMetric).where(
                AgentMetric.company_id == company_id,
                AgentMetric.agent_kind == agent_kind,
                AgentMetric.metric_date == on_date,
            )
        ).scalar_one_or_none()
        if metric is None:
            metric = AgentMetric(
                company_id=company_id, agent_kind=agent_kind, metric_date=on_date
            )
            db.add(metric)

        metric.proposals_created = int(proposal_count or 0)
        metric.proposals_approved = approved
        metric.proposals_rejected = rejected
        metric.decided_count = decided_count
        metric.avg_confidence = to_rate(D(avg_confidence)) if avg_confidence is not None else ZERO
        metric.measured_accuracy = (
            to_rate(Decimal(approved) / Decimal(decided_count))
            if decided_count >= MIN_SAMPLES_FOR_ACCURACY
            else None
        )
        updated += 1

    db.flush()
    return updated
