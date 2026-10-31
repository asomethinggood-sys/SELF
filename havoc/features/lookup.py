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


#: How many recent chats are searched for a mention. Telegram has no supported
#: global "my mentions" search — ``messages.SearchGlobal`` rejects the mention
#: filter, which is exactly the error that came back once — so this walks the
#: chats the account is already in, newest first, and stops when it has enough.
MENTION_CHATS = 20


async def _mention_rows(ctx: CommandContext, entity: Any, limit: int) -> list[Any]:
    """Mention-filtered messages of one chat — one request, no searching."""
    from telethon.tl.types import InputMessagesFilterMyMentions

    client = ctx.runtime.client
    getter = getattr(client, "get_messages", None)
    if getter is None:
        raise CommandError("errors.not_connected")
    try:
        rows = await getter(entity, limit=limit, filter=InputMessagesFilterMyMentions())
    except Exception as exc:  # noqa: BLE001 - Telegram's reason is the answer
        raise CommandError("lookup.failed", detail=escape(str(exc)[:180])) from exc
    return [row for row in (rows or []) if row is not None]


async def _mentions(ctx: CommandContext, limit: int, where: str = "") -> list[Any]:
    """Mentions in one named chat, or across the most recent ones."""
    from telethon.tl.types import InputMessagesFilterMyMentions  # noqa: F401 - version probe

    if where:
        target = await ctx.runtime.reach.resolve_or_raise(where)
        return await _mention_rows(ctx, getattr(target, "id", None) or where, limit)

    client = ctx.runtime.client
    getter = getattr(client, "get_dialogs", None)
    if getter is None:
        return await _mention_rows(ctx, "me", limit)
    try:
        dialogs = await getter(limit=MENTION_CHATS) or []
    except Exception as exc:  # noqa: BLE001
        raise CommandError("lookup.failed", detail=escape(str(exc)[:180])) from exc

    found: list[Any] = []
    for dialog in dialogs:
        entity = getattr(dialog, "entity", None) or getattr(dialog, "id", None) or dialog
        try:
            found.extend(await _mention_rows(ctx, entity, limit - len(found)))
        except CommandError:
            continue  # one chat the account cannot search must not sink the rest
        if len(found) >= limit:
            break
    # Newest first: Telegram returns per chat, and a mention list is a timeline.
    found.sort(key=lambda row: getattr(row, "date", None) or 0, reverse=True)
    return found[:limit]


async def cmd_mentions(ctx: CommandContext) -> None:
    limit = _limit(ctx, 15)
    where = ctx.flag("chat") or ""
    if not where:
        words = [word for word in ctx.args if word.strip()]
        lowered = [word.lower() for word in words]
        for marker in ("in", "from"):
            if marker in lowered:
                where = " ".join(words[lowered.index(marker) + 1 :]).strip()
                break
    rows = await _mentions(ctx, limit, where)
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
