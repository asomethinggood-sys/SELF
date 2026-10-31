"""Forum topics — the threads inside a supergroup that has forums turned on.

Telegram's own rules decide whether any of this works: a group without forums
answers with an error, and the answer here is that error, in his chat. Nothing is
simulated.

* ``.topics [chat]`` — the open topics, with unread counts;
* ``.topic new <title>`` — start one;
* ``.topic close <id>`` / ``.topic open <id>`` — stop and resume discussion;
* ``.topic pin <id>`` / ``.topic unpin <id>`` — keep one at the top;
* ``.topic rename <id> <title>`` — retitle it.

Feature areas: 12 (groups), 21 (messaging).
"""

from __future__ import annotations

from typing import Any

from ..command_registry import Command, CommandContext, CommandError
from ..services.tg_extra import ExtrasError
from ..utils.formatting import bold, code, escape
from ..utils.text import truncate

__all__ = ["register"]

DEFAULT_TOPICS = 30


def _extras(ctx: CommandContext) -> Any:
    extras = getattr(ctx.runtime, "tg", None)
    if extras is None:
        raise CommandError("errors.not_connected")
    return extras


def _fail(exc: ExtrasError, key: str = "topics.failed") -> None:
    raise CommandError(key, detail=escape(str(exc)[:180])) from exc


async def _ask_where(ctx: CommandContext) -> Any:
    """The chat he named, or the one he is standing in."""
    raw = " ".join(ctx.args).strip()
    if raw and not raw.isdigit():
        target = await ctx.runtime.reach.resolve_or_raise(raw)
        return getattr(target, "id", None) or raw
    return ctx.chat_id


async def cmd_topics(ctx: CommandContext) -> None:
    chat_id = await _ask_where(ctx)
    try:
        rows = await _extras(ctx).topics(chat_id, limit=DEFAULT_TOPICS)
    except ExtrasError as exc:
        _fail(exc)
        return
    if not rows:
        await ctx.reply(ctx.t("topics.empty", prefix=ctx.runtime.prefix))
        return
    lines = [bold(ctx.t("topics.header", count=len(rows)))]
    for row in rows:
        mark = " · " + ctx.t("topics.closed_mark") if row["closed"] else ""
        pinned = " ⟡" if row["pinned"] else ""
        unread = f" · {code(str(row['unread']))}" if row["unread"] else ""
        lines.append(
            f"• {code(str(row['id']))} {escape(truncate(row['title'], 60))}{pinned}{mark}{unread}"
        )
    lines.append(ctx.t("topics.hint", prefix=ctx.runtime.prefix))
    await ctx.reply("\n".join(lines))


def _topic_id(ctx: CommandContext) -> int:
    raw = (ctx.arg(0) or "").strip().lstrip("#")
    if not raw.isdigit():
        raise CommandError("topics.need_id", prefix=ctx.runtime.prefix)
    return int(raw)


async def cmd_topic(ctx: CommandContext) -> None:
    action = (ctx.sub or "").lower()
    extras = _extras(ctx)

    if action in {"new", "add", "create"}:
        title = (ctx.rest_raw or " ".join(ctx.args)).strip()
        if not title:
            raise CommandError("topics.usage_new", prefix=ctx.runtime.prefix)
        try:
            created = await extras.create_topic(ctx.chat_id, title)
        except ExtrasError as exc:
            _fail(exc, "topics.new_failed")
            return
        await ctx.reply(
            ctx.t(
                "topics.created",
                title=escape(truncate(created.get("title") or title, 80)),
                id=code(str(created.get("id") or "—")),
            )
        )
        return

    if action in {"close", "open", "pin", "unpin", "rename"}:
        topic_id = _topic_id(ctx)
        try:
            if action in {"close", "open"}:
                await extras.edit_topic(ctx.chat_id, topic_id, closed=action == "close")
                key = "topics.closed" if action == "close" else "topics.opened"
            elif action in {"pin", "unpin"}:
                await extras.pin_topic(ctx.chat_id, topic_id, pinned=action == "pin")
                key = "topics.pinned" if action == "pin" else "topics.unpinned"
            else:
                title = " ".join(ctx.args[1:]).strip()
                if not title:
                    raise CommandError("topics.usage_rename", prefix=ctx.runtime.prefix)
                await extras.edit_topic(ctx.chat_id, topic_id, title=title)
                key = "topics.renamed"
        except ExtrasError as exc:
            _fail(exc, "topics.change_failed")
            return
        await ctx.reply(ctx.t(key, id=code(str(topic_id))))
        return

    raise CommandError("topics.usage_topic", prefix=ctx.runtime.prefix)


def register(runtime: Any) -> None:
    runtime.registry.register_many(
        [
            Command(
                "topics",
                cmd_topics,
                category="chats",
                aliases=("threads",),
                usage="[chat]",
                description="The forum topics of a group, with unread counts.",
                examples=(".topics",),
            ),
            Command(
                "topic",
                cmd_topic,
                category="chats",
                usage="[new <title> | close <id> | open <id> | pin <id> | unpin <id> | rename <id> <title>]",
                subcommands=("new", "add", "create", "close", "open", "pin", "unpin", "rename"),
                changes_state=True,
                description="Start, close, pin or rename a forum topic.",
                examples=(".topic new Night Shift", ".topic close 12"),
            ),
        ]
    )
