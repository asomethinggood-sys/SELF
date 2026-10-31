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

import random
import time
from collections import Counter, deque
from dataclasses import dataclass
from typing import Any

from .. import identity, persona, ui
from ..command_registry import Command, CommandContext, CommandError
from ..event_engine import EventType, NormalizedEvent, normalize_message
from ..services import lessons as lessons_service
from ..services import people as people_service
from ..services import yura_chat as yura_chat_module
from ..services.context import build as build_context
from ..services.vision import IMAGE_MEDIA_TYPES, Picture, looks_like_picture
from ..services.vision import download as download_picture
from ..utils.formatting import escape, strip_html
from ..utils.markup import rich
from ..utils.mood import mood_line
from ..utils.security import redact_text
from ..utils.text import truncate
from ..utils.time import to_local, utc_now
from . import yura_extras

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
    replies = getattr(runtime, "persona_replies", None)
    style = await replies._reply_style(ctx.chat_id) if replies is not None else "thread"
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
        f"{ui.bold(ctx.t('yura.reply_row'))} {ui.code(style)}",
    ]
    if replies is not None and replies.last_skip:
        rows.append(
            f"{ui.bold(ctx.t('yura.last_skip'))} {ui.code(escape(replies.last_skip))}"
            + f" {ui.note(escape(ctx.t('yura.skip_note', count=replies.skipped)))}"
        )
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
        # A reply to someone means the fact is about *them*: "remember she is my
        # gf" said over her message belongs in her row, not in this chat's list.
        target, target_name = await _who(ctx, [])
        if target is not None:
            kept = await ctx.runtime.people.note(target, fact)
            person = await ctx.runtime.people.get(target)
            who = escape(((person.name if person and person.name else "") or target_name) or str(target))
            key = "yura.note_saved" if kept else "yura.note_known"
            await ctx.reply_ok(ctx.t(key, name=who, fact=escape(truncate(fact, 120))))
            return
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


async def _replied_to(ctx: CommandContext) -> Any:
    """The message this command is a reply to, if any (never raises)."""
    message = getattr(ctx, "message", None)
    getter = getattr(message, "get_reply_message", None)
    if getter is None:
        return None
    try:
        return await getter()
    except Exception:  # noqa: BLE001 - a deleted target is not an error
        return None


async def _who(ctx: CommandContext, words: list[str]) -> tuple[int | None, str]:
    """Which person a command is about.

    In order: the message it replies to, an ``@username`` (resolved through the
    account, which is the only way to turn a name into an id), a name already in
    her book, and finally whoever is speaking. Resolution failures are returned
    as "no target", never as an exception — a command that cannot find a person
    should say so, not crash the dispatcher.
    """
    replied = await _replied_to(ctx)
    if replied is not None:
        user_id = getattr(replied, "sender_id", None)
        if user_id and not ctx.runtime.permissions.is_owner(user_id):
            sender = getattr(replied, "sender", None)
            name = getattr(sender, "first_name", "") if sender is not None else ""
            return int(user_id), str(name or "")

    query = " ".join(word for word in words if word.strip()).strip()
    if query:
        handle = query.split()[0].lstrip("@")
        client = getattr(ctx.runtime, "client", None)
        if query.startswith("@") and client is not None:
            try:
                entity = await client.get_entity(handle)
                user_id = getattr(entity, "id", None)
                if user_id:
                    name = getattr(entity, "first_name", "") or handle
                    return int(user_id), str(name)
            except Exception:  # noqa: BLE001 - an unknown handle is a normal answer
                pass
        matches = await ctx.runtime.people.search(handle)
        if matches:
            return matches[0].user_id, matches[0].name

    speaker = getattr(getattr(ctx, "message", None), "sender_id", None)
    if speaker and not ctx.runtime.permissions.is_owner(speaker):
        return int(speaker), ""
    return None, ""


async def _tone_command(ctx: CommandContext, tone: str, words: list[str]) -> None:
    """``.yura nice`` / ``.yura rude`` / ``.yura neutral`` — how to treat someone."""
    user_id, name = await _who(ctx, words)
    if user_id is not None and ctx.runtime.permissions.is_owner(user_id):
        user_id = None
    if user_id is None:
        raise CommandError("yura.tone_needs_target", prefix=ctx.runtime.prefix)
    await ctx.runtime.people.set_tone(user_id, tone)
    person = await ctx.runtime.people.get(user_id)
    who = escape((person.name if person and person.name else name) or str(user_id))
    await ctx.reply_ok(
        ctx.t("yura.tone_set", name=who, tone=escape(ctx.t(f"yura.tone_{tone}")))
    )


async def _note_command(ctx: CommandContext, words: list[str]) -> None:
    """``.yura note she's my gf`` — keep a fact about the person replied to."""
    fact = " ".join(words).strip()
    if not fact:
        raise CommandError("yura.note_needs_text", prefix=ctx.runtime.prefix)
    user_id, name = await _who(ctx, [])
    if user_id is None:
        raise CommandError("yura.note_needs_target", prefix=ctx.runtime.prefix)
    stored = await ctx.runtime.people.note(user_id, fact)
    person = await ctx.runtime.people.get(user_id)
    who = escape((person.name if person and person.name else name) or str(user_id))
    key = "yura.note_saved" if stored else "yura.note_known"
    await ctx.reply_ok(ctx.t(key, name=who, fact=escape(truncate(fact, 120))))


async def _dm_command(ctx: CommandContext, words: list[str]) -> None:
    """``.yura dm on|off`` — may she answer this person's private messages?"""
    mode = (words[0].lower() if words else "")
    user_id, name = await _who(ctx, words[1:] if mode in {"on", "off", "yes", "no"} else words)
    if user_id is None or ctx.runtime.permissions.is_owner(user_id):
        raise CommandError("yura.tone_needs_target", prefix=ctx.runtime.prefix)
    person = await ctx.runtime.people.get(user_id)
    enabled = mode in {"on", "yes", "true", "1"} or (
        mode not in {"off", "no", "false", "0"} and not (person and person.dm)
    )
    await ctx.runtime.people.set_dm(user_id, enabled)
    person = await ctx.runtime.people.get(user_id)
    who = escape((person.name if person and person.name else name) or str(user_id))
    key = "yura.dm_on" if enabled else "yura.dm_off"
    await ctx.reply_ok(ctx.t(key, name=who))


async def _teach_command(ctx: CommandContext, words: list[str]) -> None:
    """``.yura teach don't call me that`` — a correction she keeps."""
    rule = " ".join(words).strip()
    if not rule:
        raise CommandError("yura.teach_needs_text", prefix=ctx.runtime.prefix)
    user_id, name = await _who(ctx, [])
    scope = "person" if user_id is not None else "global"
    lesson = await ctx.runtime.lessons.add(rule, scope=scope, subject_id=user_id)
    if lesson is None:
        await ctx.reply_ok(ctx.t("yura.teach_known", rule=escape(truncate(rule, 120))))
        return
    person = await ctx.runtime.people.get(user_id) if user_id is not None else None
    who = escape((person.name if person and person.name else name) or "") if user_id else ""
    key = "yura.teach_saved_person" if user_id is not None else "yura.teach_saved"
    await ctx.reply_ok(ctx.t(key, rule=escape(truncate(rule, 120)), name=who))


async def _rules_command(ctx: CommandContext, words: list[str]) -> None:
    """``.yura rules`` / ``.yura unteach <#id|phrase>``."""
    action = (words[0].lower() if words else "").strip()
    if action in {"forget", "delete", "remove", "unteach", "drop"}:
        removed = await ctx.runtime.lessons.remove(" ".join(words[1:]))
        await ctx.reply_ok(ctx.t("yura.rules_forgot", count=removed))
        return
    rows = await ctx.runtime.lessons.all(20)
    if not rows:
        await ctx.reply(ctx.t("yura.rules_empty"))
        return
    people = {}
    lines = []
    for lesson in rows:
        tag = ""
        if lesson.scope == "person":
            person = await ctx.runtime.people.get(lesson.subject_id)
            people[lesson.subject_id] = person
            tag = (person.name if person and person.name else str(lesson.subject_id))
        broken = f" ⚠{lesson.times_broken}" if lesson.times_broken else ""
        head = ui.code(f"#{lesson.id}")
        if tag:
            head += " " + ui.note(escape(tag))
        lines.append(f"{head} {escape(lesson.rule)}{broken}")
    await ctx.reply(
        "\n\n".join(
            [
                ui.title("❦", ctx.t("yura.rules_title", count=await ctx.runtime.lessons.count())),
                ui.panel(lines),
                ui.footer(ctx.t("yura.rules_hint", prefix=ctx.runtime.prefix)),
            ]
        )
    )


async def _people_command(ctx: CommandContext) -> None:
    people = await ctx.runtime.people.all(20)
    if not people:
        await ctx.reply(ctx.t("yura.people_empty"))
        return
    rows = []
    for person in people:
        bits = [ui.bold(escape(person.name or str(person.user_id)))]
        if person.relation:
            bits.append(ui.note(escape(person.relation)))
        bits.append(ui.code(escape(person.tone)))
        if person.facts:
            bits.append(ui.note(f"{len(person.facts)} ✎"))
        rows.append(" ".join(bits))
    await ctx.reply(
        "\n\n".join(
            [
                ui.title("♡", ctx.t("yura.people_title", count=await ctx.runtime.people.count())),
                ui.panel(rows),
                ui.footer(ctx.t("yura.people_hint", prefix=ctx.runtime.prefix)),
            ]
        )
    )


async def _person_command(ctx: CommandContext, words: list[str]) -> None:
    user_id, name = await _who(ctx, words)
    if user_id is None:
        await ctx.reply(ctx.t("yura.person_unknown", prefix=ctx.runtime.prefix))
        return
    person = await ctx.runtime.people.get(user_id)
    if person is None:
        await ctx.reply(ctx.t("yura.person_unknown", prefix=ctx.runtime.prefix))
        return
    rows = [
        f"{ui.bold(ctx.t('yura.person_tone'))} {ui.code(escape(person.tone))}",
    ]
    if person.relation:
        rows.append(f"{ui.bold(ctx.t('yura.person_relation'))} {escape(person.relation)}")
    for fact in person.facts[-10:]:
        rows.append(f"· {escape(fact)}")
    await ctx.reply(
        "\n\n".join(
            [
                ui.title("♡", escape(person.name or str(person.user_id))),
                ui.panel(rows),
                ui.footer(ctx.t("yura.person_hint", prefix=ctx.runtime.prefix)),
            ]
        )
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


async def _reply_command(ctx: CommandContext, action: str, words: list[str]) -> None:
    """``.yura reply on|off`` — does her answer land as a reply to yours?

    Both shapes are legitimate and the owner has asked for each of them at
    different times, so it is a switch he owns instead of a decision in the code.
    """
    replies = getattr(ctx.runtime, "persona_replies", None)
    if replies is None:
        await ctx.reply(ctx.t("yura.reply_usage", prefix=ctx.runtime.prefix))
        return
    word = words[0].lower() if words else ""
    on = {"on", "yes", "true", "thread", "reply", "بله", "روشن", "روشنش کن"}
    off = {"off", "no", "false", "plain", "bubble", "نه", "خاموش", "بدون"}
    if word in on:
        await replies.set_reply_style(ctx.chat_id, "thread")
        await ctx.reply_ok(ctx.t("yura.reply_on"))
        return
    if word in off:
        await replies.set_reply_style(ctx.chat_id, "plain")
        await ctx.reply_ok(ctx.t("yura.reply_off"))
        return
    style = await replies._reply_style(ctx.chat_id)
    await ctx.reply(
        ctx.t("yura.reply_state", style=escape(ctx.t(f"yura.reply_style_{style}")))
    )


def _local_hour(runtime: Any) -> int:
    try:
        return to_local(utc_now(), runtime.config.timezone).hour
    except Exception:  # pragma: no cover - a bad zone must not break a reply
        return 12


def _visible(answer: Any) -> str:
    """Telegram HTML for an answer.

    Only a model writes markdown. The offline bank is hand-made and ends lines
    with kaomoji (``╰(*´︶`*)╯``), which must arrive exactly as authored — so model
    text is rendered, everything else is only escaped.
    """
    if getattr(answer, "rendered", False):
        # A card is built from ui.* helpers, so it is already HTML and already
        # escaped. Running it through ``rich`` printed the tags to the owner.
        return answer.text
    if answer.source == "model":
        return rich(answer.text)
    return escape(answer.text)


def _normalized_for(ctx: CommandContext) -> Any:
    """The command's message as a normal event, so the reply path can read it."""
    message = getattr(ctx, "message", None)
    if message is None:
        return None
    try:
        return normalize_message(
            message,
            self_id=ctx.runtime.permissions.self_id,
            prefix=ctx.runtime.prefix,
            event_type=EventType.MESSAGE,
        )
    except Exception:  # noqa: BLE001 - a missing message is not an error
        return None


async def _chat(ctx: CommandContext, text: str, *, greeting: bool = False) -> None:
    probe = _invocation_text(ctx) or " ".join(filter(None, [ctx.sub or "", *ctx.args]))
    language = _language_for(ctx, text, probe)
    # `.yura` on a photo does what a caption-less call does: she looks. The
    # ambient path handles the prefix-less case; the command should not be the
    # one place where a picture is ignored.
    message = getattr(ctx, "message", None)
    picture = None
    if message is not None and looks_like_picture(getattr(message, "media", None)):
        picture = await download_picture(ctx.runtime.client, message)
    # The command path is a conversation too: in a group it needs the same room
    # context as the automatic one, or "yura what do you think" about a message
    # above is a question about nothing.
    quoted, audience = "", None
    normalized = _normalized_for(ctx)
    if normalized is not None:
        try:
            replies = ctx.runtime.persona_replies
            quoted = await replies.quote_for(normalized)
            audience = await replies.room_for(normalized)
        except Exception:  # noqa: BLE001 - context is a bonus
            quoted, audience = "", None
    reply_id = getattr(normalized, "reply_to_id", None) if normalized is not None else None
    answer = await ctx.runtime.yura_chat.answer(
        text,
        chat_id=ctx.chat_id,
        language=language,
        greeting=greeting,
        picture=picture,
        quoted=quoted,
        audience=audience,
        reply_to=reply_id,
    )
    if answer.text:
        # `rich` for model text, not `escape`: she writes **bold** and > quotes
        # because that is how a model writes, and escaping them shipped the
        # asterisks.
        #
        # ``reply=False``: a Telegram reply bubble carries the message it answers
        # above it. Calling her by name is a normal message, so answering it as a
        # reply put the caller's own words back on top of every answer — "I talk
        # to an AI to get a response, not to get my prompt back".
        style = "thread"
        try:
            style = await ctx.runtime.persona_replies._reply_style(ctx.chat_id)
        except Exception:  # noqa: BLE001 - a missing switch means the default
            style = "thread"
        await ctx.reply(_visible(answer), reply=style == "thread")
        # Remember that these words are hers: a reply to this message must be
        # recognised as a reply to *her* even after a restart.
        try:
            await ctx.runtime.persona_replies._note_said(
                ctx.chat_id, strip_html(_visible(answer))
            )
        except Exception:  # noqa: BLE001 - bookkeeping must never lose an answer
            pass
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
    if action in {"reply", "thread", "plain", "quote", "bubble"}:
        await _reply_command(ctx, action, words)
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
    if action in {"recap", "today"}:
        await yura_extras.recap(ctx)
        return
    if action in {"diary", "journal"}:
        await yura_extras.diary(ctx)
        return
    if action in {"faces", "face", "kaomoji"}:
        await yura_extras.faces(ctx, list(words))
        return
    if action in {"speak", "voice-note", "say-aloud"}:
        # `rest_raw`, not `raw_args`: the subcommand itself is not something to say.
        await yura_extras.say(ctx, (ctx.rest_raw or "").strip())
        return
    if action in {"voice", "tts"}:
        await yura_extras.voice(ctx, [word for word in words if word.strip()])
        return
    if action in {"poster", "card-art"}:
        await yura_extras.poster(ctx, list(words))
        return
    if action in {"caption", "look-again"}:
        await yura_extras.caption(ctx)
        return
    if action in {"react", "reaction"}:
        await yura_extras.react(ctx, list(words))
        return
    if action in {"nice", "kind", "polite"}:
        await _tone_command(ctx, "nice", list(words))
        return
    if action in {"rude", "mean", "savage"}:
        await _tone_command(ctx, "rude", list(words))
        return
    if action in {"neutral", "normal", "tone"}:
        await _tone_command(ctx, "neutral", list(words))
        return
    if action in {"note", "about", "facts"}:
        await _note_command(ctx, list(words))
        return
    if action in {"dm", "privates", "dms"}:
        await _dm_command(ctx, list(words))
        return
    if action in {"teach", "learn-this", "lesson"}:
        await _teach_command(ctx, list(words))
        return
    if action in {"rules", "lessons", "unteach"}:
        wait = list(words)
        if action == "unteach":
            wait = ["forget", *wait]
        await _rules_command(ctx, wait)
        return
    if action in {"people", "humans", "known"}:
        await _people_command(ctx)
        return
    if action in {"person", "them", "whois"}:
        await _person_command(ctx, list(words))
        return
    if action in {"agent", "hands", "tools", "abilities"}:
        await _agent_command(ctx, action, words)
        return
    if action in {"actions", "did", "audit"}:
        await _actions_command(ctx, words)
        return
    if action in {"auto", "asking", "permissions", "confirm"}:
        await _auto_command(ctx, words)
        return
    if action in {"yes", "approve", "ok", "بله", "آره"}:
        await _confirm_command(ctx, approve=True)
        return
    if action in {"no", "cancel", "deny", "نه", "لغو"}:
        await _confirm_command(ctx, approve=False)
        return

    # Everything else is something you said to her. `raw_args` is everything
    # after the command word, which is not the same as `args`: the parser takes
    # the *first* word as the subcommand, so "what is the capital" would lose
    # "what" and she would be asked "is the capital of France".
    text = (ctx.raw_args or " ".join(words)).strip()
    await _chat(ctx, text)


async def _agent_command(ctx: CommandContext, action: str, words: list[str]) -> None:
    """`.yura agent on|off` and the two lists that make it legible."""
    layer = getattr(ctx.runtime, "actions", None)
    if layer is None or not layer.tools:
        await ctx.reply(ctx.t("actions.agent_off"))
        return
    if action in {"tools", "abilities"}:
        rows = [
            ui.fact_line("▲", f"{tool.name}({', '.join(tool.properties) or '-'}) — {tool.summary}")
            for tool in layer.offered()
        ]
        await ctx.reply(
            ui.card(
                ctx.t("actions.tools_header").lstrip("⟡ ").strip(),
                (),
                icon="⟡",
            )
            + "\n"
            + "\n".join(rows)
        )
        return
    word = (words[0].lower() if words else "").strip()
    if not word:
        state = (
            ctx.t("settings.state_on")
            if await ctx.runtime.store.get_bool(yura_chat_module.AGENT_SETTING, True)
            else ctx.t("settings.state_off")
        )
        await ctx.reply(ctx.t("actions.agent_state", state=escape(state)))
        return
    enabled = word in {"on", "enable", "yes", "1", "روشن", "بله"}
    await ctx.runtime.store.set_setting(yura_chat_module.AGENT_SETTING, "1" if enabled else "0")
    await ctx.reply(ctx.t("actions.agent_on" if enabled else "actions.agent_off"))


async def _auto_command(ctx: CommandContext, words: list[str]) -> None:
    """`.yura auto on|off` — whether small things still stop to ask.

    Off by default is the old behaviour he complained about; on is the default and
    what he asked for. Deleting asks either way.
    """
    layer = getattr(ctx.runtime, "actions", None)
    if layer is None or not layer.tools:
        await ctx.reply(ctx.t("actions.agent_off"))
        return
    # ``words`` still carries the subcommand itself (``["auto", "off"]``).
    word = next((word.lower() for word in words[1:] if word.strip()), "")
    if word in {"on", "enable", "yes", "1", "روشن", "بله"}:
        await ctx.store.set_setting("actions.auto", "1")
        await ctx.reply(ctx.t("actions.auto_on"))
        return
    if word in {"off", "disable", "no", "0", "خاموش", "نه"}:
        await ctx.store.set_setting("actions.auto", "0")
        await ctx.reply(ctx.t("actions.auto_off"))
        return
    state = ctx.t("settings.state_on") if await layer.is_auto() else ctx.t("settings.state_off")
    await ctx.reply(
        ctx.t("actions.auto_state", state=escape(state), prefix=escape(ctx.runtime.prefix))
    )


async def _confirm_command(ctx: CommandContext, *, approve: bool) -> None:
    """The typed yes/no. A user account cannot attach an inline keyboard."""
    layer = getattr(ctx.runtime, "actions", None)
    if layer is None:
        await ctx.reply(ctx.t("actions.nothing_pending"))
        return
    if await layer.pending(ctx.chat_id) is None:
        await ctx.reply(ctx.t("actions.nothing_pending"))
        return
    if not approve:
        await layer.resolve(ctx.chat_id, approve=False)
        await ctx.reply(ctx.t("actions.cancelled"))
        return
    outcome = await layer.resolve(ctx.chat_id, approve=True)
    key = "actions.done" if outcome.ok else (
        "actions.refused" if outcome.status == "refused" else "actions.failed"
    )
    if key == "actions.refused":
        await ctx.reply(ctx.t("actions.refused"))
        return
    await ctx.reply(
        ctx.t(key, what=escape(outcome.note or outcome.tool or ctx.t("actions.empty")))
    )


async def _actions_command(ctx: CommandContext, words: list[str]) -> None:
    """`.yura actions` — the audit trail, in her handwriting instead of a log."""
    layer = getattr(ctx.runtime, "actions", None)
    rows = await layer.recent(ctx.chat_id, limit=10) if layer is not None else []
    if not rows:
        await ctx.reply(ctx.t("actions.empty"))
        return
    when = str(rows[0].get("created_at", ""))[11:16] if rows else ""
    body = [
        ui.fact_line(
            "◇",
            ctx.t(
                "actions.row",
                id=row.get("id", "—"),
                tool=row.get("tool", "?"),
                status=row.get("status", "?"),
                when=str(row.get("created_at", ""))[11:16],
            ),
        )
        for row in rows
    ]
    await ctx.reply(
        ui.card(
            ctx.t("actions.header").lstrip("⟡ ").strip(),
            (),
            icon="⟡",
            footer=when,
        )
        + "\n"
        + "\n".join(body)
    )


#: Her sent message ids, per chat, persisted so a restart does not erase the fact
#: that the message being replied to was hers.
OWN_IDS_SETTING = "yura.own_ids"
OWN_IDS_LIMIT = 60
#: The text of what she sent, per chat. The id is the proof; this is the second
#: proof, for the case that breaks the first one — a restart between her answer
#: and the reply to it, or a deleted row in the settings table.
SAID_SETTING = "yura.said"
SAID_LIMIT = 60
#: Whether her answer is sent as a Telegram *reply* to the message she answered.
#: A reply links the two bubbles and puts a one-line quote on top; a plain message
#: does not. Both are legitimate, which is why it is a switch and not a decision
#: buried in the code: ``.yura reply on|off``.
REPLY_STYLE_SETTING = "yura.reply_style"
STYLES = ("thread", "plain")


@dataclass
class Quote:
    """The message (chain) being replied to, and where it came from."""

    text: str = ""
    hers: bool = False
    #: True when the replied-to message was written by this account — hers or the
    #: owner's. Replying to it is addressing the account either way.
    from_account: bool = False
    #: Who wrote the message being replied to, so "remember this" can be attached
    #: to the right person instead of to the room.
    user_id: int | None = None
    #: The id of the newest message in the chain. "react to it" means this one.
    message_id: int | None = None


class PersonaReplies:
    """Her two automatic paths, and the gates on each.

    **Saved Messages** — you type, she answers. Gates: it is outgoing, it is your
    own chat, it is not a command, the persona is enabled, :meth:`can_act`
    agrees, and both the short cooldown and hourly valve are intact.

    **A group** — she answers *only* when the message is addressed to her: a
    Telegram reply to one of her messages, her name in the text, or a mention.
    She never starts a conversation there, never answers a message that was not
    aimed at her, and has its own slower budget. A picture is looked at only
    under the same rule, so a group photo is not fed to a model because it
    happened to appear.
    """

    #: What she sends first, so a model answer feels typed rather than materialised.
    TYPING = "…"
    #: Groups get a slower valve than the owner's own chat.
    MIN_GROUP_INTERVAL = 6.0
    MAX_GROUP_PER_HOUR = 40
    #: A private chat with someone who is not the owner: slow, and capped hard.
    #: She is a companion, not a customer-support line.
    MIN_DM_INTERVAL = 20.0
    MAX_DM_PER_HOUR = 12
    #: Every so many answers she puts her attention bar under the message.
    MOOD_EVERY = 3
    #: ...and how often in a group, where it is a flourish and not a footer.
    MOOD_EVERY_GROUP = 5

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.logger = runtime.logger
        # None, not 0.0: monotonic() has no defined origin and can be smaller
        # than the cooldown on a fresh container.
        self._last_at: float | None = None
        self._window: list[float] = []
        self._group_window: dict[int, list[float]] = {}
        self._group_last: dict[int, float] = {}
        self._dm_window: dict[int, list[float]] = {}
        self._dm_last: dict[int, float] = {}
        self.answered = 0
        self.group_answered = 0
        #: Answers in a one-to-one chat with somebody else — his side or theirs.
        self.dm_answered = 0
        self.skipped = 0
        self.reacted = 0
        self._since_mood = 0
        self._since_reaction = 0
        # Message ids she sent, per chat, persisted: a restart between her answer
        # and the owner's reply must not make her forget that the message being
        # replied to was hers. Content is not enough — formatted text loses its
        # marks in the Telegram copy — but the id never lies.
        self._own_ids: dict[int, deque[int]] = {}
        self._said: dict[int, deque[str]] = {}
        self._styles: dict[int, str] = {}
        # Why the last message in a group was not answered. "She ignored me" and
        # "a gate said no" look identical from the chat, and only one is a bug.
        self.last_skip = ""
        self.skips: Counter = Counter()

    @property
    def self_id(self) -> int | None:
        return self.runtime.permissions.self_id

    # -- budgets ---------------------------------------------------------
    def _budget_ok(self, now: float) -> bool:
        if self._last_at is not None and now - self._last_at < MIN_INTERVAL_SECONDS:
            return False
        cutoff = now - 3600
        self._window = [stamp for stamp in self._window if stamp >= cutoff]
        return len(self._window) < MAX_PER_HOUR

    def _group_budget_ok(self, chat_id: int, now: float) -> bool:
        last = self._group_last.get(chat_id)
        if last is not None and now - last < self.MIN_GROUP_INTERVAL:
            return False
        cutoff = now - 3600
        window = [stamp for stamp in self._group_window.get(chat_id, []) if stamp >= cutoff]
        self._group_window[chat_id] = window
        return len(window) < self.MAX_GROUP_PER_HOUR

    def _is_command(self, text: str) -> bool:
        stripped = text.strip()
        if self.runtime.prefix and stripped.startswith(self.runtime.prefix):
            return True
        first = stripped.split(maxsplit=1)[0].lower() if stripped else ""
        return first in _INTERNAL_WORDS

    def _skip(self, reason: str, event: NormalizedEvent | None = None) -> None:
        """Note that a message went unanswered, and why.

        "She ignored me" and "a gate said no" look identical from the chat and
        only one of them is a bug, so the reason is kept and logged — a live
        account is the only place the difference shows.
        """
        self.skipped += 1
        self.skips[reason] += 1
        self.last_skip = reason
        if event is not None:
            self.logger.info(
                "yura: did not answer %s in chat %s (%s)",
                getattr(event, "message_id", "?"),
                getattr(event, "chat_id", "?"),
                reason,
            )

    async def _own_bucket(self, chat_id: int) -> deque[int]:
        bucket = self._own_ids.get(chat_id)
        if bucket is not None:
            return bucket
        stored: Any = None
        try:
            stored = await self.runtime.store.get_setting(f"{OWN_IDS_SETTING}.{chat_id}", None)
        except Exception:  # pragma: no cover - storage trouble must not silence her
            stored = None
        ids = [int(value) for value in (stored or []) if str(value).lstrip("-").isdigit()]
        bucket = deque(ids[-OWN_IDS_LIMIT:], maxlen=OWN_IDS_LIMIT)
        self._own_ids[chat_id] = bucket
        return bucket

    async def _note_own(self, chat_id: int, message_id: Any) -> None:
        if message_id is None:
            return
        ledger = getattr(self.runtime, "sent_ledger", None)
        if ledger is not None:
            await ledger.note(chat_id, message_id)
        bucket = await self._own_bucket(chat_id)
        bucket.append(int(message_id))
        try:
            await self.runtime.store.set_setting(f"{OWN_IDS_SETTING}.{chat_id}", list(bucket))
        except Exception:  # pragma: no cover - see above
            pass

    async def _reply_style(self, chat_id: int) -> str:
        """``thread`` (a Telegram reply) or ``plain``, per chat, defaulting to thread."""
        cache = self._styles.get(chat_id)
        if cache is not None:
            return cache
        style = "thread"
        for key in (f"{REPLY_STYLE_SETTING}.{chat_id}", REPLY_STYLE_SETTING):
            try:
                stored = await self.runtime.store.get_setting(key, None)
            except Exception:  # pragma: no cover - storage trouble must not silence her
                stored = None
            if str(stored or "").lower() in STYLES:
                style = str(stored).lower()
                break
        self._styles[chat_id] = style
        return style

    async def set_reply_style(self, chat_id: int, style: str) -> str:
        """Change it for this chat and remember it."""
        style = style if style in STYLES else "thread"
        self._styles[chat_id] = style
        await self.runtime.store.set_setting(f"{REPLY_STYLE_SETTING}.{chat_id}", style)
        return style

    async def _said_bucket(self, chat_id: int) -> deque[str]:
        bucket = self._said.get(chat_id)
        if bucket is not None:
            return bucket
        stored: Any = None
        try:
            stored = await self.runtime.store.get_setting(f"{SAID_SETTING}.{chat_id}", None)
        except Exception:  # pragma: no cover - storage trouble must not silence her
            stored = None
        lines = [" ".join(str(item).split()) for item in (stored or []) if str(item).strip()]
        bucket = deque(lines[-SAID_LIMIT:], maxlen=SAID_LIMIT)
        self._said[chat_id] = bucket
        return bucket

    async def _note_said(self, chat_id: int, text: str) -> None:
        """Keep the words she sent here, so a reply to them is recognised later."""
        flat = " ".join(str(text or "").split())
        if not flat:
            return
        bucket = await self._said_bucket(chat_id)
        if flat in bucket:
            bucket.remove(flat)
        bucket.append(flat)
        try:
            await self.runtime.store.set_setting(f"{SAID_SETTING}.{chat_id}", list(bucket))
        except Exception:  # pragma: no cover - see above
            pass

    async def _is_own_text(self, chat_id: int, text: str) -> bool:
        """True when this exact text is something she said in this chat."""
        flat = " ".join(str(text or "").split())
        if not flat:
            return False
        if flat in await self._said_bucket(chat_id):
            return True
        return persona.is_own_line(flat) or self.runtime.yura_chat.was_sent(chat_id, flat)

    async def _is_own_id(self, chat_id: int, message_id: Any) -> bool:
        """True only for a message this account sent as *her*.

        The owner's messages are sent by the same account, so "outgoing" cannot
        tell the two apart — the id can. Nothing here looks at the text: a friend
        who quotes one of her lines is not her, and a reply pointed at his
        message must never be mistaken for a reply pointed at hers.

        The ids come from the shared ledger, which the sender fills on **every**
        send. The persona's own bucket is still consulted so an old build's
        settings rows keep working.
        """
        if message_id is None:
            return False
        ledger = getattr(self.runtime, "sent_ledger", None)
        if ledger is not None and await ledger.has(chat_id, message_id):
            return True
        try:
            return int(message_id) in await self._own_bucket(chat_id)
        except (TypeError, ValueError):
            return False

    async def _is_own(self, chat_id: int, message_id: Any, text: str) -> bool:
        """True when the message *arriving now* is one she wrote.

        Deliberately narrower than :meth:`_is_own_text`: being wrong here means
        staying silent, so only her id and the shapes of her own bank count. A
        short phrase she happens to have said once ("باشه") is not proof, and
        treating it as proof is how a real reply from him would vanish.
        """
        if await self._is_own_id(chat_id, message_id):
            return True
        if not text:
            return False
        return persona.is_own_line(text) or self.runtime.yura_chat.was_sent(chat_id, text)

    def _named(self, text: str) -> bool:
        """True when the message says her name, in either spelling or alphabet."""
        lowered = (text or "").lower()
        for name in ("yura", "یورا", *identity.NAMES.values()):
            if name and name.lower() in lowered:
                return True
        return False

    # -- the owner's own chat --------------------------------------------
    async def on_self_message(self, event: NormalizedEvent) -> None:
        if not event.is_outgoing:
            return
        picture = await self._picture(event)
        if picture is None and not event.text.strip():
            return
        self_id = self.self_id
        if self_id is None or event.chat_id != self_id:
            self._skip("not_own_chat")
            return
        if await self._is_own(event.chat_id, event.message_id, event.text):
            self._skip("own_message")
            return
        if self._is_command(event.text):
            self._skip("command")
            return
        quote = await self._quote(event)
        await self._learn_from_owner(event, quote)
        await self._learn_correction(event, quote)
        decision = await self.runtime.permissions.can_act(event.chat_id, "persona", is_private=True)
        if not decision.allowed:
            self._skip(f"permission:{decision.code}")
            return
        now = time.monotonic()
        if not self._budget_ok(now):
            self._skip("cooldown")
            return
        await self._answer(
            event, chat_id=event.chat_id, picture=picture, quoted=quote.text,
            reply_to=quote.message_id, is_private=True
        )
        self._last_at = now
        self._window.append(now)
        self.answered += 1

    # -- a group ----------------------------------------------------------
    async def on_group_message(self, event: NormalizedEvent) -> None:
        """Answer only what was addressed to her.

        Ways a message is addressed, and nothing else counts: a Telegram reply to
        one of *her* messages, a reply to the owner's own message (they are the
        one who installed her), her name in the text, or a mention. The gate is
        checked before the picture is fetched, so a group photo nobody asked
        about is never downloaded.
        """
        if not event.is_group:
            await self.on_dm_chat(event)
            return
        if event.message_id is None:
            self._skip("not_group")
            return
        # In a group this account writes two kinds of message: the owner's and
        # hers. Replying to her is an *outgoing* message here, which is why the
        # first version of this handler never fired — and why her own answers
        # have to be recognised by id, not by who "sent" them.
        if await self._is_own(event.chat_id, event.message_id, event.text):
            self._skip("own_message", event)
            return
        quote = await self._quote(event)
        # Learning happens before every gate, including the command one: "yura
        # stop swearing at me" is a correction *and* a sentence the dispatcher
        # will also see, and the lesson must not depend on which path wins.
        learned = await self._learn_from_owner(event, quote)
        taught = await self._learn_correction(event, quote)
        if event.is_outgoing and self._is_command(event.text):
            self._skip("command", event)
            return
        # Addressed means aimed at *her*, and only two things count now: a reply
        # to a message she sent, or her name in the text.
        #
        # ``event.mentions_me`` is deliberately not used. Telegram sets its
        # ``mentioned`` flag for a mention **and for every reply to the account**,
        # so trusting it made her answer each person who replied to the owner, and
        # each person who tagged him. His name is not her name.
        if event.sender_id is not None and await self.runtime.store.is_ignored(event.sender_id):
            self._skip("ignored", event)
            return
        addressed = quote.hers or self._named(event.text)
        # "remember this" is itself aimed at her, even when the message is a reply
        # to someone else: she should confirm it, not silently file it. A
        # correction counts the same way — if he bothered to teach her, she
        # answers about it.
        if learned or taught:
            addressed = True
        if not addressed:
            self._skip("not_addressed", event)
            return
        picture = await self._picture(event)
        if picture is None and not event.text.strip():
            self._skip("empty", event)
            return
        decision = await self.runtime.permissions.can_act(event.chat_id, "persona", is_private=False)
        if not decision.allowed:
            self._skip(f"permission:{decision.code}", event)
            return
        now = time.monotonic()
        if not self._group_budget_ok(event.chat_id, now):
            self._skip("cooldown", event)
            return
        # Every ninth group answer is a reaction instead of a line: a companion
        # that always has something to say is a broadcaster.
        self._since_reaction += 1
        if quote.hers and self._since_reaction % 9 == 0:
            target = event.reply_to_id or event.message_id
            emoji = random.choice(yura_extras.REACTIONS)
            if await self.runtime.sender.react(event.chat_id, target, emoji):
                self.reacted += 1
                self._since_reaction = 0
                self._group_last[event.chat_id] = now
                self._group_window.setdefault(event.chat_id, []).append(now)
                return
        await self._answer(
            event,
            chat_id=event.chat_id,
            picture=picture,
            quoted=quote.text,
            reply_to=quote.message_id,
            is_private=False,
        )
        self._group_last[event.chat_id] = now
        self._group_window.setdefault(event.chat_id, []).append(now)
        self.group_answered += 1

    # -- a private chat that is not Saved Messages --------------------------
    async def on_dm_chat(self, event: NormalizedEvent) -> None:
        """One-to-one with somebody else: his side, or theirs.

        This is the hole that made her look dead. Saved Messages had a handler,
        groups had a handler, and the ordinary private chat — the place where he
        actually sits and calls her — had neither: his own line arrived as
        *outgoing*, the stranger handler refused it (`not_dm`), and the self-chat
        handler refused it too (`not_own_chat`). So he typed "yura say hi" into a
        DM and nothing, ever, happened.

        Two sides now:

        * **his** outgoing line is answered when it is addressed to her — her name
          in it, or a reply to one of her messages — and only then. His ordinary
          conversation with somebody else stays his: being answered mid-sentence
          by your own bot is worse than silence.
        * **their** incoming line goes to :meth:`on_private_message`, which is
          opt-in per person, exactly as before.
        """
        if event.is_outgoing:
            await self.on_owner_in_dm(event)
        else:
            await self.on_private_message(event)

    async def on_owner_in_dm(self, event: NormalizedEvent) -> None:
        """He called her by name in a one-to-one chat. Answer *there*."""
        if event.message_id is None:
            self._skip("not_dm", event)
            return
        if await self._is_own(event.chat_id, event.message_id, event.text):
            self._skip("own_message", event)
            return
        if self._is_command(event.text):
            self._skip("command", event)
            return
        quote = await self._quote(event)
        # Teaching happens before every gate: "remember this" in a DM is both a
        # lesson and a sentence, and the lesson must not depend on the gate.
        learned = await self._learn_from_owner(event, quote)
        taught = await self._learn_correction(event, quote)
        addressed = quote.hers or self._named(event.text) or learned or taught
        if not addressed:
            self._skip("not_addressed", event)
            return
        picture = await self._picture(event)
        if picture is None and not event.text.strip():
            self._skip("empty", event)
            return
        decision = await self.runtime.permissions.can_act(event.chat_id, "persona", is_private=True)
        if not decision.allowed:
            self._skip(f"permission:{decision.code}", event)
            return
        # The same hourly budget as answering a stranger: a chat with another
        # person is not a place to flood, even when he is the one typing.
        now = time.monotonic()
        last = self._dm_last.get(event.chat_id)
        cutoff = now - 3600
        window = [stamp for stamp in self._dm_window.get(event.chat_id, []) if stamp >= cutoff]
        self._dm_window[event.chat_id] = window
        if last is not None and now - last < self.MIN_DM_INTERVAL:
            self._skip("dm_cooldown", event)
            return
        if len(window) >= self.MAX_DM_PER_HOUR:
            self._skip("dm_cooldown", event)
            return
        await self._answer(
            event,
            chat_id=event.chat_id,
            picture=picture,
            quoted=quote.text,
            reply_to=quote.message_id,
            is_private=False,
        )
        self._dm_last[event.chat_id] = now
        window.append(now)
        self.dm_answered += 1

    # -- someone else's private chat ---------------------------------------
    async def on_private_message(self, event: NormalizedEvent) -> None:
        """Answer a private message — but only from someone he introduced.

        A stranger messaging the account gets nothing: replying to unknown people
        is how a userbot starts spamming on its owner's behalf. The gate is one
        row in her book with ``dm`` switched on, which only the owner can set
        (``.yura dm on`` while replying to them).
        """
        if event.is_outgoing or event.is_group or event.message_id is None:
            self._skip("not_dm", event)
            return
        if await self._is_own(event.chat_id, event.message_id, event.text):
            self._skip("own_message", event)
            return
        speaker = event.sender_id
        if speaker is None or self.runtime.permissions.is_owner(speaker):
            self._skip("owner_chat", event)
            return
        # "Don't answer him" is an instruction, not a preference: a name in this
        # table beats every other rule, including a person he introduced earlier.
        if await self.runtime.store.is_ignored(speaker):
            self._skip("ignored", event)
            return
        # A read, not a touch: a row is only created for someone she actually
        # answers. A stranger messaging the account must not leave a file behind
        # just by knocking.
        person = await self.runtime.people.get(speaker)
        if person is None or not getattr(person, "dm", False):
            self._skip("dm_not_allowed", event)
            return
        picture = await self._picture(event)
        if picture is None and not event.text.strip():
            self._skip("empty", event)
            return
        decision = await self.runtime.permissions.can_act(event.chat_id, "persona", is_private=True)
        if not decision.allowed:
            self._skip(f"permission:{decision.code}", event)
            return
        now = time.monotonic()
        last = self._dm_last.get(event.chat_id)
        cutoff = now - 3600
        window = [stamp for stamp in self._dm_window.get(event.chat_id, []) if stamp >= cutoff]
        self._dm_window[event.chat_id] = window
        if (last is not None and now - last < self.MIN_DM_INTERVAL) or len(window) >= self.MAX_DM_PER_HOUR:
            self._skip("dm_cooldown", event)
            return
        quote = await self._quote(event)
        await self._answer(
            event,
            chat_id=event.chat_id,
            picture=picture,
            quoted=quote.text,
            reply_to=quote.message_id,
            is_private=False,
        )
        self._dm_last[event.chat_id] = now
        window.append(now)
        self.group_answered += 1

    # -- one turn ---------------------------------------------------------
    async def _answer(
        self,
        event: NormalizedEvent,
        *,
        chat_id: int,
        picture: Picture | None,
        quoted: str,
        is_private: bool,
        reply_to: int | None = None,
    ) -> None:
        language = await self._language(event)
        audience = await self._audience(event) if not is_private else None
        speaker = event.sender_id or (self.self_id if is_private else None)
        # Outgoing and not hers means it came from his account: the id can be
        # missing on some updates, and being rude to the owner because of a
        # missing field is the worst possible failure.
        owner = bool(event.is_outgoing) or self.runtime.permissions.is_owner(speaker)
        person = None if owner else await self._person(speaker, audience)
        rules, reminder = await self._lessons_for(speaker)
        answer = await self.runtime.yura_chat.answer(
            event.text,
            chat_id=chat_id,
            language=language,
            picture=picture,
            quoted=quoted,
            audience=audience,
            person=person,
            is_owner=owner,
            chat_title=getattr(audience, "chat_title", "") if audience is not None else "",
            lessons=rules,
            reminder=reminder,
            reply_to=reply_to,
        )
        self.logger.info(
            "yura: answering '%s' in %s%s",
            redact_text(truncate(event.text or event.media_type or "", 40)),
            "Saved Messages" if is_private else f"chat {chat_id}",
            " (a picture)" if picture is not None else "",
        )
        if answer.text:
            await self._deliver(
                chat_id,
                self._decorate(_visible(answer), is_private=is_private),
                answer.source,
                text=answer.text,
                reply_to=(
                    event.message_id
                    if await self._reply_style(chat_id) == "thread"
                    else None
                ),
            )
        for path in answer.files:
            await self.runtime.sender.send_media(
                chat_id, str(path), caption=escape(truncate(path.name, 90))
            )

    async def _deliver(
        self,
        chat_id: int,
        html: str,
        source: str,
        *,
        text: str = "",
        reply_to: int | None = None,
    ) -> None:
        """Send, then edit into the real answer — the message types itself.

        An offline line is instant and gets no placeholder: a "…" that is replaced
        in the same frame looks like a glitch. A model answer took a second, and
        that second is worth showing.

        A *reply* links her answer to the message it answers; a *plain* message
        stands on its own. Which one is right is the owner's call, so
        ``.yura reply on|off`` decides, and this method just obeys. What never
        comes back is a copy of their message written into the body — that was
        the actual complaint, and ``strip_echo`` handles it.
        """
        sender = self.runtime.sender
        # The words Telegram will hand back when someone replies are the *rendered*
        # text, not the markdown she wrote. Storing the markdown made the fallback
        # match only answers that happened to have no formatting at all.
        await self._note_said(chat_id, strip_html(html))
        if source != "model":
            result = await sender.send(chat_id, html, reply_to=reply_to)
            await self._note_own(chat_id, getattr(getattr(result, "message", None), "id", None))
            return
        try:
            # The placeholder is the message that exists: it has to carry the
            # reply link, because the edit that fills it cannot add one.
            placeholder = await sender.send(chat_id, self.TYPING, reply_to=reply_to)
        except Exception:  # noqa: BLE001 - fall back to one clean message
            result = await sender.send(chat_id, html, reply_to=reply_to)
            await self._note_own(chat_id, getattr(getattr(result, "message", None), "id", None))
            return
        message = getattr(placeholder, "message", None)
        message_id = getattr(message, "id", None)
        if getattr(placeholder, "dry_run", False) or message_id is None:
            return
        await self._note_own(chat_id, message_id)
        try:
            await sender.edit(chat_id, message_id, html)
        except Exception as exc:  # noqa: BLE001 - the placeholder must not be the answer
            self.logger.warning("yura: could not fill the placeholder (%s)", exc)
            try:
                await sender.delete(chat_id, message_id)
            except Exception:  # noqa: BLE001
                pass
            await sender.send(chat_id, html, reply_to=reply_to)

    def _decorate(self, html: str, *, is_private: bool = True) -> str:
        """Sometimes a bar under the message: the body clock, made visible.

        It is one of her typographic flourishes, so it belongs to her everywhere;
        it is only rarer in a group, where a bar under every third answer stopped
        looking like a mood and started looking like a status line.
        """
        self._since_mood += 1
        every = self.MOOD_EVERY if is_private else self.MOOD_EVERY_GROUP
        if self._since_mood % every:
            return html
        hour = _local_hour(self.runtime)
        if html.endswith("</blockquote>"):
            return html
        return f"{html}\n\n<i>{mood_line(hour)}</i>"

    async def quote_for(self, event: NormalizedEvent) -> str:
        """The text being replied to, for callers outside this class."""
        return (await self._quote(event)).text

    async def room_for(self, event: NormalizedEvent) -> Any:
        """Who is talking and what about, for callers outside this class."""
        if getattr(event, "is_private", False):
            return None
        return await self._audience(event)

    async def _audience(self, event: NormalizedEvent) -> Any:
        """Who is talking and what is being talked about, for a group turn."""
        try:
            return await build_context(
                self.runtime, event, own_ids=set(await self._own_bucket(event.chat_id))
            )
        except Exception as exc:  # noqa: BLE001 - context is a bonus, never a blocker
            self.logger.warning("yura: could not read the room: %s", exc)
            return None

    async def _person(self, user_id: int | None, audience: Any = None) -> Any:
        """Their row, or a fresh one built from the room (never raises)."""
        if user_id is None:
            return None
        try:
            person = await self.runtime.people.touch(
                user_id, str(getattr(audience, "speaker", "") or "")
            )
        except Exception as exc:  # noqa: BLE001 - the reply matters more than the book
            self.logger.warning("yura: could not read the person: %s", exc)
            return None
        return person

    async def _lessons_for(self, user_id: int | None) -> tuple[tuple[str, ...], str]:
        """What he has taught her, and the one she forgot most recently."""
        book = getattr(self.runtime, "lessons", None)
        if book is None:
            return (), ""
        try:
            rows = await book.for_person(user_id)
            broken = await book.recent_break(user_id)
        except Exception as exc:  # noqa: BLE001 - lessons are never worth an error
            self.logger.warning("yura: could not read the lessons: %s", exc)
            return (), ""
        reminder = broken.reminder if broken is not None else ""
        return tuple(lesson.rule for lesson in rows), reminder

    async def _learn_correction(self, event: NormalizedEvent, quote: Quote) -> str:
        """He corrected her. Keep it as something she agreed to.

        No filter is installed and nothing is suppressed: the sentence is stored,
        every later prompt carries it, and an answer that walks over it is counted
        so the next turn can own it. A reply to someone attaches the lesson to
        them ("don't call her that"), otherwise it is about her everywhere.
        """
        if not self.runtime.permissions.is_owner(event.sender_id):
            return ""
        book = getattr(self.runtime, "lessons", None)
        if book is None:
            return ""
        rule = lessons_service.extract_correction(event.text)
        if not rule:
            return ""
        target = quote.user_id if quote.user_id and not self.runtime.permissions.is_owner(
            quote.user_id
        ) else None
        try:
            lesson = await book.add(
                rule,
                scope="person" if target is not None else "global",
                subject_id=target,
                source=event.text[:400],
            )
        except Exception as exc:  # noqa: BLE001 - a lost lesson must not lose the answer
            self.logger.warning("yura: could not keep that correction: %s", exc)
            return ""
        if lesson is not None:
            self.logger.info("yura: learned a correction (%s)", lesson.scope)
        return rule

    async def _learn_from_owner(self, event: NormalizedEvent, quote: Quote) -> str:
        """Keep what the owner just told her — "remember this: …".

        Returns the payload, or ``""`` when the message was not an instruction to
        remember. A reply in a group attaches the fact to the person replied to,
        because "she's my gf" is about *her*, not about the room.
        """
        if not self.runtime.permissions.is_owner(event.sender_id):
            # A stranger cannot teach her anything about anyone.
            return ""
        payload = persona.extract_remember(event.text)
        if not payload:
            return ""
        target = quote.user_id if not quote.hers else None
        try:
            if target is not None and not self.runtime.permissions.is_owner(target):
                relation = people_service.detect_relation(payload)
                stored = await self.runtime.people.note(target, payload, relation=relation)
                self.logger.info("yura: kept a fact about %s (%s)", target, bool(stored))
            else:
                stored = await self.runtime.yura_chat.learn(event.chat_id, payload)
                self.logger.info("yura: kept a fact for this chat (%s)", bool(stored))
        except Exception as exc:  # noqa: BLE001 - a failed write must not lose the answer
            self.logger.warning("yura: could not keep that fact: %s", exc)
            return ""
        return payload

    async def _picture(self, event: NormalizedEvent) -> Picture | None:
        """The image the owner attached, if there is one worth looking at."""
        if event.media_type not in IMAGE_MEDIA_TYPES:
            return None
        message = event.raw
        if message is None or not looks_like_picture(getattr(message, "media", None)):
            return None
        return await download_picture(self.runtime.client, message)

    async def _quote(self, event: NormalizedEvent, depth: int = 3) -> Quote:
        """What is being replied to, whether she wrote it, and whether it is ours.

        One level answers "why?" about a single message. Three levels answer it
        about a thread: the model sees the shape of the conversation instead of
        the last line of it.
        """
        if not event.is_reply:
            return Quote()
        message = event.raw
        getter = getattr(message, "get_reply_message", None)
        if getter is None:
            return Quote()
        try:
            replied = await getter()
        except Exception:  # noqa: BLE001 - a deleted target is not an error
            return Quote()
        lines: list[str] = []
        hers = False
        from_account = False
        author: int | None = getattr(replied, "sender_id", None)
        pointed_at = getattr(replied, "id", None)
        current = replied
        for _ in range(max(1, depth)):
            if current is None:
                break
            text = " ".join(str(getattr(current, "raw_text", "") or "").split())
            sender = getattr(current, "sender_id", None)
            out = bool(getattr(current, "out", False))
            ours = out or (
                sender is not None and self.self_id is not None and sender == self.self_id
            )
            if _ is not None and not ours:
                # Only quote something from this account: a third party's message
                # must not be folded into a prompt by being pointed at.
                break
            from_account = from_account or ours
            # ...and "hers" means she wrote it. Only the id she sent proves that:
            # a text match would count a friend quoting her back, and "outgoing"
            # is also true for every message the owner writes from this account.
            mine = await self._is_own_id(event.chat_id, getattr(current, "id", None))
            if not mine and ours and text:
                # Ids are lost on a restart and after a settings reset; the words
                # are not. Only an outgoing message can be hers, so a member who
                # quotes her line back is still not her.
                mine = await self._is_own_text(event.chat_id, text)
            hers = hers or mine
            if text:
                lines.append(text[:400])
            elif getattr(current, "media", None) is not None:
                lines.append("a picture")
            nxt = getattr(current, "get_reply_message", None)
            if nxt is None:
                break
            try:
                current = await nxt()
            except Exception:  # noqa: BLE001
                break
        if not lines:
            return Quote(
                hers=hers, from_account=from_account, user_id=author, message_id=pointed_at
            )
        # Oldest first reads like a thread; the model answers the newest.
        return Quote(
            text="\n".join(reversed(lines)),
            hers=hers,
            from_account=from_account,
            user_id=author,
            message_id=pointed_at,
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
            usage="[on|off|status|memory|log|clear|recap|diary|faces|speak|voice|poster|caption|react|nice|rude|neutral|note|dm|reply|teach|rules|people|person|tools|agent|actions|yes|no|<anything>]",
            changes_state=True,
            subcommands=(
                "on", "off", "enable", "disable", "status", "card", "who", "state",
                "memory", "memories", "remember", "learn", "forget", "log", "history", "clear",
                "key", "apikey", "token", "recap", "today", "diary", "journal", "faces",
                "face", "kaomoji", "speak", "voice", "tts", "poster", "caption",
                "react", "reaction", "nice", "kind", "polite", "rude", "mean", "savage",
                "neutral", "normal", "tone", "note", "about", "facts", "people", "humans",
                "known", "person", "them", "whois", "dm", "privates", "dms", "teach",
                "learn-this", "lesson", "rules", "lessons", "unteach",
                "reply", "thread", "plain", "quote", "bubble", "bubbles",
                "tools", "abilities", "agent", "hands", "actions", "did", "audit",
                "yes", "approve", "no", "cancel", "deny",
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
    # Groups: only what is addressed to her. Kept as a separate subscriber so a
    # group bug can never take the owner's own chat down with it.
    runtime.event_engine.subscribe(
        "persona.group_reply",
        replies.on_group_message,
        # OUTGOING as well as MESSAGE: in a group the owner's own message —
        # including a reply to her — arrives as outgoing from this account.
        {EventType.MESSAGE, EventType.OUTGOING},
        priority=45,
        description="Yura answers a reply to her, or her name, in a group",
    )
