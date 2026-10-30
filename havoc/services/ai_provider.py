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
from typing import Any

from .http import HttpError, get_json, post_json


class AIError(Exception):
    """Provider failure. ``kind`` is a stable code for localization."""

    def __init__(self, message: str, kind: str = "ai_error") -> None:
        self.kind = kind
        super().__init__(message)


@dataclass(frozen=True)
class AIMessage:
    """One turn. ``content`` is text, or a list of OpenAI-shaped parts.

    A list is how a picture travels: ``[{"type": "text", ...},
    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,…"}}]``.
    Both providers this project speaks to accept that shape, so a photo costs no
    extra client code — only the router has to know that not every model can see.
    """

    role: str
    content: Any

    @property
    def is_text(self) -> bool:
        return isinstance(self.content, str)

    @property
    def text(self) -> str:
        """The text part, whatever shape the turn has."""
        if isinstance(self.content, str):
            return self.content
        if isinstance(self.content, list):
            return "\n".join(
                str(part.get("text", ""))
                for part in self.content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        return str(self.content or "")

    @property
    def has_image(self) -> bool:
        if not isinstance(self.content, list):
            return False
        return any(
            isinstance(part, dict) and part.get("type") in {"image_url", "input_image"}
            for part in self.content
        )


#: Substrings that make a model a good default for Yura, best first. Used only
#: when ``AI_MODEL=auto``: free slugs rotate every few weeks, so pinning one in
#: a config file is how a bot ends up mute after a quiet upstream change.
#: Slugs that are listed with the chat models but cannot answer a chat message:
#: embeddings, rerankers, speech, image endpoints and moderation models.
NON_CHAT_MARKERS: tuple[str, ...] = (
    "embed",
    "-vl",
    "vision",
    "rerank",
    "whisper",
    "tts",
    "image",
    "moderation",
    "audio",
    "guard",
    "safety",
)

MODEL_PREFERENCES: tuple[str, ...] = (
    "llama-3.3-70b",
    "qwen3-next-80b",
    "qwen3-235b",
    "gemma-4-31b",
    "deepseek",
    "mistral-large",
    "gpt-oss-120b",
    "instruct",
    "nemotron",
    "gemini",
)


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
        # ``AI_MODEL`` may be a comma-separated fallback chain: free tiers add
        # and retire models without notice, and a chat that dies because one
        # slug disappeared is a bad trade for a config value.
        self.model = model
        self.models = [part.strip() for part in (model or "").split(",") if part.strip()]
        self.active_model = self.models[0] if self.models else ""
        self._discovered = False
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
        thread: list[AIMessage] = []
        if system:
            thread.append(AIMessage("system", system))
        thread.append(AIMessage("user", prompt))
        return await self.converse(
            thread, max_tokens=max_tokens, temperature=temperature
        )

    async def probe(self, *, max_tokens: int = 8) -> tuple[bool, str]:
        """One tiny request, to find out whether the key works *now*.

        Used when a key is stored: claiming "saved, model live" without trying is
        exactly the kind of silent lie this project refuses. Returns
        ``(worked, detail)`` where ``detail`` is the answer or the failure kind.
        """
        try:
            reply = await self.complete(
                "Reply with the single word: ok",
                max_tokens=max_tokens,
                temperature=0.0,
            )
        except AIError as exc:
            return False, f"{exc.kind}: {exc}"
        except Exception as exc:  # noqa: BLE001 - a probe must never raise
            return False, f"{type(exc).__name__}: {exc}"
        reply = " ".join(str(reply).split())
        if not reply:
            return False, "empty reply"
        return True, reply[:80]

    async def discover_models(self, *, limit: int = 3, free_only: bool = True) -> list[str]:
        """Ask the gateway which models exist, best candidates first.

        Used when the configured model is ``auto`` or when every configured
        model 404s. Only reads ``/models``; a failure returns an empty list and
        the caller reports the original error instead of a confusing one.
        """
        try:
            data = await get_json(f"{self.base_url}/models", timeout=min(self.timeout, 20))
        except HttpError:
            return []
        entries = data.get("data") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            return []
        self._discovered = True
        ids = [
            str(entry.get("id"))
            for entry in entries
            if isinstance(entry, dict) and entry.get("id")
        ]
        if free_only:
            free = [model for model in ids if model.endswith(":free")]
            if free:
                ids = free
        ids = [model for model in ids if not any(marker in model.lower() for marker in NON_CHAT_MARKERS)]
        ranked: list[str] = []
        for needle in MODEL_PREFERENCES:
            for model in ids:
                if needle in model and model not in ranked:
                    ranked.append(model)
        for model in ids:
            if model not in ranked:
                ranked.append(model)
        self.models = ranked[:limit] or self.models
        return self.models

    async def converse(
        self,
        messages: Sequence[AIMessage],
        *,
        max_tokens: int = 400,
        temperature: float = 0.85,
    ) -> str:
        """Send a full role-tagged conversation and return the reply text.

        Unlike :meth:`chat`, this keeps the ``assistant`` turns, which is what a
        real conversation needs: without them the model forgets what it just
        said and repeats itself.
        """
        if not self.enabled:
            raise AIError("AI provider is not configured", kind="disabled")
        thread = [
            {"role": message.role, "content": message.content}
            for message in messages
            if message.text.strip() or message.has_image
        ]
        if not any(message["role"] == "user" for message in thread):
            raise AIError("no user message in the conversation", kind="bad_response")
        headers = {"Authorization": f"Bearer {self._api_key}"}
        self.requests += 1

        if not self.models or self.models == ["auto"]:
            await self.discover_models()
        last_error: AIError | None = None
        content = ""
        # Two passes: the budget the caller asked for, then a generous one. A
        # reasoning model that spends every token thinking returns content "",
        # and the same model answers once it is given room.
        budgets = [max_tokens] if max_tokens >= 400 else [max_tokens, max(max_tokens * 20, 400)]
        for budget in budgets:
            for candidate in self.models or [self.model]:
                payload = {
                    "model": candidate,
                    "messages": thread,
                    "max_tokens": budget,
                    "temperature": temperature,
                }
                try:
                    data = await post_json(
                        f"{self.base_url}/chat/completions",
                        payload,
                        timeout=self.timeout,
                        headers=headers,
                    )
                except HttpError as exc:
                    last_error = AIError(str(exc), kind=exc.kind)
                    if exc.kind == "unauthorized":
                        break  # a wrong key will not work with the next model either
                    continue
                # A free catalogue contains models that answer 200 with no
                # content. That is a bad candidate, not a dead provider, so the
                # next one gets a turn instead of the owner getting nothing.
                content = _content_of(data)
                if not content:
                    last_error = AIError("AI returned an empty response", kind="bad_response")
                    continue
                self.active_model = candidate
                break
            if content:
                break
            if last_error is not None and last_error.kind == "unauthorized":
                break
        if not content and not self._discovered:
            # Every configured slug is probably retired. Ask the gateway what
            # actually exists right now and retry once with its free models.
            if await self.discover_models():
                return await self.converse(
                    messages, max_tokens=max_tokens, temperature=temperature
                )
        if not content:
            self.failures += 1
            raise last_error or AIError("AI provider is not configured", kind="disabled")
        return content.strip()

    async def chat(self, messages: Sequence[AIMessage], *, max_tokens: int = 400) -> str:
        if not self.enabled:
            raise AIError("AI provider is not configured", kind="disabled")
        prompt = "\n\n".join(message.content for message in messages if message.role == "user")
        system = "\n\n".join(message.content for message in messages if message.role == "system") or None
        return await self.complete(prompt, system=system, max_tokens=max_tokens)


def _content_of(data: object) -> str:
    """The reply text in a completion, or ``""`` when the shape is unusable."""
    try:
        content = data["choices"][0]["message"]["content"]  # type: ignore[index]
    except (KeyError, IndexError, TypeError):
        return ""
    if not isinstance(content, str):
        return ""
    return content.strip()


def neutral_ai_provider() -> AIProvider:
    """An always-unavailable provider, used as a safe default in tests."""
    return AIProvider(enabled=False)
