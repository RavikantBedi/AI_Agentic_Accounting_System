"""Compliance agents: GST, tax (TDS/TCS), close, audit and payroll.

All five follow the same discipline: the rule engine decides, the model at most
suggests, and every suggestion is validated against configured rules before it
is shown to anyone.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from app.ai.agents.base import AgentResult, BaseAgent
from app.enums import (
    AgentKind,
    AgentRunStatus,
    DocumentStatus,
    ExceptionCategory,
    PayrollStatus,
    Severity,
)
from app.models import Bill, Employee, PayrollRun, TDSEntry, TDSSection, Vendor
from app.money import ZERO, D, money
from app.services import gst as gst_service
from app.services.audit import count_events, verify_chain
from app.services.close import close_status, run_checklist
from app.services.tax import validate_ai_tds_suggestion, validate_gstin


class GSTAgent(BaseAgent):
    kind = AgentKind.GST
    display_name = "GST Agent"
    role_description = (
        "You prepare GST return data from the ledger and report validation problems. "
        "You never file anything with the government."
    )
    uses_llm = False  # entirely deterministic

    async def run_agent(self, **params: Any) -> AgentResult:
        period = params.get("period") or gst_service.current_period()
        prepare = bool(params.get("prepare", True))

        summary = gst_service.gst_summary(self.db, self.company_id, period=period)
        result = AgentResult(agent_kind=self.kind, items_processed=summary["output"]["invoice_count"])

        issues: list[dict] = []
        if prepare:
            gstr1 = gst_service.prepare_gstr1(
                self.db, self.company_id, period=period, user_id=self.user_id
            )
            gstr3b = gst_service.prepare_gstr3b(
                self.db, self.company_id, period=period, user_id=self.user_id
            )
            issues = list(gstr1.validation_errors or [])

            for issue in issues[:15]:
                self.raise_exception_item(
                    category=ExceptionCategory.GST,
                    severity=Severity.MEDIUM,
                    title=f"GST data issue on {issue.get('invoice_number', 'an invoice')}",
                    reason=issue.get("message", ""),
                    recommended_action=(
                        "Correct the invoice before filing — the GST portal will reject it otherwise."
                    ),
                    dedupe_key=f"gst_issue:{period}:{issue.get('invoice_id')}:{issue.get('field')}",
                    entity_type="Invoice",
                    entity_id=issue.get("invoice_id"),
                )

            result.metrics["gstr1_id"] = gstr1.id
            result.metrics["gstr3b_id"] = gstr3b.id

        # Also verify the company's own GSTIN is well-formed.
        from app.models import Company

        company = self.db.get(Company, self.company_id)
        if company and company.gstin:
            valid, message = validate_gstin(company.gstin)
            if not valid:
                issues.append({"field": "company_gstin", "message": f"Company GSTIN: {message}"})

        net = summary["net_position"]
        result.summary = (
            f"GST for {period}: output {summary['output']['total']}, input "
            f"{summary['input']['total']}, "
            + (
                f"net payable {summary['net_payable']}."
                if net > ZERO
                else f"credit of {summary['credit_carried_forward']} carried forward."
            )
            + (f" {len(issues)} validation issue(s) need fixing before filing." if issues else "")
        )
        result.metrics.update(
            {
                "period": period,
                "output_tax": str(summary["output"]["total"]),
                "input_tax": str(summary["input"]["total"]),
                "net_payable": str(summary["net_payable"]),
                "invoice_count": summary["output"]["invoice_count"],
                "validation_issues": len(issues),
            }
        )
        result.findings = [
            {"label": issue.get("field", "issue"), "detail": issue.get("message", "")}
            for issue in issues[:20]
        ]
        result.needs_review_count = len(issues)
        result.status = AgentRunStatus.NEEDS_REVIEW if issues else AgentRunStatus.COMPLETED
        return result


class TaxAssistantAgent(BaseAgent):
    kind = AgentKind.TAX
    display_name = "Tax Assistant"
    role_description = (
        "You suggest which TDS section applies to a payment, choosing ONLY from the "
        "sections configured in the context. A deterministic rule engine then "
        "validates your suggestion — if it is not configured, it is rejected."
    )

    async def run_agent(self, **params: Any) -> AgentResult:
        limit = int(params.get("limit", 30))

        # Bills from TDS-applicable vendors where no section was applied.
        bills = list(
            self.db.execute(
                select(Bill)
                .join(Vendor, Vendor.id == Bill.vendor_id)
                .where(
                    Bill.company_id == self.company_id,
                    Bill.status != DocumentStatus.CANCELLED,
                    Bill.tds_amount == ZERO,
                    Vendor.tds_applicable.is_(True),
                )
                .order_by(Bill.doc_date.desc())
                .limit(limit)
            ).scalars()
        )

        sections = [
            {"section": row.section, "name": row.name, "rate": str(row.rate_with_pan)}
            for row in self.db.execute(
                select(TDSSection).where(
                    TDSSection.company_id == self.company_id, TDSSection.is_active.is_(True)
                )
            ).scalars()
        ]

        result = AgentResult(agent_kind=self.kind, items_processed=len(bills))
        if not bills:
            result.summary = "No bills are missing a TDS section."
            return result
        if not sections:
            result.summary = (
                "No TDS sections are configured, so no suggestions can be validated. "
                "Configure them under Settings > Tax."
            )
            result.status = AgentRunStatus.NEEDS_REVIEW
            return result

        suggested = rejected = 0
        findings: list[dict] = []

        for bill in bills:
            vendor = self.db.get(Vendor, bill.vendor_id)
            description = " ".join(
                filter(None, [line.description for line in bill.lines])
            )[:400]

            parsed, meta = await self.ask_json(
                task="tds_section",
                instruction=(
                    "Suggest the TDS section that applies to this payment, choosing only "
                    'from available_sections. Reply {"section": "...", "confidence": 0-1, '
                    '"reasoning": "..."} or {"insufficient_evidence": true, "reasoning": "..."}.'
                ),
                context={
                    "description": f"{vendor.name if vendor else ''} {description}",
                    "amount": str(bill.taxable_value),
                    "available_sections": sections,
                },
                untrusted_text=description,
                untrusted_label="bill line descriptions",
            )
            result.used_llm = result.used_llm or bool(meta.get("used_llm"))

            if parsed is None or parsed.get("insufficient_evidence"):
                continue

            candidate = str(parsed.get("section", ""))
            # Deterministic gate: the section must actually be configured.
            valid, message = validate_ai_tds_suggestion(
                self.db, self.company_id, candidate, on_date=bill.doc_date
            )
            if not valid:
                rejected += 1
                findings.append(
                    {
                        "label": f"Rejected suggestion for bill {bill.number}",
                        "detail": message,
                        "severity": "info",
                    }
                )
                continue

            suggested += 1
            self.raise_exception_item(
                category=ExceptionCategory.TAX,
                severity=Severity.MEDIUM,
                title=f"TDS may apply to bill {bill.number} ({candidate})",
                reason=(
                    f"{vendor.name if vendor else 'This vendor'} is marked TDS-applicable but "
                    f"bill {bill.number} for {bill.taxable_value} has no TDS deducted. "
                    f"Suggested section {candidate}: {parsed.get('reasoning', '')}"
                ),
                recommended_action=(
                    f"Confirm section {candidate} applies, then re-issue the bill with TDS "
                    f"deducted. The rate is taken from the configured rule, not from this suggestion."
                ),
                dedupe_key=f"tds_missing:{bill.id}",
                amount=bill.taxable_value,
                entity_type="Bill",
                entity_id=bill.id,
            )
            findings.append(
                {
                    "label": f"Bill {bill.number}",
                    "detail": f"Suggested TDS section {candidate}.",
                    "bill_id": bill.id,
                }
            )

        result.summary = (
            f"Reviewed {len(bills)} bill(s) from TDS-applicable vendors: {suggested} section "
            f"suggestion(s) passed rule validation, {rejected} were rejected because the "
            f"suggested section is not configured."
        )
        result.metrics = {
            "bills_reviewed": len(bills),
            "suggested": suggested,
            "rejected_by_rules": rejected,
            "configured_sections": len(sections),
        }
        result.findings = findings[:20]
        result.needs_review_count = suggested
        result.status = AgentRunStatus.NEEDS_REVIEW if suggested else AgentRunStatus.COMPLETED
        return result


class CloseAgent(BaseAgent):
    kind = AgentKind.CLOSE
    display_name = "Close Agent"
    role_description = (
        "You summarise the month-end close checklist. Report only the results of "
        "checks that were actually run."
    )

    async def run_agent(self, **params: Any) -> AgentResult:
        period = params.get("period")
        checklist = run_checklist(
            self.db,
            self.company_id,
            period=period,
            user_id=self.user_id,
            agent_run_id=self.run.id if self.run else None,
        )
        status = close_status(self.db, self.company_id, period=period)

        blocking = status.get("blocking_issues", [])
        result = AgentResult(
            agent_kind=self.kind,
            items_processed=status.get("total_tasks", 0),
            needs_review_count=len(blocking),
        )

        progress = float(status.get("progress_percent") or 0)
        computed = (
            f"{status.get('period_label')} close is {progress:.0f}% complete "
            f"({status.get('completed_tasks')}/{status.get('total_tasks')} checks passing). "
            + (
                f"{len(blocking)} blocking issue(s) remain: "
                + "; ".join(item["name"] for item in blocking[:4])
                + "."
                if blocking
                else "No blocking issues — the period is ready to close."
            )
        )

        parsed, meta = await self.ask_json(
            task="close_summary",
            instruction=(
                "Summarise the close status in two sentences: progress, and what is "
                'blocking it. Reply {"summary": "..."}.'
            ),
            context={
                "computed_summary": computed,
                "findings": [
                    {
                        "name": item["name"],
                        "detail": item["detail"],
                        "blocking": item.get("blocking", True),
                    }
                    for item in (checklist.ai_findings or [])
                ],
                "facts": {
                    "period": status.get("period_label"),
                    "progress_percent": f"{progress:.0f}%",
                    "completed": status.get("completed_tasks"),
                    "total": status.get("total_tasks"),
                    "blocked": status.get("blocked_tasks"),
                },
            },
        )
        result.used_llm = bool(meta.get("used_llm"))
        result.summary = str((parsed or {}).get("summary") or computed)[:800]

        checklist.ai_summary = result.summary
        self.db.flush()

        for item in blocking:
            self.raise_exception_item(
                category=ExceptionCategory.CLOSE,
                severity=Severity.HIGH,
                title=f"Close blocked: {item['name']}",
                reason=item["detail"],
                recommended_action="Resolve this before closing the period.",
                dedupe_key=f"close_block:{status.get('period')}:{item['key']}",
                action_url="/close",
            )

        result.metrics = {
            "period": status.get("period"),
            "progress_percent": str(status.get("progress_percent")),
            "completed": status.get("completed_tasks"),
            "total": status.get("total_tasks"),
            "blocked": status.get("blocked_tasks"),
            "can_close": status.get("can_close"),
            "computed_summary": computed,
        }
        result.findings = [
            {
                "label": task["name"],
                "detail": task["description"],
                "status": task["status"],
                "blocking": task["is_blocking"],
                "amount_at_risk": str(task["amount_at_risk"]),
            }
            for task in status.get("tasks", [])
            if task["status"] != "COMPLETED"
        ]
        result.status = AgentRunStatus.NEEDS_REVIEW if blocking else AgentRunStatus.COMPLETED
        return result


class AuditAgent(BaseAgent):
    kind = AgentKind.AUDIT
    display_name = "Audit Agent"
    role_description = "You verify the integrity of the audit trail and report anomalies in it."
    uses_llm = False

    async def run_agent(self, **params: Any) -> AgentResult:
        chain = verify_chain(self.db, self.company_id)
        total_events = count_events(self.db, self.company_id)

        from app.accounting.ledger import verify_invariants

        invariants = verify_invariants(self.db, self.company_id)
        failed = [check for check in invariants["checks"] if not check["passed"]]

        result = AgentResult(agent_kind=self.kind, items_processed=total_events)

        if not chain["valid"]:
            self.raise_exception_item(
                category=ExceptionCategory.ACCOUNTING,
                severity=Severity.CRITICAL,
                title="Audit trail integrity check failed",
                reason=chain["message"],
                recommended_action=(
                    "Investigate immediately — the audit log has been altered outside the "
                    "application."
                ),
                dedupe_key=f"audit_chain_broken:{chain.get('broken_at_sequence')}",
            )

        for check in failed:
            self.raise_exception_item(
                category=ExceptionCategory.ACCOUNTING,
                severity=Severity.CRITICAL,
                title=f"Accounting invariant failed: {check['name']}",
                reason=check["detail"],
                recommended_action="Investigate before relying on any financial report.",
                dedupe_key=f"invariant_failed:{check['name']}",
            )

        result.summary = (
            f"Audit trail: {chain['message']} "
            f"Accounting invariants: {len(invariants['checks']) - len(failed)}"
            f"/{len(invariants['checks'])} passing."
        )
        result.metrics = {
            "audit_events": total_events,
            "chain_valid": chain["valid"],
            "invariants_passed": len(invariants["checks"]) - len(failed),
            "invariants_total": len(invariants["checks"]),
        }
        result.findings = [
            {"label": check["name"], "detail": check["detail"], "passed": check["passed"]}
            for check in invariants["checks"]
        ]
        result.needs_review_count = len(failed) + (0 if chain["valid"] else 1)
        result.status = (
            AgentRunStatus.NEEDS_REVIEW
            if failed or not chain["valid"]
            else AgentRunStatus.COMPLETED
        )
        return result


class PayrollAgent(BaseAgent):
    kind = AgentKind.PAYROLL
    display_name = "Payroll Agent"
    role_description = "You review payroll runs for completeness and unusual movements."
    uses_llm = False

    async def run_agent(self, **params: Any) -> AgentResult:
        period = params.get("period") or _current_period()

        run = self.db.execute(
            select(PayrollRun).where(
                PayrollRun.company_id == self.company_id, PayrollRun.period == period
            )
        ).scalar_one_or_none()

        active_employees = int(
            self.db.execute(
                select(func.count(Employee.id)).where(
                    Employee.company_id == self.company_id,
                    Employee.status == "ACTIVE",
                    Employee.is_active.is_(True),
                )
            ).scalar_one()
        )

        result = AgentResult(agent_kind=self.kind, items_processed=active_employees)
        findings: list[dict] = []

        if run is None:
            result.summary = (
                f"No payroll run exists for {period}. {active_employees} active employee(s) "
                f"are on record."
                if active_employees
                else "No payroll run and no active employees for this period."
            )
            if active_employees:
                findings.append(
                    {
                        "label": "Payroll not run",
                        "detail": f"{active_employees} active employee(s) have no payslip for {period}.",
                    }
                )
                result.needs_review_count = 1
                result.status = AgentRunStatus.NEEDS_REVIEW
            result.findings = findings
            result.metrics = {"period": period, "active_employees": active_employees}
            return result

        missing = active_employees - run.employee_count
        if missing > 0:
            findings.append(
                {
                    "label": "Employees missing from payroll",
                    "detail": (
                        f"{missing} active employee(s) have no payslip in the {period} run "
                        f"({run.employee_count} of {active_employees} covered)."
                    ),
                }
            )
            self.raise_exception_item(
                category=ExceptionCategory.PAYROLL,
                severity=Severity.HIGH,
                title=f"{missing} employee(s) missing from the {period} payroll run",
                reason=(
                    f"The payroll run covers {run.employee_count} employees but "
                    f"{active_employees} are active."
                ),
                recommended_action="Add the missing employees or mark them inactive.",
                dedupe_key=f"payroll_missing:{period}",
                entity_type="PayrollRun",
                entity_id=run.id,
            )

        # Compare against the previous month for unusual movement.
        previous_period = _previous_period(period)
        previous = self.db.execute(
            select(PayrollRun).where(
                PayrollRun.company_id == self.company_id, PayrollRun.period == previous_period
            )
        ).scalar_one_or_none()

        if previous and previous.total_net > ZERO:
            change = money(run.total_net - previous.total_net)
            percent = money(change * 100 / previous.total_net)
            if abs(percent) >= D("15"):
                findings.append(
                    {
                        "label": "Payroll cost moved sharply",
                        "detail": (
                            f"Net pay is {run.total_net} against {previous.total_net} in "
                            f"{previous_period} — a change of {change} ({percent}%)."
                        ),
                    }
                )

        result.summary = (
            f"Payroll for {period} is {run.status.lower()}: {run.employee_count} employee(s), "
            f"gross {run.total_gross}, TDS {run.total_tds}, net {run.total_net}."
            + (f" {len(findings)} item(s) need attention." if findings else "")
        )
        result.metrics = {
            "period": period,
            "status": run.status,
            "employees": run.employee_count,
            "active_employees": active_employees,
            "gross": str(run.total_gross),
            "net": str(run.total_net),
            "tds": str(run.total_tds),
        }
        result.findings = findings
        result.needs_review_count = len(findings)
        result.status = AgentRunStatus.NEEDS_REVIEW if findings else AgentRunStatus.COMPLETED
        return result


def _current_period() -> str:
    today = date.today()
    return f"{today.year}-{today.month:02d}"


def _previous_period(period: str) -> str:
    year, month = (int(part) for part in period.split("-"))
    if month == 1:
        return f"{year - 1}-12"
    return f"{year}-{month - 1:02d}"
