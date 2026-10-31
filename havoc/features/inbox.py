"""Reading his own Telegram — the inbox, one conversation, and who someone is.

This is the half of 1.7 that was deferred, and the half that needs the most care,
so the rules are written here rather than remembered:

* **Only what the account can already read.** Dialogs come from the account's own
  chat list; a message comes from a chat the account is in. Nothing is searched
  for, scraped, or guessed.
* **Names, not words.** ``.inbox`` answers with who wrote, when, and how many are
  unread — it does **not** paste strangers' messages into a chat. Reading one
  conversation is an explicit act (``.read @sara``), and even then it is a summary
  unless he asks for ``--raw``.
* **Nothing leaves the database.** No text from anyone else's chat is sent
  anywhere except into a summary in *his* chat, and the raw lines only on request.

Feature areas: 44 (search), 47 (privacy), 96 (feature switches).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .. import ui
from ..command_registry import Command, CommandContext, CommandError
from ..utils.formatting import bold, code, escape
from ..utils.text import truncate
from ..utils.time import format_datetime, format_short, parse_iso, utc_now
from .helpers import display_name, media_label

__all__ = ["register"]

#: How many dialogs one call may look at. Telegram returns them newest first, and
#: a thousand is a page of his chat list, not a crawl of his account.
_DIALOG_LIMIT = 200
#: Default number of messages when he asks to read a conversation.
_READ_DEFAULT = 12
_READ_MAX = 50


def _when(value: Any, tz: str, language: str) -> str:
    """A date-ish value (datetime, iso string, timestamp) as local time."""
    moment: datetime | None = None
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        moment = parse_iso(value)
    elif isinstance(value, (int, float)) and value:
        try:
            moment = datetime.fromtimestamp(float(value), tz=utc_now().tzinfo)
        except (OverflowError, OSError, ValueError):
            moment = None
    if moment is None:
        return "—"
    return format_datetime(moment, tz, language)


def _dialog_date(dialog: Any) -> Any:
    """The best "last activity" a dialog object offers."""
    last = getattr(dialog, "message", None)
    return (
        getattr(dialog, "date", None)
        or getattr(last, "date", None)
        or getattr(dialog, "last_message_date", None)
    )


def _is_private(dialog: Any) -> bool:
    entity = getattr(dialog, "entity", None) or dialog
    if getattr(dialog, "is_user", None) is not None:
        return bool(dialog.is_user)
    # Fall back to the entity itself: a User has first_name and no title.
    return bool(getattr(entity, "first_name", None)) and not getattr(entity, "title", None)


def _unread(dialog: Any) -> int:
    try:
        return max(0, int(getattr(dialog, "unread_count", 0) or 0))
    except (TypeError, ValueError):
        return 0


async def _dialogs(ctx: CommandContext) -> list[Any]:
    client = ctx.runtime.client
    getter = getattr(client, "get_dialogs", None)
    if getter is None:
        raise CommandError("inbox.no_client")
    try:
        rows = await getter(limit=_DIALOG_LIMIT)
    except Exception as exc:  # noqa: BLE001 - a read failure is reported, not raised raw
        raise CommandError("inbox.read_failed", detail=escape(str(exc)[:150])) from exc
    return list(rows or [])


def _limit(ctx: CommandContext, default: int = 10, ceiling: int = 30) -> int:
    raw = (ctx.arg(0) or "").strip()
    if not raw.isdigit():
        return default
    return max(1, min(int(raw), ceiling))


# ---------------------------------------------------------------------------
# .inbox / .unread
# ---------------------------------------------------------------------------
def _row_line(
    ctx: CommandContext,
    entity: Any,
    dialog: Any,
    *,
    tz: str,
    unread: int,
    with_id: bool,
) -> str:
    """One chat, in the order the eye reads: who, how to reach them, when, how many."""
    name = display_name(entity, str(getattr(dialog, "id", "?")))
    handle = getattr(entity, "username", None)
    bits = [f"▸ {escape(name)}"]
    if handle:
        bits.append(code("@" + str(handle)))
    moment = _dialog_date(dialog)
    stamp = ""
    if moment:
        try:
            stamp = format_short(parse_iso(moment), tz, ctx.language)
        except Exception:  # noqa: BLE001 - a bad timestamp never sinks the list
            stamp = ""
    if unread:
        bits.append(f"{code(str(unread))} {ctx.t('inbox.unread_mark')}")
    if stamp:
        bits.append(escape(stamp))
    if with_id:
        bits.append(code(str(getattr(dialog, "id", "?"))))
    return " · ".join(bits)


async def cmd_inbox(ctx: CommandContext) -> None:
    wanted = _limit(ctx)
    tz = getattr(ctx.config, "timezone", "UTC")
    with_id = ctx.has_flag("ids")
    rows = [dialog for dialog in await _dialogs(ctx) if _is_private(dialog)]
    rows.sort(key=lambda dialog: _dialog_date(dialog) or "", reverse=True)
    if not rows:
        await ctx.reply(ctx.t("inbox.empty"))
        return
    total_unread = sum(_unread(dialog) for dialog in rows)
    head = bold(ctx.t("inbox.header", count=len(rows[:wanted])))
    if total_unread:
        head += "\n" + ctx.t("inbox.unread_total", total=total_unread)
    lines = [head, ""]
    for dialog in rows[:wanted]:
        entity = getattr(dialog, "entity", None) or dialog
        lines.append(
            _row_line(ctx, entity, dialog, tz=tz, unread=_unread(dialog), with_id=with_id)
        )
    lines.append(ctx.t("inbox.hint", prefix=ctx.runtime.prefix))
    await ctx.reply("\n".join(lines))


async def cmd_unread(ctx: CommandContext) -> None:
    tz = getattr(ctx.config, "timezone", "UTC")
    rows = [
        dialog
        for dialog in await _dialogs(ctx)
        if _is_private(dialog) and _unread(dialog) > 0
    ]
    rows.sort(key=lambda dialog: _dialog_date(dialog) or "")
    if not rows:
        await ctx.reply(ui.card(ctx.t("inbox.unread_header", count=0), [ctx.t("inbox.nothing_unread")], icon="◍"))
        return
    # One row, three facts, in the order the eye reads them: who, how many, when.
    # The full date with its weekday was three facts where one was wanted.
    lines = []
    for dialog in rows:
        entity = getattr(dialog, "entity", None) or dialog
        name = display_name(entity, str(getattr(dialog, "id", "?")))
        stamp = format_short(parse_iso(_dialog_date(dialog)) or utc_now(), tz, ctx.language)
        lines.append(f"{name} — {_unread(dialog)} · {stamp}")
    await ctx.reply(
        ui.card(
            ctx.t("inbox.unread_header", count=len(rows)),
            lines,
            icon="◍",
            footer=ctx.t("inbox.hint", prefix=ctx.runtime.prefix),
        )
    )


# ---------------------------------------------------------------------------
# .read — one conversation
# ---------------------------------------------------------------------------
async def _fetch(ctx: CommandContext, chat_id: Any, limit: int) -> list[Any]:
    client = ctx.runtime.client
    getter = getattr(client, "get_messages", None)
    if getter is None:
        return []
    try:
        rows = await getter(chat_id, limit=limit)
    except Exception as exc:  # noqa: BLE001
        raise CommandError("inbox.read_failed", detail=escape(str(exc)[:150])) from exc
    return list(rows or [])


def _speaker(message: Any, ctx: CommandContext) -> str:
    sender = getattr(message, "sender", None)
    if sender is not None:
        return display_name(sender, "—")
    if getattr(message, "out", False):
        return ctx.t("inbox.you")
    return str(getattr(message, "sender_id", "—"))


async def _summarise(ctx: CommandContext, name: str, rows: list[Any]) -> str:
    """A short account of a conversation, in her voice, from lines only."""
    provider = getattr(ctx.runtime, "ai", None)
    if provider is None or not getattr(provider, "available", False):
        return ""
    transcript = "\n".join(
        f"{_speaker(message, ctx)}: {truncate(reply_text_of(message), 200)}" for message in rows
    )
    if not transcript.strip():
        return ""
    try:
        answer = await provider.converse(
            [
                {
                    "role": "system",
                    "content": (
                        "You summarise a Telegram conversation for its owner in two "
                        "short sentences. Be factual; no advice, no greetings, no "
                        "markdown. Write in the language of the conversation."
                    ),
                },
                {"role": "user", "content": f"With {name}:\n{transcript}"},
            ],
            max_tokens=160,
        )
    except Exception as exc:  # noqa: BLE001 - a summary is a nicety, never a failure
        ctx.runtime.logger.warning("inbox: could not summarise (%s)", exc)
        return ""
    return " ".join(str(answer or "").split())[:500]


def reply_text_of(message: Any) -> str:
    text = getattr(message, "raw_text", None) or getattr(message, "text", None) or ""
    if not text and getattr(message, "media", None) is not None:
        return f"[{media_label(message)}]"
    return str(text)


async def cmd_read(ctx: CommandContext) -> None:
    who = " ".join(ctx.args).strip()
    if not who:
        raise CommandError("inbox.usage_read", prefix=ctx.runtime.prefix)
    target = await ctx.runtime.reach.resolve_or_raise(who)
    try:
        wanted = int(ctx.flag("limit") or _READ_DEFAULT)
    except ValueError:
        wanted = _READ_DEFAULT
    wanted = max(1, min(wanted, _READ_MAX))
    rows = await _fetch(ctx, target.id, wanted)
    if not rows:
        await ctx.reply(ctx.t("inbox.no_messages", name=escape(target.label)))
        return
    rows = list(reversed(rows))  # oldest first, the way a conversation reads
    raw = ctx.has_flag("raw")
    tz = getattr(ctx.config, "timezone", "UTC")
    header = bold(ctx.t("inbox.read_header", name=escape(target.label), count=len(rows)))
    body: list[str] = []
    if raw:
        for message in rows:
            when = _when(getattr(message, "date", None), tz, ctx.language)
            body.append(
                f"{code(when)} {escape(_speaker(message, ctx))}: {escape(truncate(reply_text_of(message), 300))}"
            )
    else:
        summary = await _summarise(ctx, target.label, rows)
        if summary:
            body.append(escape(summary))
        last = rows[-1]
        body.append(
            ctx.t(
                "inbox.read_last",
                who=escape(_speaker(last, ctx)),
                when=escape(_when(getattr(last, "date", None), tz, ctx.language)),
            )
        )
    body.append(ctx.t("inbox.read_raw_hint", prefix=ctx.runtime.prefix))
    await ctx.reply("\n".join([header, *body]))


# ---------------------------------------------------------------------------
# .whois / .people
# ---------------------------------------------------------------------------
async def cmd_whois(ctx: CommandContext) -> None:
    who = " ".join(ctx.args).strip()
    if not who:
        raise CommandError("inbox.usage_whois", prefix=ctx.runtime.prefix)
    target = await ctx.runtime.reach.resolve_or_raise(who)
    person = await ctx.runtime.people.get(target.id)
    ignored = await ctx.store.is_ignored(target.id)
    entity = None
    if ctx.runtime.telegram is not None:
        entity = await ctx.runtime.telegram.resolve_entity(target.id)
    rows = [
        (ctx.t("inbox.field_id"), code(target.id)),
        (ctx.t("inbox.field_name"), escape(person.label if person else target.label)),
        (
            ctx.t("inbox.field_username"),
            escape("@" + str(getattr(entity, "username", "")))
            if getattr(entity, "username", None)
            else "—",
        ),
        (
            ctx.t("inbox.field_phone"),
            escape(str(getattr(entity, "phone", "")) or "—"),
        ),
        (ctx.t("inbox.field_tone"), escape(person.tone if person else "—")),
        (ctx.t("inbox.field_dm"), ctx.t("common.on" if person and person.dm else "common.off")),
        (ctx.t("inbox.field_ignored"), ctx.t("common.on" if ignored else "common.off")),
        (ctx.t("inbox.field_seen"), escape(_when(person.updated_at, getattr(ctx.config, "timezone", "UTC"), ctx.language)) if person else "—"),
    ]
    lines = [bold(ctx.t("inbox.whois_header", name=escape(target.label)))]
    lines.extend(f"{escape(label)}: {value}" for label, value in rows)
    if person and person.facts:
        lines.append("")
        lines.append(bold(ctx.t("inbox.field_facts", count=len(person.facts))))
        lines.extend(f"◦ {escape(fact)}" for fact in person.facts[:8])
    await ctx.reply("\n".join(lines))


async def cmd_people(ctx: CommandContext) -> None:
    rows = await ctx.runtime.people.all(20)
    total = await ctx.runtime.people.count()
    if not rows:
        await ctx.reply(ctx.t("inbox.people_empty", prefix=ctx.runtime.prefix))
        return
    lines = [bold(ctx.t("inbox.people_header", count=total))]
    for person in rows:
        lines.append(f"• {escape(person.line())} — {code(person.user_id)}")
    await ctx.reply("\n".join(lines))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register(runtime: Any) -> None:
    runtime.registry.register_many(
        [
            Command(
                "inbox",
                cmd_inbox,
                category="inbox",
                aliases=("dms", "messages"),
                usage="[count]",
                description="The last private chats: who, when, how many unread — never the words.",
                examples=(".inbox", ".inbox 20"),
            ),
            Command(
                "unread",
                cmd_unread,
                category="inbox",
                aliases=("unanswered",),
                usage="",
                description="Every private chat with something waiting in it.",
            ),
            Command(
                "read",
                cmd_read,
                category="inbox",
                aliases=("convo", "transcript"),
                usage="<who> [--limit n] [--raw]",
                description="What was said in one conversation, summarised; --raw for the lines.",
                examples=(".read @sara", ".read sara --limit 30 --raw"),
            ),
            Command(
                "whois",
                cmd_whois,
                category="inbox",
                aliases=("who",),
                usage="<who>",
                description="Everything she knows about one person.",
                examples=(".whois @hmmm_6",),
            ),
            Command(
                "people",
                cmd_people,
                category="inbox",
                usage="",
                description="Everyone in her book: name, tone, whether she answers their DMs.",
            ),
        ]
    )
