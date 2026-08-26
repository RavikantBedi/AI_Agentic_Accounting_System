"""Agent registry.

Seventeen specialist agents plus the copilot. Each is addressable by its
``AgentKind`` so the orchestrator and the API can look them up by name.
"""
from __future__ import annotations

from app.ai.agents.base import AgentResult, BaseAgent
from app.ai.agents.bookkeeping import BookkeepingAgent, ExpenseAgent
from app.ai.agents.compliance import (
    AuditAgent,
    CloseAgent,
    GSTAgent,
    PayrollAgent,
    TaxAssistantAgent,
)
from app.ai.agents.copilot import SUGGESTED_PROMPTS, CopilotAgent
from app.ai.agents.documents import DocumentAgent, InventoryAgent
from app.ai.agents.insight import (
    AnomalyAgent,
    CashForecastAgent,
    FinancialAnalystAgent,
    FraudMonitoringAgent,
    ReportingAgent,
)
from app.ai.agents.payables import APAgent
from app.ai.agents.receivables import ARAgent
from app.ai.agents.reconciliation import ReconciliationAgent
from app.enums import AgentKind

AGENT_REGISTRY: dict[str, type[BaseAgent]] = {
    AgentKind.BOOKKEEPING.value: BookkeepingAgent,
    AgentKind.RECONCILIATION.value: ReconciliationAgent,
    AgentKind.AP.value: APAgent,
    AgentKind.AR.value: ARAgent,
    AgentKind.DOCUMENT.value: DocumentAgent,
    AgentKind.INVENTORY.value: InventoryAgent,
    AgentKind.GST.value: GSTAgent,
    AgentKind.TAX.value: TaxAssistantAgent,
    AgentKind.ANOMALY.value: AnomalyAgent,
    AgentKind.FRAUD.value: FraudMonitoringAgent,
    AgentKind.CASH_FORECAST.value: CashForecastAgent,
    AgentKind.REPORTING.value: ReportingAgent,
    AgentKind.CLOSE.value: CloseAgent,
    AgentKind.AUDIT.value: AuditAgent,
    AgentKind.PAYROLL.value: PayrollAgent,
    AgentKind.EXPENSE.value: ExpenseAgent,
    AgentKind.ANALYST.value: FinancialAnalystAgent,
    AgentKind.COPILOT.value: CopilotAgent,
}


AGENT_DESCRIPTIONS: dict[str, dict[str, str]] = {
    AgentKind.BOOKKEEPING.value: {
        "name": "Bookkeeping Agent",
        "description": "Categorises bank transactions using learned rules, history, then the model.",
        "produces": "Categorisation proposals",
    },
    AgentKind.RECONCILIATION.value: {
        "name": "Reconciliation Agent",
        "description": "Matches bank lines to ledger entries across eight scoring strategies.",
        "produces": "Match proposals with evidence",
    },
    AgentKind.AP.value: {
        "name": "Accounts Payable Agent",
        "description": "Prepares the payment run, detects duplicates, ranks urgency. Never pays.",
        "produces": "Prioritised payment schedule",
    },
    AgentKind.AR.value: {
        "name": "Accounts Receivable Agent",
        "description": "Prioritises collections from measured payment behaviour and drafts reminders.",
        "produces": "Collection plan and reminder drafts",
    },
    AgentKind.DOCUMENT.value: {
        "name": "Document Processing Agent",
        "description": "Extracts invoice fields from uploads and proposes bills for approval.",
        "produces": "Bill proposals",
    },
    AgentKind.INVENTORY.value: {
        "name": "Inventory Agent",
        "description": "Detects low, dead and fast-moving stock and recommends reorders.",
        "produces": "Inventory recommendations",
    },
    AgentKind.GST.value: {
        "name": "GST Agent",
        "description": "Prepares GSTR-1 and GSTR-3B data and reports validation problems.",
        "produces": "Prepared return data",
    },
    AgentKind.TAX.value: {
        "name": "Tax Assistant",
        "description": "Suggests TDS sections; the rule engine validates every suggestion.",
        "produces": "Validated TDS suggestions",
    },
    AgentKind.ANOMALY.value: {
        "name": "Anomaly Agent",
        "description": "Runs twelve deterministic detectors over transactions and balances.",
        "produces": "Anomalies with evidence",
    },
    AgentKind.FRAUD.value: {
        "name": "Fraud Monitoring Agent",
        "description": "Watches duplicate payments, bank-detail changes and after-hours journals.",
        "produces": "Control signals",
    },
    AgentKind.CASH_FORECAST.value: {
        "name": "Cash Forecast Agent",
        "description": "Projects cash from committed receivables, payables and recurring items.",
        "produces": "Forecast with stated assumptions",
    },
    AgentKind.REPORTING.value: {
        "name": "Reporting Agent",
        "description": "Explains period-on-period movements from account-level drivers.",
        "produces": "Variance explanations",
    },
    AgentKind.CLOSE.value: {
        "name": "Close Agent",
        "description": "Runs sixteen month-end checks and reports what is blocking the close.",
        "produces": "Close checklist status",
    },
    AgentKind.AUDIT.value: {
        "name": "Audit Agent",
        "description": "Verifies the audit hash chain and every accounting invariant.",
        "produces": "Integrity report",
    },
    AgentKind.PAYROLL.value: {
        "name": "Payroll Agent",
        "description": "Checks payroll completeness and unusual cost movements.",
        "produces": "Payroll findings",
    },
    AgentKind.EXPENSE.value: {
        "name": "Expense Agent",
        "description": "Categorises expense claims and flags missing receipts.",
        "produces": "Expense categorisations",
    },
    AgentKind.ANALYST.value: {
        "name": "Financial Analyst Agent",
        "description": "Interprets liquidity, leverage, profitability and efficiency ratios.",
        "produces": "Financial health assessment",
    },
    AgentKind.COPILOT.value: {
        "name": "AI Finance Copilot",
        "description": "Answers questions by routing to typed query tools — never raw SQL.",
        "produces": "Grounded answers with evidence",
    },
}


def get_agent_class(kind: str) -> type[BaseAgent]:
    from app.errors import ValidationError

    agent = AGENT_REGISTRY.get(str(kind).upper())
    if agent is None:
        raise ValidationError(
            f"'{kind}' is not a known agent.",
            details={"available": sorted(AGENT_REGISTRY)},
            code="unknown_agent",
        )
    return agent


__all__ = [
    "AGENT_REGISTRY",
    "AGENT_DESCRIPTIONS",
    "SUGGESTED_PROMPTS",
    "AgentResult",
    "BaseAgent",
    "get_agent_class",
    "BookkeepingAgent",
    "ReconciliationAgent",
    "APAgent",
    "ARAgent",
    "DocumentAgent",
    "InventoryAgent",
    "GSTAgent",
    "TaxAssistantAgent",
    "AnomalyAgent",
    "FraudMonitoringAgent",
    "CashForecastAgent",
    "ReportingAgent",
    "CloseAgent",
    "AuditAgent",
    "PayrollAgent",
    "ExpenseAgent",
    "FinancialAnalystAgent",
    "CopilotAgent",
]
