"""Database engine, session management and the exact-decimal column types.

Local development runs on SQLite; production runs on PostgreSQL. The only
schema-level difference is handled here, so application code is portable.

IMPORTANT: SQLAlchemy's stock ``Numeric`` round-trips through ``float`` on
SQLite, which silently corrupts money. ``Money``/``Quantity`` below store the
value as a lexically-safe string on SQLite and as real NUMERIC on PostgreSQL,
so a Decimal always comes back bit-for-bit identical.
"""
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import Numeric, String, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.types import TypeDecorator

from app.config import settings
from app.money import D, MONEY_DP, QTY_DP, RATE_DP


class _ExactDecimal(TypeDecorator):
    """Store Decimal exactly on every backend."""

    impl = String
    cache_ok = True
    quant = MONEY_DP
    precision = 20
    scale = 2

    def load_dialect_impl(self, dialect):  # noqa: ANN001
        if dialect.name == "sqlite":
            # 30 chars comfortably holds sign + 20 digits + point + scale.
            return dialect.type_descriptor(String(40))
        return dialect.type_descriptor(Numeric(self.precision, self.scale, asdecimal=True))

    def process_bind_param(self, value: Any, dialect):  # noqa: ANN001
        if value is None:
            return None
        dec = D(value).quantize(self.quant)
        if dialect.name == "sqlite":
            # Zero-padded, sign-prefixed => string ordering matches numeric
            # ordering, so ORDER BY / MIN / MAX behave correctly in SQL.
            sign = "-" if dec < 0 else "+"
            digits = f"{abs(dec):0{self.precision + self.scale + 1}.{self.scale}f}"
            return f"{sign}{digits}"
        return dec

    def process_result_value(self, value: Any, dialect):  # noqa: ANN001
        if value is None:
            return None
        if isinstance(value, Decimal):
            return value.quantize(self.quant)
        text = str(value)
        if text and text[0] == "+":
            text = text[1:]
        return D(text).quantize(self.quant)


class Money(_ExactDecimal):
    cache_ok = True
    quant = MONEY_DP
    precision = 20
    scale = 2


class Quantity(_ExactDecimal):
    cache_ok = True
    quant = QTY_DP
    precision = 20
    scale = 4


class Rate(_ExactDecimal):
    cache_ok = True
    quant = RATE_DP
    precision = 14
    scale = 6


def _json_default(value: Any) -> Any:
    """Make JSON columns accept the types this domain actually uses.

    Audit payloads, AI proposals and report snapshots routinely contain Decimal
    and date values. Decimals become strings (never floats — that would defeat
    the exact-decimal guarantee), and dates become ISO strings.
    """
    from datetime import date as _date
    from datetime import datetime as _datetime

    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (_datetime, _date)):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if hasattr(value, "value"):  # Enum
        return value.value
    return str(value)


def _json_serializer(value: Any) -> str:
    import json

    return json.dumps(value, default=_json_default, ensure_ascii=False)


def _engine_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "echo": settings.SQL_ECHO,
        "future": True,
        "pool_pre_ping": True,
        "json_serializer": _json_serializer,
    }
    if settings.is_sqlite:
        # check_same_thread=False: FastAPI serves requests from a threadpool.
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    else:
        kwargs.update(pool_size=10, max_overflow=20)
    return kwargs


def _ensure_sqlite_directory(url: str) -> None:
    """Create the directory a SQLite file lives in, if it does not exist.

    SQLite will not create a missing parent directory; it fails with a bare
    "unable to open database file", which says nothing about the actual cause.
    A relative ``DATABASE_URL`` such as ``sqlite:///./data/ailedger.db``
    resolves against the working directory, so the same configuration works
    from the repository root and fails from ``backend/`` — a difference no
    error message hints at.
    """
    if not url.startswith("sqlite"):
        return
    path = url.split("///", 1)[-1].split("?", 1)[0]
    if not path or path == ":memory:":
        return
    parent = Path(path).expanduser().resolve().parent
    parent.mkdir(parents=True, exist_ok=True)


_ensure_sqlite_directory(settings.DATABASE_URL)

engine: Engine = create_engine(settings.DATABASE_URL, **_engine_kwargs())


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, connection_record):  # noqa: ANN001
    """Enforce referential integrity and use WAL for concurrent readers."""
    if not settings.is_sqlite:
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
    finally:
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency: one transaction-scoped session per request."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """Transactional scope for scripts, seeders and background work."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def create_all() -> None:
    from app.models import Base  # noqa: PLC0415  (avoid circular import at module load)

    Base.metadata.create_all(bind=engine)


def drop_all() -> None:
    from app.models import Base  # noqa: PLC0415

    Base.metadata.drop_all(bind=engine)
