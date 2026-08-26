"""AI endpoints: copilot, agents, workflows, proposals and approvals.

Approving a proposal is where AI output finally meets the ledger — and it goes
through the same accounting engine as any human entry, with the same
validation. Approval is permission-gated by the role the policy engine chose.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select

from app.ai.agents import AGENT_DESCRIPTIONS, AGENT_REGISTRY, SUGGESTED_PROMPTS
from app.ai.orchestrator import AgentOrchestrator, list_workflows
from app.ai.providers import provider_status
from app.ai.tools import tool_specs
from app.deps import CurrentUserDep, DbDep, audit_context, require_permission
from app.enums import (
    ActorType,
    AgentKind,
    AuditAction,
    Permission,
    ProposalStatus,
    ProposalType,
)
from app.errors import ConflictError, NotFoundError, PolicyViolation, ValidationError
from app.models import AgentRun, AIProposal, Conversation, ConversationMessage
from app.models.base import utcnow
from app.money import ZERO, D, money
from app.schemas import (
    AgentRunOut,
    AskRequest,
    AskResponse,
    MessageResponse,
    Page,
    ProposalDecision,
    ProposalOut,
    ProviderStatusOut,
    RunAgentRequest,
    WorkflowRequest,
)
from app.schemas.common import confidence_label

router = APIRouter(prefix="/ai", tags=["AI"])


# --------------------------------------------------------------- copilot --

@router.post("/ask", response_model=AskResponse)
async def ask(payload: AskRequest, db: DbDep, current: CurrentUserDep) -> AskResponse:
    """Ask the finance copilot a question about the books."""
    current.require(Permission.READ_REPORTS.value)

    orchestrator = AgentOrchestrator(db, current.company_id, user_id=current.id)
    result = await orchestrator.ask(payload.question, conversation_id=payload.conversation_id)

    message_id = result.metrics.get("message_id")
    message = db.get(ConversationMessage, message_id) if message_id else None
    answer = (message.answer if message else {}) or {}

    confidence = D(str(answer.get("confidence", result.metrics.get("confidence", "0"))))
    impact = answer.get("financial_impact")

    return AskResponse(
        answer=answer.get("answer", result.summary),
        key_figures=answer.get("key_figures", []),
        suggested_actions=answer.get("suggested_actions", []),
        evidence=answer.get("evidence", []),
        source_records=answer.get("source_records", []),
        confidence=confidence,
        confidence_label=confidence_label(confidence),
        intent=result.metrics.get("intent", ""),
        tools_used=result.metrics.get("tools_used", []),
        used_llm=result.used_llm,
        provider=answer.get("provider", result.provider),
        insufficient_evidence=bool(answer.get("insufficient_evidence")),
        conversation_id=result.metrics.get("conversation_id"),
        message_id=message_id,
        financial_impact=money(impact) if impact not in (None, "") else None,
        grounding_warning=answer.get("grounding_warning"),
    )


@router.get("/suggested-prompts")
def suggested_prompts(current: CurrentUserDep) -> dict:
    return {"prompts": SUGGESTED_PROMPTS}


@router.get("/conversations")
def list_conversations(
    db: DbDep, current: CurrentUserDep, limit: int = Query(default=20, ge=1, le=100)
) -> dict:
    current.require(Permission.READ_REPORTS.value)
    conversations = list(
        db.execute(
            select(Conversation)
            .where(
                Conversation.company_id == current.company_id,
                Conversation.is_archived.is_(False),
            )
            .order_by(Conversation.last_message_at.desc().nullslast())
            .limit(limit)
        ).scalars()
    )
    return {
        "conversations": [
            {
                "id": conversation.id,
                "title": conversation.title,
                "message_count": conversation.message_count,
                "last_message_at": conversation.last_message_at,
                "created_at": conversation.created_at,
            }
            for conversation in conversations
        ]
    }


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: str, db: DbDep, current: CurrentUserDep) -> dict:
    current.require(Permission.READ_REPORTS.value)
    conversation = db.get(Conversation, conversation_id)
    if conversation is None or conversation.company_id != current.company_id:
        raise NotFoundError("Conversation not found.")

    return {
        "id": conversation.id,
        "title": conversation.title,
        "messages": [
            {
                "id": message.id,
                "role": message.role,
                "content": message.content,
                "answer": message.answer,
                "intent": message.intent,
                "tools_used": message.tools_used,
                "provider": message.provider,
                "created_at": message.created_at,
            }
            for message in conversation.messages
        ],
    }


# ---------------------------------------------------------------- agents --

@router.get("/agents")
def list_agents(db: DbDep, current: CurrentUserDep) -> dict:
    """Every agent with its measured recent activity — no fabricated metrics."""
    current.require(Permission.READ_REPORTS.value)

    since = utcnow() - timedelta(days=30)
    rows = db.execute(
        select(
            AgentRun.agent_kind,
            func.count(AgentRun.id),
            func.sum(AgentRun.items_processed),
            func.sum(AgentRun.proposals_created),
            func.sum(AgentRun.high_confidence_count),
            func.sum(AgentRun.needs_review_count),
            func.max(AgentRun.finished_at),
        )
        .where(AgentRun.company_id == current.company_id, AgentRun.created_at >= since)
        .group_by(AgentRun.agent_kind)
    ).all()
    stats = {row[0]: row for row in rows}

    latest_status = {}
    for kind in AGENT_REGISTRY:
        run = db.execute(
            select(AgentRun)
            .where(AgentRun.company_id == current.company_id, AgentRun.agent_kind == kind)
            .order_by(AgentRun.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        latest_status[kind] = run

    from app.services.ai_learning import measured_accuracy

    agents = []
    for kind, meta in AGENT_DESCRIPTIONS.items():
        row = stats.get(kind)
        run = latest_status.get(kind)
        accuracy = measured_accuracy(db, current.company_id, agent_kind=kind)

        agents.append(
            {
                "kind": kind,
                "name": meta["name"],
                "description": meta["description"],
                "produces": meta["produces"],
                "runs_30d": int(row[1]) if row else 0,
                "items_processed_30d": int(row[2] or 0) if row else 0,
                "proposals_30d": int(row[3] or 0) if row else 0,
                "high_confidence_30d": int(row[4] or 0) if row else 0,
                "needs_review_30d": int(row[5] or 0) if row else 0,
                "last_run_at": run.finished_at if run else None,
                "last_status": run.status if run else "NEVER_RUN",
                "last_summary": (run.output_summary or {}).get("summary", "") if run else "",
                "measured_accuracy": (
                    str(accuracy["accuracy"]) if accuracy["sufficient_data"] else None
                ),
                "accuracy_sample_size": accuracy["sample_size"],
                "accuracy_note": accuracy["message"],
            }
        )

    return {"agents": agents}


@router.post("/agents/{agent_kind}/run", response_model=dict)
async def run_agent(
    agent_kind: str,
    payload: RunAgentRequest,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.PROPOSE_JOURNAL.value))],
) -> dict:
    """Run one agent. Agents propose; they never post."""
    orchestrator = AgentOrchestrator(db, current.company_id, user_id=current.id)
    result = await orchestrator.run_agent(agent_kind.upper(), **payload.params)
    return result.as_dict()


@router.get("/runs", response_model=Page[AgentRunOut])
def list_runs(
    db: DbDep,
    current: CurrentUserDep,
    agent_kind: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[AgentRunOut]:
    current.require(Permission.READ_REPORTS.value)

    stmt = select(AgentRun).where(AgentRun.company_id == current.company_id)
    if agent_kind:
        stmt = stmt.where(AgentRun.agent_kind == agent_kind.upper())

    total = int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    runs = list(
        db.execute(stmt.order_by(AgentRun.created_at.desc()).limit(limit).offset(offset)).scalars()
    )
    return Page(
        items=[AgentRunOut.model_validate(run) for run in runs],
        total=total,
        limit=limit,
        offset=offset,
    )


# ------------------------------------------------------------- workflows --

@router.get("/workflows")
def get_workflows(current: CurrentUserDep) -> dict:
    return {"workflows": list_workflows()}


@router.post("/workflows/{workflow_key}/run")
async def run_workflow(
    workflow_key: str,
    payload: WorkflowRequest,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.PROPOSE_JOURNAL.value))],
) -> dict:
    orchestrator = AgentOrchestrator(db, current.company_id, user_id=current.id)
    result = await orchestrator.run_workflow(workflow_key, params=payload.params)
    return result.as_dict()


@router.get("/briefing")
async def briefing(db: DbDep, current: CurrentUserDep) -> dict:
    """The dashboard's "here's what I found" summary, computed from live data."""
    current.require(Permission.READ_REPORTS.value)
    orchestrator = AgentOrchestrator(db, current.company_id, user_id=current.id)
    return await orchestrator.morning_briefing()


# ------------------------------------------------------------- proposals --

@router.get("/proposals", response_model=Page[ProposalOut])
def list_proposals(
    db: DbDep,
    current: CurrentUserDep,
    status: str | None = Query(default=None),
    proposal_type: str | None = Query(default=None),
    agent_kind: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[ProposalOut]:
    current.require(Permission.READ_REPORTS.value)

    stmt = select(AIProposal).where(AIProposal.company_id == current.company_id)
    if status:
        stmt = stmt.where(AIProposal.status == status.upper())
    else:
        stmt = stmt.where(AIProposal.status == ProposalStatus.PENDING)
    if proposal_type:
        stmt = stmt.where(AIProposal.proposal_type == proposal_type.upper())
    if agent_kind:
        stmt = stmt.where(AIProposal.agent_kind == agent_kind.upper())

    total = int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    proposals = list(
        db.execute(
            stmt.order_by(AIProposal.created_at.desc()).limit(limit).offset(offset)
        ).scalars()
    )

    items = []
    for proposal in proposals:
        out = ProposalOut.model_validate(proposal)
        out.confidence_label = confidence_label(proposal.confidence)
        items.append(out)

    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get("/proposals/{proposal_id}", response_model=ProposalOut)
def get_proposal(proposal_id: str, db: DbDep, current: CurrentUserDep) -> ProposalOut:
    current.require(Permission.READ_REPORTS.value)
    proposal = _get_proposal(db, current.company_id, proposal_id)
    out = ProposalOut.model_validate(proposal)
    out.confidence_label = confidence_label(proposal.confidence)
    return out


@router.post("/proposals/{proposal_id}/approve", response_model=ProposalOut)
def approve_proposal(
    proposal_id: str,
    payload: ProposalDecision,
    request: Request,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.APPROVE_PROPOSAL.value))],
) -> ProposalOut:
    """Approve a proposal and apply it through the deterministic engine."""
    from app.services.ai_learning import log_decision
    from app.services.audit import record_event
    from app.services.policy import role_at_least

    proposal = _get_proposal(db, current.company_id, proposal_id)

    if proposal.status not in (ProposalStatus.PENDING, ProposalStatus.FAILED):
        raise ConflictError(
            f"This proposal is already {proposal.status.lower()}.", code="proposal_decided"
        )

    # The policy engine decided who may approve this; enforce that here.
    if not role_at_least(current.role, proposal.required_role) and not current.user.is_superuser:
        raise PolicyViolation(
            f"This {proposal.proposal_type.lower()} of {proposal.financial_impact} requires "
            f"{proposal.required_role} approval. Your role is {current.role}.",
            code="insufficient_approval_role",
        )

    payload_data = dict(proposal.payload or {})
    if payload.edits:
        payload_data.update(payload.edits)

    result_type, result_id, error = _apply_proposal(db, current, proposal, payload_data)

    if error:
        proposal.status = ProposalStatus.FAILED
        proposal.apply_error = error
        db.flush()
        raise ValidationError(
            f"The proposal could not be applied: {error}",
            details={"proposal_id": proposal.id},
            code="proposal_apply_failed",
        )

    proposal.status = ProposalStatus.APPLIED
    proposal.decided_by_user_id = current.id
    proposal.decided_at = utcnow()
    proposal.decision_reason = payload.reason
    proposal.applied_at = utcnow()
    proposal.result_type = result_type
    proposal.result_id = result_id
    db.flush()

    log_decision(
        db,
        current.company_id,
        agent_kind=proposal.agent_kind,
        proposal_type=proposal.proposal_type,
        prediction=proposal.payload or {},
        human_decision=payload_data,
        decision="EDITED" if payload.edits else "APPROVED",
        was_correct=not payload.edits,
        confidence=proposal.confidence,
        user_id=current.id,
        proposal_id=proposal.id,
        reason=payload.reason,
        context={"target_type": proposal.target_type, "target_id": proposal.target_id},
    )

    record_event(
        db,
        company_id=current.company_id,
        action=AuditAction.APPROVE,
        entity_type="AIProposal",
        entity_id=proposal.id,
        entity_label=proposal.title,
        summary=(
            f"Approved {proposal.agent_kind} proposal '{proposal.title}' "
            f"({proposal.financial_impact}); applied as {result_type} {result_id}."
        ),
        reason=payload.reason,
        actor_type=ActorType.USER,
        actor_id=current.id,
        actor_label=current.email,
        amount=proposal.financial_impact,
        proposal_id=proposal.id,
        before={"status": ProposalStatus.PENDING.value, "confidence": str(proposal.confidence)},
        after={"status": proposal.status, "result_type": result_type, "result_id": result_id},
        **audit_context(request),
    )

    out = ProposalOut.model_validate(proposal)
    out.confidence_label = confidence_label(proposal.confidence)
    return out


@router.post("/proposals/{proposal_id}/reject", response_model=ProposalOut)
def reject_proposal(
    proposal_id: str,
    payload: ProposalDecision,
    request: Request,
    db: DbDep,
    current: Annotated[object, Depends(require_permission(Permission.APPROVE_PROPOSAL.value))],
) -> ProposalOut:
    """Reject a proposal. The rejection feeds the accuracy measurement."""
    from app.services.ai_learning import log_decision
    from app.services.audit import record_event

    proposal = _get_proposal(db, current.company_id, proposal_id)
    if proposal.status not in (ProposalStatus.PENDING, ProposalStatus.FAILED):
        raise ConflictError(
            f"This proposal is already {proposal.status.lower()}.", code="proposal_decided"
        )

    proposal.status = ProposalStatus.REJECTED
    proposal.decided_by_user_id = current.id
    proposal.decided_at = utcnow()
    proposal.decision_reason = payload.reason
    db.flush()

    log_decision(
        db,
        current.company_id,
        agent_kind=proposal.agent_kind,
        proposal_type=proposal.proposal_type,
        prediction=proposal.payload or {},
        human_decision={"action": "rejected", "reason": payload.reason},
        decision="REJECTED",
        was_correct=False,
        confidence=proposal.confidence,
        user_id=current.id,
        proposal_id=proposal.id,
        reason=payload.reason,
    )

    record_event(
        db,
        company_id=current.company_id,
        action=AuditAction.REJECT,
        entity_type="AIProposal",
        entity_id=proposal.id,
        entity_label=proposal.title,
        summary=f"Rejected {proposal.agent_kind} proposal '{proposal.title}'",
        reason=payload.reason,
        actor_type=ActorType.USER,
        actor_id=current.id,
        actor_label=current.email,
        amount=proposal.financial_impact,
        proposal_id=proposal.id,
        **audit_context(request),
    )

    out = ProposalOut.model_validate(proposal)
    out.confidence_label = confidence_label(proposal.confidence)
    return out


# --------------------------------------------------------------- metrics --

@router.get("/metrics")
def ai_metrics(db: DbDep, current: CurrentUserDep) -> dict:
    """Measured AI performance. Confidence and accuracy are reported separately."""
    current.require(Permission.READ_REPORTS.value)

    from app.services.ai_learning import calibration_report, measured_accuracy

    overall = measured_accuracy(db, current.company_id)
    calibration = calibration_report(db, current.company_id)

    status_rows = db.execute(
        select(AIProposal.status, func.count(AIProposal.id))
        .where(AIProposal.company_id == current.company_id)
        .group_by(AIProposal.status)
    ).all()

    run_rows = db.execute(
        select(
            func.count(AgentRun.id),
            func.sum(AgentRun.items_processed),
            func.sum(AgentRun.prompt_tokens),
            func.sum(AgentRun.completion_tokens),
        ).where(AgentRun.company_id == current.company_id)
    ).one()

    return {
        "measured_accuracy": overall,
        "calibration": calibration,
        "proposals_by_status": {status: int(count) for status, count in status_rows},
        "total_runs": int(run_rows[0] or 0),
        "total_items_processed": int(run_rows[1] or 0),
        "prompt_tokens": int(run_rows[2] or 0),
        "completion_tokens": int(run_rows[3] or 0),
        "note": (
            "Accuracy is computed from human decisions on AI proposals. Model confidence "
            "is reported separately and is never treated as accuracy."
        ),
    }


@router.get("/providers", response_model=list[ProviderStatusOut])
async def providers(current: CurrentUserDep) -> list[ProviderStatusOut]:
    """Live provider status. API keys are never returned."""
    statuses = await provider_status()
    return [ProviderStatusOut(**status) for status in statuses]


@router.get("/tools")
def list_tools(current: CurrentUserDep) -> dict:
    """The typed query tools an agent may call. There is no raw SQL path."""
    return {"tools": tool_specs()}


# -------------------------------------------------------------- internals --

def _get_proposal(db, company_id: str, proposal_id: str) -> AIProposal:
    proposal = db.get(AIProposal, proposal_id)
    if proposal is None or proposal.company_id != company_id:
        raise NotFoundError("Proposal not found.")
    return proposal


def _apply_proposal(
    db, current, proposal: AIProposal, payload: dict
) -> tuple[str | None, str | None, str]:
    """Apply an approved proposal through the normal, validated service path."""
    try:
        if proposal.proposal_type == ProposalType.CATEGORISATION:
            from app.services.banking import post_bank_transaction

            transaction = post_bank_transaction(
                db,
                current.company_id,
                payload["bank_transaction_id"],
                account_id=payload["account_id"],
                user_id=current.id,
                learn=True,
            )
            return "BankTransaction", transaction.id, ""

        if proposal.proposal_type in (ProposalType.JOURNAL, ProposalType.ADJUSTMENT):
            from app.accounting.engine import AccountingEngine, EntryInput, LineInput
            from app.ai.grounding import ground_journal_proposal

            grounding, resolved = ground_journal_proposal(db, current.company_id, payload)
            if not grounding.grounded:
                return None, None, "; ".join(grounding.errors)

            engine = AccountingEngine(db, current.company_id, actor_user_id=current.id)
            entry = engine.create_and_post(
                EntryInput(
                    entry_date=date.fromisoformat(str(payload["entry_date"])[:10]),
                    voucher_type=payload.get("voucher_type", "JOURNAL"),
                    narration=payload.get("narration", proposal.title),
                    reference=payload.get("reference", ""),
                    ai_proposal_id=proposal.id,
                    ai_confidence=proposal.confidence,
                    source="AI_PROPOSAL",
                    lines=[
                        LineInput(
                            account_id=line["account_id"],
                            debit=line["debit"],
                            credit=line["credit"],
                            description=line.get("description", ""),
                            party_type=line.get("party_type"),
                            party_id=line.get("party_id"),
                            cost_center_id=line.get("cost_center_id"),
                        )
                        for line in resolved
                    ],
                )
            )
            return "JournalEntry", entry.id, ""

        if proposal.proposal_type == ProposalType.BILL:
            from app.services.purchases import create_bill
            from app.services.sales import LineDraft

            lines = payload.get("lines") or []
            bill = create_bill(
                db,
                current.company_id,
                vendor_id=payload["vendor_id"],
                doc_date=date.fromisoformat(str(payload["doc_date"])[:10]),
                vendor_invoice_number=payload["vendor_invoice_number"],
                due_date=(
                    date.fromisoformat(str(payload["due_date"])[:10])
                    if payload.get("due_date")
                    else None
                ),
                lines=[
                    LineDraft(
                        description=line.get("description", ""),
                        hsn_sac=line.get("hsn_sac", ""),
                        quantity=D(str(line.get("quantity", 1))),
                        rate=D(str(line.get("rate", 0))),
                    )
                    for line in lines
                ]
                or [
                    LineDraft(
                        description=f"As per invoice {payload['vendor_invoice_number']}",
                        quantity=D("1"),
                        rate=D(str(payload.get("taxable_value", "0"))),
                    )
                ],
                document_id=payload.get("document_id"),
                user_id=current.id,
                post=True,
                allow_duplicate=bool(payload.get("allow_duplicate")),
            )
            if payload.get("document_id"):
                from app.services.documents import link_document

                link_document(
                    db,
                    current.company_id,
                    payload["document_id"],
                    linked_type="Bill",
                    linked_id=bill.id,
                    user_id=current.id,
                )
            return "Bill", bill.id, ""

        if proposal.proposal_type == ProposalType.RECONCILIATION_MATCH:
            from app.services.reconciliation import confirm_match

            match = confirm_match(
                db, current.company_id, payload["match_id"], user_id=current.id
            )
            return "ReconciliationMatch", match.id, ""

        # Advisory proposals (payment schedules, collection plans, inventory
        # recommendations) are acknowledged rather than executed: acting on them
        # means creating a payment or a purchase order, which is a separate,
        # explicitly authorised action.
        if proposal.proposal_type in (
            ProposalType.PAYMENT_PRIORITY,
            ProposalType.COLLECTION_ACTION,
            ProposalType.INVENTORY_ACTION,
            ProposalType.CLOSE_TASK,
        ):
            return "Acknowledgement", proposal.id, ""

        return None, None, f"No apply handler exists for '{proposal.proposal_type}'."

    except Exception as exc:  # noqa: BLE001 — surfaced to the caller as a clean error
        return None, None, str(exc)
