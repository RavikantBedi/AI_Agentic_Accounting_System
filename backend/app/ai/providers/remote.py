"""HTTP-backed providers: Gemini, Grok (xAI) and Ollama.

API keys are read from settings (environment) and never logged, never returned
by any endpoint, and never sent to the frontend.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from app.ai.providers.base import LLMProvider, LLMResponse, Message, _TimedProvider
from app.config import settings
from app.errors import AIProviderUnavailable


# Extra output tokens granted on top of what the caller asks for, to cover a
# reasoning model's internal thinking. Thoughts are billed against
# maxOutputTokens but never appear in the response, so a budget sized for the
# visible answer alone truncates it.
_REASONING_HEADROOM_TOKENS = 2048


class GeminiProvider(_TimedProvider):
    """Google Gemini via the Generative Language REST API."""

    name = "gemini"
    is_local = False
    requires_key = True

    def __init__(self, api_key: str = "", model: str = "", timeout: float = 45.0):
        super().__init__(model or settings.GEMINI_MODEL, timeout)
        self.api_key = api_key or settings.GEMINI_API_KEY
        self.base_url = settings.GEMINI_BASE_URL.rstrip("/")
        self.unavailable_reason = ""

    async def available(self) -> bool:
        """Confirm the key works *and* the model exists.

        A non-empty key string proves nothing. A retired model name, a revoked
        key or a key issued for a different product all return a healthy-looking
        configuration while every real call fails — and because a failed call
        falls through to the next provider, the symptom is silently degraded
        answers rather than an error. Probing the model list catches all three,
        and records why so the Settings screen can say it out loud.
        """
        if not self.api_key:
            self.unavailable_reason = "GEMINI_API_KEY is not set."
            return False
        # Probe the operation we actually use, not the model metadata.
        # A retired model still returns 200 from GET /models/<name> and still
        # lists "generateContent" among its supported methods, while the real
        # call fails with 404. Only generateContent tells the truth.
        # Probe the same chain `complete()` will use. Reporting the provider
        # down because the *first* model is rate-limited would take the whole
        # provider out of the selection loop while a fallback was ready.
        response = None
        for candidate_model in settings.gemini_model_chain:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    response = await client.post(
                        f"{self.base_url}/models/{candidate_model}:generateContent",
                        params={"key": self.api_key},
                        json={
                            "contents": [{"parts": [{"text": "ping"}]}],
                            "generationConfig": {"maxOutputTokens": 1},
                        },
                    )
            except Exception as exc:  # noqa: BLE001 - a probe must never raise
                self.unavailable_reason = (
                    f"Could not reach the Gemini API ({type(exc).__name__})."
                )
                return False

            if response.status_code == 200:
                self.model = candidate_model
                self.unavailable_reason = ""
                return True
            if response.status_code in (429, 404):
                continue
            break

        if response is None:
            self.unavailable_reason = "No Gemini model is configured."
            return False
        if response.status_code in (401, 403):
            self.unavailable_reason = "The Gemini API key was rejected."
        elif response.status_code == 429:
            self.unavailable_reason = (
                "Every configured Gemini model is rate-limited: "
                + ", ".join(settings.gemini_model_chain)
                + ". Free-tier quota is per model and per minute, so this "
                "usually clears shortly. Answers use the deterministic "
                "provider until it does."
            )
        elif response.status_code == 404:
            # Google's 404 body usually names the replacement model; pass it on.
            hint = ""
            try:
                hint = response.json().get("error", {}).get("message", "")
            except Exception:  # noqa: BLE001
                hint = ""
            self.unavailable_reason = (
                hint.strip()
                or f"Model '{self.model}' is not available to this key."
            ) + " Set GEMINI_MODEL in backend/.env to a current model."
        else:
            self.unavailable_reason = f"Gemini returned HTTP {response.status_code}."
        return False

    async def complete(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.1,
        max_tokens: int = 2048,
        json_mode: bool = False,
    ) -> LLMResponse:
        if not self.api_key:
            raise AIProviderUnavailable("Gemini is not configured (GEMINI_API_KEY is empty).")

        system_parts = [m.content for m in messages if m.role == "system"]
        contents = [
            {
                "role": "model" if m.role == "assistant" else "user",
                "parts": [{"text": m.content}],
            }
            for m in messages
            if m.role != "system"
        ]

        # Reasoning models spend maxOutputTokens on internal thinking before
        # they emit a single visible character, and the amount varies with the
        # prompt. A budget sized for the answer alone gets consumed by thoughts,
        # the reply is cut off mid-JSON, and the caller sees "invalid JSON" and
        # silently falls back — the failure looks like the model was never used.
        # Give the thinking room so the answer still fits.
        budget = max(max_tokens, max_tokens + _REASONING_HEADROOM_TOKENS)

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": budget,
            },
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
        if json_mode:
            payload["generationConfig"]["responseMimeType"] = "application/json"

        # Free-tier quota is counted per model, so a 429 on one model says
        # nothing about the next. Walk the chain before giving up: falling back
        # to the deterministic provider costs the user a real answer, and doing
        # that while another model was ready to reply is a self-inflicted
        # downgrade.
        started = time.perf_counter()
        data = None
        last_error = ""

        for candidate_model in settings.gemini_model_chain:
            url = f"{self.base_url}/models/{candidate_model}:generateContent"
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(
                        url, params={"key": self.api_key}, json=payload
                    )
            except httpx.HTTPError as exc:
                raise AIProviderUnavailable(f"Could not reach Gemini: {exc}") from exc

            if response.status_code == 200:
                # Stick with whatever answered, so the rest of this process
                # stops paying the cost of the exhausted model.
                self.model = candidate_model
                data = response.json()
                break

            if response.status_code in (429, 404):
                last_error = (
                    f"'{candidate_model}' is rate-limited"
                    if response.status_code == 429
                    else f"'{candidate_model}' is not available"
                )
                continue

            raise AIProviderUnavailable(
                f"Gemini returned HTTP {response.status_code}.",
                details={"body": response.text[:400], "model": candidate_model},
            )

        if data is None:
            raise AIProviderUnavailable(
                "Every configured Gemini model is rate-limited or unavailable "
                f"({last_error}). Answers fall back to the deterministic "
                "provider until the quota window resets.",
                details={"models_tried": settings.gemini_model_chain},
            )

        candidates = data.get("candidates") or []
        if not candidates:
            feedback = data.get("promptFeedback", {})
            raise AIProviderUnavailable(
                "Gemini returned no candidates.", details={"promptFeedback": feedback}
            )

        parts = candidates[0].get("content", {}).get("parts", [])
        # Skip any reasoning parts: they are the model's private working, not
        # its answer, and concatenating them corrupts otherwise-valid JSON.
        text = "".join(
            part.get("text", "") for part in parts if not part.get("thought")
        )
        usage = data.get("usageMetadata", {})

        if candidates[0].get("finishReason") == "MAX_TOKENS" and not text.strip():
            thoughts = usage.get("thoughtsTokenCount", 0)
            raise AIProviderUnavailable(
                "Gemini used its whole output budget on reasoning and returned "
                f"no answer (thinking used {thoughts} tokens). Raise "
                "LLM_MAX_OUTPUT_TOKENS or choose a lighter model.",
                details={"usage": usage},
            )

        return LLMResponse(
            text=text,
            provider=self.name,
            model=self.model,
            prompt_tokens=int(usage.get("promptTokenCount", 0)),
            completion_tokens=int(usage.get("candidatesTokenCount", 0)),
            latency_ms=self._elapsed_ms(started),
            finish_reason=candidates[0].get("finishReason", ""),
        )


class GrokProvider(_TimedProvider):
    """xAI Grok via its OpenAI-compatible chat completions API."""

    name = "grok"
    is_local = False
    requires_key = True

    def __init__(self, api_key: str = "", model: str = "", timeout: float = 45.0):
        super().__init__(model or settings.GROK_MODEL, timeout)
        self.api_key = api_key or settings.GROK_API_KEY
        self.base_url = settings.GROK_BASE_URL.rstrip("/")
        self.unavailable_reason = ""

    async def available(self) -> bool:
        """Confirm the key is accepted by xAI and the model exists."""
        if not self.api_key:
            self.unavailable_reason = "GROK_API_KEY is not set."
            return False
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{self.base_url}/models",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
        except Exception as exc:  # noqa: BLE001 - a probe must never raise
            self.unavailable_reason = f"Could not reach the xAI API ({type(exc).__name__})."
            return False

        if response.status_code == 200:
            models = {item.get("id") for item in response.json().get("data", [])}
            if models and self.model not in models:
                self.unavailable_reason = (
                    f"Model '{self.model}' is not available to this key. "
                    f"Available: {', '.join(sorted(m for m in models if m))[:120]}."
                )
                return False
            self.unavailable_reason = ""
            return True
        if response.status_code in (400, 401, 403):
            # xAI answers a wrong key with 400 as readily as 401, and a Groq
            # key (gsk_...) pasted into GROK_API_KEY lands here.
            self.unavailable_reason = (
                "The xAI API key was rejected. Keys from console.x.ai begin "
                "with 'xai-'; a 'gsk_' key belongs to Groq, which is a "
                "different service."
            )
        else:
            self.unavailable_reason = f"xAI returned HTTP {response.status_code}."
        return False

    async def complete(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.1,
        max_tokens: int = 2048,
        json_mode: bool = False,
    ) -> LLMResponse:
        if not self.api_key:
            raise AIProviderUnavailable("Grok is not configured (GROK_API_KEY is empty).")

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
                if response.status_code != 200:
                    raise AIProviderUnavailable(
                        f"Grok returned HTTP {response.status_code}.",
                        details={"body": response.text[:400]},
                    )
                data = response.json()
        except httpx.HTTPError as exc:
            raise AIProviderUnavailable(f"Could not reach Grok: {exc}") from exc

        choices = data.get("choices") or []
        if not choices:
            raise AIProviderUnavailable("Grok returned no choices.")

        usage = data.get("usage", {})
        return LLMResponse(
            text=choices[0].get("message", {}).get("content", ""),
            provider=self.name,
            model=data.get("model", self.model),
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            latency_ms=self._elapsed_ms(started),
            finish_reason=choices[0].get("finish_reason", ""),
        )


class OllamaProvider(_TimedProvider):
    """Local models via Ollama — free, private, no key required."""

    name = "ollama"
    is_local = True
    requires_key = False

    def __init__(self, base_url: str = "", model: str = "", timeout: float = 120.0):
        super().__init__(model or settings.OLLAMA_MODEL, timeout)
        self.base_url = (base_url or settings.OLLAMA_BASE_URL).rstrip("/")

    async def available(self) -> bool:
        """Probe the local daemon and confirm the configured model is pulled."""
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(f"{self.base_url}/api/tags")
                if response.status_code != 200:
                    return False
                models = [m.get("name", "") for m in response.json().get("models", [])]
                if not models:
                    return False
                # "llama3.1" should match a pulled "llama3.1:8b".
                return any(name.split(":")[0] == self.model.split(":")[0] for name in models)
        except (httpx.HTTPError, ValueError):
            return False

    async def complete(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.1,
        max_tokens: int = 2048,
        json_mode: bool = False,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if json_mode:
            payload["format"] = "json"

        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(f"{self.base_url}/api/chat", json=payload)
                if response.status_code != 200:
                    raise AIProviderUnavailable(
                        f"Ollama returned HTTP {response.status_code}.",
                        details={"body": response.text[:400]},
                    )
                data = response.json()
        except httpx.HTTPError as exc:
            raise AIProviderUnavailable(
                f"Could not reach Ollama at {self.base_url}: {exc}"
            ) from exc

        return LLMResponse(
            text=data.get("message", {}).get("content", ""),
            provider=self.name,
            model=data.get("model", self.model),
            prompt_tokens=int(data.get("prompt_eval_count", 0)),
            completion_tokens=int(data.get("eval_count", 0)),
            latency_ms=self._elapsed_ms(started),
            finish_reason=data.get("done_reason", ""),
        )
