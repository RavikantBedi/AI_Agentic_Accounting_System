"""Agent base class.

The shape every agent follows:

    deterministic analysis  ->  optional LLM enrichment  ->  validated proposal

The deterministic step does the accounting work and is what makes the product
useful with no API key at all. The LLM step only ever *classifies* or *phrases*
things, and its output is validated and grounded before it is stored.

An agent can never write to the ledger. It creates ``AIProposal`` rows, which
the policy engine routes and a human approves.
"""
from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.grounding import GroundingResult, ground_journal_proposal
from app.ai.providers import LLMProvider, Message, select_provider
from app.ai.security import assert_no_secrets, build_system_prompt, sanitise_untrusted
from app.config import settings
from app.enums import (
    ActorType,
    AgentKind,
    AgentRunStatus,
    AuditAction,
    ProposalStatus,
    ProposalType,
    Severity,
)
from app.errors import AIError, AIProviderUnavailable, AIResponseInvalid
from app.models import AgentRun, AIProposal, ExceptionItem
from app.models.base import utcnow
from app.money import ZERO, D, money, rate as to_rate
from app.services.policy import evaluate_approval


@dataclass
class AgentResult:
    """What an agent produced in one run."""

    agent_kind: str
    status: str = AgentRunStatus.COMPLETED
    summary: str = ""
    findings: list[dict] = field(default_factory=list)
    proposals: list[str] = field(default_factory=list)
    items_processed: int = 0
    high_confidence_count: int = 0
    needs_review_count: int = 0
    used_llm: bool = False
    provider: str = ""
    model: str = ""
    fallback_used: bool = False
    fallback_reason: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    run_id: str | None = None

    def as_dict(self) -> dict:
        return {
            "agent_kind": self.agent_kind,
            "run_id": self.run_id,
            "status": self.status,
            "summary": self.summary,
            "findings": self.findings,
            "proposal_ids": self.proposals,
            "items_processed": self.items_processed,
            "high_confidence_count": self.high_confidence_count,
            "needs_review_count": self.needs_review_count,
            "used_llm": self.used_llm,
            "provider": self.provider,
            "model": self.model,
            "fallback_used": self.fallback_used,
            "fallback_reason": self.fallback_reason,
            "metrics": self.metrics,
            "error": self.error,
        }


class BaseAgent(ABC):
    """Common machinery: run tracking, LLM access, proposal creation."""

    kind: str = AgentKind.BOOKKEEPING
    display_name: str = "Agent"
    role_description: str = "Assist with accounting analysis."
    #: Agents that never need a model at all set this to False.
    uses_llm: bool = True

    def __init__(self, db: Session, company_id: str, *, user_id: str | None = None):
        self.db = db
        self.company_id = company_id
        self.user_id = user_id
        self.run: AgentRun | None = None
        self._provider: LLMProvider | None = None
        self._fallback_reason = ""

    # ------------------------------------------------------------ running --

    async def execute(self, **params: Any) -> AgentResult:
        """Run the agent, always recording an ``AgentRun`` — success or failure."""
        started = time.perf_counter()
        self.run = AgentRun(
            company_id=self.company_id,
            agent_kind=self.kind,
            status=AgentRunStatus.RUNNING,
            trigger=params.pop("trigger", "MANUAL"),
            triggered_by_user_id=self.user_id,
            parent_run_id=params.pop("parent_run_id", None),
            input_summary={k: str(v)[:200] for k, v in params.items()},
            started_at=utcnow(),
        )
        self.db.add(self.run)
        self.db.flush()

        try:
            result = await self.run_agent(**params)
        except Exception as exc:  # noqa: BLE001 — an agent failure must not break the request
            self.run.status = AgentRunStatus.FAILED
            self.run.error_message = str(exc)[:2000]
            self.run.finished_at = utcnow()
            self.run.duration_ms = int((time.perf_counter() - started) * 1000)
            self.db.flush()
            return AgentResult(
                agent_kind=self.kind,
                status=AgentRunStatus.FAILED,
                summary=f"{self.display_name} could not complete: {exc}",
                error=str(exc),
                run_id=self.run.id,
            )

        result.run_id = self.run.id
        result.provider = result.provider or (self._provider.name if self._provider else "")
        result.model = result.model or (self._provider.model if self._provider else "")
        result.fallback_reason = result.fallback_reason or self._fallback_reason
        result.fallback_used = result.fallback_used or bool(self._fallback_reason)

        self.run.status = result.status
        self.run.output_summary = {"summary": result.summary, **result.metrics}
        self.run.findings = result.findings[:100]
        self.run.items_processed = result.items_processed
        self.run.proposals_created = len(result.proposals)
        self.run.high_confidence_count = result.high_confidence_count
        self.run.needs_review_count = result.needs_review_count
        self.run.used_llm = result.used_llm
        self.run.provider = result.provider
        self.run.model = result.model
        self.run.fallback_used = result.fallback_used
        self.run.fallback_reason = result.fallback_reason[:250]
        self.run.finished_at = utcnow()
        self.run.duration_ms = int((time.perf_counter() - started) * 1000)
        self.db.flush()

        self._audit_run(result)
        return result

    @abstractmethod
    async def run_agent(self, **params: Any) -> AgentResult:
        """The agent's actual work."""

    # ---------------------------------------------------------------- LLM --

    async def provider(self) -> LLMProvider:
        if self._provider is None:
            self._provider = await select_provider()
            self._fallback_reason = getattr(self._provider, "fallback_reason", "")
        return self._provider

    async def ask_json(
        self,
        *,
        task: str,
        instruction: str,
        context: dict,
        untrusted_text: str = "",
        untrusted_label: str = "external content",
        temperature: float = 0.0,
        max_tokens: int = 1500,
    ) -> tuple[dict | None, dict]:
        """Ask the model for JSON, safely.

        Returns ``(parsed_or_None, meta)``. A provider failure or malformed
        response yields ``None`` — callers must handle that and fall back to
        their deterministic result rather than failing the run.
        """
        meta: dict[str, Any] = {"used_llm": False, "threats": [], "error": ""}
        provider = await self.provider()

        payload = json.dumps(context, default=str, ensure_ascii=False)
        user_parts = [
            f"<task>{task}</task>",
            instruction.strip(),
            f"<context>{payload}</context>",
        ]

        if untrusted_text:
            sanitised = sanitise_untrusted(untrusted_text, label=untrusted_label)
            user_parts.append(sanitised.fenced)
            meta["threats"] = sanitised.threats
            if sanitised.threats:
                meta["injection_detected"] = True

        prompt = "\n\n".join(user_parts)
        try:
            assert_no_secrets(prompt)
        except Exception as exc:  # noqa: BLE001
            meta["error"] = str(exc)
            return None, meta

        messages = [
            Message(role="system", content=build_system_prompt(self.role_description)),
            Message(role="user", content=prompt),
        ]

        # A malformed reply is worth retrying; an unreachable provider is not.
        # Without this, one stray non-JSON response silently demotes the answer
        # to template text, which reads to the user as "the AI stopped working"
        # even though the next attempt would have succeeded.
        attempts = max(1, int(settings.LLM_MAX_RETRIES))
        parsed = response = None
        last_invalid: Exception | None = None

        for attempt in range(attempts):
            try:
                parsed, response = await provider.complete_json(
                    messages, temperature=temperature, max_tokens=max_tokens
                )
                break
            except AIResponseInvalid as exc:
                last_invalid = exc
                meta["retries"] = attempt + 1
                continue
            except (AIProviderUnavailable, AIError) as exc:
                meta["error"] = str(exc)
                return None, meta
            except Exception as exc:  # noqa: BLE001
                meta["error"] = f"Unexpected provider error: {exc}"
                return None, meta

        if response is None:
            meta["error"] = str(last_invalid or "The model did not return valid JSON.")
            return None, meta

        if self.run is not None:
            self.run.prompt_tokens += response.prompt_tokens
            self.run.completion_tokens += response.completion_tokens

        meta.update(
            # The deterministic provider is a provider, but it is not a language
            # model. Reporting used_llm=True for it would tell the user an AI
            # composed an answer that was in fact assembled from templates —
            # precisely the claim this product must not make about itself.
            used_llm=response.provider != "mock",
            provider=response.provider,
            model=response.model,
            latency_ms=response.latency_ms,
        )
        if not isinstance(parsed, dict):
            meta["error"] = "The model returned JSON that was not an object."
            return None, meta
        return parsed, meta

    # ---------------------------------------------------------- proposals --

    def create_proposal(
        self,
        *,
        proposal_type: str,
        title: str,
        payload: dict,
        summary: str = "",
        reasoning: str = "",
        confidence: Decimal | float = 0.0,
        financial_impact: Decimal | float = ZERO,
        evidence: list[dict] | None = None,
        source_records: list[dict] | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        severity: str = Severity.INFO,
        validation_errors: list[str] | None = None,
    ) -> AIProposal:
        """Persist a validated proposal with its policy decision attached."""
        confidence_value = to_rate(confidence)
        impact = money(abs(D(financial_impact)))

        entity_type = {
            ProposalType.JOURNAL.value: "JOURNAL",
            ProposalType.BILL.value: "BILL",
            ProposalType.INVOICE.value: "INVOICE",
            ProposalType.ADJUSTMENT.value: "JOURNAL",
            ProposalType.CATEGORISATION.value: "CATEGORISATION",
            ProposalType.RECONCILIATION_MATCH.value: "RECONCILIATION_MATCH",
            ProposalType.PAYMENT_PRIORITY.value: "PAYMENT",
        }.get(proposal_type, "JOURNAL")

        decision = evaluate_approval(
            self.db,
            self.company_id,
            entity_type=entity_type,
            amount=impact,
            confidence=confidence_value,
            proposed_by_ai=True,
        )

        proposal = AIProposal(
            company_id=self.company_id,
            agent_run_id=self.run.id if self.run else None,
            agent_kind=self.kind,
            proposal_type=proposal_type,
            status=ProposalStatus.PENDING,
            title=title[:250],
            summary=summary,
            reasoning=reasoning,
            payload=payload,
            evidence=evidence or [],
            source_records=source_records or [],
            target_type=target_type,
            target_id=target_id,
            confidence=confidence_value,
            financial_impact=impact,
            severity=severity,
            requires_approval=decision.requires_approval,
            required_role=decision.required_role,
            policy_reason=decision.reason[:400],
            validation_errors=validation_errors or [],
        )
        self.db.add(proposal)
        self.db.flush()
        return proposal

    def validate_journal_payload(self, payload: dict) -> tuple[GroundingResult, list[dict]]:
        """Ground a proposed journal and dry-run it through the engine.

        A proposal only reaches a human if it would actually post cleanly.
        """
        from app.accounting.engine import AccountingEngine, EntryInput, LineInput

        result, resolved = ground_journal_proposal(self.db, self.company_id, payload)
        if not result.grounded:
            return result, resolved

        try:
            entry_date = payload.get("entry_date")
            parsed_date = (
                date.fromisoformat(str(entry_date)[:10]) if entry_date else date.today()
            )
        except ValueError:
            result.fail(f"'{payload.get('entry_date')}' is not a valid date.")
            return result, resolved

        engine = AccountingEngine(self.db, self.company_id, actor_user_id=self.user_id)
        report = engine.validate(
            EntryInput(
                entry_date=parsed_date,
                voucher_type=payload.get("voucher_type", "JOURNAL"),
                narration=payload.get("narration", ""),
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
        for error in report.errors:
            result.fail(error)
        result.warnings.extend(report.warnings)
        return result, resolved

    def raise_exception_item(
        self,
        *,
        category: str,
        severity: str,
        title: str,
        reason: str,
        recommended_action: str,
        dedupe_key: str,
        evidence: list[dict] | None = None,
        amount: Decimal | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        action_url: str = "",
    ) -> ExceptionItem | None:
        """Add an item to the exception centre, deduplicated."""
        key = dedupe_key[:250]
        existing = self.db.execute(
            select(ExceptionItem).where(
                ExceptionItem.company_id == self.company_id,
                ExceptionItem.dedupe_key == key,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return None

        item = ExceptionItem(
            company_id=self.company_id,
            category=category,
            severity=severity,
            title=title[:250],
            reason=reason,
            recommended_action=recommended_action,
            evidence=evidence or [],
            amount=amount,
            entity_type=entity_type,
            entity_id=entity_id,
            action_url=action_url,
            dedupe_key=key,
            detected_by="AI",
            agent_run_id=self.run.id if self.run else None,
        )
        self.db.add(item)
        self.db.flush()
        return item

    # -------------------------------------------------------------- audit --

    def _audit_run(self, result: AgentResult) -> None:
        from app.services.audit import record_event

        record_event(
            self.db,
            company_id=self.company_id,
            action=AuditAction.AI_RUN,
            entity_type="AgentRun",
            entity_id=self.run.id if self.run else None,
            entity_label=self.display_name,
            summary=result.summary[:500] or f"{self.display_name} run {result.status}",
            actor_type=ActorType.AI_AGENT,
            actor_id=self.run.id if self.run else None,
            actor_label=self.display_name,
            agent_kind=self.kind,
            agent_run_id=self.run.id if self.run else None,
            after={
                "status": result.status,
                "items_processed": result.items_processed,
                "proposals": len(result.proposals),
                "provider": result.provider,
                "used_llm": result.used_llm,
            },
        )
