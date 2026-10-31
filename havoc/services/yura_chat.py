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
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .. import persona, ui
from ..utils import glyphs
from ..utils.formatting import strip_html
from ..utils.markup import rich
from ..utils.security import injection_notice, looks_like_injection
from ..utils.time import to_local, utc_now
from . import intent as intent_module
from . import self_console
from .actions import ActionLayer, Turn, scrub_tool_text
from .agent import Agent
from .ai_provider import AIError, AIMessage, AIProvider
from .self_console import catalogue as command_catalogue

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
#: The reply itself, before any file block. One Telegram message holds 4096
#: characters; this leaves room for the tags `rich` adds, so a long answer arrives
#: as one bubble instead of being split in the middle of a sentence.
MAX_REPLY_CHARS = 4000
#: The ceiling for an ordinary answer. The owner does not care about tokens and a
#: message can hold 4096 characters, so the budget is the length of a *message*,
#: not the length of a text message someone types with two thumbs. Persian costs
#: more tokens per character than English, so this is generous on purpose.
CHAT_TOKENS = 2500
#: A reply that is asked for a program needs room for the program: three hundred
#: lines of pygame do not fit anywhere else. The file block is computed by the
#: model, so the ceiling has to move with the request.
FILE_TOKENS = 6000
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
#: Whether she may reach for a tool at all. On by default, because the upgrade is
#: the point; one setting turns it back into a pure conversation without a deploy.
AGENT_SETTING = "yura.agent"


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
    source: str = "offline"  # "model" | "offline" | "action"
    #: True when ``text`` is already Telegram HTML (a card), not markdown to be
    #: rendered. Without this the card went to the chat as visible ``<b>`` tags,
    #: which is exactly what the first version of this feature did.
    rendered: bool = False

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
        #: Tools this turn actually used, for the receipt line under the answer.
        self._receipts: list[str] = []
        #: What the sentence itself asked for and got done this turn (1.7).
        self._intent_done: list[str] = []
        #: The cards of the tools that ran, drawn under her answer.
        self._cards: list[tuple[str, tuple[str, ...]]] = []
        #: The last tool turn, so ``.yura actions`` can point at it.
        self.last_turn: Turn | None = None

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
    def agent_layer(self) -> ActionLayer | None:
        """Her hands, if this runtime has any (tests may not)."""
        return getattr(self.runtime, "actions", None)

    @property
    def agent(self) -> Agent | None:
        layer = self.agent_layer
        if layer is None or not layer.tools:
            return None
        return Agent(layer, logger=self.logger)

    async def agent_enabled(self) -> bool:
        try:
            return bool(await self.runtime.store.get_bool(AGENT_SETTING, True))
        except Exception:  # noqa: BLE001 - no store means no hands, not a crash
            return False

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
        # Whose account this is, spelled out: her own name for him and the ids the
        # permission layer actually checks, so recognition and authority agree.
        owner_block = persona.owner_prompt(
            language=language,
            names=(owner_name,) if owner_name else (),
            ids=tuple(sorted(getattr(self.runtime.permissions, "owner_ids", set())))[:4],
            handle=(getattr(self.runtime.config, "owner_usernames", ()) or ("",))[0],
        )
        if owner_block:
            system = f"{system}\n\n{owner_block}"
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
        reply_to: int | None = None,
        message_id: int | None = None,
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
        self._receipts = []
        self._cards = []
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
        # The ornaments and faces of her last few answers: what she may not use
        # again, and what the typography brief hands her instead. Eight answers is
        # a couple of minutes of chat — long enough that a reader never sees the
        # same sparkle twice, short enough that the pool stays rich.
        worn: set[str] = set()
        worn_faces: set[str] = set()
        for line in said[-8:]:
            worn |= glyphs.collect(line)
            worn_faces |= glyphs.collect_faces(line)
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
        # Her hands, described once and last-but-one: the typography brief below
        # stays the closing instruction, because that is the one that decides how
        # the answer *looks*, and a tool block after it would win.
        hands = ""
        layer = self.agent_layer
        # Only the owner's turns are told that hands exist. A stranger gets her
        # voice and the room's manners; they do not get a manual.
        if layer is not None and layer.tools and owner and await self.agent_enabled():
            hands = layer.catalogue(language)
            # ...and what those hands can pull: her own command line, so "yura fal
            # page 166" needs no special-case code, only knowing that fal exists.
            listing = command_catalogue(self.runtime, language)
            if listing:
                hands = f"{hands}\n\n{listing}"
        ornaments = persona.ornament_prompt(language, used=worn, faces=worn_faces)
        if hands:
            block = f"{block}\n\n{hands}" if block else hands
        block = f"{block}\n\n{ornaments}" if block else ornaments
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
        did: list[str] = []
        if self.enabled and self._quota_spent():
            self.logger.debug("yura: daily model allowance spent, answering offline")
        elif self.enabled:
            budget = FILE_TOKENS if _wants_file(prompt) else CHAT_TOKENS
            try:
                step = await self._with_hands(
                    messages,
                    budget=budget,
                    chat_id=chat_id,
                    language=language,
                    who=who,
                    owner=owner,
                    person=person,
                    reply_to=reply_to,
                    message_id=message_id,
                )
                if step is not None and step.waiting is not None:
                    # The only answer to a pending action is the question about it.
                    return self._ask_permission(step, chat_id=chat_id, language=language)
                reply = step.text if step is not None else await self.provider.converse(
                    messages, max_tokens=budget
                )
                if step is not None and step.did:
                    self._receipts = step.did
                    self._cards = list(getattr(step, "cards", ()))
                    did = list(step.did)
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
        if reply is not None and source == "model":
            # The fence was supposed to be parsed and executed upstream. Anything
            # still wearing machinery — a malformed block, a bare JSON line — is
            # removed here, so no path can hand him a copy-code card again.
            scrubbed = scrub_tool_text(reply)
            if scrubbed != reply:
                self.logger.warning("yura: removed a leftover tool block from a reply")
                reply = scrubbed
        # Whether anything actually happened. The model's block is the first route
        # and the sentence itself is the second: "turn off my live clock" must turn
        # the clock off even on a turn where the model only wrote a paragraph.
        if owner:
            # Always offer the sentence to the floor: a model that narrates instead
            # of acting must not be the reason nothing happens. An intent whose work
            # a tool already did is skipped inside.
            await self._run_intent(
                spoken,
                chat_id=chat_id,
                language=language,
                reply_to=reply_to,
                already=tuple(did),
            )
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
            reply,
            chat_id=chat_id,
            language=language,
            incoming=spoken,
            # Only her model answers get restyled. The offline bank is hand-written
            # and line-perfect; swapping its ornaments would be editing a poem to
            # fix its punctuation.
            worn=worn if source == "model" else set(),
            worn_faces=worn_faces if source == "model" else set(),
        )
        if answer.text and (self._cards or self._receipts):
            # A card, not a status report: what she did, drawn under her words.
            # Only appears on a turn where something actually happened — an
            # ordinary answer is byte-for-byte what it was before hands existed.
            # The prose is rendered *here*, because the card is HTML and the two
            # have to be concatenated in the same language. Rendering after the
            # fact would have run ``rich`` over the card's tags.
            body = rich(answer.text) if source == "model" else answer.text
            answer.text = self._with_receipt(body, language)
            answer.rendered = True
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
            rendered=answer.rendered,
        )

    #: A sentence's label, and the tools that would have done the same thing. When
    #: the model already used one of them this turn, the floor stays out of the way
    #: — but only for that label: "he is busy lil bro, and turn the clock off" is
    #: two instructions, and one of them may have gone through a tool.
    _SAME_AS: dict[str, tuple[str, ...]] = {
        "react": ("react",),
        "send": ("send_message",),
        "plan": ("plan", "remind", "schedule"),
        "remember": ("remember",),
        "forget": ("forget",),
        # `recall` is the hand that reads memory; the console's `.recall` is the
        # same work done twice, and the second card pushed the real one off.
        "lookup": ("recall", "command", "search"),
    }

    async def _run_intent(
        self,
        text: str,
        *,
        chat_id: int,
        language: str,
        reply_to: int | None = None,
        already: tuple[str, ...] = (),
    ) -> None:
        """Read the sentence as an instruction and *do* it.

        This is the floor under her hands: a model that answers but sends no block
        no longer means "nothing happens". One console line, dispatched by the same
        code ``.clock off`` uses, so the state change, the reply and the audit trail
        are the real ones — not a second implementation that can drift.
        """
        if not text.strip():
            return
        try:
            found = intent_module.detect(
                text,
                features=frozenset(self.runtime.feature_manager.names()),
                commands=frozenset(self.runtime.registry.reserved_names()),
            )
        except Exception as exc:  # noqa: BLE001 - a reader that throws is not a lockout
            self.logger.warning("yura: could not read the sentence as an instruction: %s", exc)
            return
        if found is None:
            return
        done_by_tool = self._SAME_AS.get(found.label or "", ())
        if any(name in already for name in done_by_tool):
            # The hands already did this one. Doing it again is the duplicate
            # message he photographed.
            return
        lines: list[str] = []
        for line in found.lines:
            if line in {"help", "commands"}:
                # A menu is not "work", so the console refuses it — but "what can
                # you do" deserves a real answer, counted from the real registry.
                lines.extend(self._capability_lines(language))
                continue
            try:
                reply = await self_console.run(
                    self.runtime,
                    line,
                    chat_id=chat_id,
                    sender_id=int(getattr(self.runtime.permissions, "self_id", None) or 0),
                    language=language,
                    reply_to=reply_to,
                )
            except Exception as exc:  # noqa: BLE001 - a failed hand is a sentence
                self.logger.warning("yura: intent %r failed: %s", line, exc)
                continue
            if getattr(reply, "ok", False):
                ran_any = True
            said = getattr(reply, "said", None) or []
            if isinstance(said, str):
                said = [said]
            if not said:
                said = [getattr(reply, "note", "")]
            for chunk in self._intent_lines([str(item) for item in said]):
                if chunk.startswith("[media"):
                    # The picture itself is the answer; a caption about it is noise.
                    continue
                lines.append(chunk)
                if len(lines) >= 6:
                    break
        if not lines and not ran_any:
            return
        title = self._intent_title(language, found.detail or found.label)
        # Under the tool's own card, never over it: the hand did the work, the
        # sentence is the footnote.
        self._cards = list(self._cards) + [(title, tuple(lines[:6]))]
        self._intent_done.append(found.detail or found.label)

    @staticmethod
    def _intent_lines(said: list[str]) -> list[str]:
        """The useful words of a command's reply, without its own drawing.

        The command already drew a card. Nesting that card inside this one would be
        two frames and two closing glyphs, so the rule lines and the lone glyphs are
        dropped and the facts are kept.
        """
        out: list[str] = []
        for item in said:
            for raw in strip_html(item).splitlines():
                chunk = raw.strip()
                if not chunk:
                    continue
                # A line that is nothing but rules and glyphs is the command's own
                # drawing; this card draws its own.
                ignore = set("ـ-—_.·•~ ") | set(ui.RULE_GLYPHS) | set(ui.CLOSING_GLYPHS)
                if set(chunk) <= ignore:
                    continue
                if len(chunk) <= 2 and not chunk.isalnum():
                    continue
                out.append(chunk[:220])
        return out

    def _capability_lines(self, language: str) -> list[str]:
        registry = self.runtime.registry
        features = self.runtime.feature_manager.names()
        groups = registry.by_category()
        lines = [
            str(
                self.runtime.t(
                    "actions.intent_counts",
                    language,
                    commands=len(registry.visible()),
                    features=len(features),
                )
            )
        ]
        for category, commands in list(groups.items())[:5]:
            names = " ".join(f"{self.runtime.prefix}{command.name}" for command in commands[:6])
            lines.append(f"{category}: {names}")
        return lines

    def _intent_title(self, language: str, detail: str) -> str:
        try:
            return str(self.runtime.t("actions.intent_title", language, what=detail))
        except Exception:  # pragma: no cover - a stripped runtime has no locales
            return detail

    def _receipt(self, language: str) -> str:
        """The card under her answer: what ran, and the minute it was asked for."""
        if self._cards:
            title, lines = self._cards[0]
            return ui.card(
                title or self._tool_title(language),
                lines,
                icon="◈",
                footer=self._stamp(language),
            )
        if not self._receipts:
            return ""
        did = " · ".join(dict.fromkeys(self._receipts))
        stamp = ""
        try:
            stamp = to_local(utc_now(), self.runtime.config.timezone).strftime("%H:%M")
        except Exception:  # pragma: no cover - a bad zone must not eat a receipt
            stamp = ""
        try:
            return str(self.runtime.t("actions.receipt", language, did=did, when=stamp)).strip()
        except Exception:  # pragma: no cover - no locales in a stripped runtime
            return f"⌁ {did} · {stamp}".strip()

    def _tool_title(self, language: str) -> str:
        try:
            return str(self.runtime.t("actions.card_title", language))
        except Exception:  # pragma: no cover
            return "Tool called"

    def _stamp(self, language: str) -> str:
        try:
            when = to_local(utc_now(), self.runtime.config.timezone).strftime("%H:%M")
        except Exception:  # pragma: no cover
            when = ""
        try:
            return str(self.runtime.t("actions.card_footer", language, when=when))
        except Exception:  # pragma: no cover
            return f"asked at {when}"

    def _with_receipt(self, text: str, language: str) -> str:
        line = self._receipt(language)
        return f"{text}\n\n{line}" if line else text

    async def _with_hands(
        self,
        messages: list[AIMessage],
        *,
        budget: int,
        chat_id: int,
        language: str,
        who: Any = None,
        owner: bool = True,
        person: Any = None,
        reply_to: int | None = None,
        message_id: int | None = None,
    ) -> Any:
        """Run the tool loop, or return ``None`` when there is nothing to run.

        ``None`` is the common case and it is deliberate: no tool catalogue means
        the turn goes down the old path, untouched.
        """
        agent = self.agent
        if agent is None or not owner or not await self.agent_enabled():
            return None
        actor = (
            getattr(person, "user_id", None)
            or getattr(who, "user_id", None)
            or getattr(self.runtime.permissions, "self_id", None)
            or 0
        )
        turn = Turn(
            chat_id=int(chat_id),
            actor_id=int(actor or 0),
            language=language,
            is_owner=owner,
            # "react to it", "delete that": the message he pointed at, not the
            # sentence he typed. Without this, every aimed verb missed.
            reply_to=int(reply_to) if reply_to else None,
            # ...and when he pointed at nothing, his own line is the target.
            message_id=int(message_id) if message_id else None,
        )
        self.last_turn = turn
        return await agent.run(self.provider, messages, turn=turn, max_tokens=budget)

    def _ask_permission(self, step: Any, *, chat_id: int, language: str) -> Answer:
        """The question that replaces an answer when a hand needs a yes."""
        outcome = getattr(step, "waiting", None)
        tool = outcome.tool if outcome is not None else ""
        layer = self.agent_layer
        detail = ""
        if layer is not None:
            record = None
            try:
                record = layer.get(tool)
            except Exception:  # pragma: no cover
                record = None
            detail = record.signature() if record is not None else tool
        prefix = ""
        try:
            prefix = self.runtime.prefix
        except Exception:  # pragma: no cover
            prefix = "."
        try:
            text = ui.card(
                str(self.runtime.t("actions.card_confirm_title", language)),
                [
                    str(self.runtime.t("actions.card_confirm_what", language, what=detail or tool)),
                    str(self.runtime.t("actions.card_confirm_how", language, prefix=prefix)),
                ],
                icon="◈",
                footer=str(self.runtime.t("actions.card_confirm_window", language)),
            )
        except Exception:  # pragma: no cover
            text = f"may I? ({detail}) — {prefix}yura yes / {prefix}yura no"
        self.turns += 1
        return Answer(text=text, source="action", rendered=True)

    async def _apply(
        self,
        reply: str,
        *,
        chat_id: int,
        language: str,
        incoming: str = "",
        worn: set[str] | None = None,
        worn_faces: set[str] | None = None,
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
        # A repeat of an ornament she used a moment ago is swapped for a fresh one
        # of the same kind — that, together with the brief above, is why the same
        # four sparkles stop appearing under every message.
        visible = glyphs.restyle(visible, worn)
        # ...and the same for the face she wears: three answers in a row with ᵕ̈ is
        # the exact thing that made her look like a stamp.
        visible = glyphs.restyle_faces(visible, worn_faces)
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


