"""Sending pictures and files — the capability, gated and explained.

``.pic <path|url>`` sends one file to the current chat; ``--to`` sends it
somewhere else; several sources become an album. There is no bundled image and
no automatic picture sending: this is the plumbing that future features (Yura
sending a picture in a scenario, a scheduled image, a plugin) will call through
:class:`havoc.services.media`, so every one of them inherits the same checks.

Nothing here reads file contents, downloads a URL, or sends anything the owner
did not name explicitly.

Feature areas: 45 (media), 96 (feature switches).
"""

from __future__ import annotations

from typing import Any

from ..command_registry import Command, CommandContext, CommandError
from ..services import media as media_service
from ..services.media import MediaError
from ..services.sender import SendError
from ..utils.formatting import bold, escape, key_value_block, pre
from .helpers import resolve_target


#: Flags are stripped from the argument list by the parser, so sources are just
#: the remaining words. Quoted paths with spaces need quotes.
def _sources(ctx: CommandContext) -> list[str]:
    raw = " ".join(ctx.args).strip()
    if not raw:
        return []
    # A single quoted argument may contain spaces.
    if raw.startswith(("'", '"')) and raw.endswith(raw[0]) and len(raw) > 1:
        return [raw[1:-1]]
    return [part for part in raw.split() if part]


def _info(ctx: CommandContext) -> str:
    lines = [
        bold(ctx.t("media.info_header")),
        ctx.t("media.info_usage", prefix=ctx.runtime.prefix),
        ctx.t("media.info_enable", prefix=ctx.runtime.prefix),
        "",
        key_value_block(
            [
                (ctx.t("media.info_types"), escape(", ".join(sorted({ext.lstrip('.') for ext in media_service.ALLOWED_EXTENSIONS})[:14])) + " …"),
                (ctx.t("media.info_photo_max"), f"{media_service.MAX_PHOTO_BYTES // (1024 * 1024)} MB"),
                (ctx.t("media.info_max"), f"{media_service.MAX_BYTES // (1024 * 1024)} MB"),
                (ctx.t("media.info_album"), str(media_service.MAX_SOURCES)),
            ]
        ),
        "",
        ctx.t("media.info_blocked", types=escape(", ".join(sorted(ext.lstrip('.') for ext in media_service.BLOCKED_EXTENSIONS)))),
        "",
        pre(
            "\n".join(
                [
                    f"{ctx.runtime.prefix}pic ~/Pictures/sunset.jpg",
                    f"{ctx.runtime.prefix}pic https://example.com/a.png --caption hello",
                    f"{ctx.runtime.prefix}pic one.png two.png   # album",
                    f"{ctx.runtime.prefix}pic photo.jpg --to @username",
                ]
            )
        ),
    ]
    return "\n".join(lines)


async def cmd_pic(ctx: CommandContext) -> None:
    runtime = ctx.runtime
    action = (ctx.sub or "").lower()
    if action in {"info", "help", "types"} or not ctx.args:
        await ctx.reply(_info(ctx))
        return

    # Sending is what the switch guards; reading this card is never blocked.
    decision = await runtime.permissions.can_act(ctx.chat_id, "media", is_private=True)
    if not decision.allowed:
        raise CommandError(
            "errors.feature_blocked",
            feature=escape("media"),
            reason=runtime.t(f"reason.{decision.code}", ctx.language, default=decision.reason),
        )

    try:
        resolved = media_service.resolve_many(_sources(ctx))
    except MediaError as exc:
        raise CommandError(exc.key, detail=escape(exc.detail)) from exc

    caption = ctx.flag("caption", "") or ""
    target_id = ctx.chat_id
    raw_target = ctx.flag("to", "")
    if raw_target:
        chat_id, _entity, error = await resolve_target(ctx, raw_target)
        if error or chat_id is None:
            raise CommandError(error or "errors.entity_not_found", target=escape(raw_target))
        target_id = chat_id

    try:
        result = await runtime.sender.send_media(
            target_id,
            media_service.as_telegram_input(resolved),
            caption=caption,
            reply_to=None,
            silent=ctx.has_flag("silent"),
        )
    except SendError as exc:
        raise CommandError("media.send_failed", detail=escape(str(exc))) from exc

    if result.dry_run:
        await ctx.reply_ok(
            ctx.t("media.dry_run", preview=escape(media_service.summary(resolved)))
        )
        return
    await ctx.reply_ok(
        ctx.t("media.sent", count=str(len(resolved)), preview=escape(media_service.summary(resolved)))
    )


def register(runtime: Any) -> None:
    runtime.registry.register(
        Command(
            "pic",
            cmd_pic,
            category="media",
            aliases=("image", "file", "sendfile"),
            usage="<path|url> [...] [--caption text] [--to chat] [--silent]",
            changes_state=True,
            subcommands=("info", "help", "types"),
        )
    )
