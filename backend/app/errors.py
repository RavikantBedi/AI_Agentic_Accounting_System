"""Domain errors mapped to stable HTTP responses.

Errors carry a machine-readable ``code`` so the frontend can react precisely
instead of string-matching prose.
"""
from __future__ import annotations

from typing import Any


class AILedgerError(Exception):
    status_code = 400
    code = "error"

    def __init__(self, message: str, *, details: Any = None, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.details = details
        if code:
            self.code = code

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class ValidationError(AILedgerError):
    status_code = 422
    code = "validation_error"


class NotFoundError(AILedgerError):
    status_code = 404
    code = "not_found"


class ConflictError(AILedgerError):
    status_code = 409
    code = "conflict"


class PermissionDenied(AILedgerError):
    status_code = 403
    code = "permission_denied"


class Unauthorized(AILedgerError):
    status_code = 401
    code = "unauthorized"


class RateLimited(AILedgerError):
    status_code = 429
    code = "rate_limited"


# ---- Accounting-specific -------------------------------------------------

class AccountingError(AILedgerError):
    status_code = 422
    code = "accounting_error"


class UnbalancedJournalError(AccountingError):
    code = "unbalanced_journal"


class ClosedPeriodError(AccountingError):
    status_code = 409
    code = "closed_period"


class ImmutableEntryError(AccountingError):
    status_code = 409
    code = "immutable_entry"


class InsufficientStockError(AccountingError):
    code = "insufficient_stock"


class DuplicateDocumentError(ConflictError):
    code = "duplicate_document"


# ---- AI-specific ---------------------------------------------------------

class AIError(AILedgerError):
    status_code = 502
    code = "ai_error"


class AIProviderUnavailable(AIError):
    code = "ai_provider_unavailable"


class AIResponseInvalid(AIError):
    code = "ai_response_invalid"


class AIGroundingError(AIError):
    status_code = 422
    code = "ai_grounding_failed"


class PolicyViolation(AILedgerError):
    status_code = 403
    code = "policy_violation"
