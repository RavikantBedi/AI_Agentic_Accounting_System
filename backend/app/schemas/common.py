"""Shared Pydantic schemas and validation helpers.

Money crosses the API as a JSON **string**, not a float. A float would corrupt
values in transit exactly as it would in the database, so ``Money`` serialises
Decimal to a fixed 2dp string and the frontend parses it deliberately.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Generic, TypeVar

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
)

from app.money import D, money as to_money, quantity as to_quantity

T = TypeVar("T")


def _serialise_money(value: Decimal | None) -> str | None:
    return None if value is None else f"{to_money(value):.2f}"


def _serialise_quantity(value: Decimal | None) -> str | None:
    return None if value is None else f"{to_quantity(value):.4f}"


def _serialise_rate(value: Decimal | None) -> str | None:
    return None if value is None else f"{D(value):f}"


# Decimal that always leaves the API as an exact 2dp string.
Money = Annotated[Decimal, PlainSerializer(_serialise_money, return_type=str, when_used="json")]
Quantity = Annotated[Decimal, PlainSerializer(_serialise_quantity, return_type=str, when_used="json")]
Rate = Annotated[Decimal, PlainSerializer(_serialise_rate, return_type=str, when_used="json")]


class AppModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class Page(AppModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


class PaginationParams(AppModel):
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class DateRange(AppModel):
    start_date: date
    end_date: date

    @field_validator("end_date")
    @classmethod
    def _check_order(cls, value: date, info) -> date:
        start = info.data.get("start_date")
        if start and value < start:
            raise ValueError("end_date cannot be before start_date")
        return value


class MessageResponse(AppModel):
    message: str
    detail: dict[str, Any] | None = None


class ErrorResponse(AppModel):
    code: str
    message: str
    details: Any | None = None
    request_id: str | None = None


class HealthResponse(AppModel):
    status: str
    version: str
    environment: str
    database: str
    ai_provider: str
    timestamp: datetime


class IdResponse(AppModel):
    id: str
    message: str = "Created"


class EvidenceItem(AppModel):
    """A click-through reference shown next to an AI conclusion."""

    type: str
    id: str | None = None
    label: str = ""
    amount: str | None = None
    date: str | None = None
    detail: str | None = None


class ConfidenceBlock(AppModel):
    """AI confidence, always presented separately from measured accuracy."""

    confidence: Rate
    confidence_label: str
    measured_accuracy: Rate | None = None
    accuracy_sample_size: int = 0
    note: str = (
        "Confidence is the model's own estimate. Measured accuracy is the share of "
        "past proposals a human accepted unchanged."
    )


def confidence_label(value: Decimal | float) -> str:
    numeric = float(value or 0)
    if numeric >= 0.95:
        return "Very high"
    if numeric >= 0.85:
        return "High"
    if numeric >= 0.70:
        return "Moderate"
    if numeric >= 0.50:
        return "Low"
    return "Very low"

# ---------------------------------------------------------------- email ----

_EMAIL_PATTERN = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def _normalise_email(value: str) -> str:
    """Validate an email address without rejecting local-network domains.

    Pydantic's ``EmailStr`` refuses special-use TLDs such as ``.local``. That is
    wrong for a local-first product: a self-hosted install legitimately uses
    addresses like ``owner@company.local``, and the shipped demo account is one.
    Format is enforced here; deliverability is not something this app can or
    should assert.
    """
    address = str(value).strip().lower()
    if len(address) > 254:
        raise ValueError("email address is too long")
    if not _EMAIL_PATTERN.match(address):
        raise ValueError("not a valid email address")
    local_part = address.split("@", 1)[0]
    if len(local_part) > 64:
        raise ValueError("the part before @ is too long")
    if ".." in address:
        raise ValueError("not a valid email address")
    return address


#: Email address that accepts local-network domains.
Email = Annotated[str, AfterValidator(_normalise_email)]
