"""Optional AI integration behind a small, failure-tolerant interface.

Rules honoured here (spec §2 and feature 70)
--------------------------------------------
* Never required: ``provider=none`` disables the whole module and every
  caller must have an offline fallback.
* Never sends private data without an explicit user action: :meth:`AIProvider.complete`
  is only invoked by commands the user typed (``.fight``, ``.suggest``,
  ``.summarize``), never by detection handlers.
* Credentials come only from configuration, never from message text.
* Bounded timeouts and clear error kinds so the UI can say what happened.

The default implementation speaks the OpenAI-compatible ``/chat/completions``
protocol, which most self-hosted gateways also support.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .http import HttpError, post_json


class AIError(Exception):
    """Provider failure. ``kind`` is a stable code for localization."""

    def __init__(self, message: str, kind: str = "ai_error") -> None:
        self.kind = kind
        super().__init__(message)


@dataclass(frozen=True)
class AIMessage:
    role: str
    content: str


class AIProvider:
    """OpenAI-compatible chat completion client."""

    name = "none"

    def __init__(
        self,
        *,
        base_url: str = "",
        api_key: str = "",
        model: str = "gpt-4o-mini",
        timeout: float = 30.0,
        enabled: bool = False,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self._api_key = api_key
        self.model = model
        self.timeout = timeout
        self.enabled = enabled and bool(self.base_url) and bool(api_key)
        self.requests = 0
        self.failures = 0

    @classmethod
    def from_config(cls, config) -> AIProvider:
        enabled = config.ai_provider == "openai_compatible"
        return cls(
            base_url=config.ai_base_url if enabled else "",
            api_key=config.ai_api_key if enabled else "",
            model=config.ai_model,
            timeout=config.ai_timeout,
            enabled=enabled,
        )

    @property
    def available(self) -> bool:
        return self.enabled

    def describe(self) -> dict[str, object]:
        return {
            "provider": self.name if self.enabled else "none",
            "model": self.model if self.enabled else None,
            "requests": self.requests,
            "failures": self.failures,
        }

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 300,
        temperature: float = 0.8,
    ) -> str:
        """Return the assistant's reply text.

        Raises :class:`AIError` with ``kind`` in
        ``disabled|timeout|rate_limited|unauthorized|network|bad_response``.
        """
        if not self.enabled:
            raise AIError("AI provider is not configured", kind="disabled")
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        self.requests += 1
        try:
            data = await post_json(
                f"{self.base_url}/chat/completions", payload, timeout=self.timeout, headers=headers
            )
        except HttpError as exc:
            self.failures += 1
            raise AIError(str(exc), kind=exc.kind) from exc

        try:
            choices = data["choices"]
            content = choices[0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            self.failures += 1
            raise AIError("unexpected AI response shape", kind="bad_response") from exc
        if not isinstance(content, str) or not content.strip():
            self.failures += 1
            raise AIError("AI returned an empty response", kind="bad_response")
        return content.strip()

    async def chat(self, messages: Sequence[AIMessage], *, max_tokens: int = 400) -> str:
        if not self.enabled:
            raise AIError("AI provider is not configured", kind="disabled")
        prompt = "\n\n".join(message.content for message in messages if message.role == "user")
        system = "\n\n".join(message.content for message in messages if message.role == "system") or None
        return await self.complete(prompt, system=system, max_tokens=max_tokens)


def neutral_ai_provider() -> AIProvider:
    """An always-unavailable provider, used as a safe default in tests."""
    return AIProvider(enabled=False)
