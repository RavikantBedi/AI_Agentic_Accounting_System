"""Provider registry and selection.

``LLM_PROVIDER=auto`` walks the priority chain Gemini -> Grok -> Ollama -> Mock
and uses the first one actually reachable. Availability is cached briefly so a
down provider does not add latency to every request, and re-probed afterwards
so recovery is automatic.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from app.ai.providers.base import LLMProvider, LLMResponse, Message, extract_json
from app.ai.providers.mock import MockProvider
from app.ai.providers.remote import GeminiProvider, GrokProvider, OllamaProvider
from app.config import settings
from app.errors import AIProviderUnavailable

PROVIDER_PRIORITY = ["gemini", "grok", "ollama", "mock"]

_AVAILABILITY_TTL_SECONDS = 60.0
_availability_cache: dict[str, tuple[bool, float]] = {}
_lock = asyncio.Lock()


def build_provider(name: str) -> LLMProvider:
    name = (name or "").lower()
    if name == "gemini":
        return GeminiProvider(timeout=settings.LLM_TIMEOUT_SECONDS)
    if name == "grok":
        return GrokProvider(timeout=settings.LLM_TIMEOUT_SECONDS)
    if name == "ollama":
        return OllamaProvider(timeout=max(settings.LLM_TIMEOUT_SECONDS, 120.0))
    if name == "mock":
        return MockProvider()
    raise AIProviderUnavailable(f"Unknown LLM provider '{name}'.")


async def _is_available(provider: LLMProvider, *, use_cache: bool = True) -> bool:
    now = time.monotonic()
    if use_cache:
        cached = _availability_cache.get(provider.name)
        if cached and (now - cached[1]) < _AVAILABILITY_TTL_SECONDS:
            return cached[0]
    try:
        available = await provider.available()
    except Exception:  # noqa: BLE001 - a probe must never raise into a request
        available = False
    _availability_cache[provider.name] = (available, now)
    return available


async def select_provider(preferred: str | None = None) -> LLMProvider:
    """Return the first usable provider. Mock is the guaranteed floor."""
    requested = (preferred or settings.LLM_PROVIDER or "auto").lower()

    if requested != "auto":
        provider = build_provider(requested)
        if await _is_available(provider):
            return provider
        # An explicitly requested provider that is down still falls back rather
        # than breaking the app, but the caller is told via fallback_reason.
        fallback = MockProvider()
        fallback.fallback_reason = (  # type: ignore[attr-defined]
            f"'{requested}' is configured but unreachable; using the deterministic provider."
        )
        return fallback

    async with _lock:
        for name in PROVIDER_PRIORITY:
            provider = build_provider(name)
            if await _is_available(provider):
                return provider
    return MockProvider()


async def provider_status() -> list[dict[str, Any]]:
    """Live status of every provider, for the Settings screen.

    Never exposes key material — only whether a key is present.
    """
    statuses: list[dict[str, Any]] = []
    for name in PROVIDER_PRIORITY:
        provider = build_provider(name)
        available = await _is_available(provider, use_cache=False)
        configured = True
        detail = ""

        # A key being present is not the same as a key that works. The probe
        # in `available()` records why it failed; show that rather than the
        # reassuring-but-useless "API key set".
        reason = getattr(provider, "unavailable_reason", "")

        if name == "gemini":
            configured = bool(settings.GEMINI_API_KEY)
            if not configured:
                detail = "Set GEMINI_API_KEY to enable."
            elif available:
                detail = f"Key accepted; model '{provider.model}' is available."
            else:
                detail = reason or "Configured, but the API did not respond."
        elif name == "grok":
            configured = bool(settings.GROK_API_KEY)
            if not configured:
                detail = "Set GROK_API_KEY to enable."
            elif available:
                detail = f"Key accepted; model '{provider.model}' is available."
            else:
                detail = reason or "Configured, but the API did not respond."
        elif name == "ollama":
            detail = (
                f"Reachable at {settings.OLLAMA_BASE_URL} with model '{settings.OLLAMA_MODEL}'."
                if available
                else f"Not reachable at {settings.OLLAMA_BASE_URL}, or model "
                f"'{settings.OLLAMA_MODEL}' is not pulled."
            )
        else:
            detail = "Always available. Deterministic, runs locally, needs no key."

        statuses.append(
            {
                "name": name,
                "model": provider.model,
                "is_local": provider.is_local,
                "requires_key": provider.requires_key,
                "configured": configured,
                "available": available,
                "detail": detail,
            }
        )
    return statuses


async def active_provider_name() -> str:
    provider = await select_provider()
    return provider.name


def clear_availability_cache() -> None:
    _availability_cache.clear()


__all__ = [
    "LLMProvider",
    "LLMResponse",
    "Message",
    "MockProvider",
    "GeminiProvider",
    "GrokProvider",
    "OllamaProvider",
    "build_provider",
    "select_provider",
    "provider_status",
    "active_provider_name",
    "extract_json",
    "clear_availability_cache",
    "PROVIDER_PRIORITY",
]
