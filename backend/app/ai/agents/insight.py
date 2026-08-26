"""Analysis agents: anomaly, fraud, cash forecast, reporting and financial analyst.

Each one computes its numbers deterministically first. The model is used only
to phrase the explanation, and only over figures already derived from the
ledger — so an explanation can never contradict the books.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.ai.agents.base import AgentResult, BaseAgent
from app.enums import (
    AgentKind,
    AgentRunStatus,
    AnomalyKind,
    ExceptionCategory,
    ExceptionStatus,
    Severity,
)
from app.models import Anomaly
from app.money import ZERO, D, fmt_inr, money
from app.services import anomaly as anomaly_service
from app.services.forecast import cash_forecast, multi_horizon_forecast
from app.services.reports import profit_and_loss, ratio_analysis

FRAUD_KINDS = {
    AnomalyKind.DUPLICATE_PAYMENT,
    AnomalyKind.VENDOR_BANK_CHANGE,
    AnomalyKind.AFTER_HOURS_JOURNAL,
    AnomalyKind.ROUND_NUMBER,
    AnomalyKind.NEW_VENDOR,
    AnomalyKind.UNUSUAL_JOURNAL,
    AnomalyKind.DUPLICATE_BANK_POSTING,
}


class AnomalyAgent(BaseAgent):
    kind = AgentKind.ANOMALY
    display_name = "Anomaly Agent"
    role_description = (
        "You summarise anomalies that deterministic detectors have already found. "
        "You never invent findings — you only explain the ones supplied."
    )

    async def run_agent(self, **params: Any) -> AgentResult:
        lookback = int(params.get("lookback_days", 180))
        detection = anomaly_service.detect_all(
            self.db,
            self.company_id,
            lookback_days=lookback,
            agent_run_id=self.run.id if self.run else None,
        )

        open_anomalies = list(
            self.db.execute(
                select(Anomaly)
                .where(
                    Anomaly.company_id == self.company_id,
                    Anomaly.status == ExceptionStatus.OPEN,
                )
                .order_by(Anomaly.created_at.desc())
                .limit(50)
            ).scalars()
        )

        critical = [a for a in open_anomalies if a.severity in (Severity.HIGH, Severity.CRITICAL)]
        result = AgentResult(
            agent_kind=self.kind,
            items_processed=detection["findings"],
            needs_review_count=len(critical),
        )
        result.summary = (
            f"Scanned {lookback} days of activity: {detection['created']} new anomal"
            f"{'y' if detection['created'] == 1 else 'ies'} detected "
            f"({detection['findings']} total findings, {len(critical)} high severity). "
            f"{fmt_inr(detection['total_amount_flagged'])} of activity flagged for review."
        )
        result.metrics = {
            "new": detection["created"],
            "refreshed": detection["refreshed"],
            "open_total": len(open_anomalies),
            "critical": len(critical),
            "amount_flagged": str(detection["total_amount_flagged"]),
            "by_severity": {str(k): v for k, v in detection["by_severity"].items()},
        }
        result.findings = [
            {
                "id": a.id,
                "kind": a.kind,
                "severity": a.severity,
                "label": a.title,
                "detail": a.reason[:300],
                "amount": str(a.amount) if a.amount else None,
                "recommendation": a.recommendation[:200],
            }
            for a in open_anomalies[:20]
        ]
        result.status = AgentRunStatus.NEEDS_REVIEW if critical else AgentRunStatus.COMPLETED
        return result


class FraudMonitoringAgent(BaseAgent):
    kind = AgentKind.FRAUD
    display_name = "Fraud Monitoring Agent"
    role_description = (
        "You review control-failure and fraud signals. You describe what was found "
        "and what a human should verify. You never accuse anyone."
    )

    async def run_agent(self, **params: Any) -> AgentResult:
        anomaly_service.detect_all(
            self.db,
            self.company_id,
            lookback_days=int(params.get("lookback_days", 180)),
            agent_run_id=self.run.id if self.run else None,
        )

        signals = list(
            self.db.execute(
                select(Anomaly)
                .where(
                    Anomaly.company_id == self.company_id,
                    Anomaly.status == ExceptionStatus.OPEN,
                    Anomaly.kind.in_(list(FRAUD_KINDS)),
                )
                .order_by(Anomaly.severity.desc(), Anomaly.created_at.desc())
                .limit(50)
            ).scalars()
        )

        critical = [s for s in signals if s.severity == Severity.CRITICAL]
        total = money(sum((s.amount for s in signals if s.amount), ZERO))

        result = AgentResult(
            agent_kind=self.kind,
            items_processed=len(signals),
            needs_review_count=len(signals),
        )
        result.summary = (
            f"{len(signals)} control signal(s) are open, {len(critical)} critical, "
            f"covering {fmt_inr(total)} of activity."
            if signals
            else "No open fraud or control signals."
        )
        result.metrics = {
            "signals": len(signals),
            "critical": len(critical),
            "amount": str(total),
            "by_kind": {
                kind: sum(1 for s in signals if s.kind == kind)
                for kind in {s.kind for s in signals}
            },
        }
        result.findings = [
            {
                "id": s.id,
                "kind": s.kind,
                "severity": s.severity,
                "label": s.title,
                "detail": s.reason[:300],
                "verify": s.recommendation[:200],
                "amount": str(s.amount) if s.amount else None,
            }
            for s in signals[:20]
        ]
        result.status = AgentRunStatus.NEEDS_REVIEW if signals else AgentRunStatus.COMPLETED
        return result


class CashForecastAgent(BaseAgent):
    kind = AgentKind.CASH_FORECAST
    display_name = "Cash Forecast Agent"
    role_description = (
        "You explain a cash forecast that has already been computed. State the "
        "assumptions plainly and never present a projection as a certainty."
    )

    async def run_agent(self, **params: Any) -> AgentResult:
        horizon = int(params.get("horizon_days", 30))
        forecast = cash_forecast(self.db, self.company_id, horizon_days=horizon)

        result = AgentResult(agent_kind=self.kind, items_processed=1)

        # The deterministic sentence is computed first and handed to the model,
        # so a provider that cannot improve on it returns it unchanged rather
        # than reciting the raw figures back at the reader.
        base_summary = (
            f"Over the next {horizon} days cash is projected to move from "
            f"{fmt_inr(forecast['opening_cash'])} to {fmt_inr(forecast['closing_cash'])}, "
            f"with {fmt_inr(forecast['expected_inflow'])} expected in and "
            f"{fmt_inr(forecast['expected_outflow'])} out (range "
            f"{fmt_inr(forecast['confidence_low'])} to {fmt_inr(forecast['confidence_high'])})."
        )

        parsed, meta = await self.ask_json(
            task="summarise",
            instruction=(
                "Write a two-sentence plain-English summary of this cash forecast. "
                "Mention the closing position and the biggest driver. Do not invent "
                'numbers. Reply {"summary": "..."}.'
            ),
            context={
                "computed_summary": base_summary,
                "facts": {
                    "horizon_days": horizon,
                    "opening_cash": str(forecast["opening_cash"]),
                    "expected_inflow": str(forecast["expected_inflow"]),
                    "expected_outflow": str(forecast["expected_outflow"]),
                    "closing_cash": str(forecast["closing_cash"]),
                    "open_invoices": forecast["basis"]["open_invoices"],
                    "open_bills": forecast["basis"]["open_bills"],
                },
            },
        )
        narrative = (
            str(parsed["summary"])[:600] if parsed and parsed.get("summary") else None
        )
        result.used_llm = bool(meta.get("used_llm"))
        result.summary = narrative or base_summary

        for warning in forecast["warnings"]:
            self.raise_exception_item(
                category=ExceptionCategory.ACCOUNTING,
                severity=Severity.HIGH,
                title=f"Cash warning over the next {horizon} days",
                reason=warning,
                recommended_action=(
                    "Review the payment schedule and accelerate collections before the shortfall."
                ),
                dedupe_key=f"cash_warning:{horizon}:{date.today()}",
                amount=abs(forecast["closing_cash"]) if forecast["closing_cash"] < ZERO else None,
            )

        result.metrics = {
            "horizon_days": horizon,
            "opening_cash": str(forecast["opening_cash"]),
            "closing_cash": str(forecast["closing_cash"]),
            "expected_inflow": str(forecast["expected_inflow"]),
            "expected_outflow": str(forecast["expected_outflow"]),
            "assumptions": forecast["assumptions"],
            "warnings": forecast["warnings"],
            "computed_summary": base_summary,
        }
        result.findings = [
            {"label": "Assumption", "detail": assumption} for assumption in forecast["assumptions"]
        ] + [{"label": "Warning", "detail": warning} for warning in forecast["warnings"]]
        result.needs_review_count = len(forecast["warnings"])
        result.status = (
            AgentRunStatus.NEEDS_REVIEW if forecast["warnings"] else AgentRunStatus.COMPLETED
        )
        return result


class ReportingAgent(BaseAgent):
    kind = AgentKind.REPORTING
    display_name = "Reporting Agent"
    role_description = (
        "You explain period-on-period movements in the financial statements using "
        "the account-level drivers supplied. Every driver you mention must appear "
        "in the context — never invent a reason."
    )

    async def run_agent(self, **params: Any) -> AgentResult:
        end = _as_date(params.get("end_date"), date.today())
        start = _as_date(params.get("start_date"), end.replace(day=1))
        previous_end = start - timedelta(days=1)
        previous_start = _as_date(params.get("compare_start"), previous_end.replace(day=1))

        current = profit_and_loss(self.db, self.company_id, start_date=start, end_date=end)
        previous = profit_and_loss(
            self.db, self.company_id, start_date=previous_start, end_date=previous_end
        )

        drivers = _rank_drivers(current, previous)
        profit_change = money(current["net_profit"] - previous["net_profit"])
        profit_change_percent = (
            money(profit_change * 100 / abs(previous["net_profit"]))
            if previous["net_profit"] != ZERO
            else None
        )

        result = AgentResult(agent_kind=self.kind, items_processed=len(drivers))

        parsed, meta = await self.ask_json(
            task="explain_variance",
            instruction=(
                "Explain the change in net profit using ONLY the drivers supplied. "
                'Reply {"summary": "...", "drivers": [{"name": "...", "change": "...", '
                '"direction": "increase|decrease"}]}. Do not speculate about causes '
                "that are not visible in the data."
            ),
            context={
                "metric": "Net profit",
                "current": str(current["net_profit"]),
                "previous": str(previous["net_profit"]),
                "change": str(profit_change),
                "change_percent": str(profit_change_percent) if profit_change_percent is not None else None,
                "drivers": [
                    {
                        "account_id": driver["account_id"],
                        "name": driver["name"],
                        "change": str(driver["change"]),
                        "change_percent": str(driver["change_percent"])
                        if driver["change_percent"] is not None
                        else None,
                    }
                    for driver in drivers[:8]
                ],
            },
        )
        result.used_llm = bool(meta.get("used_llm"))

        direction = "increased" if profit_change > ZERO else "decreased" if profit_change < ZERO else "was unchanged"
        computed = (
            f"Net profit {direction} from {previous['net_profit']} to {current['net_profit']} "
            f"({profit_change}"
            + (f", {profit_change_percent}%" if profit_change_percent is not None else "")
            + ")."
        )
        result.summary = str(parsed.get("summary", computed))[:800] if parsed else computed

        result.metrics = {
            "current_period": f"{start} to {end}",
            "previous_period": f"{previous_start} to {previous_end}",
            "revenue": str(current["revenue"]["total"]),
            "revenue_previous": str(previous["revenue"]["total"]),
            "net_profit": str(current["net_profit"]),
            "net_profit_previous": str(previous["net_profit"]),
            "change": str(profit_change),
            "change_percent": str(profit_change_percent) if profit_change_percent is not None else None,
            "computed_summary": computed,
        }
        result.findings = [
            {
                "account_id": driver["account_id"],
                "label": driver["name"],
                "detail": (
                    f"{driver['name']} moved from {driver['previous']} to {driver['current']} "
                    f"({driver['change']}"
                    + (f", {driver['change_percent']}%" if driver["change_percent"] is not None else "")
                    + ")."
                ),
                "change": str(driver["change"]),
                "change_percent": str(driver["change_percent"])
                if driver["change_percent"] is not None
                else None,
                "direction": "increase" if driver["change"] > ZERO else "decrease",
            }
            for driver in drivers[:10]
        ]
        return result


class FinancialAnalystAgent(BaseAgent):
    kind = AgentKind.ANALYST
    display_name = "Financial Analyst Agent"
    role_description = (
        "You interpret financial ratios that have already been computed, saying what "
        "each implies for the business. Use only the supplied values."
    )

    async def run_agent(self, **params: Any) -> AgentResult:
        as_of = _as_date(params.get("as_of"), date.today())
        start = _as_date(params.get("start_date"), as_of.replace(month=1, day=1))

        ratios = ratio_analysis(self.db, self.company_id, as_of=as_of, start_date=start)
        forecast = multi_horizon_forecast(self.db, self.company_id)

        flat: dict[str, Any] = {}
        for group, metrics in ratios.items():
            if not isinstance(metrics, dict):
                continue
            for name, payload in metrics.items():
                if isinstance(payload, dict) and "value" in payload:
                    flat[name] = str(payload["value"]) if payload["value"] is not None else None

        observations = _ratio_observations(ratios)

        result = AgentResult(agent_kind=self.kind, items_processed=len(flat))
        parsed, meta = await self.ask_json(
            task="summarise",
            instruction=(
                "Summarise the company's financial health in two or three sentences using "
                'ONLY these ratios. Reply {"summary": "..."}.'
            ),
            context={"facts": flat, "observations": observations},
        )
        result.used_llm = bool(meta.get("used_llm"))

        computed = "; ".join(observations) if observations else "Not enough data for ratio analysis."
        result.summary = str(parsed.get("summary", computed))[:800] if parsed else computed
        result.metrics = {"ratios": flat, "computed_observations": observations}
        result.findings = [{"label": "Observation", "detail": text} for text in observations]
        return result


# ------------------------------------------------------------- helpers -----

def _as_date(value: Any, default: date) -> date:
    if isinstance(value, date):
        return value
    if not value:
        return default
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return default


def _rank_drivers(current: dict, previous: dict) -> list[dict]:
    """Account-level movements between two P&L reports, largest first."""

    def index(report: dict) -> dict[str, dict]:
        rows: dict[str, dict] = {}
        for section in ("revenue", "cogs", "operating_expenses", "other_income", "other_expenses"):
            for line in report[section]["lines"]:
                rows[line["account_id"]] = line
        return rows

    now, before = index(current), index(previous)
    drivers = []
    for account_id in set(now) | set(before):
        current_amount = money(now.get(account_id, {}).get("amount", ZERO))
        previous_amount = money(before.get(account_id, {}).get("amount", ZERO))
        change = money(current_amount - previous_amount)
        if change == ZERO:
            continue
        drivers.append(
            {
                "account_id": account_id,
                "name": now.get(account_id, before.get(account_id, {})).get("name", ""),
                "code": now.get(account_id, before.get(account_id, {})).get("code", ""),
                "current": current_amount,
                "previous": previous_amount,
                "change": change,
                "change_percent": (
                    money(change * 100 / abs(previous_amount)) if previous_amount != ZERO else None
                ),
            }
        )
    drivers.sort(key=lambda d: abs(d["change"]), reverse=True)
    return drivers


def _ratio_observations(ratios: dict) -> list[str]:
    """Deterministic interpretation, so the analyst works without a model."""
    observations: list[str] = []

    current_ratio = ratios["liquidity"]["current_ratio"]["value"]
    if current_ratio is not None:
        if current_ratio < D("1"):
            observations.append(
                f"Current ratio is {current_ratio} — current liabilities exceed current "
                f"assets, which is a short-term liquidity risk"
            )
        elif current_ratio < D("1.5"):
            observations.append(f"Current ratio is {current_ratio}, which is tight but workable")
        else:
            observations.append(f"Current ratio is {current_ratio}, indicating comfortable liquidity")

    debt_equity = ratios["leverage"]["debt_to_equity"]["value"]
    if debt_equity is not None:
        if debt_equity > D("2"):
            observations.append(f"Debt-to-equity is {debt_equity}, which is highly leveraged")
        else:
            observations.append(f"Debt-to-equity is {debt_equity}")

    net_margin = ratios["profitability"]["net_margin_percent"]["value"]
    if net_margin is not None:
        if net_margin < ZERO:
            observations.append(f"Net margin is {net_margin}% — the business is loss-making")
        else:
            observations.append(f"Net margin is {net_margin}%")

    dso = ratios["efficiency"]["days_sales_outstanding"]["value"]
    if dso is not None and dso > D("60"):
        observations.append(
            f"Days sales outstanding is {dso}, so cash is tied up in receivables far longer "
            f"than a typical 30-day term"
        )
    elif dso is not None:
        observations.append(f"Days sales outstanding is {dso}")

    turnover = ratios["efficiency"]["inventory_turnover"]["value"]
    if turnover is not None and turnover < D("2"):
        observations.append(
            f"Inventory turnover is only {turnover} times, suggesting slow-moving stock"
        )

    return observations
