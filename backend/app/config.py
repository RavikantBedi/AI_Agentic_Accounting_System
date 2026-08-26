"""Application configuration.

Every setting is environment-driven. Nothing secret is ever hardcoded, and no
value here is required for the application to boot: sane local-first defaults
keep the whole stack runnable with zero configuration.
"""
from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(BASE_DIR / ".env", BASE_DIR.parent / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Application ----
    APP_NAME: str = "AI Ledger"
    APP_TAGLINE: str = "Your books, managed by AI."
    ENVIRONMENT: Literal["local", "development", "staging", "production"] = "local"
    DEBUG: bool = True
    API_PREFIX: str = "/api/v1"

    # ---- Database ----
    # SQLite for local development, PostgreSQL-compatible for production.
    DATABASE_URL: str = f"sqlite:///{(DATA_DIR / 'ailedger.db').as_posix()}"
    SQL_ECHO: bool = False

    # ---- Security ----
    # Auto-generated per-process if unset so local dev needs no secret file.
    # Production start-up refuses to run without an explicit key (see validator).
    SECRET_KEY: str = Field(default_factory=lambda: secrets.token_urlsafe(48))
    ACCESS_TOKEN_TTL_MINUTES: int = 60 * 12
    REFRESH_TOKEN_TTL_MINUTES: int = 60 * 24 * 14
    PASSWORD_HASH_ITERATIONS: int = 240_000
    CORS_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173"
    RATE_LIMIT_PER_MINUTE: int = 300
    AUTH_RATE_LIMIT_PER_MINUTE: int = 10
    MAX_UPLOAD_BYTES: int = 25 * 1024 * 1024

    # ---- Storage ----
    STORAGE_DIR: str = str(DATA_DIR / "storage")

    # ---- AI providers ----
    # auto => Gemini -> Grok -> Ollama -> Mock (first one actually configured wins)
    LLM_PROVIDER: Literal["auto", "gemini", "grok", "ollama", "mock"] = "auto"
    LLM_TIMEOUT_SECONDS: float = 45.0
    LLM_MAX_RETRIES: int = 2

    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-3.6-flash"
    GEMINI_BASE_URL: str = "https://generativelanguage.googleapis.com/v1beta"
    # Tried in order when the configured model is rate-limited or retired.
    # Free-tier quota is counted per model, so a second model is usually still
    # answering when the first one starts refusing. Without this, one 429 drops
    # every answer to template text until the quota window rolls over.
    GEMINI_FALLBACK_MODELS: str = (
        "gemini-3.5-flash,gemini-3.1-flash-lite,gemini-3-flash-preview"
    )

    @property
    def gemini_model_chain(self) -> list[str]:
        """The configured model first, then distinct fallbacks."""
        chain = [self.GEMINI_MODEL.strip()]
        for name in self.GEMINI_FALLBACK_MODELS.split(","):
            candidate = name.strip()
            if candidate and candidate not in chain:
                chain.append(candidate)
        return [name for name in chain if name]

    GROK_API_KEY: str = ""
    GROK_MODEL: str = "grok-2-latest"
    GROK_BASE_URL: str = "https://api.x.ai/v1"

    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "llama3.1"

    # ---- External regulatory / banking providers ----
    # Adapters ship with mock implementations. Real integrations activate only
    # when credentials are supplied; nothing is ever fabricated.
    GST_PROVIDER: Literal["mock", "government"] = "mock"
    EINVOICE_PROVIDER: Literal["mock", "irp"] = "mock"
    EWAYBILL_PROVIDER: Literal["mock", "nic"] = "mock"
    BANK_PROVIDER: Literal["mock", "file"] = "file"

    EINVOICE_API_BASE: str = ""
    EINVOICE_USERNAME: str = ""
    EINVOICE_PASSWORD: str = ""
    EINVOICE_CLIENT_ID: str = ""
    EINVOICE_CLIENT_SECRET: str = ""

    # ---- Localisation ----
    DEFAULT_CURRENCY: str = "INR"
    DEFAULT_COUNTRY: str = "IN"
    DEFAULT_TIMEZONE: str = "Asia/Kolkata"

    @field_validator("CORS_ORIGINS")
    @classmethod
    def _strip_origins(cls, v: str) -> str:
        return ",".join(part.strip() for part in v.split(",") if part.strip())

    @property
    def cors_origin_list(self) -> list[str]:
        return [o for o in self.CORS_ORIGINS.split(",") if o]

    @property
    def is_sqlite(self) -> bool:
        return self.DATABASE_URL.startswith("sqlite")

    @property
    def storage_path(self) -> Path:
        p = Path(self.STORAGE_DIR)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def validate_runtime(self) -> list[str]:
        """Return a list of fatal misconfigurations for the current environment."""
        problems: list[str] = []
        if self.ENVIRONMENT == "production":
            import os

            if not os.getenv("SECRET_KEY"):
                problems.append(
                    "SECRET_KEY must be set explicitly in production "
                    "(an ephemeral key invalidates every session on restart)."
                )
            if self.DEBUG:
                problems.append("DEBUG must be false in production.")
            if "*" in self.cors_origin_list:
                problems.append("CORS_ORIGINS must not be '*' in production.")
        return problems

    def configured_ai_providers(self) -> list[str]:
        """Providers that have enough configuration to even be attempted."""
        available = []
        if self.GEMINI_API_KEY:
            available.append("gemini")
        if self.GROK_API_KEY:
            available.append("grok")
        available.append("ollama")  # probed at runtime; local and free
        available.append("mock")    # always available
        return available


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
