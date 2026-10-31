"""``.exportchat`` — one conversation, as a text file he can keep.

Telegram already has an export, buried three menus deep and slow. This is the
version that fits on one line: read the last N messages of a chat, write them out
with who said what and when, and send the file back into the chat he asked from.

Rules:

* **One chat, bounded.** The limit is his or the default, never "everything";
* **text only** — a photo is written as ``[photo]``, the file itself is not
  fetched, so an export cannot quietly download a stranger's media;
* **nothing leaves the account.** The file goes to the chat he typed in, and
  nowhere else.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..command_registry import Command, CommandContext, CommandError
from ..utils.formatting import bold, escape
from ..utils.text import truncate
from ..utils.time import format_datetime
from .helpers import display_name, media_label, reply_text, tz_for

__all__ = ["register"]

DEFAULT_COUNT = 200
MAX_COUNT = 2000
#: A text file this size is a book, not a chat export.
MAX_BYTES = 20 * 1024 * 1024


def _limit(ctx: CommandContext) -> int:
    for value in (ctx.flag("limit"), ctx.arg(1)):
        if not str(value or "").strip():
            continue
        try:
            return max(1, min(int(str(value).strip()), MAX_COUNT))
        except ValueError:
            continue
    return DEFAULT_COUNT


def _line(message: Any, ctx: CommandContext) -> str:
    who = display_name(getattr(message, "sender", None), ctx.t("inbox.you"))
    if getattr(message, "out", False) and who == "—":
        who = ctx.t("inbox.you")
    stamp = ""
    date = getattr(message, "date", None)
    if date is not None:
        try:
            stamp = format_datetime(date, tz_for(ctx), "en")  # a file is for machines too
        except Exception:  # noqa: BLE001 - a timestamp is never worth failing an export
            stamp = ""
    body = reply_text(message) or (
        f"[{media_label(message)}]" if getattr(message, "media", None) is not None else ""
    )
    # One line per message keeps a transcript readable and greppable; the
    # newlines themselves are not information worth keeping.
    flat = " ".join(str(body).replace("\r", " ").split())
    return f"[{stamp}] {who}: {truncate(flat, 4000)}"


async def cmd_exportchat(ctx: CommandContext) -> None:
    where = (ctx.arg(0) or "").strip()
    if not where:
        raise CommandError("export.usage", prefix=ctx.runtime.prefix)
    target = await ctx.runtime.reach.resolve_or_raise(where)
    chat_id = getattr(target, "id", None) or where
    name = str(getattr(target, "label", "") or chat_id)
    count = _limit(ctx)

    client = ctx.runtime.client
    getter = getattr(client, "get_messages", None)
    if getter is None:
        raise CommandError("errors.not_connected")
    try:
        rows = await getter(chat_id, limit=count)
    except Exception as exc:  # noqa: BLE001 - Telegram's reason is the answer
        raise CommandError("export.failed", detail=escape(str(exc)[:180])) from exc
    rows = [row for row in (rows or []) if row is not None]
    if not rows:
        await ctx.reply(ctx.t("export.empty", name=escape(truncate(name, 60))))
        return

    ordered = list(reversed(rows))  # oldest first, the way a transcript reads
    lines = [f"# {name} · {len(ordered)} messages · exported by yura", ""]
    lines.extend(_line(message, ctx) for message in ordered)
    body = "\n".join(lines)
    if len(body.encode("utf-8")) > MAX_BYTES:
        raise CommandError("export.too_big", limit=MAX_BYTES // (1024 * 1024))

    directory = Path(ctx.config.data_dir) / "exports"
    directory.mkdir(parents=True, exist_ok=True)
    safe = "".join(character if character.isalnum() or character in "-_" else "_" for character in name)[:40]
    path = directory / f"chat-{safe or 'export'}-{len(ordered)}.txt"
    path.write_text(body, encoding="utf-8")

    try:
        result = await ctx.runtime.sender.send_media(
            ctx.chat_id, str(path), caption=escape(name)
        )
    except Exception as exc:  # noqa: BLE001
        raise CommandError("export.send_failed", detail=escape(str(exc)[:180])) from exc
    if not getattr(result, "ok", False):
        raise CommandError("export.send_failed", detail=escape(str(getattr(result, "error", ""))))
    await ctx.reply(
        "\n".join(
            [
                bold(ctx.t("export.done", count=len(ordered))),
                ctx.t("export.where", name=escape(truncate(name, 60)), file=escape(path.name)),
            ]
        )
    )


def register(runtime: Any) -> None:
    runtime.registry.register_many(
        [
            Command(
                "exportchat",
                cmd_exportchat,
                category="read",
                aliases=("chatfile",),
                usage="<chat> [count] [--limit n]",
                description="Save a conversation as a text file and send it back here.",
                examples=(".exportchat @sara 300",),
            ),
        ]
    )
