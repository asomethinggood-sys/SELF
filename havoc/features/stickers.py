"""Stickers: the one thing a Telegram account can do that these hands could not.

``.sticker ❤️`` sends a sticker that means a heart; ``.sticker love`` works too,
because a word for the feeling is how he says it. The pack list is the account's
own installed packs — nothing is downloaded, nothing is invented, and a pack that
Telegram refuses is skipped rather than turning into an error.

Feature areas: 45 (media), 96 (feature switches).
"""

from __future__ import annotations

import random
from typing import Any

from ..command_registry import Command, CommandContext, CommandError
from ..utils.formatting import bold, escape, italic, key_value_block

#: What he says, and the glyph the packs are indexed by. Words only: an actual
#: emoji passes straight through and never needs this table.
_SPOKEN = {
    "love": "❤️",
    "heart": "❤️",
    "قلب": "❤️",
    "عشق": "❤️",
    "laugh": "😂",
    "lol": "😂",
    "خنده": "😂",
    "cry": "😭",
    "sad": "😭",
    "گریه": "😭",
    "angry": "😡",
    "mad": "😡",
    "عصبانی": "😡",
    "wink": "😉",
    "kiss": "😘",
    "بوس": "😘",
    "cool": "😎",
    "hug": "🤗",
    "بغل": "🤗",
    "think": "🤔",
    "فکر": "🤔",
    "sleep": "😴",
    "خواب": "😴",
    "party": "🥳",
    "dance": "💃",
    "رقص": "💃",
    "fire": "🔥",
    "آتش": "🔥",
    "star": "⭐",
    "ستاره": "⭐",
    "flower": "💐",
    "گل": "💐",
    "sun": "☀️",
    "خورشید": "☀️",
    "cat": "🐱",
    "گربه": "🐱",
    "dog": "🐶",
    "سگ": "🐶",
    "shy": "😳",
    "خجالت": "😳",
    "thanks": "🙏",
    "ممنون": "🙏",
    "ok": "👌",
    "clap": "👏",
    "تشویق": "👏",
    "cowboy": "🤠",
    "rocket": "🚀",
}

#: How much of his pack list one call will walk. A person with two hundred packs
#: should still get an answer inside a second.
MAX_PACKS = 12


def _is_emoji(text: str) -> bool:
    return any(ord(character) > 0x2000 and not character.isspace() for character in text)


def _wanted(raw: str) -> str:
    """The glyph to look for, from either an emoji or a word for it."""
    text = (raw or "").strip()
    if not text:
        return ""
    if _is_emoji(text):
        return "".join(
            character
            for character in text
            if ord(character) > 0x2000 and not character.isspace()
        )[:4]
    return _SPOKEN.get(text.lower(), "")


def _alt(document: Any) -> str:
    """The emoji a sticker is tagged with."""
    for attribute in getattr(document, "attributes", ()) or ():
        alt = getattr(attribute, "alt", None)
        if alt:
            return str(alt)
    return ""


async def _documents(client: Any, emoji: str) -> list[Any]:
    """Every sticker in the installed packs tagged with ``emoji``.

    One pack that fails does not lose the rest: a corporate pack with no hash is
    common enough that treating it as fatal would make the command useless.
    """
    try:
        from telethon.tl.functions.messages import GetAllStickersRequest, GetStickerSetRequest
        from telethon.tl.types import InputStickerSetID
    except ImportError:  # pragma: no cover - Telethon is a dependency
        return []
    try:
        installed = await client(GetAllStickersRequest(hash=0))
    except Exception:  # noqa: BLE001 - no packs, no stickers, not a crash
        return []
    found: list[Any] = []
    for pack in list(getattr(installed, "sets", ()) or ())[:MAX_PACKS]:
        try:
            full = await client(
                GetStickerSetRequest(
                    stickerset=InputStickerSetID(
                        id=int(getattr(pack, "id", 0)),
                        access_hash=int(getattr(pack, "access_hash", 0)),
                    ),
                    hash=0,
                )
            )
        except Exception:  # noqa: BLE001 - a refused pack is a skipped pack
            continue
        for document in getattr(full, "documents", ()) or ():
            stickers = _alt(document)
            if emoji and emoji in stickers:
                found.append(document)
    return found


async def cmd_sticker(ctx: CommandContext) -> None:
    runtime = ctx.runtime
    raw = " ".join(ctx.args).strip() or (ctx.flag("emoji") or "").strip()
    if not raw:
        raise CommandError("stickers.usage", prefix=runtime.prefix)
    emoji = _wanted(raw)
    if not emoji:
        raise CommandError("stickers.unknown_emoji", word=escape(raw[:40]))

    decision = await runtime.permissions.can_act(ctx.chat_id, "media", is_private=True)
    if not decision.allowed:
        raise CommandError(
            "errors.feature_blocked",
            feature=escape("media"),
            reason=runtime.t(f"reason.{decision.code}", ctx.language, default=decision.reason),
        )

    documents = await _documents(runtime.client, emoji)
    if not documents:
        raise CommandError("stickers.none", emoji=escape(emoji))

    pick = documents[0] if ctx.has_flag("first") else random.choice(documents)
    target_id = ctx.chat_id
    raw_target = (ctx.flag("to") or "").strip()
    if raw_target:
        from .helpers import resolve_target

        chat_id, _entity, error = await resolve_target(ctx, raw_target)
        if error or chat_id is None:
            raise CommandError(error or "errors.entity_not_found", target=escape(raw_target))
        target_id = chat_id

    result = await runtime.sender.send_media(
        target_id,
        pick,
        silent=ctx.has_flag("silent"),
        reply_to=getattr(ctx.message, "id", None) if ctx.has_flag("reply") else None,
    )
    if getattr(result, "dry_run", False):
        await ctx.reply_ok(ctx.t("media.dry_run", preview=escape(f"sticker {emoji}")))
        return
    lines = [
        bold(ctx.t("stickers.sent", emoji=escape(emoji))),
        key_value_block(
            (
                (ctx.t("stickers.pack"), escape(str(getattr(pick, "id", "—")))),
                (ctx.t("stickers.pool"), str(len(documents))),
            )
        ),
        italic(ctx.t("stickers.more")),
    ]
    await ctx.reply("\n".join(lines))


def register(runtime: Any) -> None:
    runtime.registry.register(
        Command(
            "sticker",
            cmd_sticker,
            category="media",
            aliases=("stikers", "stix"),
            usage="<emoji|word> [--to chat] [--silent] [--first] [--reply]",
            description="Send a sticker from the account's own packs.",
            changes_state=True,
            examples=(
                ".sticker ❤️",
                ".sticker love --to @sara",
                ".sticker خنده --first",
            ),
        )
    )
