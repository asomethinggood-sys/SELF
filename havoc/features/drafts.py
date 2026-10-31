"""Drafts and the archive — the two places a chat sits without being a message.

A draft is the safest kind of help she can give: the sentence appears in his own
input box, where he can edit it, send it or delete it, and nothing leaves the
account until he presses send. The archive is the other side of the same idea —
putting a conversation away without deleting a word of it.

* ``.draft <chat> <text>`` — write, do not send; ``.draft <chat> off`` clears it;
* ``.drafts`` — every chat Telegram is holding a draft for;
* ``.archive <chat>`` / ``.unarchive <chat>`` — into the archive, and back out.

Feature areas: 12 (chats), 48 (sending safely).
"""

from __future__ import annotations

from typing import Any

from ..command_registry import Command, CommandContext, CommandError
from ..services.tg_extra import ExtrasError
from ..utils.formatting import bold, code, escape
from ..utils.text import truncate
from ..utils.time import format_datetime
from .helpers import tz_for

__all__ = ["register"]

MAX_DRAFT = 4000


def _extras(ctx: CommandContext) -> Any:
    extras = getattr(ctx.runtime, "tg", None)
    if extras is None:
        raise CommandError("errors.not_connected")
    return extras


async def _who(ctx: CommandContext) -> tuple[Any, str, Any]:
    raw = (ctx.arg(0) or "").strip()
    if not raw:
        raise CommandError("drafts.usage_draft", prefix=ctx.runtime.prefix)
    target = await ctx.runtime.reach.resolve_or_raise(raw)
    return getattr(target, "id", None) or raw, escape(getattr(target, "label", "") or raw), target


async def cmd_draft(ctx: CommandContext) -> None:
    chat_id, label, _target = await _who(ctx)
    # ``rest_raw`` is everything after the command name, so the chat token is
    # still in front of it when ``draft`` declares no subcommands. Split it off
    # by hand: the sentence after it must reach Telegram exactly as typed.
    raw = (ctx.raw_args or "").strip()
    _, _, body = raw.partition(" ")
    body = body.strip() or (ctx.rest_raw or "").strip()
    # ``.draft @sara off`` clears; anything else is the text itself.
    if body.lower() in {"off", "clear", "delete", "none"}:
        try:
            await _extras(ctx).clear_draft(chat_id)
        except ExtrasError as exc:
            raise CommandError("drafts.failed", detail=escape(str(exc)[:180])) from exc
        await ctx.reply(ctx.t("drafts.cleared", name=label))
        return
    if not body:
        raise CommandError("drafts.usage_draft", prefix=ctx.runtime.prefix)
    try:
        await _extras(ctx).save_draft(chat_id, body[:MAX_DRAFT])
    except ExtrasError as exc:
        raise CommandError("drafts.failed", detail=escape(str(exc)[:180])) from exc
    await ctx.reply(
        "\n".join(
            [
                bold(ctx.t("drafts.saved", name=label)),
                f"◦ {escape(truncate(body, 160))}",
                ctx.t("drafts.nothing_sent", name=label),
            ]
        )
    )


async def cmd_drafts(ctx: CommandContext) -> None:
    try:
        rows = await _extras(ctx).drafts()
    except ExtrasError as exc:
        raise CommandError("drafts.failed", detail=escape(str(exc)[:180])) from exc
    if not rows:
        await ctx.reply(ctx.t("drafts.empty"))
        return
    lines = [bold(ctx.t("drafts.header", count=len(rows)))]
    tz = tz_for(ctx)
    for row in rows[:20]:
        stamp = ""
        if row.get("date") is not None:
            try:
                stamp = " · " + format_datetime(row["date"], tz, ctx.language)
            except Exception:  # noqa: BLE001 - a timestamp never fails the answer
                stamp = ""
        lines.append(
            f"• {code(str(row.get('id')))} — {escape(truncate(row.get('text', ''), 90))}{escape(stamp)}"
        )
    await ctx.reply("\n".join(lines))


async def _archive(ctx: CommandContext, *, archived: bool) -> None:
    where = ctx.flag("chat") or " ".join(ctx.args).strip()
    if not where:
        raise CommandError("drafts.usage_archive", prefix=ctx.runtime.prefix)
    target = await ctx.runtime.reach.resolve_or_raise(where)
    chat_id = getattr(target, "id", None) or where
    label = escape(getattr(target, "label", "") or str(where))
    try:
        await _extras(ctx).set_archived(chat_id, archived=archived)
    except ExtrasError as exc:
        raise CommandError("drafts.failed", detail=escape(str(exc)[:180])) from exc
    key = "drafts.archived" if archived else "drafts.unarchived"
    await ctx.reply(ctx.t(key, name=label))


async def cmd_archive(ctx: CommandContext) -> None:
    await _archive(ctx, archived=True)


async def cmd_unarchive(ctx: CommandContext) -> None:
    await _archive(ctx, archived=False)


def register(runtime: Any) -> None:
    runtime.registry.register_many(
        [
            Command(
                "draft",
                cmd_draft,
                category="chats",
                aliases=("drafts_put",),
                usage="<chat> <text|off>",
                changes_state=True,
                description="Write a message into a chat's input box without sending it.",
                examples=(".draft @sara running late, sorry",),
            ),
            Command(
                "drafts",
                cmd_drafts,
                category="chats",
                usage="",
                description="Every chat that is holding a draft.",
                examples=(".drafts",),
            ),
            Command(
                "archive",
                cmd_archive,
                category="chats",
                usage="<chat>",
                changes_state=True,
                description="Put a chat in the archive without deleting anything.",
                examples=(".archive @noise",),
            ),
            Command(
                "unarchive",
                cmd_unarchive,
                category="chats",
                usage="<chat>",
                changes_state=True,
                description="Bring a chat back out of the archive.",
                examples=(".unarchive @noise",),
            ),
        ]
    )
