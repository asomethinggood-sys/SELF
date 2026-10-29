"""The conversation engine behind Yura.

One turn looks like this:

1. recall — last :data:`havoc.services.memory.HISTORY_TURNS` messages plus the
   durable facts about the owner;
2. build a system prompt from :mod:`havoc.persona` (voice + rules + memory);
3. ask the provider for a reply;
4. strip the hidden directives the prompt allows (``[[remember: …]]``,
   ``[[forget: …]]``, ``[[file: name]]…[[/file]]``), act on them, and return the
   visible text plus any file that should be sent.

Everything degrades: no provider, a timeout, a 429 — the caller gets an offline
line from :mod:`havoc.persona` instead of an error message. A companion that
sulks because the network dropped is not a companion.

Files the model produces are written under ``<data_dir>/yura`` with a sanitised
name and a whitelisted extension, size-capped, and sent as a document. Nothing
executable is ever written: a model that hallucinates a ``.sh`` gets a ``.txt``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import persona
from ..utils.time import to_local, utc_now
from .ai_provider import AIError, AIMessage, AIProvider

#: Extensions a generated file may have. All text: nothing can be executed by
#: opening a `.sh` in a chat, but refusing binaries keeps the surface tiny.
FILE_EXTENSIONS = frozenset(
    {".txt", ".md", ".csv", ".json", ".py", ".html", ".svg", ".tex", ".yaml", ".yml", ".srt"}
)
#: A generated file is a note or a script, not a dataset dump.
MAX_FILE_BYTES = 100_000
#: The reply itself, before any file block.
MAX_REPLY_CHARS = 3500

_REMEMBER_RE = re.compile(r"\[\[\s*remember\s*[::]\s*(.+?)\s*\]\]", re.IGNORECASE | re.DOTALL)
_FORGET_RE = re.compile(r"\[\[\s*forget\s*[::]\s*(.+?)\s*\]\]", re.IGNORECASE | re.DOTALL)
_FILE_RE = re.compile(
    r"\[\[\s*file\s*[::]\s*([^\]\n]{1,64}?)\s*\]\](.*?)\[\[\s*/\s*file\s*\]\]",
    re.IGNORECASE | re.DOTALL,
)
_UNSAFE_NAME = re.compile(r"[^0-9A-Za-z._\u0600-\u06ff-]+")


@dataclass
class Answer:
    """What one turn produced."""

    text: str
    files: list[Path] = field(default_factory=list)
    remembered: list[str] = field(default_factory=list)
    forgotten: int = 0
    source: str = "offline"  # "model" | "offline"

    @property
    def ok(self) -> bool:
        return bool(self.text or self.files)


class YuraChat:
    """Turns owner text into a Yura reply, with memory."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.memory = runtime.memory
        self.logger = runtime.logger
        self.turns = 0
        self.fallbacks = 0

    @property
    def provider(self) -> AIProvider:
        return self.runtime.ai

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.provider, "available", False))

    # ------------------------------------------------------------------
    # Prompt assembly
    # ------------------------------------------------------------------
    async def _context(
        self, chat_id: int, language: str, *, greeting: bool = False
    ) -> tuple[str, list[AIMessage]]:
        facts = tuple(memory.line() for memory in await self.memory.recall(chat_id))
        owner_name = await self._owner_name()
        local_time = None
        try:
            config = self.runtime.config
            local_time = to_local(utc_now(), config.timezone).strftime("%Y-%m-%d %H:%M")
        except Exception:  # pragma: no cover - a bad zone must not break a reply
            local_time = None
        system = persona.system_prompt(
            language,
            memories=facts,
            owner_name=owner_name,
            local_time=local_time,
            greeting=greeting,
        )
        messages = [AIMessage("system", system)]
        for role, content in await self.memory.history(chat_id):
            messages.append(AIMessage(role, content))
        return system, messages

    async def _owner_name(self) -> str | None:
        try:
            me = await self.runtime.client.get_me()
        except Exception:  # pragma: no cover - network
            return None
        for attribute in ("first_name", "username", "title"):
            value = getattr(me, attribute, None)
            if value:
                return str(value)
        return None

    # ------------------------------------------------------------------
    # The turn
    # ------------------------------------------------------------------
    async def answer(
        self,
        text: str,
        *,
        chat_id: int,
        language: str = "en",
        greeting: bool = False,
    ) -> Answer:
        """Reply to ``text``. Never raises for provider problems."""
        prompt = text.strip() or persona.call_prompt(language)
        if not prompt:
            return Answer(persona.build_line(language))

        # Context first, then the current turn: appending before reading would
        # put the same message in the prompt twice.
        _, messages = await self._context(chat_id, language, greeting=greeting)
        messages.append(AIMessage("user", prompt))
        await self.memory.append(chat_id, "user", prompt)

        reply: str | None = None
        source = "model"
        if self.enabled:
            try:
                reply = await self.provider.converse(messages, max_tokens=700)
            except AIError as exc:
                self.logger.warning("yura: provider failed (%s)", exc.kind)
                reply = None
        if reply is None:
            source = "offline"
            self.fallbacks += 1
            reply = persona.build_line(language)

        answer = await self._apply(reply, chat_id=chat_id, language=language)
        if not answer.text and not answer.files:
            answer.text = persona.build_line(language)
        await self.memory.append(chat_id, "assistant", answer.text or "(file sent)")
        self.turns += 1
        return Answer(
            text=answer.text,
            files=answer.files,
            remembered=answer.remembered,
            forgotten=answer.forgotten,
            source=source,
        )

    async def _apply(self, reply: str, *, chat_id: int, language: str) -> Answer:
        """Pull directives out of a raw reply and perform them."""
        remembered: list[str] = []
        forgotten = 0

        for fact in _REMEMBER_RE.findall(reply):
            if await self.memory.remember(chat_id, fact, kind=_kind_of(fact)):
                remembered.append(" ".join(fact.split()))
        for phrase in _FORGET_RE.findall(reply):
            forgotten += await self.memory.forget_matching(chat_id, phrase)

        files: list[Path] = []
        for name, body in _FILE_RE.findall(reply):
            path = self._write_file(name, body)
            if path is not None:
                files.append(path)

        visible = _FILE_RE.sub("", _REMEMBER_RE.sub("", _FORGET_RE.sub("", reply)))
        visible = re.sub(r"\n{3,}", "\n\n", visible).strip()
        if len(visible) > MAX_REPLY_CHARS:
            visible = visible[:MAX_REPLY_CHARS].rstrip() + " …"
        return Answer(text=visible, files=files, remembered=remembered, forgotten=forgotten)

    # ------------------------------------------------------------------
    # Files
    # ------------------------------------------------------------------
    def _write_file(self, raw_name: str, body: str) -> Path | None:
        """Save generated text under ``data_dir/yura``; never overwrite."""
        name = _UNSAFE_NAME.sub("_", raw_name.strip().strip("/\\"))[:64] or "yura.txt"
        stem, dot, suffix = name.rpartition(".")
        suffix = f".{suffix.lower()}" if dot else ""
        if suffix not in FILE_EXTENSIONS:
            stem, suffix = name if not dot else stem, ".txt"
            name = f"{stem or 'yura'}{suffix}"
        content = body.strip("\n")
        data = content.encode("utf-8")
        if not content:
            return None
        if len(data) > MAX_FILE_BYTES:
            content = data[:MAX_FILE_BYTES].decode("utf-8", errors="ignore")
        folder = Path(self.runtime.config.data_dir) / "yura"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            stamp = utc_now().strftime("%Y%m%d-%H%M%S")
            target = folder / f"{stamp}-{name}"
            target.write_text(content, encoding="utf-8")
        except OSError as exc:  # pragma: no cover - disk problems
            self.logger.warning("yura: cannot write file: %s", exc)
            return None
        return target

    # ------------------------------------------------------------------
    # Learning
    # ------------------------------------------------------------------
    async def learn(self, chat_id: int, fact: str, *, kind: str = "fact") -> bool:
        """Explicit ``.yura remember …``. Returns False for a duplicate."""
        return bool(await self.memory.remember(chat_id, fact, kind=kind))


def _kind_of(fact: str) -> str:
    """A rough label, only used for grouping in ``.yura memory``."""
    lowered = fact.lower()
    if any(word in lowered for word in ("likes", "loves", "hates", "prefers", "favourite", "favorite", "دوست دار")):
        return "preference"
    if any(word in lowered for word in ("is ", "works", "lives", "نام", "زندگی", "کار می")):
        return "fact"
    return "note"


def clean_messages(messages: Sequence[tuple[str, str]]) -> list[AIMessage]:
    """Small helper used by tests and by any future summariser."""
    return [AIMessage(role, content) for role, content in messages]
