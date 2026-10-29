"""Persian/Arabic text utilities and language helpers.

Everything here is non-destructive and preview-first: the command shows the
transformed text, it never edits or resends the original message.

Feature areas: 3, 37, 63, 66, 73.
"""

from __future__ import annotations

from typing import Any

from ..command_registry import Command, CommandContext, CommandError
from ..language_detection import detect_language
from ..utils.formatting import bold, key_value_block, pre
from ..utils.text import (
    cleanup_text,
    is_rtl,
    mixed_direction_safe,
    normalize_persian,
    script_counts,
    to_arabic_digits,
    to_latin_digits,
    to_persian_digits,
)
from .helpers import reply_text


async def _source_text(ctx: CommandContext) -> str:
    """Use the replied-to message when present, otherwise the command text."""
    reply = await ctx.reply_message()
    if reply is not None:
        return reply_text(reply)
    # ``.digits fa 123`` -> the text is everything after the target token
    return (ctx.rest_raw if ctx.sub else ctx.raw_args).strip()


async def cmd_digits(ctx: CommandContext) -> None:
    target = (ctx.sub or ctx.arg(0) or "").lower()
    if target not in {"fa", "en", "ar", "latin", "persian"}:
        raise CommandError("errors.invalid_choice", field="target", choices="fa, en, ar")
    text = await _source_text(ctx)
    if not text:
        raise CommandError("errors.no_text")
    if target in {"fa", "persian"}:
        converted = to_persian_digits(text)
    elif target == "ar":
        converted = to_arabic_digits(text)
    else:
        converted = to_latin_digits(text)
    await ctx.reply(
        "\n".join(
            [
                bold(ctx.t("digits.header", target=target)),
                pre(converted[:3500]),
                ctx.t("digits.note"),
            ]
        )
    )


async def cmd_normalize(ctx: CommandContext) -> None:
    text = await _source_text(ctx)
    if not text:
        raise CommandError("errors.no_text")
    normalized = cleanup_text(text)
    await ctx.reply(
        "\n".join(
            [
                bold(ctx.t("normalize.header")),
                pre(normalized[:3500]),
                "",
                ctx.t("normalize.note"),
            ]
        )
    )


async def cmd_rtl(ctx: CommandContext) -> None:
    text = await _source_text(ctx)
    if not text:
        raise CommandError("errors.no_text")
    fa, latin = script_counts(text)
    estimate = detect_language(text)
    pairs = [
        (ctx.t("rtl.is_rtl"), ctx.t("common.yes" if is_rtl(text) else "common.no")),
        (ctx.t("rtl.persian_letters"), fa),
        (ctx.t("rtl.latin_letters"), latin),
        (ctx.t("rtl.language"), estimate.language),
        (ctx.t("rtl.confidence"), f"{estimate.confidence:.2f}"),
    ]
    await ctx.reply(
        "\n".join(
            [
                bold(ctx.t("rtl.header")),
                key_value_block(pairs),
                "",
                bold(ctx.t("rtl.safe_preview")),
                pre(mixed_direction_safe(text)[:1500]),
            ]
        )
    )


async def cmd_detect_text(ctx: CommandContext) -> None:
    if (ctx.sub or ctx.arg(0)).lower() in {"status", "engine", "state"}:
        from .detection import detect_status_text

        await ctx.reply(await detect_status_text(ctx))
        return
    text = await _source_text(ctx)
    if not text:
        raise CommandError("errors.no_text")
    estimate = detect_language(text)
    pairs = [
        (ctx.t("detect.language"), estimate.language),
        (ctx.t("detect.primary"), estimate.primary),
        (ctx.t("detect.confidence"), f"{estimate.confidence:.2f}"),
        (ctx.t("detect.mixed"), ctx.t("common.yes" if estimate.mixed else "common.no")),
        (ctx.t("detect.persian_ratio"), f"{estimate.persian_ratio:.2f}"),
        (ctx.t("detect.reason"), estimate.reason),
    ]
    await ctx.reply(bold(ctx.t("detect.text_header")) + "\n" + key_value_block(pairs))


async def cmd_persianize(ctx: CommandContext) -> None:
    """Normalize Arabic letter variants (ي/ك -> ی/ک) in the replied text."""
    text = await _source_text(ctx)
    if not text:
        raise CommandError("errors.no_text")
    await ctx.reply(bold(ctx.t("persianize.header")) + "\n" + pre(normalize_persian(text)[:3500]))


def register(runtime: Any) -> None:
    runtime.registry.register_many(
        [
            Command("digits", cmd_digits, category="information", usage="<fa|en|ar> [text]", requires_reply=False, subcommands=('fa', 'en', 'ar', 'latin', 'persian')),
            Command("normalize", cmd_normalize, category="information", requires_reply=False),
            Command("rtl", cmd_rtl, category="information", requires_reply=False),
            Command("detect", cmd_detect_text, category="detection", aliases=("langdetect",), requires_reply=False, subcommands=('status', 'engine', 'state')),
            Command("persianize", cmd_persianize, category="information", requires_reply=False, hidden=True),
        ]
    )
