"""Text utilities, translation, calculator, randomness, jokes and time tools.

Every command is preview-first and side-effect free; nothing here edits or
forwards the user's messages without another explicit command.

Feature areas: 50, 61, 62, 63-70, 53, 54, 66, 67, 68.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta
from typing import Any

from .. import ui
from ..command_registry import Command, CommandContext, CommandError
from ..content import random_joke, random_quote
from ..localization import digits_for
from ..services.translation_provider import TranslationError, is_lang_token, normalize_lang
from ..utils.formatting import bold, code, escape, key_value_block, pre
from ..utils.safe_math import CalcError, evaluate, format_result
from ..utils.text import (
    cleanup_text,
    dedupe_lines,
    reverse_text,
    sort_lines,
    text_stats,
    to_latin_digits,
    truncate,
    word_wrap,
)
from ..utils.time import (
    TimeParseError,
    format_datetime,
    parse_clock,
    parse_local_datetime,
    safe_zone,
    utc_now,
)
from .helpers import reply_text, snippet

CITY_ZONES: dict[str, str] = {
    "tehran": "Asia/Tehran", "تهران": "Asia/Tehran",
    "berlin": "Europe/Berlin", "برلین": "Europe/Berlin",
    "london": "Europe/London", "لندن": "Europe/London",
    "paris": "Europe/Paris", "پاریس": "Europe/Paris",
    "istanbul": "Europe/Istanbul", "استانبول": "Europe/Istanbul",
    "dubai": "Asia/Dubai", "دبی": "Asia/Dubai",
    "newyork": "America/New_York", "نیویورک": "America/New_York",
    "losangeles": "America/Los_Angeles", "لس‌آنجلس": "America/Los_Angeles",
    "toronto": "America/Toronto", "تورنتو": "America/Toronto",
    "sydney": "Australia/Sydney", "سیدنی": "Australia/Sydney",
    "tokyo": "Asia/Tokyo", "توکیو": "Asia/Tokyo",
    "moscow": "Europe/Moscow", "مسکو": "Europe/Moscow",
    "delhi": "Asia/Kolkata", "دهلی": "Asia/Kolkata",
    "shanghai": "Asia/Shanghai", "شانگهای": "Asia/Shanghai",
    "utc": "UTC", "gmt": "UTC",
}


async def _text_source(ctx: CommandContext) -> tuple[str, bool]:
    """Return (text, came_from_reply)."""
    reply = await ctx.reply_message()
    if reply is not None:
        return reply_text(reply), True
    texts = " ".join(ctx.args).strip()
    return texts, False


# ---------------------------------------------------------------------------
# Text statistics / cleanup / transformation
# ---------------------------------------------------------------------------
async def cmd_textstats(ctx: CommandContext) -> None:
    text, _ = await _text_source(ctx)
    if not text.strip():
        raise CommandError("errors.no_text")
    stats = text_stats(text)
    digits = ctx.language == "fa"
    pairs = [
        (ctx.t("textstats.characters"), digits_for(ctx.language, stats.characters) if digits else stats.characters),
        (ctx.t("textstats.characters_no_spaces"), digits_for(ctx.language, stats.characters_no_spaces) if digits else stats.characters_no_spaces),
        (ctx.t("textstats.words"), digits_for(ctx.language, stats.words) if digits else stats.words),
        (ctx.t("textstats.persian_words"), stats.persian_words),
        (ctx.t("textstats.latin_words"), stats.latin_words),
        (ctx.t("textstats.sentences"), stats.sentences),
        (ctx.t("textstats.lines"), stats.lines),
        (ctx.t("textstats.emojis"), stats.emojis),
        (ctx.t("textstats.urls"), stats.urls),
        (ctx.t("textstats.reading_time"), f"{max(1, round(stats.words / 200))} min"),
    ]
    await ctx.reply(bold(ctx.t("textstats.header")) + "\n" + key_value_block(pairs))


async def cmd_cleanup(ctx: CommandContext) -> None:
    text, _ = await _text_source(ctx)
    if not text.strip():
        raise CommandError("errors.no_text")
    cleaned = cleanup_text(text)
    await ctx.reply(
        "\n".join(
            [
                bold(ctx.t("cleanup.header")),
                pre(truncate(cleaned, 3500)),
                "",
                ctx.t("cleanup.note"),
            ]
        )
    )


TRANSFORMS = {
    "upper": lambda text: text.upper(),
    "lower": lambda text: text.lower(),
    "title": lambda text: text.title(),
    "reverse": reverse_text,
    "sort": sort_lines,
    "sortd": lambda text: sort_lines(text, reverse=True),
    "dedupe": dedupe_lines,
    "wrap": lambda text: word_wrap(text, 60),
    "quote": lambda text: "\n".join(f"> {line}" for line in text.splitlines()),
    "trim": lambda text: " ".join(text.split()),
}


async def cmd_transform(ctx: CommandContext) -> None:
    mode = (ctx.sub or "").lower()
    if mode not in TRANSFORMS:
        raise CommandError("errors.invalid_choice", field="mode", choices=", ".join(sorted(TRANSFORMS)))
    text, _ = await _text_source(ctx)
    if not text.strip():
        raise CommandError("errors.no_text")
    result = TRANSFORMS[mode](text)
    await ctx.reply(bold(ctx.t("transform.header", mode=mode)) + "\n" + pre(truncate(result, 3500)))


# ---------------------------------------------------------------------------
# Calculator / randomness
# ---------------------------------------------------------------------------
async def cmd_calc(ctx: CommandContext) -> None:
    expression = " ".join(ctx.args).strip()
    if not expression:
        raise CommandError("errors.missing_argument", usage=ctx.usage_text())
    try:
        value = evaluate(expression)
    except CalcError as exc:
        raise CommandError("calc.error", detail=escape(str(exc))) from exc
    rendered = format_result(value)
    if ctx.language == "fa":
        rendered = digits_for("fa", rendered)
    await ctx.reply(
        f"{bold(ctx.t('calc.header'))}\n{code(escape(expression))} = {code(rendered)}"
    )


async def cmd_random(ctx: CommandContext) -> None:
    if ctx.sub in {"uuid", "guid"}:
        await ctx.reply(code(str(uuid.uuid4())))
        return
    try:
        low = int(to_latin_digits(ctx.arg(0) or "1"))
        high = int(to_latin_digits(ctx.arg(1) or "100"))
    except ValueError as exc:
        raise CommandError("errors.not_a_number", field="range") from exc
    if low > high:
        low, high = high, low
    if high - low > 10_000_000:
        raise CommandError("errors.too_large", field="range", maximum=10_000_000)
    value = secrets.randbelow(high - low + 1) + low
    await ctx.reply(ctx.t("random.result", value=digits_for(ctx.language, value) if ctx.language == "fa" else str(value)))


async def cmd_coin(ctx: CommandContext) -> None:
    await ctx.reply(ctx.t("coin." + ("heads" if secrets.randbelow(2) == 0 else "tails")))


# ---------------------------------------------------------------------------
# Jokes / quotes
# ---------------------------------------------------------------------------
async def cmd_joke(ctx: CommandContext) -> None:
    await ctx.reply(
        "\n\n".join(
            [
                ui.title("✧", ctx.t("joke.header")),
                ui.quote_card(random_joke(ctx.language)),
                ui.footer(ctx.t("joke.hint", prefix=ctx.runtime.prefix)),
            ]
        )
    )


async def cmd_quote(ctx: CommandContext) -> None:
    # Same shape as a poem: the line in a quote block, the author in italics
    # underneath. These aphorisms are the bot's own, so Yura signs them.
    await ctx.reply(ui.quote_card(random_quote(ctx.language), ctx.t("quote.author")))


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------
def _zone_for(name: str) -> str:
    key = name.strip().lower().replace(" ", "")
    if key in CITY_ZONES:
        return CITY_ZONES[key]
    return name.strip()


async def cmd_time(ctx: CommandContext) -> None:
    names = ctx.args or ("utc",)
    rows: list[str] = []
    for name in names:
        zone_name = _zone_for(name)
        try:
            zone = safe_zone(zone_name)
        except TimeParseError:
            rows.append(ctx.t("time.unknown_zone", zone=escape(name)))
            continue
        local = utc_now().astimezone(zone)
        rendered = format_datetime(local, zone_name, ctx.language)
        rows.append(f"{bold(escape(name))} — {escape(rendered)} ({escape(str(local.tzname() or ''))})")
    header = ctx.t("time.header", tz=escape(ctx.config.timezone), now=escape(format_datetime(utc_now(), ctx.config.timezone, ctx.language)))
    await ctx.reply(header + "\n\n" + "\n".join(rows))


async def cmd_convert_time(ctx: CommandContext) -> None:
    if len(ctx.args) < 3:
        raise CommandError("errors.missing_argument", usage=ctx.usage_text())
    when_text = " ".join(ctx.args[:-2])
    from_zone = _zone_for(ctx.args[-2])
    to_zone = _zone_for(ctx.args[-1])
    try:
        if ":" in when_text and not any(sep in when_text for sep in ("-", "/", ".")):
            clock = parse_clock(when_text)
            base = datetime.now(safe_zone(from_zone)).replace(hour=clock.hour, minute=clock.minute, second=0, microsecond=0)
            moment = base
        else:
            moment = parse_local_datetime(when_text, from_zone).astimezone(safe_zone(from_zone))
    except TimeParseError as exc:
        raise CommandError("errors.invalid_time", detail=escape(str(exc)), example="14:30 Berlin Tehran") from exc
    converted = moment.astimezone(safe_zone(to_zone))
    await ctx.reply(
        "\n".join(
            [
                bold(ctx.t("convert.header")),
                key_value_block(
                    [
                        (ctx.t("convert.from"), f"{format_datetime(moment, from_zone, ctx.language)} ({from_zone})"),
                        (ctx.t("convert.to"), f"{format_datetime(converted, to_zone, ctx.language)} ({to_zone})"),
                        (ctx.t("convert.offset"), _offset_label(moment, converted)),
                    ]
                ),
            ]
        )
    )


def _offset_label(first: datetime, second: datetime) -> str:
    delta = second.utcoffset() - first.utcoffset() if first.utcoffset() and second.utcoffset() else timedelta(0)
    hours = delta.total_seconds() / 3600
    return f"{hours:+.1f}h"


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------
async def cmd_translate(ctx: CommandContext) -> None:
    runtime = ctx.runtime
    if not runtime.translator.available:
        await ctx.reply(ctx.t("translate.disabled") + "\n" + ctx.t("translate.setup_hint"))
        return
    tokens = list(ctx.args)
    target_raw = None
    if tokens and len(tokens) > 1 and is_lang_token(tokens[0]):
        target_raw = tokens.pop(0)
    target = normalize_lang(target_raw, "fa" if ctx.language == "en" else "en")
    text, _from_reply = await _text_source(ctx)
    if target_raw:
        text = " ".join(tokens).strip() or text
    if not text.strip():
        raise CommandError("errors.no_text")
    source = "auto"
    try:
        result = await runtime.translator.translate(text, target, source)
    except TranslationError as exc:
        raise CommandError(f"provider.{getattr(exc, 'kind', 'provider_error')}", detail=escape(str(exc)[:200])) from exc
    target_name = runtime.locales.language_name(target, ctx.language) if target in {"en", "fa"} else target
    await ctx.reply(
        "\n".join(
            [
                bold(ctx.t("translate.header", target=escape(target_name))),
                bold(ctx.t("translate.original")),
                pre(truncate(text, 1500)),
                bold(ctx.t("translate.result")),
                pre(truncate(result.text, 1500)),
                "",
                ctx.t("translate.provider", provider=escape(result.provider)),
            ]
        )
    )


# ---------------------------------------------------------------------------
# AI summary (optional)
# ---------------------------------------------------------------------------
async def cmd_summarize(ctx: CommandContext) -> None:
    runtime = ctx.runtime
    count = 10
    if ctx.arg(0).isdigit():
        count = max(2, min(50, int(ctx.arg(0))))
    reply = await ctx.reply_message()
    text = ""
    if reply is not None:
        text = reply_text(reply)
    elif runtime.telegram is not None and runtime.client is not None:
        try:
            messages = await runtime.client.get_messages(ctx.chat_id, limit=count)
        except Exception as exc:  # noqa: BLE001
            raise CommandError("errors.send_failed", detail=escape(str(exc)[:150])) from exc
        parts = [reply_text(message) for message in (messages or [])]
        text = "\n".join(part for part in parts if part)
    if not text.strip():
        raise CommandError("errors.no_text")

    if not runtime.ai.available:
        excerpts = [snippet(line, 120) for line in text.splitlines() if line.strip()][:10]
        await ctx.reply(
            "\n".join(
                [
                    bold(ctx.t("summarize.offline_header")),
                    "\n".join(f"• {line}" for line in excerpts) or "—",
                    "",
                    ctx.t("summarize.setup_hint"),
                ]
            )
        )
        return

    from ..services.ai_provider import AIError

    try:
        answer = await runtime.ai.complete(
            ctx.t("summarize.prompt", text=truncate(text, 6000)),
            system=ctx.t("summarize.system"),
            max_tokens=350,
            temperature=0.3,
        )
    except AIError as exc:
        raise CommandError(f"provider.{exc.kind}", detail=escape(str(exc)[:200])) from exc
    await ctx.reply(bold(ctx.t("summarize.header")) + "\n" + escape(answer))


def register(runtime: Any) -> None:
    runtime.registry.register_many(
        [
            Command("textstats", cmd_textstats, category="information", aliases=("stats",), requires_reply=False, parameters=("text",)),
            Command("cleanup", cmd_cleanup, category="information", requires_reply=False, hidden=True),
            Command("transform", cmd_transform, category="information", usage="<mode>", requires_reply=False, subcommands=('upper', 'lower', 'title', 'reverse', 'sort', 'sortd', 'dedupe', 'wrap', 'quote', 'trim')),
            Command("calc", cmd_calc, category="information", aliases=("math",), usage="<expression>", parameters=("expression",)),
            Command("random", cmd_random, category="fun", usage="[low] [high] | random uuid", subcommands=('uuid', 'guid')),
            Command("coin", cmd_coin, category="fun", aliases=("flip",)),
            Command("joke", cmd_joke, category="fun"),
            Command("quote", cmd_quote, category="fun"),
            Command("time", cmd_time, category="information", usage="[city...]", aliases=("clock-time",)),
            Command("convert-time", cmd_convert_time, category="information", usage="<HH:MM> <from> <to>", aliases=("convert",)),
            Command("translate", cmd_translate, category="information", aliases=("tr",), usage="[lang] [text]", requires_reply=False),
            Command("summarize", cmd_summarize, category="information", usage="[count]", aliases=("summary",)),
        ]
    )
