"""Policy and approval engine.

Business policy is *data*. Thresholds, AI autonomy limits and matching
tolerances live in the ``policies`` / ``approval_rules`` tables and are edited
in Settings — never compiled into the code.

The engine answers one question for every proposed action:

    "Can this be applied automatically, or who must approve it?"

The answer is deterministic, explainable, and recorded on the proposal.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import ActorType, AuditAction, Permission, ProposalType, UserRole
from app.errors import PolicyViolation
from app.models import ApprovalRule, Policy
from app.money import ZERO, D, money, rate as to_rate

# ---------------------------------------------------------------------------
# Default policy values, written into the database on company creation so an
# administrator can change any of them without a deploy.
# ---------------------------------------------------------------------------

DEFAULT_POLICIES: dict[str, dict[str, Any]] = {
    "ai.autonomy": {
        "description": "How much an AI agent may do without a human.",
        "value": {
            # AI may never post to the ledger directly, at any confidence.
            "allow_direct_posting": False,
            # Auto-apply is limited to reversible, non-ledger actions.
            "auto_apply_categorisation": True,
            "auto_apply_reconciliation_match": True,
            "auto_apply_journal": False,
            "min_confidence_auto_apply": 0.95,
            "max_amount_auto_apply": 10000,
            "never_auto_release_payments": True,
        },
    },
    "approval.thresholds": {
        "description": "Amount bands that decide who must approve a document.",
        "value": {
            "currency": "INR",
            "bands": [
                {"max_amount": 10000, "required_role": "ACCOUNTANT", "auto_approve_allowed": True},
                {"max_amount": 100000, "required_role": "ACCOUNTANT", "auto_approve_allowed": False},
                {"max_amount": None, "required_role": "FINANCE_MANAGER", "auto_approve_allowed": False},
            ],
        },
    },
    "reconciliation.matching": {
        "description": "Tolerances used by the bank reconciliation engine.",
        "value": {
            "date_tolerance_days": 5,
            "amount_tolerance": 1.00,
            "amount_tolerance_percent": 0.5,
            "auto_confirm_min_confidence": 0.95,
            "review_min_confidence": 0.60,
            "fuzzy_name_threshold": 82,
        },
    },
    "anomaly.detection": {
        "description": "Sensitivity of the anomaly and fraud monitors.",
        "value": {
            "large_transaction_amount": 500000,
            "large_transaction_sigma": 3.0,
            "expense_spike_percent": 40,
            "round_number_min_amount": 50000,
            "duplicate_window_days": 90,
            "new_vendor_days": 30,
            "after_hours_start": 21,
            "after_hours_end": 6,
            "flag_weekend_transactions": True,
        },
    },
    "ar.collections": {
        "description": "Receivables follow-up policy.",
        "value": {
            "reminder_days": [-3, 1, 7, 15, 30],
            "escalate_after_days": 45,
            "high_risk_days": 60,
            "min_amount_to_chase": 1000,
        },
    },
    "ap.payments": {
        "description": "Payables scheduling policy.",
        "value": {
            "pay_early_discount_threshold_percent": 2,
            "warn_days_before_due": 5,
            "msme_payment_days": 45,
            "block_payment_on_duplicate": True,
            "block_payment_on_bank_change_days": 7,
        },
    },
    "close.requirements": {
        "description": "What must be true before a period may be closed.",
        "value": {
            "require_bank_reconciliation": True,
            "require_zero_suspense": True,
            "require_depreciation_posted": True,
            "require_no_draft_entries": True,
            "max_unreconciled_items": 0,
        },
    },
    "inventory.alerts": {
        "description": "Stock monitoring thresholds.",
        "value": {
            "dead_stock_days": 90,
            "slow_moving_days": 45,
            "fast_moving_top_percent": 20,
            "negative_stock_allowed": False,
        },
    },
}


DEFAULT_APPROVAL_RULES = [
    # (name, entity_type, min, max, role, auto_approve, min_confidence, priority)
    ("Journal — small", "JOURNAL", 0, 10000, UserRole.ACCOUNTANT, False, 0.90, 10),
    ("Journal — medium", "JOURNAL", 10000, 100000, UserRole.ACCOUNTANT, False, 0.95, 20),
    ("Journal — large", "JOURNAL", 100000, None, UserRole.FINANCE_MANAGER, False, 0.98, 30),
    ("Bill — small", "BILL", 0, 10000, UserRole.ACCOUNTANT, False, 0.90, 10),
    ("Bill — medium", "BILL", 10000, 100000, UserRole.ACCOUNTANT, False, 0.95, 20),
    ("Bill — large", "BILL", 100000, None, UserRole.FINANCE_MANAGER, False, 0.98, 30),
    ("Payment — any", "PAYMENT", 0, 100000, UserRole.FINANCE_MANAGER, False, 1.00, 10),
    ("Payment — large", "PAYMENT", 100000, None, UserRole.OWNER, False, 1.00, 20),
    ("Categorisation", "CATEGORISATION", 0, None, UserRole.ACCOUNTANT, True, 0.95, 10),
    ("Reconciliation match", "RECONCILIATION_MATCH", 0, None, UserRole.ACCOUNTANT, True, 0.95, 10),
]


# Role -> permissions. Deliberately explicit rather than hierarchical, so
# auditing "who can do what" needs no mental evaluation.
ROLE_PERMISSIONS: dict[str, set[str]] = {
    UserRole.OWNER: {p.value for p in Permission},
    UserRole.ADMIN: {p.value for p in Permission} - {Permission.CHANGE_ACCOUNTING_POLICY.value},
    UserRole.ACCOUNTANT: {
        Permission.READ_ACCOUNTS.value,
        Permission.READ_TRANSACTIONS.value,
        Permission.READ_INVOICES.value,
        Permission.READ_BILLS.value,
        Permission.READ_INVENTORY.value,
        Permission.READ_REPORTS.value,
        Permission.READ_TAX.value,
        Permission.READ_AUDIT.value,
        Permission.PROPOSE_JOURNAL.value,
        Permission.PROPOSE_RECONCILIATION.value,
        Permission.PROPOSE_INVOICE.value,
        Permission.PROPOSE_BILL.value,
        Permission.PROPOSE_ADJUSTMENT.value,
        Permission.POST_JOURNAL.value,
        Permission.APPROVE_PROPOSAL.value,
        Permission.MANAGE_SALES.value,
        Permission.MANAGE_PURCHASES.value,
        Permission.MANAGE_TAX.value,
    },
    UserRole.FINANCE_MANAGER: {
        Permission.READ_ACCOUNTS.value,
        Permission.READ_TRANSACTIONS.value,
        Permission.READ_INVOICES.value,
        Permission.READ_BILLS.value,
        Permission.READ_INVENTORY.value,
        Permission.READ_REPORTS.value,
        Permission.READ_TAX.value,
        Permission.READ_PAYROLL.value,
        Permission.READ_AUDIT.value,
        Permission.PROPOSE_JOURNAL.value,
        Permission.PROPOSE_RECONCILIATION.value,
        Permission.PROPOSE_ADJUSTMENT.value,
        Permission.POST_JOURNAL.value,
        Permission.APPROVE_PROPOSAL.value,
        Permission.RELEASE_PAYMENT.value,
        Permission.CLOSE_PERIOD.value,
        Permission.MANAGE_SALES.value,
        Permission.MANAGE_PURCHASES.value,
        Permission.MANAGE_PAYROLL.value,
        Permission.MANAGE_TAX.value,
    },
    UserRole.SALES_MANAGER: {
        Permission.READ_INVOICES.value,
        Permission.READ_INVENTORY.value,
        Permission.READ_REPORTS.value,
        Permission.READ_TRANSACTIONS.value,
        Permission.PROPOSE_INVOICE.value,
        Permission.MANAGE_SALES.value,
    },
    UserRole.PURCHASE_MANAGER: {
        Permission.READ_BILLS.value,
        Permission.READ_INVENTORY.value,
        Permission.READ_REPORTS.value,
        Permission.READ_TRANSACTIONS.value,
        Permission.PROPOSE_BILL.value,
        Permission.MANAGE_PURCHASES.value,
        Permission.MANAGE_INVENTORY.value,
    },
    UserRole.AUDITOR: {
        Permission.READ_ACCOUNTS.value,
        Permission.READ_TRANSACTIONS.value,
        Permission.READ_INVOICES.value,
        Permission.READ_BILLS.value,
        Permission.READ_INVENTORY.value,
        Permission.READ_REPORTS.value,
        Permission.READ_TAX.value,
        Permission.READ_PAYROLL.value,
        Permission.READ_AUDIT.value,
    },
    UserRole.VIEWER: {
        Permission.READ_REPORTS.value,
        Permission.READ_INVOICES.value,
        Permission.READ_TRANSACTIONS.value,
    },
}

# Permissions an AI agent may hold. Execute-class permissions are absent by
# construction: an agent physically cannot be granted them.
AI_ALLOWED_PERMISSIONS: set[str] = {
    Permission.READ_ACCOUNTS.value,
    Permission.READ_TRANSACTIONS.value,
    Permission.READ_INVOICES.value,
    Permission.READ_BILLS.value,
    Permission.READ_INVENTORY.value,
    Permission.READ_REPORTS.value,
    Permission.READ_TAX.value,
    Permission.PROPOSE_JOURNAL.value,
    Permission.PROPOSE_RECONCILIATION.value,
    Permission.PROPOSE_INVOICE.value,
    Permission.PROPOSE_BILL.value,
    Permission.PROPOSE_ADJUSTMENT.value,
}

FORBIDDEN_FOR_AI: set[str] = {
    Permission.POST_JOURNAL.value,
    Permission.APPROVE_PROPOSAL.value,
    Permission.RELEASE_PAYMENT.value,
    Permission.CLOSE_PERIOD.value,
    Permission.CHANGE_ACCOUNTING_POLICY.value,
    Permission.MANAGE_USERS.value,
    Permission.MANAGE_SETTINGS.value,
}


def role_permissions(role: str) -> set[str]:
    return set(ROLE_PERMISSIONS.get(role, ROLE_PERMISSIONS[UserRole.VIEWER]))


def role_has_permission(role: str, permission: str) -> bool:
    return permission in role_permissions(role)


def role_at_least(role: str, required: str) -> bool:
    """Seniority comparison used by approval routing."""
    order = [
        UserRole.VIEWER,
        UserRole.AUDITOR,
        UserRole.SALES_MANAGER,
        UserRole.PURCHASE_MANAGER,
        UserRole.ACCOUNTANT,
        UserRole.FINANCE_MANAGER,
        UserRole.ADMIN,
        UserRole.OWNER,
    ]
    try:
        return order.index(role) >= order.index(required)
    except ValueError:
        return False


def assert_ai_permission(permission: str) -> None:
    """Hard stop: an AI agent requesting an execute-class permission."""
    if permission in FORBIDDEN_FOR_AI or permission not in AI_ALLOWED_PERMISSIONS:
        raise PolicyViolation(
            f"AI agents cannot hold the '{permission}' permission. "
            "Agents propose; humans execute.",
            code="ai_permission_forbidden",
        )


# ------------------------------------------------------------- policy I/O --

def get_policy(db: Session, company_id: str, key: str) -> dict[str, Any]:
    """Read the active policy value, falling back to the shipped default."""
    policy = (
        db.execute(
            select(Policy)
            .where(Policy.company_id == company_id, Policy.key == key, Policy.is_active.is_(True))
            .order_by(Policy.version.desc())
        )
        .scalars()
        .first()
    )
    if policy is not None:
        return dict(policy.value or {})
    return dict(DEFAULT_POLICIES.get(key, {}).get("value", {}))


def set_policy(
    db: Session,
    company_id: str,
    key: str,
    value: dict[str, Any],
    *,
    user_id: str | None,
    description: str = "",
) -> Policy:
    """Write a *new version* of a policy. Old versions are retained and audited."""
    from app.services.audit import record_event

    current = (
        db.execute(
            select(Policy)
            .where(Policy.company_id == company_id, Policy.key == key)
            .order_by(Policy.version.desc())
        )
        .scalars()
        .first()
    )
    before = dict(current.value) if current else None
    if current:
        current.is_active = False

    policy = Policy(
        company_id=company_id,
        key=key,
        version=(current.version + 1) if current else 1,
        value=value,
        description=description or (current.description if current else ""),
        is_active=True,
        changed_by_user_id=user_id,
    )
    db.add(policy)
    db.flush()

    record_event(
        db,
        company_id=company_id,
        action=AuditAction.POLICY_CHANGE,
        entity_type="Policy",
        entity_id=policy.id,
        entity_label=key,
        summary=f"Policy '{key}' updated to version {policy.version}",
        actor_type=ActorType.USER if user_id else ActorType.SYSTEM,
        actor_id=user_id,
        before=before,
        after=value,
    )
    return policy


def seed_policies(db: Session, company_id: str) -> int:
    """Write the default policies and approval rules for a new company."""
    created = 0
    for key, spec in DEFAULT_POLICIES.items():
        exists = db.execute(
            select(Policy).where(Policy.company_id == company_id, Policy.key == key)
        ).scalar_one_or_none()
        if exists:
            continue
        db.add(
            Policy(
                company_id=company_id,
                key=key,
                version=1,
                value=spec["value"],
                description=spec["description"],
                is_active=True,
            )
        )
        created += 1

    for name, entity, low, high, role, auto, confidence, priority in DEFAULT_APPROVAL_RULES:
        exists = db.execute(
            select(ApprovalRule).where(
                ApprovalRule.company_id == company_id, ApprovalRule.name == name
            )
        ).scalar_one_or_none()
        if exists:
            continue
        db.add(
            ApprovalRule(
                company_id=company_id,
                name=name,
                entity_type=entity,
                min_amount=money(low),
                max_amount=money(high) if high is not None else None,
                required_role=role,
                auto_approve=auto,
                min_ai_confidence=to_rate(confidence),
                priority=priority,
            )
        )
        created += 1

    db.flush()
    return created


# ------------------------------------------------------- approval routing --

@dataclass
class ApprovalDecision:
    """The engine's verdict on one proposed action."""

    requires_approval: bool
    required_role: str
    auto_apply: bool
    reason: str
    matched_rule_id: str | None = None
    min_confidence: Decimal = ZERO

    def as_dict(self) -> dict:
        return {
            "requires_approval": self.requires_approval,
            "required_role": self.required_role,
            "auto_apply": self.auto_apply,
            "reason": self.reason,
            "matched_rule_id": self.matched_rule_id,
            "min_confidence": self.min_confidence,
        }


# Proposal types that touch the ledger or move money. These can never be
# auto-applied regardless of confidence or configured rules.
LEDGER_AFFECTING = {
    ProposalType.JOURNAL.value,
    ProposalType.INVOICE.value,
    ProposalType.BILL.value,
    ProposalType.ADJUSTMENT.value,
}


def evaluate_approval(
    db: Session,
    company_id: str,
    *,
    entity_type: str,
    amount: Decimal,
    confidence: Decimal = ZERO,
    proposed_by_ai: bool = True,
) -> ApprovalDecision:
    """Decide whether an action auto-applies, and who may approve it.

    The safety rule is applied *before* any configured rule: nothing that posts
    to the ledger or releases money is ever auto-applied by an agent.
    """
    amount = money(abs(D(amount)))
    confidence = to_rate(confidence)
    autonomy = get_policy(db, company_id, "ai.autonomy")

    rules = list(
        db.execute(
            select(ApprovalRule)
            .where(
                ApprovalRule.company_id == company_id,
                ApprovalRule.entity_type == entity_type,
                ApprovalRule.is_active.is_(True),
            )
            .order_by(ApprovalRule.priority.asc())
        ).scalars()
    )

    matched = None
    for rule in rules:
        if amount < rule.min_amount:
            continue
        if rule.max_amount is not None and amount > rule.max_amount:
            continue
        matched = rule
        break

    if matched is None:
        return ApprovalDecision(
            requires_approval=True,
            required_role=UserRole.FINANCE_MANAGER,
            auto_apply=False,
            reason=(
                f"No approval rule matches a {entity_type} of {amount}; "
                "defaulting to finance manager approval."
            ),
        )

    # --- Hard safety rules, ahead of configuration ---
    if proposed_by_ai and entity_type in LEDGER_AFFECTING:
        if not autonomy.get("allow_direct_posting", False):
            return ApprovalDecision(
                requires_approval=True,
                required_role=matched.required_role,
                auto_apply=False,
                reason=(
                    f"{entity_type} proposals affect the ledger, so a human must approve "
                    f"them. Routed to {matched.required_role} for {amount}."
                ),
                matched_rule_id=matched.id,
                min_confidence=matched.min_ai_confidence,
            )

    if entity_type == "PAYMENT" and autonomy.get("never_auto_release_payments", True):
        return ApprovalDecision(
            requires_approval=True,
            required_role=matched.required_role,
            auto_apply=False,
            reason="Payments are never released automatically; a human must authorise them.",
            matched_rule_id=matched.id,
            min_confidence=matched.min_ai_confidence,
        )

    # --- Configured auto-apply path (reversible, non-ledger actions only) ---
    max_auto = money(autonomy.get("max_amount_auto_apply", 0))
    min_auto_confidence = to_rate(autonomy.get("min_confidence_auto_apply", 0.95))

    if (
        matched.auto_approve
        and confidence >= matched.min_ai_confidence
        and confidence >= min_auto_confidence
        and (max_auto == ZERO or amount <= max_auto)
    ):
        return ApprovalDecision(
            requires_approval=False,
            required_role=matched.required_role,
            auto_apply=True,
            reason=(
                f"Auto-applied under '{matched.name}': confidence {confidence:.2f} meets the "
                f"{matched.min_ai_confidence:.2f} threshold and {amount} is within the "
                f"{max_auto} auto-apply limit. The action is reversible and fully audited."
            ),
            matched_rule_id=matched.id,
            min_confidence=matched.min_ai_confidence,
        )

    shortfall = (
        f"confidence {confidence:.2f} is below the {matched.min_ai_confidence:.2f} threshold"
        if confidence < matched.min_ai_confidence
        else f"amount {amount} exceeds the {max_auto} auto-apply limit"
        if max_auto != ZERO and amount > max_auto
        else "this action type always requires review"
    )
    return ApprovalDecision(
        requires_approval=True,
        required_role=matched.required_role,
        auto_apply=False,
        reason=f"Routed to {matched.required_role} under '{matched.name}': {shortfall}.",
        matched_rule_id=matched.id,
        min_confidence=matched.min_ai_confidence,
    )


def can_user_approve(role: str, decision: ApprovalDecision) -> bool:
    return role_at_least(role, decision.required_role)
