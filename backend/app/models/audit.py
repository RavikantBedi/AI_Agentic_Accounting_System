"""Immutable audit trail.

Rows are append-only. Nothing in the application updates or deletes an audit
event, and each row carries a hash chained to the previous one, so tampering
with history is detectable (``services.audit.verify_chain``).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Money
from app.enums import ActorType, AuditAction
from app.models.base import Base, CompanyScoped, TimestampMixin, UTCDateTime, UUIDPk


class AuditEvent(Base, UUIDPk, CompanyScoped, TimestampMixin):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_entity", "company_id", "entity_type", "entity_id", "created_at"),
        Index("ix_audit_actor", "company_id", "actor_type", "actor_id", "created_at"),
        Index("ix_audit_action", "company_id", "action", "created_at"),
        Index("ix_audit_seq", "company_id", "sequence"),
    )

    sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(30), default=AuditAction.UPDATE, nullable=False)

    actor_type: Mapped[str] = mapped_column(String(20), default=ActorType.USER, nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    actor_label: Mapped[str] = mapped_column(String(200), default="")
    agent_kind: Mapped[str] = mapped_column(String(30), default="")

    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    entity_label: Mapped[str] = mapped_column(String(250), default="")

    summary: Mapped[str] = mapped_column(String(500), default="")
    reason: Mapped[str] = mapped_column(Text, default="")

    # Full before/after snapshots — the auditor's "what changed and why".
    before: Mapped[dict | None] = mapped_column(nullable=True)
    after: Mapped[dict | None] = mapped_column(nullable=True)
    changed_fields: Mapped[list] = mapped_column(default=list)

    amount: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    approval_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    proposal_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    agent_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    request_id: Mapped[str] = mapped_column(String(40), default="")
    ip_address: Mapped[str] = mapped_column(String(45), default="")
    user_agent: Mapped[str] = mapped_column(String(300), default="")

    # Tamper-evidence: sha256(prev_hash + canonical(payload)).
    prev_hash: Mapped[str] = mapped_column(String(64), default="")
    hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
