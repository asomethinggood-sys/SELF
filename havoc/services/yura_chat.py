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
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .. import persona
from ..utils.security import injection_notice, looks_like_injection
from ..utils.time import to_local, utc_now
from .ai_provider import AIError, AIMessage, AIProvider

#: Extensions a generated file may have. All text: nothing can be executed by
#: opening a `.sh` in a chat, but refusing binaries keeps the surface tiny.
FILE_EXTENSIONS = frozenset(
    {".txt", ".md", ".csv", ".json", ".py", ".html", ".svg", ".tex", ".yaml", ".yml", ".srt"}
)
#: A generated file is a note or a script, not a dataset dump.
MAX_FILE_BYTES = 100_000
#: How many exact lines a chat remembers. The bank holds ~2,800 distinct lines per
#: language, so this is the whole catalogue: inside the lifetime of a conversation
#: no line comes back twice.
SENT_MEMORY = 4000
#: The reply itself, before any file block.
MAX_REPLY_CHARS = 3500
#: A reply that is asked for a program needs room for the program. 700 tokens is
#: about 500 words: enough for a paragraph, not for three hundred lines of
#: pygame. The file block is computed by the model, so the ceiling has to move
#: with the request.
FILE_TOKENS = 4000
#: Words that mean "give me an actual file", in both languages the owner uses.
_FILE_WORDS = (
    "file", "code", "script", "program", "snippet", "write me", "make me a game",
    "pygame", "python", "function", "class ", "html", "css", "json", "csv", "sql",
    "فایل", "کد", "برنامه", "اسکریپت", "بنویس", "بساز",
)
_FILE_NAME_HINT = re.compile(r"\b[\w-]+\.(?:py|txt|md|csv|json|html|svg|tex|ya?ml|srt)\b", re.IGNORECASE)
#: An opening block that never closed — what a truncated reply looks like.
_OPEN_FILE_RE = re.compile(r"\[\[\s*file\s*[::]\s*([^\]\n]{1,64}?)\s*\]\](.*)\Z", re.IGNORECASE | re.DOTALL)

_REMEMBER_RE = re.compile(r"\[\[\s*remember\s*[::]\s*(.+?)\s*\]\]", re.IGNORECASE | re.DOTALL)
_FORGET_RE = re.compile(r"\[\[\s*forget\s*[::]\s*(.+?)\s*\]\]", re.IGNORECASE | re.DOTALL)
_FILE_RE = re.compile(
    r"\[\[\s*file\s*[::]\s*([^\]\n]{1,64}?)\s*\]\](.*?)\[\[\s*/\s*file\s*\]\]",
    re.IGNORECASE | re.DOTALL,
)
_UNSAFE_NAME = re.compile(r"[^0-9A-Za-z._\u0600-\u06ff-]+")
#: Where the last picture she looked at is kept, per chat. A description is
#: remembered because "make it funnier" a day later still points at *this* photo.
PICTURE_SETTING = "yura.last_picture"


def _audience_block(audience: Any, language: str) -> str:
    """The room, in one block — speaker, tone, what she knows, what was said."""
    if audience is None or not getattr(audience, "usable", False):
        return ""
    return persona.audience_prompt(
        language=language,
        chat_title=str(getattr(audience, "chat_title", "") or ""),
        speaker=str(getattr(audience, "speaker", "") or ""),
        is_owner=bool(getattr(audience, "is_owner", False)),
        is_group=bool(getattr(audience, "is_group", False)),
        transcript=tuple(getattr(audience, "transcript", ()) or ()),
        tone=str(getattr(audience, "tone", "") or ""),
        relation=str(getattr(audience, "relation", "") or ""),
        facts=tuple(getattr(audience, "known", ()) or ()),
    )


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
        self.last_error = ""
        # What she has said in each chat, in this process. Two jobs: stop the
        # offline bank repeating itself, and make the loop guard airtight now
        # that lines are composed rather than listed.
        # Ordered *unique* lines per chat: a dict is an insertion-ordered set, so
        # the oldest line is the first key when the cap is reached. A deque would
        # store the same line twice (the raw pick and the visible copy) and halve
        # how far back the memory reaches.
        self._sent: dict[int, dict[str, None]] = {}
        self._recent: dict[int, deque[str]] = {}
        # The sentences used most recently, oldest first. This is a *window*, not
        # a lifetime tally: the bank only has ~140 sentences per language, so a
        # picker told to avoid all of them forever starts inventing new faces for
        # an old joke. Keeping the last half in view means a sentence can return
        # only after every other sentence has had a turn.
        self._said_bodies: dict[int, deque[str]] = {}
        # A spent daily allowance is not a bug to retry into: once the gateway
        # says the day's requests are gone, every later message this day goes
        # straight to the offline bank instead of burning a request and a
        # timeout to learn the same thing again.
        self.quota_until: datetime | None = None
        #: The rules the last answer walked over, for a caller that wants to say so.
        self.broken: tuple[str, ...] = ()

    @property
    def lessons(self) -> Any:
        """The lesson book, if the runtime has one (tests may not)."""
        return getattr(self.runtime, "lessons", None)

    async def _note_violations(
        self, reply: str, lessons: tuple[str, ...], person_id: int | None = None
    ) -> tuple[str, ...]:
        """Count any of her own rules this answer walked over. Returns their text."""
        book = self.lessons
        if book is None or not lessons or not reply:
            return ()
        try:
            rows = await book.for_person(person_id)
            hits = await book.violations(reply, rows)
            for lesson in hits:
                await book.note_break(lesson)
            return tuple(lesson.rule for lesson in hits)
        except Exception:  # noqa: BLE001 - counting must never cost an answer
            return ()

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
        hour = None
        try:
            hour = to_local(utc_now(), self.runtime.config.timezone).hour
        except Exception:  # pragma: no cover - a bad zone must not break a reply
            hour = None
        system = persona.system_prompt(
            language,
            memories=facts,
            owner_name=owner_name,
            local_time=local_time,
            greeting=greeting,
            hour=hour,
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
        picture: Any = None,
        quoted: str = "",
        audience: Any = None,
        person: Any = None,
        is_owner: bool | None = None,
        chat_title: str = "",
        lessons: tuple[str, ...] = (),
        reminder: str = "",
    ) -> Answer:
        """Reply to ``text``. Never raises for provider problems.

        ``picture`` is a :class:`havoc.services.vision.Picture` the owner sent —
        she looks at it and answers about it. ``quoted`` is the message they used
        Telegram's reply button on, so "this one?" has something to point at.
        ``audience`` is a :class:`havoc.services.context.ChatContext`: who is
        speaking and what was being said in the room, so a group turn is not
        answered as if the owner had typed it. ``person`` is their
        :class:`havoc.services.people.Person` row — the tone she owes them and
        everything the owner told her about them. ``lessons`` are the corrections
        he has given her — carried into the prompt as things she agreed to, not
        applied as a filter on the way out.
        """
        who = person if person is not None else getattr(audience, "person", None)
        owner = bool(is_owner) if is_owner is not None else bool(
            getattr(audience, "is_owner", True) if audience is not None else True
        )
        tone = str(getattr(who, "tone", "") or getattr(audience, "tone", "") or "rude")
        spoken = text.strip()
        prompt = spoken or ("" if picture is not None else persona.call_prompt(language))
        # Who is on the other side decides what the fallback sounds like: the
        # offline bank is written for the owner, and calling a stranger
        # "daddaaa" would be both wrong and a leak.
        fallback = self._fallback(chat_id, language, tone=tone, is_owner=owner)
        if not prompt and picture is None:
            return Answer(fallback)
        if picture is not None and not prompt:
            # A photo with no caption is still a question: "what is this?".
            prompt = persona.picture_prompt(language)
        if quoted.strip():
            prompt = persona.quoted_prompt(quoted, language, prompt)
        # A picture is evidence, not an order. When the words attached to it are
        # shaped like an instruction to the model, the model is told so in the
        # same breath — and told to say it out loud rather than comply.
        if looks_like_injection(prompt):
            prompt = f"{injection_notice(language)}\n{prompt}"

        history = await self.memory.history(chat_id)
        # Lines already used in this chat: the model is asked for variety, the
        # offline bank is *made* to give it.
        avoid = {content for role, content in history if role == "assistant"}
        avoid |= set(self._recent.get(chat_id, ()))
        avoid |= set(self._said_bodies.get(chat_id, ()))
        avoid |= set(self._sent.get(chat_id, ()))
        # Context first, then the current turn: appending before reading would
        # put the same message in the prompt twice.
        _, messages = await self._context(chat_id, language, greeting=greeting)
        block = _audience_block(audience, language)
        relationship = persona.relationship_prompt(
            language=language,
            name=str(getattr(who, "name", "") or getattr(audience, "speaker", "") or ""),
            is_owner=owner,
            tone=tone,
            relation=str(getattr(who, "relation", "") or getattr(audience, "relation", "") or ""),
            facts=tuple(getattr(who, "facts", ()) or getattr(audience, "known", ()) or ()),
            chat_title=chat_title,
        )
        taught = persona.lessons_prompt(language=language, rules=lessons, reminder=reminder)
        # Her own last lines in this chat: not context to build on, a list of
        # things she may not reach for again. Repetition is what made her sound
        # like a broken record rather than a personality.
        said = tuple(content for role, content in history if role == "assistant" and content)
        repeated = persona.repeat_prompt(said, language)
        if relationship and not block:
            # A private chat has no room to describe, so the relationship block is
            # the whole thing there. In a group the room block already says who
            # this is and how to treat them — saying it twice is what made her
            # sound confused.
            block = relationship
        if taught:
            block = f"{block}\n\n{taught}" if block else taught
        if repeated:
            block = f"{block}\n\n{repeated}" if block else repeated
        if block:
            system = messages[0].content if messages else ""
            messages[0] = AIMessage("system", f"{system}\n\n{block}")
        if picture is not None:
            # A multimodal turn: the words and the picture in one message, which
            # is the only shape every provider accepts for a photo plus a
            # question about it.
            messages.append(
                AIMessage(
                    "user",
                    [
                        {"type": "text", "text": prompt},
                        picture.part(),
                    ],
                )
            )
        else:
            messages.append(AIMessage("user", prompt))
        # What goes into history is what *they* said, not the prompt built around
        # it. Storing the wrapper ("(you replied to this message: …)") taught her
        # to answer in the wrapper's shape — quoting prompt scaffolding back at
        # people. The decorated prompt is for this request only.
        await self.memory.append(
            chat_id, "user", spoken or persona.picture_prompt(language)
        )

        reply: str | None = None
        source = "model"
        if self.enabled and self._quota_spent():
            self.logger.debug("yura: daily model allowance spent, answering offline")
        elif self.enabled:
            try:
                reply = await self.provider.converse(
                    messages,
                    max_tokens=FILE_TOKENS if _wants_file(prompt) else 700,
                )
            except AIError as exc:
                self.last_error = f"{exc.kind}: {exc}"
                self.logger.warning("yura: provider failed (%s)", exc.kind)
                if exc.kind == "rate_limited" and not hasattr(self.provider, "plan"):
                    # A single-provider setup has one allowance; when it is gone,
                    # stop asking until midnight. The rotating router spends its
                    # own per-key counters instead and knows which keys are left,
                    # so muting the day would be wrong there.
                    self._spend_quota()
                reply = None
        elif not self.enabled:
            self.last_error = "AI provider is not configured"
        if reply is None:
            source = "offline"
            self.fallbacks += 1
            reply = fallback

        # There is no filter in front of her mouth: she may swear, insult and be
        # nasty, and he corrects her when it is wrong — that correction is stored
        # and shaped the prompt above. The one thing not negotiable is telling
        # someone to end themselves, which is not a tone.
        if persona.is_self_harm(reply):
            self.logger.warning("yura: a reply encouraged self-harm; answering in her own words")
            reply = fallback
            source = "offline"
        # A rule she walked over is not suppressed. It is counted, and the next
        # turn tells her — so she can own it and change.
        self.broken = await self._note_violations(
            reply, lessons, getattr(who, "user_id", None)
        )
        answer = await self._apply(
            reply, chat_id=chat_id, language=language, incoming=spoken
        )
        if not answer.text and not answer.files:
            answer.text = fallback
        if answer.text:
            self._remember_said(chat_id, answer.text)
        if picture is not None and answer.text:
            await self.remember_picture(chat_id, answer.text, source=source)
        await self.memory.append(chat_id, "assistant", answer.text or "(file sent)")
        self.turns += 1
        return Answer(
            text=answer.text,
            files=answer.files,
            remembered=answer.remembered,
            forgotten=answer.forgotten,
            source=source,
        )

    async def _apply(
        self, reply: str, *, chat_id: int, language: str, incoming: str = ""
    ) -> Answer:
        """Pull directives out of a raw reply and perform them.

        ``incoming`` is what they said. It is not used to build the answer — it is
        used to recognise the answer *copying* it, which a model does when it has
        been handed the message it is replying to.
        """
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

        cleaned = _FILE_RE.sub("", _REMEMBER_RE.sub("", _FORGET_RE.sub("", reply)))
        # A reply that hit the token ceiling stops mid-program, without the
        # closing marker. Salvage what arrived instead of dropping the file
        # entirely: a program missing its last line is still a program the owner
        # can run, and saying "here you go" over nothing is worse.
        if not files:
            opened = _OPEN_FILE_RE.search(cleaned)
            if opened is not None:
                salvaged = self._write_file(opened.group(1), opened.group(2))
                if salvaged is not None:
                    files.append(salvaged)
                    cleaned = cleaned[: opened.start()]
                    self.logger.warning("yura: salvaged a truncated file block (%s)", salvaged.name)

        visible = persona.strip_leaks(cleaned)
        visible = persona.strip_echo(visible, incoming)
        visible = persona.finish_sentence(visible)
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
    # The last picture
    # ------------------------------------------------------------------
    async def remember_picture(self, chat_id: int, description: str, *, source: str = "model") -> None:
        """Keep what she said about the last picture, so it can be revisited."""
        record = {
            "at": utc_now().isoformat(),
            "text": " ".join(str(description).split())[:800],
            "source": source,
        }
        try:
            await self.runtime.store.set_setting(f"{PICTURE_SETTING}.{chat_id}", record)
        except Exception as exc:  # pragma: no cover - a full disk must not mute her
            self.logger.warning("yura: could not keep the picture note: %s", exc)

    async def last_picture(self, chat_id: int) -> dict[str, str] | None:
        """What she said about the last picture in this chat, if any."""
        try:
            record = await self.runtime.store.get_setting(f"{PICTURE_SETTING}.{chat_id}", None)
        except Exception:  # pragma: no cover - defensive
            return None
        if not isinstance(record, dict) or not record.get("text"):
            return None
        return {str(key): str(value) for key, value in record.items()}

    async def caption_last_picture(self, chat_id: int, language: str = "en") -> Answer:
        """Say something new about the picture she already looked at."""
        record = await self.last_picture(chat_id)
        if record is None:
            return Answer(self._offline(chat_id, language))
        prompt = persona.caption_prompt(record["text"], language)
        _, messages = await self._context(chat_id, language)
        messages.append(AIMessage("user", prompt))
        reply: str | None = None
        if self._quota_spent():
            self.last_error = "daily allowance spent"
        elif self.enabled:
            try:
                reply = await self.provider.converse(messages, max_tokens=300)
            except AIError as exc:
                self.last_error = f"{exc.kind}: {exc}"
        source = "model" if reply else "offline"
        if reply is None:
            self.fallbacks += 1
            reply = self._offline(chat_id, language)
        answer = await self._apply(reply, chat_id=chat_id, language=language)
        if not answer.text:
            answer.text = self._offline(chat_id, language)
        await self.memory.append(chat_id, "assistant", answer.text)
        return Answer(text=answer.text, source=source)

    # ------------------------------------------------------------------
    # The daily allowance
    # ------------------------------------------------------------------
    def _quota_spent(self) -> bool:
        """True when today's requests are gone and the reset has not happened."""
        if self.quota_until is None:
            return False
        if utc_now() >= self.quota_until:
            self.quota_until = None
            return False
        return True

    def _spend_quota(self) -> None:
        """Remember a spent allowance until the next UTC midnight.

        OpenRouter's free plan allows 50 model requests per day and resets at
        00:00 UTC; the provider reports that as ``rate_limited``. Retrying every
        message until then would cost a request and a timeout each time.
        """
        now = utc_now()
        self.quota_until = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        self.logger.warning(
            "yura: daily model allowance spent; offline lines until %s",
            self.quota_until.strftime("%Y-%m-%d %H:%M UTC"),
        )

    # ------------------------------------------------------------------
    # Variety and the loop guard
    # ------------------------------------------------------------------
    def _offline(self, chat_id: int, language: str, *, avoid: set[str] | None = None) -> str:
        """An unused line from the offline bank for this chat.

        The bank is asked to avoid the recent window; the loop afterwards is the
        belt to that braces — a picker that returns something already said in this
        chat gets asked again, rather than the owner getting the same joke twice.
        """
        used = set(avoid or ())
        used |= set(self._recent.get(chat_id, ()))
        used |= set(self._said_bodies.get(chat_id, ()))
        used |= set(self._sent.get(chat_id, ()))
        line = persona.build_line(language, avoid=used)
        for _attempt in range(12):
            if not self.was_sent(chat_id, line):
                break
            used.add(line)
            line = persona.build_line(language, avoid=used)
        self._remember_said(chat_id, line)
        return line

    def _fallback(
        self, chat_id: int, language: str, *, tone: str = "rude", is_owner: bool = True
    ) -> str:
        """What she says when no model answers, in the right voice for the room."""
        if is_owner:
            return self._offline(chat_id, language)
        used = set(self._sent.get(chat_id, ()))
        line = persona.stranger_line(language, tone=tone, avoid=used)
        if not persona.is_acceptable(line):  # pragma: no cover - bank is checked
            line = persona.stranger_line(language, tone="neutral", avoid=used)
        self._remember_said(chat_id, line)
        return line

    def _remember_said(self, chat_id: int, text: str) -> None:
        language = "fa" if _looks_persian(text) else "en"
        sentence = persona.sentence_of(text, language)
        if sentence:
            window = self._said_bodies.get(chat_id)
            if window is None:
                window = self._said_bodies[chat_id] = deque(maxlen=_body_window(language))
            if sentence in window:
                window.remove(sentence)
            window.append(sentence)
        ring = self._recent.get(chat_id)
        if ring is None:
            ring = self._recent[chat_id] = deque(maxlen=60)
        ring.append(text)
        sent = self._sent.get(chat_id)
        if sent is None:
            # Long enough that a line cannot come back inside a conversation
            # someone would actually notice: the bank holds ~2,800 distinct lines
            # per language, and this is most of a day of talking.
            sent = self._sent[chat_id] = {}
        sent[" ".join(text.split())] = None
        while len(sent) > SENT_MEMORY:
            sent.pop(next(iter(sent)))

    def was_sent(self, chat_id: int, text: str) -> bool:
        """True when this process already sent exactly this text in this chat."""
        sent = self._sent.get(chat_id)
        return bool(sent and " ".join(text.split()) in sent)

    # ------------------------------------------------------------------
    # Learning
    # ------------------------------------------------------------------
    async def learn(self, chat_id: int, fact: str, *, kind: str = "fact") -> bool:
        """Explicit ``.yura remember …``. Returns False for a duplicate."""
        return bool(await self.memory.remember(chat_id, fact, kind=kind))


def _body_window(language: str) -> int:
    """How many recent sentences a chat keeps out of reach.

    Half the bank: small enough that the picker never runs out of material, large
    enough that a sentence cannot come back before seventy others have been said.
    """
    return max(1, len(persona.bodies(language)) // 2)


def _looks_persian(text: str) -> bool:
    """True when the line is written in the Persian alphabet."""
    return any("\u0600" <= character <= "\u06ff" for character in text)


def _wants_file(prompt: str) -> bool:
    """True when the owner asked for something to be written, not said."""
    lowered = prompt.lower()
    if _FILE_NAME_HINT.search(prompt):
        return True
    return any(word in lowered for word in _FILE_WORDS)


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
