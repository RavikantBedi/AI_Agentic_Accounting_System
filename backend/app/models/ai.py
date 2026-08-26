"""AI-layer persistence: runs, proposals, exceptions, anomalies and learning.

Nothing in this module is authoritative for the books. Every row here is either
a *proposal* awaiting deterministic validation and human decision, or a record
of what an agent did and why.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Money, Rate
from app.enums import (
    AgentKind,
    AgentRunStatus,
    AnomalyKind,
    ExceptionCategory,
    ExceptionStatus,
    ProposalStatus,
    ProposalType,
    Severity,
)
from app.models.base import Base, CompanyScoped, TimestampMixin, UTCDateTime, UUIDPk


class AgentRun(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """One execution of one agent. Always recorded, success or failure."""

    __tablename__ = "agent_runs"
    __table_args__ = (Index("ix_agent_run_kind", "company_id", "agent_kind", "created_at"),)

    agent_kind: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default=AgentRunStatus.QUEUED, index=True)
    trigger: Mapped[str] = mapped_column(String(40), default="MANUAL")  # MANUAL|ORCHESTRATOR|SCHEDULE

    provider: Mapped[str] = mapped_column(String(20), default="")
    model: Mapped[str] = mapped_column(String(80), default="")
    used_llm: Mapped[bool] = mapped_column(Boolean, default=False)
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False)
    fallback_reason: Mapped[str] = mapped_column(String(250), default="")

    input_summary: Mapped[dict] = mapped_column(default=dict)
    output_summary: Mapped[dict] = mapped_column(default=dict)
    findings: Mapped[list] = mapped_column(default=list)

    items_processed: Mapped[int] = mapped_column(Integer, default=0)
    proposals_created: Mapped[int] = mapped_column(Integer, default=0)
    high_confidence_count: Mapped[int] = mapped_column(Integer, default=0)
    needs_review_count: Mapped[int] = mapped_column(Integer, default=0)

    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)

    error_message: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    triggered_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    parent_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)


class AIProposal(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """A structured, validated proposal awaiting policy + human decision.

    ``payload`` has already passed Pydantic validation and, for journals, the
    accounting rule engine's dry-run — so approving it cannot produce an
    unbalanced or illegal entry.
    """

    __tablename__ = "ai_proposals"
    __table_args__ = (
        Index("ix_proposal_status", "company_id", "status", "created_at"),
        Index("ix_proposal_target", "company_id", "target_type", "target_id"),
    )

    agent_run_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    agent_kind: Mapped[str] = mapped_column(String(30), default=AgentKind.BOOKKEEPING, index=True)
    proposal_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default=ProposalStatus.PENDING, index=True)

    title: Mapped[str] = mapped_column(String(250), nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="")
    reasoning: Mapped[str] = mapped_column(Text, default="")

    payload: Mapped[dict] = mapped_column(default=dict, nullable=False)
    evidence: Mapped[list] = mapped_column(default=list)  # [{type,id,label,url,...}]
    source_records: Mapped[list] = mapped_column(default=list)

    target_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    confidence: Mapped[Decimal] = mapped_column(Rate, default=Decimal("0.000000"))
    financial_impact: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    severity: Mapped[str] = mapped_column(String(10), default=Severity.INFO)

    # Policy decision, computed deterministically before a human ever sees it.
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=True)
    required_role: Mapped[str] = mapped_column(String(30), default="ACCOUNTANT")
    policy_reason: Mapped[str] = mapped_column(String(400), default="")
    validation_errors: Mapped[list] = mapped_column(default=list)

    decided_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    decision_reason: Mapped[str] = mapped_column(Text, default="")

    applied_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    result_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    result_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    apply_error: Mapped[str] = mapped_column(Text, default="")

    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class ExceptionItem(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """A row in the "Needs Your Attention" centre."""

    __tablename__ = "exception_items"
    __table_args__ = (
        Index("ix_exception_open", "company_id", "status", "severity"),
        UniqueConstraint("company_id", "dedupe_key", name="uq_exception_dedupe"),
    )

    category: Mapped[str] = mapped_column(String(20), default=ExceptionCategory.ACCOUNTING, index=True)
    severity: Mapped[str] = mapped_column(String(10), default=Severity.MEDIUM, index=True)
    status: Mapped[str] = mapped_column(String(20), default=ExceptionStatus.OPEN, index=True)

    title: Mapped[str] = mapped_column(String(250), nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="")
    recommended_action: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[list] = mapped_column(default=list)

    amount: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    entity_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action_url: Mapped[str] = mapped_column(String(300), default="")

    dedupe_key: Mapped[str] = mapped_column(String(250), nullable=False, index=True)
    detected_by: Mapped[str] = mapped_column(String(30), default="ENGINE")  # ENGINE | AI | HUMAN
    agent_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    proposal_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    assigned_to_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolved_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    resolution_note: Mapped[str] = mapped_column(Text, default="")


class Anomaly(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """A detected control/fraud signal with its evidence and recommendation."""

    __tablename__ = "anomalies"
    __table_args__ = (
        Index("ix_anomaly_kind", "company_id", "kind", "status"),
        UniqueConstraint("company_id", "dedupe_key", name="uq_anomaly_dedupe"),
    )

    kind: Mapped[str] = mapped_column(String(40), default=AnomalyKind.LARGE_TRANSACTION, index=True)
    severity: Mapped[str] = mapped_column(String(10), default=Severity.MEDIUM, index=True)
    status: Mapped[str] = mapped_column(String(20), default=ExceptionStatus.OPEN, index=True)

    title: Mapped[str] = mapped_column(String(250), nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="")
    recommendation: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[list] = mapped_column(default=list)
    detection_method: Mapped[str] = mapped_column(String(30), default="RULE")  # RULE | STATISTICAL | AI

    amount: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    entity_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    related_entity_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    related_entity_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    occurred_on: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)

    # Statistical context so the number is explainable, not a black box.
    baseline_value: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    observed_value: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    deviation_score: Mapped[Decimal | None] = mapped_column(Rate, nullable=True)

    dedupe_key: Mapped[str] = mapped_column(String(250), nullable=False, index=True)
    agent_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reviewed_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    resolution_note: Mapped[str] = mapped_column(Text, default="")
    is_true_positive: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class AIDecisionLog(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """Prediction vs human decision — the ground truth for accuracy metrics.

    Measured accuracy is computed from these rows. Model *confidence* is stored
    separately and never presented as accuracy.
    """

    __tablename__ = "ai_decision_logs"
    __table_args__ = (
        Index("ix_decision_agent", "company_id", "agent_kind", "created_at"),
        Index("ix_decision_outcome", "company_id", "was_correct"),
    )

    proposal_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("ai_proposals.id", ondelete="SET NULL"), nullable=True, index=True
    )
    agent_kind: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    proposal_type: Mapped[str] = mapped_column(String(30), default=ProposalType.CATEGORISATION)

    prediction: Mapped[dict] = mapped_column(default=dict)
    human_decision: Mapped[dict] = mapped_column(default=dict)
    decision: Mapped[str] = mapped_column(String(20), default="APPROVED")  # APPROVED|REJECTED|EDITED
    was_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True, index=True)

    confidence: Mapped[Decimal] = mapped_column(Rate, default=Decimal("0.000000"))
    context: Mapped[dict] = mapped_column(default=dict)
    reason: Mapped[str] = mapped_column(Text, default="")
    decided_by_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Approved decisions become retrieval examples for future prompts. Model
    # weights are never modified.
    use_as_example: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    example_key: Mapped[str] = mapped_column(String(250), default="", index=True)


class Conversation(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """A copilot thread."""

    __tablename__ = "conversations"

    title: Mapped[str] = mapped_column(String(250), default="New conversation")
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    last_message_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)

    messages: Mapped[list["ConversationMessage"]] = relationship(
        cascade="all, delete-orphan", order_by="ConversationMessage.created_at"
    )


class ConversationMessage(Base, UUIDPk, CompanyScoped, TimestampMixin):
    __tablename__ = "conversation_messages"

    conversation_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # user | assistant | system
    content: Mapped[str] = mapped_column(Text, default="")

    # Structured answer payload: evidence, impact, actions, confidence, sources.
    answer: Mapped[dict] = mapped_column(default=dict)
    intent: Mapped[str] = mapped_column(String(60), default="")
    tools_used: Mapped[list] = mapped_column(default=list)
    provider: Mapped[str] = mapped_column(String(20), default="")
    model: Mapped[str] = mapped_column(String(80), default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class AgentMetric(Base, UUIDPk, CompanyScoped, TimestampMixin):
    """Daily, measured agent performance. Every number is computed, never fixed."""

    __tablename__ = "agent_metrics"
    __table_args__ = (UniqueConstraint("company_id", "agent_kind", "metric_date", name="uq_agent_metric"),)

    agent_kind: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    metric_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    runs: Mapped[int] = mapped_column(Integer, default=0)
    items_processed: Mapped[int] = mapped_column(Integer, default=0)
    proposals_created: Mapped[int] = mapped_column(Integer, default=0)
    proposals_approved: Mapped[int] = mapped_column(Integer, default=0)
    proposals_rejected: Mapped[int] = mapped_column(Integer, default=0)
    proposals_edited: Mapped[int] = mapped_column(Integer, default=0)

    # Mean confidence the model *claimed*.
    avg_confidence: Mapped[Decimal] = mapped_column(Rate, default=Decimal("0.000000"))
    # Share of decided proposals a human accepted unchanged — the real number.
    measured_accuracy: Mapped[Decimal | None] = mapped_column(Rate, nullable=True)
    decided_count: Mapped[int] = mapped_column(Integer, default=0)

    failures: Mapped[int] = mapped_column(Integer, default=0)
    avg_duration_ms: Mapped[int] = mapped_column(Integer, default=0)
