"""Yura: the e-kitten who answers in your own chat.

There is no scenario engine here any more. A bare ``yura`` / ``یورا`` gives a
random line from :data:`havoc.persona.LINES`; anything else you type in your own
chat goes to the model with her memory attached
(:mod:`havoc.services.yura_chat`), and she answers like herself.

Two paths, both owner-only
--------------------------
* the **command** ``.yura`` — works even with the ``persona`` switch off, because
  it is a direct request;
* the **ambient reply** — every non-command message the owner sends in their own
  Saved Messages, when the ``persona`` switch is on. Nobody else is ever
  answered, and nothing is ever sent into another chat.

Feature areas: 12 (personality), 21 (messaging), 70 (AI), 96 (feature switches).
"""

from __future__ import annotations

import time
from typing import Any

from .. import identity, persona, ui
from ..command_registry import Command, CommandContext, CommandError
from ..event_engine import EventType, NormalizedEvent
from ..utils.formatting import escape
from ..utils.security import redact_text
from ..utils.text import truncate

#: A chat companion cannot wait 45 seconds between sentences. Short cooldown
#: only, to swallow double-sends and update loops.
MIN_INTERVAL_SECONDS = 2.0
#: Safety valve, not a conversation budget: a stuck loop cannot flood forever.
MAX_PER_HOUR = 300

#: Commands that belong to the dispatcher — never answered by the persona path.
_INTERNAL_WORDS = frozenset({"yura", "یورا", "stop", "بسه", "off"})


def _invocation_text(ctx: CommandContext) -> str:
    """What the owner actually typed, before the parser threw it away.

    A bare command is rebuilt as ``.yura`` before dispatch, so the Persian word
    that triggered it only survives on the event itself.
    """
    event = getattr(ctx, "event", None)
    message = getattr(event, "message", None) or getattr(ctx, "message", None)
    for attribute in ("raw_text", "message", "text"):
        value = getattr(message, attribute, None)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _language_for(ctx: CommandContext, text: str, probe: str = "") -> str:
    """The language to answer in, read from what the owner typed.

    The detector gives up on a single word like ``یورا`` or ``hi`` and falls back
    to the chat default, which used to answer an English ``hi`` in Persian. The
    typed words are the better witness: the command name itself is stripped
    first, because ``.yura`` is Latin in both languages.
    """
    tail = (text or "").strip()
    if not tail:
        tail = _invocation_tail(ctx)
    if any("\u0600" <= char <= "\u06ff" for char in tail):
        return "fa"
    if any("a" <= char.lower() <= "z" for char in tail):
        return "en"
    # No letters at all (``.yura`` alone): fall back to the original invocation,
    # so a bare ``یورا`` still answers in Farsi.
    original = _invocation_text(ctx)
    if any("\u0600" <= char <= "\u06ff" for char in original):
        return "fa"
    return ctx.language


def _invocation_tail(ctx: CommandContext) -> str:
    """The message minus the prefix and the command word."""
    raw = _invocation_text(ctx).strip()
    if ctx.runtime.prefix and raw.startswith(ctx.runtime.prefix):
        raw = raw[len(ctx.runtime.prefix):]
    parts = raw.split()
    return " ".join(parts[1:]) if len(parts) > 1 else ""


def _name(language: str) -> str:
    return identity.display(language)


async def _card(ctx: CommandContext) -> str:
    runtime = ctx.runtime
    enabled = await runtime.feature_manager.is_enabled("persona")
    facts = await runtime.memory.count(ctx.chat_id)
    provider = runtime.ai
    chat = getattr(runtime, "yura_chat", None)
    rows = [
        f"{ui.bold(ctx.t('yura.state'))} {ui.state(enabled, on=ctx.t('common.on'), off=ctx.t('common.off'))}",
        f"{ui.bold(ctx.t('yura.mode'))} "
        + ui.note(
            escape(
                ctx.t("yura.mode_model", model=getattr(provider, "active_model", "") or provider.model)
                if getattr(provider, "available", False)
                else ctx.t("yura.mode_offline")
            )
        ),
        f"{ui.bold(ctx.t('yura.memory_count'))} {ui.code(str(facts))}",
        f"{ui.bold(ctx.t('yura.turns'))} {ui.code(str(getattr(chat, 'turns', 0)))}",
        f"{ui.bold(ctx.t('yura.bank'))} {ui.code(str(persona.capacity()))}",
    ]
    if chat is not None and chat.fallbacks:
        rows.append(
            f"{ui.bold(ctx.t('yura.fallbacks'))} {ui.code(str(chat.fallbacks))}"
            + (f" {ui.note(escape(truncate(chat.last_error, 120)))}" if chat.last_error else "")
        )
    return "\n\n".join(
        [
            ui.title("♡", ctx.t("yura.card_title", name=escape(_name(ctx.language)))),
            ui.note(escape(identity.tagline(ctx.language))),
            ui.panel(rows),
            ui.footer(ctx.t("yura.card_hint", prefix=runtime.prefix)),
        ]
    )


async def _set_enabled(ctx: CommandContext, enabled: bool) -> None:
    await ctx.store.set_feature("persona", enabled)
    await ctx.runtime.feature_manager.refresh()
    await ctx.reply_ok(
        ctx.t(
            "yura.switched",
            name=escape(_name(ctx.language)),
            state=ctx.t("common.on" if enabled else "common.off"),
        )
    )
    if enabled:
        await ctx.reply(ctx.t("yura.enabled_hint", prefix=ctx.runtime.prefix))


def _memory_card(ctx: CommandContext, rows: list[Any]) -> str:
    if not rows:
        return "\n\n".join(
            [
                ui.title("❥", ctx.t("yura.memory_header")),
                ui.note(ctx.t("yura.memory_empty", prefix=ctx.runtime.prefix)),
            ]
        )
    lines = [
        f"{ui.code(str(memory.id))} {escape(truncate(memory.line(), 120))} {ui.note(memory.kind)}"
        for memory in rows
    ]
    return "\n\n".join(
        [
            ui.title("❥", ctx.t("yura.memory_header")),
            ui.panel(lines),
            ui.footer(ctx.t("yura.memory_hint", prefix=ctx.runtime.prefix)),
        ]
    )


async def _memory_command(ctx: CommandContext, words: list[str]) -> None:
    store = ctx.runtime.memory
    action = words[0].lower() if words else "list"
    rest = " ".join(words[1:]).strip()

    if action in {"list", "ls", "all", ""}:
        await ctx.reply(_memory_card(ctx, await store.list(ctx.chat_id)))
        return
    if action in {"clear", "wipe", "reset"}:
        removed = await store.clear(ctx.chat_id)
        await ctx.reply_ok(ctx.t("yura.memory_cleared", count=removed))
        return
    if action in {"add", "remember", "learn", "save", "یادم", "یادمباشه"}:
        fact = rest or " ".join(words[1:]).strip()
        if not fact:
            raise CommandError("yura.memory_needs_text", prefix=ctx.runtime.prefix)
        stored = await ctx.runtime.yura_chat.learn(ctx.chat_id, fact)
        await ctx.reply_ok(
            ctx.t("yura.memory_saved" if stored else "yura.memory_known", fact=escape(truncate(fact, 120)))
        )
        return
    if action in {"forget", "delete", "del", "فراموش"}:
        if not rest:
            raise CommandError("yura.memory_needs_text", prefix=ctx.runtime.prefix)
        if rest.lstrip("#").isdigit():
            removed = await store.forget(ctx.chat_id, int(rest.lstrip("#")))
            await ctx.reply_ok(ctx.t("yura.memory_forgot_one", count=int(bool(removed))))
            return
        removed = await store.forget_matching(ctx.chat_id, rest)
        await ctx.reply_ok(ctx.t("yura.memory_forgot", count=removed))
        return

    # Anything else after "memory" is a fact to keep: ``.yura memory I am 27``.
    fact = " ".join(words).strip()
    stored = await ctx.runtime.yura_chat.learn(ctx.chat_id, fact)
    await ctx.reply_ok(
        ctx.t("yura.memory_saved" if stored else "yura.memory_known", fact=escape(truncate(fact, 120)))
    )


async def _key_command(ctx: CommandContext, words: list[str]) -> str:
    """``.yura key <key>`` — store the provider key without publishing it.

    The message that carried the key is deleted immediately when the account is
    allowed to delete its own messages; the confirmation never echoes it. This
    is the whole reason the key lives in the database: a repository secret needs
    someone to open GitHub, and a committed key is refused by push protection.
    """
    runtime = ctx.runtime
    value = " ".join(word for word in words if word).strip()
    chat = getattr(runtime, "yura_chat", None)
    if not value:
        stored = bool(await ctx.store.get_setting("ai.api_key", None))
        state = ctx.t("common.yes") if stored else ctx.t("common.no")
        answer = ctx.t("yura.key_state", state=state, prefix=runtime.prefix)
        if stored and chat is not None and chat.last_error:
            # The card says "live model" from configuration alone; this line says
            # what actually happened the last time she tried to speak.
            answer += "\n" + ui.note(escape(truncate(chat.last_error, 160)))
        return answer

    from ..services.ai_router import AIRouter

    if isinstance(runtime.ai, AIRouter):
        # The rotating setup takes the key into its rotation instead of replacing
        # it: one more credential, billed and health-checked like the others.
        credential = await runtime.ai.add_key("auto", value)
        provider = credential.provider
    else:
        await ctx.store.set_setting("ai.api_key", value)
        from ..services.ai_provider import AIProvider

        runtime.ai = AIProvider(
            base_url=runtime.config.ai_base_url,
            api_key=value,
            model=runtime.config.ai_model,
            timeout=runtime.config.ai_timeout,
            enabled=True,
        )
        provider = "provider"
    try:
        await runtime.sender.delete(ctx.chat_id, ctx.message.id)
    except Exception as exc:  # noqa: BLE001 - a failed delete must not lose the key
        runtime.logger.warning("yura: could not delete the key message: %s", exc)
    # Try it once, now, and say what happened. "Saved" is not "working".
    worked, detail = await runtime.ai.probe()
    if chat is not None:
        chat.last_error = "" if worked else detail
    model = getattr(runtime.ai, "active_model", "") or runtime.config.ai_model
    if worked:
        return ctx.t("yura.key_saved", model=f"{provider} · {model}") + "\n" + ui.note(
            escape(ctx.t("yura.key_verified", reply=detail))
        )
    return ctx.t("yura.key_saved", model=f"{provider} · {model}") + "\n" + ui.note(
        escape(ctx.t("yura.key_unverified", error=truncate(detail, 160)))
    )


async def _chat(ctx: CommandContext, text: str, *, greeting: bool = False) -> None:
    probe = _invocation_text(ctx) or " ".join(filter(None, [ctx.sub or "", *ctx.args]))
    language = _language_for(ctx, text, probe)
    answer = await ctx.runtime.yura_chat.answer(
        text, chat_id=ctx.chat_id, language=language, greeting=greeting
    )
    if answer.text:
        await ctx.reply(escape(answer.text))
    for path in answer.files:
        await ctx.runtime.sender.send_media(
            ctx.chat_id,
            str(path),
            caption=escape(truncate(path.name, 90)),
        )


async def cmd_yura(ctx: CommandContext) -> None:
    action = (ctx.sub or "").lower()
    words = [word for word in ctx.args if word.strip()]
    if not action and words:
        action = words[0].lower()

    if action in {"", "me", "hi", "hey", "salam", "سلام"}:
        # A bare call is a random line — never a menu, never a scenario list.
        await _chat(ctx, "", greeting=True)
        return
    if action in {"on", "enable", "off", "disable"}:
        await _set_enabled(ctx, action in {"on", "enable"})
        return
    if action in {"status", "card", "who", "state"}:
        await ctx.reply(await _card(ctx))
        return
    if action in {"memory", "memories", "حافظه"}:
        await _memory_command(ctx, words)
        return
    if action in {"remember", "learn"}:
        await _memory_command(ctx, ["add", *words])
        return
    if action == "forget":
        await _memory_command(ctx, ["forget", *words])
        return
    if action in {"key", "apikey", "token"}:
        await ctx.reply(await _key_command(ctx, words))
        return
    if action in {"log", "history"}:
        transcript = await ctx.runtime.memory.transcript(
            ctx.chat_id, await ctx.runtime.memory.history(ctx.chat_id)
        )
        await ctx.reply(escape(transcript) if transcript else ctx.t("yura.log_empty"))
        return
    if action == "clear":
        removed = await ctx.runtime.memory.clear_history(ctx.chat_id)
        await ctx.reply_ok(ctx.t("yura.log_cleared", count=removed))
        return

    # Everything else is something you said to her.
    text = " ".join(words).strip()
    await _chat(ctx, text)


class PersonaReplies:
    """The ambient path: your own messages, your own chat, her voice.

    Gates, in order — every one must pass:

    1. the message is outgoing (you typed it),
    2. it is in your own Saved Messages chat,
    3. it is not a command (the dispatcher already answered those),
    4. the ``persona`` feature is enabled,
    5. :meth:`Permissions.can_act` allows it (emergency stop, quiet hours,
       private-chat policy),
    6. the short cooldown and hourly valve are intact.
    """

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.logger = runtime.logger
        # None, not 0.0: monotonic() has no defined origin and can be smaller
        # than the cooldown on a fresh container.
        self._last_at: float | None = None
        self._window: list[float] = []
        self.answered = 0
        self.skipped = 0

    @property
    def self_id(self) -> int | None:
        return self.runtime.permissions.self_id

    def _budget_ok(self, now: float) -> bool:
        if self._last_at is not None and now - self._last_at < MIN_INTERVAL_SECONDS:
            return False
        cutoff = now - 3600
        self._window = [stamp for stamp in self._window if stamp >= cutoff]
        return len(self._window) < MAX_PER_HOUR

    def _is_command(self, text: str) -> bool:
        stripped = text.strip()
        if self.runtime.prefix and stripped.startswith(self.runtime.prefix):
            return True
        first = stripped.split(maxsplit=1)[0].lower() if stripped else ""
        return first in _INTERNAL_WORDS

    async def on_self_message(self, event: NormalizedEvent) -> None:
        if not event.is_outgoing or not event.text.strip():
            return
        self_id = self.self_id
        if self_id is None or event.chat_id != self_id:
            self.skipped += 1
            return
        if (
            persona.is_own_line(event.text)
            or self.runtime.yura_chat.was_sent(event.chat_id, event.text)
            or self._is_command(event.text)
        ):
            self.skipped += 1
            return
        decision = await self.runtime.permissions.can_act(event.chat_id, "persona", is_private=True)
        if not decision.allowed:
            self.skipped += 1
            return
        now = time.monotonic()
        if not self._budget_ok(now):
            self.skipped += 1
            return

        language = await self._language(event)
        answer = await self.runtime.yura_chat.answer(
            event.text, chat_id=event.chat_id, language=language
        )
        self._last_at = now
        self._window.append(now)
        self.answered += 1
        self.logger.info(
            "yura: answering '%s' in Saved Messages", redact_text(truncate(event.text, 40))
        )
        if answer.text:
            await self.runtime.sender.send(event.chat_id, escape(answer.text))
        for path in answer.files:
            await self.runtime.sender.send_media(
                event.chat_id, str(path), caption=escape(truncate(path.name, 90))
            )

    async def _language(self, event: NormalizedEvent) -> str:
        if event.language in {"en", "fa"}:
            return event.language
        owner = self.runtime.permissions.self_id
        if owner:
            stored = await self.runtime.store.get_language("user", str(owner))
            if stored in {"en", "fa"}:
                return stored
        return self.runtime.config.default_language


def register(runtime: Any) -> None:
    runtime.registry.register(
        Command(
            "yura",
            cmd_yura,
            category="persona",
            aliases=("person", "self"),
            usage="[on|off|status|memory|log|clear|<anything>]",
            changes_state=True,
            subcommands=(
                "on", "off", "enable", "disable", "status", "card", "who", "state",
                "memory", "memories", "remember", "learn", "forget", "log", "history", "clear",
                "key", "apikey", "token",
            ),
        )
    )
    replies = PersonaReplies(runtime)
    runtime.persona_replies = replies
    runtime.event_engine.subscribe(
        "persona.self_message",
        replies.on_self_message,
        {EventType.OUTGOING},
        priority=40,
        description="Yura answers the owner's own messages",
    )
