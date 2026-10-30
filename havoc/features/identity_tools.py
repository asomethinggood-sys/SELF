"""Her own identity: the username, the photo, and an honest list of abilities.

Three things were missing from "change my name" and "set my bio":

* **the username** — a real Telegram rule set (5–32, letters, digits, underscore,
  no leading digit), reported exactly as Telegram states it, because guessing
  produces "username invalid" and no reason;
* **the photo** — an image he replies to, downloaded, size-checked, uploaded and
  set. Telegram keeps the previous ones, so there is nothing to restore; the
  answer says that plainly instead of pretending there is;
* **what she can do** — built from the live registry, the feature table and the
  tool list. A help page that is hard-coded goes stale the day it is written.

Feature areas: 12 (identity), 70 (AI), 96 (feature switches).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..command_registry import Command, CommandContext, CommandError
from ..utils.formatting import bold, code, escape
from ..utils.security import redact_text
from ..utils.text import truncate

__all__ = ["register"]

#: Telegram's own rule for a username. Kept as a literal so the refusal can quote
#: it instead of saying "invalid".
USERNAME_RULE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
#: A profile photo has to be square-ish and small enough to upload in one go.
MAX_PHOTO_BYTES = 10 * 1024 * 1024


async def _me(ctx: CommandContext) -> Any:
    telegram = ctx.runtime.telegram
    me = getattr(telegram, "me", None)
    if me is not None:
        return me
    client = ctx.runtime.client
    getter = getattr(client, "get_me", None)
    if getter is None:
        raise CommandError("errors.not_connected")
    try:
        return await getter()
    except Exception as exc:  # noqa: BLE001
        raise CommandError("errors.not_connected") from exc


# ---------------------------------------------------------------------------
# .username
# ---------------------------------------------------------------------------
async def cmd_username(ctx: CommandContext) -> None:
    raw = " ".join(ctx.args).strip().lstrip("@")
    if not raw:
        me = await _me(ctx)
        current = getattr(me, "username", None)
        await ctx.reply(
            ctx.t(
                "identity.username_current",
                value=code("@" + current) if current else escape(ctx.t("identity.none")),
            )
        )
        return
    remove = raw.lower() in {"off", "none", "remove", "delete", "clear"}
    if not remove and not USERNAME_RULE.match(raw):
        raise CommandError("identity.username_rule", value=escape(raw))
    from telethon.tl.functions.account import UpdateUsernameRequest

    try:
        await ctx.runtime.client(UpdateUsernameRequest(username=None if remove else raw))
    except Exception as exc:  # noqa: BLE001 - Telegram's reason is the message
        ctx.runtime.health.record_error("profile.username", exc)
        raise CommandError("identity.username_failed", detail=escape(redact_text(str(exc))[:180])) from exc
    me = await _me(ctx)
    try:
        me.username = None if remove else raw
    except Exception:  # noqa: BLE001 - Telethon objects are usually writable
        pass
    await ctx.store.add_profile_history(
        "username", None, None if remove else raw, "command"
    )
    if remove:
        await ctx.reply(ctx.t("identity.username_removed"))
        return
    await ctx.reply(ctx.t("identity.username_set", value=code("@" + raw)))


# ---------------------------------------------------------------------------
# .photo
# ---------------------------------------------------------------------------
async def cmd_photo(ctx: CommandContext) -> None:
    reply = await ctx.reply_message()
    if reply is None:
        raise CommandError("identity.photo_needs_reply", prefix=ctx.runtime.prefix)
    media = getattr(reply, "media", None)
    if media is None:
        raise CommandError("identity.photo_not_media")
    document = getattr(media, "document", None)
    size = int(getattr(document, "size", 0) or 0)
    if size and size > MAX_PHOTO_BYTES:
        raise CommandError("identity.photo_too_big", size=f"{size / 1024 / 1024:.1f} MB")

    directory = Path(ctx.config.data_dir) / "profile"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"photo-{getattr(reply, 'id', 'new')}.jpg"
    telegram = ctx.runtime.telegram
    downloader = getattr(telegram, "download_media", None) if telegram is not None else None
    if downloader is None:
        raise CommandError("errors.not_connected")
    try:
        path = await downloader(reply, target)
    except Exception as exc:  # noqa: BLE001
        raise CommandError("identity.photo_download_failed", detail=escape(redact_text(str(exc))[:150])) from exc
    if not path:
        raise CommandError("identity.photo_download_failed", detail="no file came back")

    client = ctx.runtime.client
    uploader = getattr(client, "upload_file", None)
    if uploader is None:
        raise CommandError("identity.photo_failed", detail="this build cannot upload files")
    from telethon.tl.functions.photos import UploadProfilePhotoRequest

    try:
        uploaded = await uploader(str(path))
        await client(UploadProfilePhotoRequest(file=uploaded))
    except Exception as exc:  # noqa: BLE001
        ctx.runtime.health.record_error("profile.photo", exc)
        raise CommandError("identity.photo_failed", detail=escape(redact_text(str(exc))[:180])) from exc
    await ctx.store.add_profile_history("photo", None, str(path), "command")
    await ctx.reply(ctx.t("identity.photo_set"))


# ---------------------------------------------------------------------------
# .abilities
# ---------------------------------------------------------------------------
async def cmd_abilities(ctx: CommandContext) -> None:
    runtime = ctx.runtime
    commands = runtime.registry.visible()
    groups: dict[str, int] = {}
    for command in commands:
        groups[command.category] = groups.get(command.category, 0) + 1
    tools = getattr(getattr(runtime, "actions", None), "tools", []) or []
    states = await runtime.features.snapshot()
    enabled = [state for state in states if getattr(state, "enabled", False)]

    lines = [
        bold(ctx.t("identity.abilities_header", commands=len(commands), tools=len(tools))),
        ctx.t(
            "identity.abilities_features",
            enabled=len(enabled),
            total=len(states),
        ),
    ]
    for category, count in sorted(groups.items(), key=lambda pair: pair[1], reverse=True):
        lines.append(f"◦ {escape(category)} · {code(str(count))}")
    if tools:
        names = ", ".join(getattr(tool, "name", "?") for tool in tools)
        lines.append(ctx.t("identity.abilities_tools", names=escape(truncate(names, 220))))
    lines.append(ctx.t("identity.abilities_hint", prefix=runtime.prefix))
    await ctx.reply("\n".join(lines))


def register(runtime: Any) -> None:
    runtime.registry.register_many(
        [
            Command(
                "username",
                cmd_username,
                category="profile",
                aliases=("handle",),
                usage="[new|off]",
                changes_state=True,
                description="Change her @username, or remove it; Telegram's rules apply.",
                examples=(".username yura_17", ".username off"),
            ),
            Command(
                "photo",
                cmd_photo,
                category="profile",
                aliases=("avatar", "pfp", "setpic"),
                usage="(reply to a picture)",
                requires_reply=True,
                changes_state=True,
                needs_confirmation=True,
                description="Use the picture you replied to as her profile photo.",
            ),
            Command(
                "abilities",
                cmd_abilities,
                category="core",
                aliases=("skills",),
                usage="",
                description="What she can actually do, from the live registry.",
            ),
        ]
    )
