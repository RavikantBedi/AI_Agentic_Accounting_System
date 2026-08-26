"""LLM provider abstraction.

Every provider returns the same ``LLMResponse``, so agents are written once and
run against Gemini, Grok, a local Ollama model, or the deterministic Mock.

The application must work with **no API keys at all** — MockProvider is always
available and every agent degrades to its deterministic path rather than
failing. See ``app/ai/agents`` : the rule engines do the accounting work, the
model adds classification and explanation on top.
"""
from __future__ import annotations

import json
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.errors import AIProviderUnavailable, AIResponseInvalid


@dataclass
class Message:
    role: str  # system | user | assistant
    content: str


@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    finish_reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "latency_ms": self.latency_ms,
            "finish_reason": self.finish_reason,
        }


def extract_json(text: str) -> Any:
    """Pull a JSON value out of a model response.

    Models wrap JSON in prose or fences no matter how firmly they are asked not
    to, so this is defensive by design. Raises ``AIResponseInvalid`` rather than
    returning something half-parsed — a malformed response must never be
    silently treated as a valid accounting proposal.
    """
    if text is None:
        raise AIResponseInvalid("The model returned an empty response.")

    candidate = text.strip()
    if not candidate:
        raise AIResponseInvalid("The model returned an empty response.")

    # ```json ... ``` fences
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1).strip()

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    # Fall back to the outermost balanced object/array.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = candidate.find(opener)
        end = candidate.rfind(closer)
        if start != -1 and end > start:
            snippet = candidate[start : end + 1]
            try:
                return json.loads(snippet)
            except json.JSONDecodeError:
                continue

    raise AIResponseInvalid(
        "The model did not return valid JSON.",
        details={"response_preview": text[:500]},
    )


class LLMProvider(ABC):
    """Base class for all providers."""

    name: str = "base"
    is_local: bool = False
    requires_key: bool = True

    def __init__(self, model: str = "", timeout: float = 45.0):
        self.model = model
        self.timeout = timeout

    @abstractmethod
    async def available(self) -> bool:
        """Cheap check that this provider can actually be used right now."""

    @abstractmethod
    async def complete(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.1,
        max_tokens: int = 2048,
        json_mode: bool = False,
    ) -> LLMResponse:
        """Run a completion. Must raise ``AIProviderUnavailable`` on failure."""

    async def complete_json(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> tuple[Any, LLMResponse]:
        """Completion that must yield JSON. Returns ``(parsed, response)``."""
        response = await self.complete(
            messages, temperature=temperature, max_tokens=max_tokens, json_mode=True
        )
        return extract_json(response.text), response

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "model": self.model,
            "is_local": self.is_local,
            "requires_key": self.requires_key,
        }


class _TimedProvider(LLMProvider):
    """Shared latency measurement for HTTP-backed providers."""

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((time.perf_counter() - started) * 1000)
