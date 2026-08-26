"""Agent orchestrator.

Routes work to the right agents and chains them into named workflows:

    invoice uploaded -> Document -> AP -> GST -> Anomaly
    bank imported    -> Bookkeeping -> Reconciliation -> Anomaly
    month end        -> Close -> Reconciliation -> GST -> Reporting -> Anomaly

Each step runs as its own audited ``AgentRun`` linked to the parent, so a
workflow is fully traceable. A failing step never aborts the workflow — the
remaining steps still run and the failure is reported.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.ai.agents import AGENT_DESCRIPTIONS, AgentResult, get_agent_class
from app.enums import AgentKind, AgentRunStatus
from app.errors import ValidationError


@dataclass
class WorkflowStep:
    agent_kind: str
    params: dict[str, Any] = field(default_factory=dict)
    optional: bool = True


@dataclass
class Workflow:
    key: str
    name: str
    description: str
    steps: list[WorkflowStep]


WORKFLOWS: dict[str, Workflow] = {
    "document_intake": Workflow(
        key="document_intake",
        name="Process uploaded documents",
        description=(
            "Extract invoice data, match the vendor, check for duplicates and propose "
            "a bill for approval."
        ),
        steps=[
            WorkflowStep(AgentKind.DOCUMENT.value),
            WorkflowStep(AgentKind.AP.value),
            WorkflowStep(AgentKind.ANOMALY.value),
        ],
    ),
    "bank_intake": Workflow(
        key="bank_intake",
        name="Process bank transactions",
        description="Categorise new bank lines, reconcile them, then scan for anomalies.",
        steps=[
            WorkflowStep(AgentKind.BOOKKEEPING.value),
            WorkflowStep(AgentKind.RECONCILIATION.value),
            WorkflowStep(AgentKind.ANOMALY.value),
        ],
    ),
    "month_end_close": Workflow(
        key="month_end_close",
        name="Prepare month-end close",
        description=(
            "Run every close check, reconcile the banks, prepare GST, explain the "
            "variance and scan for anomalies."
        ),
        steps=[
            WorkflowStep(AgentKind.RECONCILIATION.value),
            WorkflowStep(AgentKind.ANOMALY.value),
            WorkflowStep(AgentKind.GST.value),
            WorkflowStep(AgentKind.REPORTING.value),
            WorkflowStep(AgentKind.CLOSE.value),
        ],
    ),
    "daily_review": Workflow(
        key="daily_review",
        name="Daily review",
        description="The morning sweep: categorise, reconcile, check receivables, payables and cash.",
        steps=[
            WorkflowStep(AgentKind.BOOKKEEPING.value),
            WorkflowStep(AgentKind.RECONCILIATION.value),
            WorkflowStep(AgentKind.AR.value),
            WorkflowStep(AgentKind.AP.value),
            WorkflowStep(AgentKind.CASH_FORECAST.value),
            WorkflowStep(AgentKind.ANOMALY.value),
        ],
    ),
    "controls_review": Workflow(
        key="controls_review",
        name="Controls and integrity review",
        description="Fraud signals, anomaly scan and audit-trail verification.",
        steps=[
            WorkflowStep(AgentKind.FRAUD.value),
            WorkflowStep(AgentKind.ANOMALY.value),
            WorkflowStep(AgentKind.AUDIT.value),
        ],
    ),
    "financial_review": Workflow(
        key="financial_review",
        name="Financial performance review",
        description="Variance explanation, ratio analysis and a cash forecast.",
        steps=[
            WorkflowStep(AgentKind.REPORTING.value),
            WorkflowStep(AgentKind.ANALYST.value),
            WorkflowStep(AgentKind.CASH_FORECAST.value),
        ],
    ),
}


@dataclass
class WorkflowResult:
    workflow: str
    name: str
    status: str
    summary: str
    steps: list[dict] = field(default_factory=list)
    total_proposals: int = 0
    total_findings: int = 0
    needs_review: int = 0
    failed_steps: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "workflow": self.workflow,
            "name": self.name,
            "status": self.status,
            "summary": self.summary,
            "steps": self.steps,
            "total_proposals": self.total_proposals,
            "total_findings": self.total_findings,
            "needs_review": self.needs_review,
            "failed_steps": self.failed_steps,
        }


class AgentOrchestrator:
    """Runs a single agent or a named workflow."""

    def __init__(self, db: Session, company_id: str, *, user_id: str | None = None):
        self.db = db
        self.company_id = company_id
        self.user_id = user_id

    async def run_agent(self, agent_kind: str, **params: Any) -> AgentResult:
        agent_class = get_agent_class(agent_kind)
        agent = agent_class(self.db, self.company_id, user_id=self.user_id)
        return await agent.execute(**params)

    async def run_workflow(
        self, workflow_key: str, *, params: dict[str, Any] | None = None
    ) -> WorkflowResult:
        workflow = WORKFLOWS.get(workflow_key)
        if workflow is None:
            raise ValidationError(
                f"'{workflow_key}' is not a known workflow.",
                details={"available": sorted(WORKFLOWS)},
                code="unknown_workflow",
            )

        params = params or {}
        result = WorkflowResult(
            workflow=workflow.key,
            name=workflow.name,
            status=AgentRunStatus.COMPLETED,
            summary="",
        )

        parent_run_id: str | None = None
        for step in workflow.steps:
            step_params = {**step.params, **params.get(step.agent_kind, {})}
            step_params["trigger"] = "ORCHESTRATOR"
            if parent_run_id:
                step_params["parent_run_id"] = parent_run_id

            try:
                step_result = await self.run_agent(step.agent_kind, **step_params)
            except Exception as exc:  # noqa: BLE001 — keep the workflow going
                result.failed_steps.append(step.agent_kind)
                result.steps.append(
                    {
                        "agent_kind": step.agent_kind,
                        "name": AGENT_DESCRIPTIONS.get(step.agent_kind, {}).get(
                            "name", step.agent_kind
                        ),
                        "status": AgentRunStatus.FAILED,
                        "summary": f"Step failed: {exc}",
                        "error": str(exc),
                    }
                )
                continue

            parent_run_id = parent_run_id or step_result.run_id

            if step_result.status == AgentRunStatus.FAILED:
                result.failed_steps.append(step.agent_kind)

            result.total_proposals += len(step_result.proposals)
            result.total_findings += len(step_result.findings)
            result.needs_review += step_result.needs_review_count
            result.steps.append(
                {
                    "agent_kind": step.agent_kind,
                    "name": AGENT_DESCRIPTIONS.get(step.agent_kind, {}).get(
                        "name", step.agent_kind
                    ),
                    "run_id": step_result.run_id,
                    "status": step_result.status,
                    "summary": step_result.summary,
                    "proposals": len(step_result.proposals),
                    "findings": len(step_result.findings),
                    "needs_review": step_result.needs_review_count,
                    "used_llm": step_result.used_llm,
                    "provider": step_result.provider,
                    "metrics": _stringify(step_result.metrics),
                }
            )

        completed = [s for s in result.steps if s["status"] != AgentRunStatus.FAILED]
        if result.failed_steps and not completed:
            result.status = AgentRunStatus.FAILED
        elif result.needs_review or result.failed_steps:
            result.status = AgentRunStatus.NEEDS_REVIEW
        else:
            result.status = AgentRunStatus.COMPLETED

        result.summary = (
            f"{workflow.name}: {len(completed)}/{len(workflow.steps)} step(s) completed, "
            f"{result.total_proposals} proposal(s) created, "
            f"{result.needs_review} item(s) need review."
            + (
                f" Failed step(s): {', '.join(result.failed_steps)}."
                if result.failed_steps
                else ""
            )
        )
        return result

    async def ask(self, question: str, *, conversation_id: str | None = None) -> AgentResult:
        """Route a natural-language question to the copilot."""
        return await self.run_agent(
            AgentKind.COPILOT.value, question=question, conversation_id=conversation_id
        )

    async def morning_briefing(self) -> dict:
        """The "good morning" summary shown on the dashboard.

        Computed from live data, not from a model — the model would add nothing
        that the numbers do not already say.
        """
        from sqlalchemy import func, select

        from app.enums import ExceptionStatus, ReconStatus
        from app.models import Anomaly, BankTransaction, ExceptionItem
        from app.money import ZERO, money
        from app.services.close import close_status
        from app.services.forecast import current_cash
        from app.services.reports import ap_aging, ar_aging

        unreconciled = int(
            self.db.execute(
                select(func.count(BankTransaction.id)).where(
                    BankTransaction.company_id == self.company_id,
                    BankTransaction.recon_status.in_(
                        [ReconStatus.UNMATCHED, ReconStatus.AMBIGUOUS, ReconStatus.EXCEPTION]
                    ),
                )
            ).scalar_one()
        )
        open_exceptions = int(
            self.db.execute(
                select(func.count(ExceptionItem.id)).where(
                    ExceptionItem.company_id == self.company_id,
                    ExceptionItem.status.in_([ExceptionStatus.OPEN, ExceptionStatus.IN_REVIEW]),
                )
            ).scalar_one()
        )
        open_anomalies = int(
            self.db.execute(
                select(func.count(Anomaly.id)).where(
                    Anomaly.company_id == self.company_id,
                    Anomaly.status == ExceptionStatus.OPEN,
                )
            ).scalar_one()
        )

        ar = ar_aging(self.db, self.company_id)
        ap = ap_aging(self.db, self.company_id)
        cash = current_cash(self.db, self.company_id)
        close = close_status(self.db, self.company_id)

        # Amounts in the briefing are formatted for reading, not dumped raw:
        # "₹1.26Cr in overdue receivables" beats "12564292.00 in overdue
        # receivables" for the person glancing at the dashboard.
        from app.money import fmt_inr

        def compact(amount) -> str:
            rupees = float(money(amount))
            if rupees >= 1_00_00_000:
                return f"₹{rupees / 1_00_00_000:.2f}Cr"
            if rupees >= 1_00_000:
                return f"₹{rupees / 1_00_000:.2f}L"
            if rupees >= 1_000:
                return f"₹{rupees / 1_000:.1f}K"
            return fmt_inr(amount)

        highlights: list[dict] = []
        if unreconciled:
            highlights.append(
                {
                    "kind": "reconciliation",
                    "text": (
                        f"{unreconciled} unreconciled bank transaction"
                        f"{'' if unreconciled == 1 else 's'}"
                    ),
                    "count": unreconciled,
                    "action_url": "/banking/reconciliation",
                }
            )
        if ar["total_overdue"] > ZERO:
            highlights.append(
                {
                    "kind": "receivables",
                    "text": f"{compact(ar['total_overdue'])} in overdue receivables",
                    "amount": str(ar["total_overdue"]),
                    "action_url": "/sales/invoices?status=OVERDUE",
                }
            )
        if ap["total_overdue"] > ZERO:
            highlights.append(
                {
                    "kind": "payables",
                    "text": f"{compact(ap['total_overdue'])} in overdue payables",
                    "amount": str(ap["total_overdue"]),
                    "action_url": "/purchases/bills?status=OVERDUE",
                }
            )
        if open_anomalies:
            highlights.append(
                {
                    "kind": "anomalies",
                    "text": f"{open_anomalies} open anomal{'y' if open_anomalies == 1 else 'ies'}",
                    "count": open_anomalies,
                    "action_url": "/exceptions?category=ANOMALY",
                }
            )
        if open_exceptions:
            highlights.append(
                {
                    "kind": "exceptions",
                    "text": f"{open_exceptions} item(s) awaiting your decision",
                    "count": open_exceptions,
                    "action_url": "/exceptions",
                }
            )

        return {
            "generated_at": date.today(),
            "cash": cash,
            "receivable": ar["total_outstanding"],
            "receivable_overdue": ar["total_overdue"],
            "payable": ap["total_outstanding"],
            "payable_overdue": ap["total_overdue"],
            "unreconciled_count": unreconciled,
            "open_exceptions": open_exceptions,
            "open_anomalies": open_anomalies,
            "close_progress": close.get("progress_percent", ZERO),
            "close_period": close.get("period_label", ""),
            "highlights": highlights,
            "summary": _briefing_text(highlights, close),
        }


def _briefing_text(highlights: list[dict], close: dict) -> str:
    if not highlights:
        base = "Everything looks clear — nothing needs your attention right now."
    elif len(highlights) == 1:
        base = f"I found {highlights[0]['text']}."
    else:
        visible = [item["text"] for item in highlights[:4]]
        base = f"I found {', '.join(visible[:-1])} and {visible[-1]}."

    if close.get("exists"):
        # The stored percentage carries six decimal places; nobody wants to read
        # "87.500000% complete".
        progress = float(close.get("progress_percent") or 0)
        base += f" The {close.get('period_label')} close is {progress:.0f}% complete."
    return base


def list_workflows() -> list[dict]:
    return [
        {
            "key": workflow.key,
            "name": workflow.name,
            "description": workflow.description,
            "steps": [
                {
                    "agent_kind": step.agent_kind,
                    "name": AGENT_DESCRIPTIONS.get(step.agent_kind, {}).get(
                        "name", step.agent_kind
                    ),
                }
                for step in workflow.steps
            ],
        }
        for workflow in WORKFLOWS.values()
    ]


def _stringify(value: Any) -> Any:
    from decimal import Decimal

    if isinstance(value, dict):
        return {key: _stringify(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_stringify(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    return value
