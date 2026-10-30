"""Looking through what the account already has: saved messages and mentions.

Two reads, one rule: **she reads, she does not crawl.** Each call is one Telegram
page, bounded by a limit he can see in the answer, and every line is a message
the account could already open by hand.

* ``.diary [20]`` — his own Saved Messages: the notepad Telegram already is;
* ``.mentions [20]`` — where someone wrote his name.

``.search`` already exists in the information family and covers chat history;
this module deliberately does not repeat it.

Bodies are shown **as text**, escaped, one line each: a search result is only
useful if it shows what matched, and only when he asked for it.

Feature areas: 44 (search), 47 (privacy).
"""

from __future__ import annotations

from typing import Any

from ..command_registry import Command, CommandContext, CommandError
from ..utils.formatting import bold, code, escape
from ..utils.text import truncate
from ..utils.time import format_datetime
from .helpers import display_name, media_label, tz_for

__all__ = ["register"]

DEFAULT_LIMIT = 10
MAX_LIMIT = 50
#: A snippet long enough to recognise a message, short enough that ten of them
#: fit in one Telegram message.
SNIPPET = 120


def _limit(ctx: CommandContext, default: int = DEFAULT_LIMIT) -> int:
    raw = ctx.flag("limit") or ctx.arg(0) or ""
    try:
        wanted = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(1, min(wanted, MAX_LIMIT))


def _body(message: Any) -> str:
    text = getattr(message, "raw_text", None) or getattr(message, "text", None) or ""
    if not text and getattr(message, "media", None) is not None:
        return f"[{media_label(message)}]"
    return " ".join(str(text).split())


def _when(message: Any, ctx: CommandContext) -> str:
    date = getattr(message, "date", None)
    if date is None:
        return ""
    try:
        return format_datetime(date, tz_for(ctx), ctx.language)
    except Exception:  # noqa: BLE001 - a timestamp is never worth a failure
        return ""


def _line(message: Any, ctx: CommandContext, *, chat: str = "") -> str:
    who = display_name(getattr(message, "sender", None), ctx.t("inbox.you"))
    identifier = getattr(message, "id", "?")
    where = f" · {escape(chat)}" if chat else ""
    stamp = _when(message, ctx)
    head = f"• {code('#' + str(identifier))} {escape(who)}{where}"
    if stamp:
        head += f" · {escape(stamp)}"
    return f"{head}\n  {escape(truncate(_body(message), SNIPPET))}"


async def _fetch(
    ctx: CommandContext, entity: Any, limit: int, *, query: str = "", extra: dict[str, Any] | None = None
) -> list[Any]:
    client = ctx.runtime.client
    getter = getattr(client, "get_messages", None)
    if getter is None:
        raise CommandError("errors.not_connected")
    kwargs: dict[str, Any] = {"limit": limit}
    if query:
        kwargs["search"] = query
    if extra:
        kwargs.update(extra)
    try:
        rows = await getter(entity, **kwargs)
    except Exception as exc:  # noqa: BLE001 - Telegram's reason is the answer
        raise CommandError("lookup.failed", detail=escape(str(exc)[:180])) from exc
    return [row for row in (rows or []) if row is not None]


async def cmd_diary(ctx: CommandContext) -> None:
    """His Saved Messages — the notepad Telegram already is."""
    limit = _limit(ctx)
    rows = await _fetch(ctx, "me", limit)
    if not rows:
        await ctx.reply(ctx.t("lookup.saved_empty"))
        return
    lines = [bold(ctx.t("lookup.saved_header", count=len(rows)))]
    for message in rows:
        lines.append(_line(message, ctx))
    await ctx.reply("\n".join(lines))


async def _mentions(ctx: CommandContext, limit: int) -> list[Any]:
    client = ctx.runtime.client
    iterator = getattr(client, "iter_messages", None)
    if iterator is None:
        raise CommandError("errors.not_connected")
    try:
        from telethon.tl.types import InputMessagesFilterMyMentions
    except Exception as exc:  # noqa: BLE001 - an old Telethon is not a crash
        raise CommandError("lookup.mentions_unsupported", detail=escape(str(exc)[:120])) from exc
    rows: list[Any] = []
    try:
        async for message in iterator(None, limit=limit, filter=InputMessagesFilterMyMentions()):
            if message is not None:
                rows.append(message)
    except Exception as exc:  # noqa: BLE001
        raise CommandError("lookup.failed", detail=escape(str(exc)[:180])) from exc
    return rows


async def cmd_mentions(ctx: CommandContext) -> None:
    limit = _limit(ctx, 15)
    rows = await _mentions(ctx, limit)
    if not rows:
        await ctx.reply(ctx.t("lookup.no_mentions"))
        return
    lines = [bold(ctx.t("lookup.mentions_header", count=len(rows)))]
    for message in rows:
        lines.append(_line(message, ctx, chat=str(getattr(message, "chat_id", "") or "")))
    await ctx.reply("\n".join(lines))


def register(runtime: Any) -> None:
    runtime.registry.register_many(
        [
            Command(
                "diary",
                cmd_diary,
                category="read",
                aliases=("savedmsgs", "tgnotes"),
                usage="[count]",
                description="Read the Saved Messages notepad, newest first.",
                examples=(".diary 20",),
            ),
            Command(
                "mentions",
                cmd_mentions,
                category="read",
                aliases=("whereami",),
                usage="[count]",
                description="Where someone wrote his name.",
                examples=(".mentions",),
            ),
        ]
    )
