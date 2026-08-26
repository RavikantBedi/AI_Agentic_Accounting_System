"""Authentication primitives: password hashing, tokens and rate limiting.

Password hashing uses PBKDF2-HMAC-SHA256 from the standard library. That is a
deliberate choice: it is a NIST-approved KDF, needs no native build tools on
any platform, and keeps the "runs locally with no paid dependency" promise. The
iteration count is configurable and stored with each hash, so it can be raised
later without invalidating existing passwords.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from app.config import settings
from app.errors import Unauthorized

HASH_ALGORITHM = "pbkdf2_sha256"
SALT_BYTES = 16
JWT_ALGORITHM = "HS256"


# ------------------------------------------------------------- passwords ----

def hash_password(password: str, *, iterations: int | None = None) -> str:
    """Return ``pbkdf2_sha256$iterations$salt$hash``."""
    if not password or len(password) < 8:
        from app.errors import ValidationError

        raise ValidationError("Password must be at least 8 characters long.")

    iterations = iterations or settings.PASSWORD_HASH_ITERATIONS
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join(
        [
            HASH_ALGORITHM,
            str(iterations),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        ]
    )


def verify_password(password: str, stored: str) -> bool:
    """Constant-time password check that never raises on malformed input."""
    if not password or not stored:
        return False
    try:
        algorithm, iterations, salt_b64, hash_b64 = stored.split("$")
        if algorithm != HASH_ALGORITHM:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iterations)
        )
        return hmac.compare_digest(candidate, expected)
    except (ValueError, TypeError):
        return False


def needs_rehash(stored: str) -> bool:
    """True when a stored hash uses fewer iterations than currently configured."""
    try:
        _, iterations, _, _ = stored.split("$")
        return int(iterations) < settings.PASSWORD_HASH_ITERATIONS
    except (ValueError, TypeError):
        return True


def password_strength(password: str) -> tuple[bool, list[str]]:
    """Report every problem at once rather than one at a time."""
    problems: list[str] = []
    if len(password) < 8:
        problems.append("Use at least 8 characters.")
    if not any(char.isdigit() for char in password):
        problems.append("Include at least one number.")
    if not any(char.isalpha() for char in password):
        problems.append("Include at least one letter.")
    if password.lower() in {
        "password", "password1", "12345678", "qwerty123", "admin123", "letmein1",
    }:
        problems.append("This password is too common.")
    return not problems, problems


# ---------------------------------------------------------------- tokens ----

def create_token(
    subject: str,
    *,
    token_type: str = "access",
    company_id: str | None = None,
    role: str | None = None,
    token_version: int = 0,
    expires_minutes: int | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    if expires_minutes is None:
        expires_minutes = (
            settings.ACCESS_TOKEN_TTL_MINUTES
            if token_type == "access"
            else settings.REFRESH_TOKEN_TTL_MINUTES
        )

    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=expires_minutes)).timestamp()),
        "jti": secrets.token_urlsafe(12),
        "tv": token_version,
    }
    if company_id:
        payload["company_id"] = company_id
    if role:
        payload["role"] = role
    if extra:
        payload.update(extra)

    return jwt.encode(payload, settings.SECRET_KEY, algorithm=JWT_ALGORITHM)


def decode_token(token: str, *, expected_type: str = "access") -> dict[str, Any]:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise Unauthorized("Your session has expired. Please sign in again.", code="token_expired") from exc
    except jwt.InvalidTokenError as exc:
        raise Unauthorized("Invalid authentication token.", code="token_invalid") from exc

    if payload.get("type") != expected_type:
        raise Unauthorized(
            f"Expected a {expected_type} token but received a {payload.get('type')} token.",
            code="token_wrong_type",
        )
    return payload


# ---------------------------------------------------------- rate limiting ----

@dataclass
class _Bucket:
    hits: list[float] = field(default_factory=list)


class RateLimiter:
    """In-process sliding-window limiter.

    Deliberately simple and dependency-free: it protects a single-node local or
    small-VM deployment. For a multi-node deployment, put a shared limiter in
    front (documented in docs/DEPLOYMENT.md) — this one is per-process.
    """

    def __init__(self) -> None:
        self._buckets: dict[str, _Bucket] = {}
        self._last_sweep = time.monotonic()

    def check(self, key: str, *, limit: int, window_seconds: int = 60) -> tuple[bool, int]:
        now = time.monotonic()
        self._sweep(now, window_seconds)

        bucket = self._buckets.setdefault(key, _Bucket())
        cutoff = now - window_seconds
        bucket.hits = [hit for hit in bucket.hits if hit > cutoff]

        if len(bucket.hits) >= limit:
            retry_after = int(window_seconds - (now - bucket.hits[0])) + 1
            return False, max(1, retry_after)

        bucket.hits.append(now)
        return True, 0

    def reset(self, key: str) -> None:
        self._buckets.pop(key, None)

    def _sweep(self, now: float, window_seconds: int) -> None:
        """Drop empty buckets periodically so memory cannot grow without bound."""
        if now - self._last_sweep < 300:
            return
        cutoff = now - window_seconds * 2
        self._buckets = {
            key: bucket
            for key, bucket in self._buckets.items()
            if bucket.hits and bucket.hits[-1] > cutoff
        }
        self._last_sweep = now


rate_limiter = RateLimiter()


def constant_time_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
