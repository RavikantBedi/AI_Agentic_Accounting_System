"""Declarative base and shared mixins."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON, TypeDecorator


def utcnow() -> datetime:
    """Timezone-aware UTC now. Never use ``datetime.utcnow()`` (naive)."""
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class UTCDateTime(TypeDecorator):
    """Always store UTC and always return tz-aware datetimes.

    SQLite drops tzinfo, which makes naive/aware comparisons explode at runtime.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: Any, dialect):  # noqa: ANN001
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value: Any, dialect):  # noqa: ANN001
        if value is None:
            return None
        if isinstance(value, datetime) and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSON, list: JSON}

    def to_dict(self, exclude: set[str] | None = None) -> dict[str, Any]:
        exclude = exclude or set()
        out: dict[str, Any] = {}
        for column in self.__table__.columns:
            if column.name in exclude:
                continue
            out[column.name] = getattr(self, column.name)
        return out


class UUIDPk:
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, server_default=func.now(), nullable=False
    )


class CompanyScoped:
    """Multi-tenant scoping. Every business object belongs to exactly one company."""

    @classmethod
    def __declare_first__(cls) -> None:  # pragma: no cover - declarative hook
        pass

    company_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )


class SoftDelete:
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False, index=True)


class Sequenced:
    """Monotonic display sequence, useful for stable ordering in the UI."""

    seq: Mapped[int] = mapped_column(Integer, autoincrement=False, default=0, nullable=False)
