"""Shared helpers for feature modules.

These keep handlers short: extracting reply text, formatting lists, resolving
targets and normalising language/feature checks all live here (and are unit
testable) rather than being duplicated in every module.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from ..command_registry import CommandContext, CommandError
from ..utils.formatting import bold, escape, key_value_block, pre
from ..utils.text import split_message, truncate
from ..utils.time import format_datetime, parse_iso, utc_now


# ---------------------------------------------------------------------------
# Reply handling
# ---------------------------------------------------------------------------
async def get_reply(ctx: CommandContext) -> Any | None:
    return await ctx.reply_message()


async def require_reply(ctx: CommandContext) -> Any:
    reply = await get_reply(ctx)
    if reply is None:
        raise CommandError("errors.reply_required", prefix=ctx.runtime.prefix, name=ctx.command.name)
    return reply


def reply_text(reply: Any) -> str:
    if reply is None:
        return ""
    text = getattr(reply, "raw_text", None) or getattr(reply, "message", None) or getattr(reply, "text", None) or ""
    return text if isinstance(text, str) else ""


def media_label(message: Any) -> str:
    """Human key for a message's media type."""
    media = getattr(message, "media", None)
    if media is None:
        return "text"
    for name in (
        "photo", "video", "document", "audio", "voice", "video_note", "sticker",
        "gif", "poll", "geo", "contact", "dice",
    ):
        if getattr(media, name, None) is not None:
            return "animation" if name == "gif" else name
    return "unknown"


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def bullet(items: Sequence[str], empty: str = "—") -> str:
    if not items:
        return empty
    return "\n".join(f"• {item}" for item in items)


def section(title: str, body: str) -> str:
    return f"{bold(title)}\n{body}"


def kv_block(pairs: Iterable[tuple[str, Any]]) -> str:
    return key_value_block(pairs)


def mono_list(lines: Sequence[str]) -> str:
    return pre("\n".join(lines)) if lines else "—"


def fmt_time(iso_value: str | None, tz: str, language: str) -> str:
    moment = parse_iso(iso_value)
    if moment is None:
        return "—"
    return format_datetime(moment, tz, language)


def snippet(text: str, limit: int = 60) -> str:
    return escape(truncate(" ".join(text.split()), limit))


def link_to_message(chat_id: int, message_id: int | None) -> str | None:
    """Build a t.me link for public supergroups/channels when possible."""
    if message_id is None:
        return None
    raw = str(chat_id)
    if raw.startswith("-100"):
        return f"https://t.me/c/{raw[4:]}/{message_id}"
    return None


# ---------------------------------------------------------------------------
# Scope helpers
# ---------------------------------------------------------------------------
def tz_for(ctx: CommandContext) -> str:
    return getattr(ctx.config, "timezone", "UTC") or "UTC"


async def chat_language(ctx: CommandContext) -> str:
    return await ctx.runtime.resolve_language(
        ctx.event.text if hasattr(ctx.event, "text") else "",
        chat_id=ctx.chat_id,
        sender_id=getattr(ctx.event, "sender_id", None),
    )


async def feature_on(ctx: CommandContext, name: str) -> bool:
    return await ctx.runtime.feature_manager.is_enabled(name)


async def require_feature(ctx: CommandContext, name: str) -> None:
    if not await feature_on(ctx, name):
        raise CommandError("errors.feature_disabled", feature=name, prefix=ctx.runtime.prefix)


async def resolve_target(ctx: CommandContext, raw: str) -> tuple[int | None, Any | None, str | None]:
    """Resolve ``@username``, an id, or ``me`` to chat information.

    Returns ``(chat_id, entity, error_key)``. In dry-run/test mode without a
    Telegram client this returns the numeric id when provided.
    """
    token = (raw or "").strip()
    if not token:
        return None, None, "errors.missing_target"
    if token in {"me", "self", "خودم"}:
        chat_id = ctx.runtime.permissions.self_id
        return chat_id, None, None
    if re.fullmatch(r"-?\d+", token):
        try:
            chat_id = int(token)
        except ValueError:
            return None, None, "errors.invalid_chat_id"
        return chat_id, None, None
    entity = None
    if ctx.runtime.telegram is not None:
        entity = await ctx.runtime.telegram.resolve_entity(token)
    if entity is None:
        return None, None, "errors.entity_not_found"
    return getattr(entity, "id", None), entity, None


def display_name(entity: Any, fallback: str = "—") -> str:
    if entity is None:
        return fallback
    title = getattr(entity, "title", None)
    if title:
        return str(title)
    parts = [getattr(entity, "first_name", None), getattr(entity, "last_name", None)]
    name = " ".join(part for part in parts if part)
    if name:
        return name
    username = getattr(entity, "username", None)
    if username:
        return f"@{username}"
    identifier = getattr(entity, "id", None)
    return str(identifier) if identifier is not None else fallback


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------
def paginate(items: Sequence[Any], page: int, per_page: int = 10) -> tuple[list[Any], int]:
    per_page = max(1, per_page)
    total_pages = max(1, (len(items) + per_page - 1) // per_page)
    page = min(max(1, page), total_pages)
    start = (page - 1) * per_page
    return list(items[start : start + per_page]), total_pages


def rows_to_lines(rows: Sequence[Any], renderer) -> list[str]:
    return [renderer(row) for row in rows]


async def send_long(ctx: CommandContext, text: str) -> None:
    for chunk in split_message(text):
        await ctx.reply(chunk)


def now_local(tz: str) -> str:
    return format_datetime(utc_now(), tz, "en")


def json_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw]
    if not raw:
        return []
    return [item for item in str(raw).split(",") if item]


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_page_arg(raw: str | None) -> int:
    try:
        return max(1, int(str(raw).strip()))
    except (TypeError, ValueError):
        return 1


def format_count(language: str, value: int) -> str:
    from ..localization import digits_for

    return digits_for(language, value)


def iso_now() -> str:
    return utc_now().isoformat()


def truncate_lines(text: str, lines: int = 5) -> str:
    parts = text.splitlines()
    if len(parts) <= lines:
        return escape(text)
    return escape("\n".join(parts[:lines]) + "…")


def strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        return stripped.strip("`").strip()
    return text


def detect_reply_language(text: str) -> str:
    from ..language_detection import detect_language

    return detect_language(text).resolve("en")


def chat_link(entity: Any) -> str | None:
    username = getattr(entity, "username", None)
    if username:
        return f"https://t.me/{username}"
    identifier = getattr(entity, "id", None)
    if isinstance(identifier, int) and str(identifier).startswith("-100"):
        return f"https://t.me/c/{str(identifier)[4:]}"
    return None


def human_size(size: int | None) -> str:
    if not size:
        return "—"
    units = ("B", "KB", "MB", "GB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def human_duration(seconds: int | None) -> str:
    if not seconds:
        return "—"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def human_datetime(value: datetime | None, tz: str, language: str) -> str:
    if value is None:
        return "—"
    return format_datetime(value, tz, language)
