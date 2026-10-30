"""Messaging tools: auto-replies, away mode, templates, scheduled messages.

These commands *configure* behaviour. The actual automatic replies run in
``havoc.features.automation`` only when the corresponding feature is enabled,
the chat is allowed, quiet hours are inactive and the emergency stop is off.

Feature areas: 21, 22, 23, 24, 25, 26, 27, 28.
"""

from __future__ import annotations

import re
from typing import Any

from .. import fight, ui
from ..command_registry import Command, CommandContext, CommandError
from ..content import (
    build_offline_comeback,
    contains_harassment,
    extract_hook,
)
from ..language_detection import detect_language
from ..services.ai_provider import AIError
from ..utils.formatting import bold, code, escape, key_value_block, pre
from ..utils.templates import TemplateError, render_template, template_values, validate_template
from ..utils.text import truncate
from ..utils.time import (
    TimeParseError,
    format_datetime,
    humanize_delta,
    parse_iso,
    parse_recurrence,
    parse_when,
    utc_now,
)
from ..utils.validation import validate_message_body, validate_template_name
from .helpers import fmt_time, reply_text, require_reply, resolve_target, tz_for

MATCH_TYPES = ("contains", "exact", "startswith", "endswith", "regex", "any")
AWAY_COOLDOWN = 900  # per person, 15 minutes


# ---------------------------------------------------------------------------
# Auto-replies
# ---------------------------------------------------------------------------
def _split_rule(raw: str) -> tuple[str, str]:
    for separator in ("=>", "->", "::", ":"):
        if separator in raw:
            pattern, _, response = raw.partition(separator)
            return pattern.strip(), response.strip()
    raise CommandError("autoreply.separator_hint", separator="=>")


async def cmd_autoreply(ctx: CommandContext) -> None:
    action = (ctx.sub or "list").lower()
    store = ctx.store
    if action in {"list", "ls"}:
        rows = await store.list_autoreplies()
        if not rows:
            await ctx.reply(ctx.t("autoreply.empty", prefix=ctx.runtime.prefix))
            return
        lines = [bold(ctx.t("autoreply.header", count=len(rows)))]
        for row in rows:
            state = "●" if row["enabled"] else "⚪"
            where = str(row["scope"] or "any") if "scope" in row.keys() else "any"
            tag = "" if where == "any" else f" · {escape(ctx.t(f'autoreply.scope_{where}'))}"
            lines.append(
                f"{state} #{row['id']} [{escape(row['match_type'])}{tag}] {escape(truncate(row['pattern'], 30))} → "
                f"{escape(truncate(row['response'], 40))}"
            )
        lines.append("")
        lines.append(ctx.t("autoreply.list_hint", prefix=ctx.runtime.prefix))
        await ctx.reply("\n".join(lines))
        return
    if action == "add":
        raw = " ".join(ctx.args).strip()
        if not raw:
            raise CommandError("errors.missing_argument", usage=ctx.usage_text())
        pattern, response = _split_rule(raw)
        match_type = (ctx.flag("type") or "contains").lower()
        # "reply to anyone who DMs me" needs no trigger word — the trigger is the
        # scope. ``--type any --private`` is that request written as flags.
        if not pattern and match_type == "any":
            pattern = "*"
        if not pattern or not response:
            raise CommandError("autoreply.separator_hint", separator="=>")
        if match_type not in MATCH_TYPES:
            raise CommandError("errors.invalid_choice", field="type", choices=", ".join(MATCH_TYPES))
        if match_type == "regex":
            try:
                re.compile(pattern)
            except re.error as exc:
                raise CommandError("errors.invalid_regex", detail=escape(str(exc))) from exc
        try:
            cooldown = int(ctx.flag("cooldown") or 300)
        except ValueError as exc:
            raise CommandError("errors.not_a_number", field="cooldown") from exc
        scope = ctx.chat_id if ctx.has_flag("chat") else None
        audience = "any"
        if ctx.has_flag("private"):
            audience = "private"
        elif ctx.has_flag("group"):
            audience = "group"
        validate_message_body(response)
        rule_id = await store.add_autoreply(
            pattern,
            response,
            match_type=match_type,
            language=ctx.flag("lang") or "auto",
            scope_chat_id=scope,
            cooldown=cooldown,
            scope=audience,
        )
        await ctx.reply(
            ctx.t("autoreply.added", id=rule_id, pattern=escape(truncate(pattern, 40)))
            + "\n"
            + ctx.t("autoreply.enable_hint", prefix=ctx.runtime.prefix)
        )
        return
    if action in {"on", "off"}:
        await ctx.runtime.feature_manager.set_enabled("autoreply", action == "on")
        await ctx.reply(ctx.t("autoreply.toggled", state=ctx.t("common.on" if action == "on" else "common.off")))
        return
    if action in {"remove", "delete", "rm"}:
        rule_id = _int_arg(ctx, 0)
        if not await store.delete_autoreply(rule_id):
            raise CommandError("errors.not_found", what="auto-reply", id=rule_id)
        await ctx.reply(ctx.t("autoreply.removed", id=rule_id))
        return
    if action == "enable":
        rule_id = _int_arg(ctx, 0)
        if not await store.set_autoreply_enabled(rule_id, True):
            raise CommandError("errors.not_found", what="auto-reply", id=rule_id)
        await ctx.reply(ctx.t("autoreply.rule_enabled", id=rule_id))
        return
    if action == "disable":
        rule_id = _int_arg(ctx, 0)
        if not await store.set_autoreply_enabled(rule_id, False):
            raise CommandError("errors.not_found", what="auto-reply", id=rule_id)
        await ctx.reply(ctx.t("autoreply.rule_disabled", id=rule_id))
        return
    if action == "clear":
        count = await store.clear_autoreplies()
        await ctx.reply(ctx.t("autoreply.cleared", count=count))
        return
    raise CommandError("errors.invalid_choice", field="action", choices="list, add, on, off, remove, enable, disable, clear")


# ---------------------------------------------------------------------------
# Away mode
# ---------------------------------------------------------------------------
async def cmd_away(ctx: CommandContext) -> None:
    action = (ctx.sub or "status").lower()
    if action == "on":
        message = ctx.rest_raw.strip()
        if not message:
            raise CommandError("errors.missing_argument", usage=ctx.usage_text())
        message = validate_message_body(message)
        await ctx.store.set_setting("away.message", message)
        await ctx.store.set_setting("away.enabled", True)
        await ctx.store.set_setting("away.since", utc_now().isoformat())
        await ctx.runtime.feature_manager.set_enabled("away", True)
        await ctx.reply(ctx.t("away.enabled", text=escape(truncate(message, 200))))
        return
    if action == "off":
        await ctx.store.set_setting("away.enabled", False)
        await ctx.runtime.feature_manager.set_enabled("away", False)
        await ctx.reply(ctx.t("away.disabled"))
        return
    if action in {"status", "show"}:
        enabled = await ctx.store.get_bool("away.enabled", False)
        message = await ctx.store.get_setting("away.message", "") or "—"
        since = parse_iso(await ctx.store.get_setting("away.since", ""))
        await ctx.reply(
            bold(ctx.t("away.header"))
            + "\n"
            + key_value_block(
                [
                    (ctx.t("away.state"), ctx.t("common.on" if enabled else "common.off")),
                    (ctx.t("away.message"), truncate(str(message), 120)),
                    (ctx.t("away.since"), fmt_time(since.isoformat() if since else None, tz_for(ctx), ctx.language)),
                    (ctx.t("away.cooldown"), f"{AWAY_COOLDOWN // 60} min"),
                ]
            )
        )
        return
    raise CommandError("errors.invalid_choice", field="action", choices="on, off, status")


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
async def cmd_template(ctx: CommandContext) -> None:
    action = (ctx.sub or "list").lower()
    store = ctx.store
    if action in {"list", "ls"}:
        rows = await store.list_templates()
        if not rows:
            await ctx.reply(ctx.t("template.empty", prefix=ctx.runtime.prefix))
            return
        lines = [bold(ctx.t("template.header", count=len(rows)))]
        for row in rows:
            lines.append(f"• {code(escape(row['name']))} — {escape(truncate(row['body'], 60))}")
        await ctx.reply("\n".join(lines))
        return
    if action == "add":
        name = validate_template_name(ctx.arg(0))
        body = ctx.rest_raw
        if body.startswith(ctx.arg(0)):
            body = body[len(ctx.arg(0)) :].strip()
        if not body:
            raise CommandError("errors.missing_argument", usage=ctx.usage_text())
        body = validate_message_body(body)
        try:
            validation = validate_template(body)
        except TemplateError as exc:
            raise CommandError(getattr(exc, "code", "template.invalid"), detail=escape(str(exc))) from exc
        if validation.unknown:
            raise CommandError("template.unknown_placeholder", names=escape(", ".join(validation.unknown)))
        existing = await store.get_template(name)
        if existing is not None:
            if not ctx.has_flag("replace"):
                raise CommandError("template.exists", name=escape(name), prefix=ctx.runtime.prefix)
            await store.update_template(name, body)
            await ctx.reply(
                ctx.t("template.updated", name=escape(name), placeholders=escape(", ".join(validation.placeholders) or "—"))
            )
            return
        await store.add_template(name, body, language=ctx.flag("lang") or "auto")
        await ctx.reply(ctx.t("template.created", name=escape(name), placeholders=escape(", ".join(validation.placeholders) or "—")))
        return
    if action in {"show", "preview"}:
        name = (ctx.arg(0) or "").strip().lower()
        row = await store.get_template(name)
        if row is None:
            raise CommandError("errors.not_found", what="template", id=escape(name))
        await ctx.reply(
            bold(ctx.t("template.preview_header", name=escape(name)))
            + "\n"
            + pre(truncate(row["body"], 3000))
        )
        return
    if action == "send":
        name = (ctx.arg(0) or "").strip().lower()
        row = await store.get_template(name)
        if row is None:
            raise CommandError("errors.not_found", what="template", id=escape(name))
        target_raw = ctx.arg(1)
        values = await _template_values(ctx)
        try:
            body = render_template(row["body"], values)
        except TemplateError as exc:
            raise CommandError(getattr(exc, "code", "template.invalid"), detail=escape(str(exc))) from exc
        if target_raw:
            chat_id, _entity, error = await resolve_target(ctx, target_raw)
            if error or chat_id is None:
                raise CommandError(error or "errors.entity_not_found")
            await ctx.runtime.sender.send(chat_id, body)
        else:
            await ctx.reply(body, reply=False)
        runtime = ctx.runtime
        await runtime.store.bump_usage("template", "sent")
        await ctx.reply_ok(ctx.t("template.sent", name=escape(name)))
        return
    if action in {"delete", "rm"}:
        name = (ctx.arg(0) or "").strip().lower()
        if not await store.delete_template(name):
            raise CommandError("errors.not_found", what="template", id=escape(name))
        await ctx.reply(ctx.t("template.deleted", name=escape(name)))
        return
    raise CommandError("errors.invalid_choice", field="action", choices="list, add, show, send, delete")


async def _template_values(ctx: CommandContext) -> dict[str, Any]:
    from .helpers import display_name

    me = getattr(ctx.runtime.telegram, "me", None) if ctx.runtime.telegram else None
    entity = getattr(ctx.event, "chat", None)
    values = template_values(
        name=display_name(entity, ""),
        chat=display_name(entity, ""),
        me=getattr(me, "first_name", "") if me is not None else "",
        id=ctx.chat_id,
    )
    # ``--name Ali`` and friends override the automatic values.
    for key, value in ctx.parsed.flags.items():
        if key not in {"yes", "replace", "ai", "chat"}:
            values[key] = value
    return values


# ---------------------------------------------------------------------------
# Scheduled messages
# ---------------------------------------------------------------------------
async def cmd_schedule(ctx: CommandContext) -> None:
    if not ctx.args:
        raise CommandError("errors.missing_argument", usage=ctx.usage_text())
    when_raw = ctx.arg(0)
    body = " ".join(ctx.args[1:]).strip()
    if not body:
        raise CommandError("errors.missing_argument", usage=ctx.usage_text())
    tz = tz_for(ctx)
    try:
        when = parse_when(when_raw, tz)
    except TimeParseError as exc:
        raise CommandError("errors.invalid_time", detail=escape(str(exc)), example="in 2h | 2026-11-01 09:00") from exc
    if when <= utc_now():
        raise CommandError("errors.time_in_past", when=escape(format_datetime(when, tz, ctx.language)))
    recurrence = parse_recurrence(ctx.flag("repeat") or None)
    if ctx.flag("repeat") and not recurrence:
        raise CommandError("errors.invalid_recurrence", value=escape(ctx.flag("repeat")))
    target_raw = ctx.flag("to")
    chat_id = ctx.chat_id
    if target_raw:
        resolved, _entity, error = await resolve_target(ctx, target_raw)
        if error or resolved is None:
            raise CommandError(error or "errors.entity_not_found")
        chat_id = resolved
    body = validate_message_body(body, limit=4000)
    schedule_id = await ctx.store.add_schedule(
        chat_id,
        body,
        when,
        recurrence=recurrence,
        timezone_name=tz,
        created_by=getattr(ctx.event, "sender_id", None),
        meta={"language": ctx.language},
    )
    await ctx.reply(
        ctx.t(
            "schedule.created",
            id=schedule_id,
            when=escape(format_datetime(when, tz, ctx.language)),
            relative=escape(humanize_delta(when - utc_now(), ctx.language)),
            chat=chat_id,
        )
        + (("\n" + ctx.t("schedule.recurring", spec=escape(recurrence))) if recurrence else "")
    )
    ctx.runtime.metrics.count("schedules_created")


async def cmd_scheduled(ctx: CommandContext) -> None:
    action = (ctx.sub or "list").lower()
    if action in {"list", "ls"}:
        rows = await ctx.store.list_schedules(enabled_only=True)
        if not rows:
            await ctx.reply(ctx.t("scheduled.empty"))
            return
        tz = tz_for(ctx)
        lines = [bold(ctx.t("scheduled.header", count=len(rows)))]
        for row in rows:
            when = parse_iso(row["run_at"])
            state = "●" if row["enabled"] else "⚪"
            lines.append(
                f"{state} #{row['id']} {code(escape(format_datetime(when, tz, ctx.language) if when else '—'))} — "
                f"{escape(truncate(row['text'], 50))} → {row['chat_id']}"
                + (f" ↻ {escape(str(row['recurrence']))}" if row["recurrence"] else "")
            )
        await ctx.reply("\n".join(lines))
        return
    if action in {"cancel", "delete", "rm"}:
        schedule_id = _int_arg(ctx, 0)
        if not await ctx.store.cancel_schedule(schedule_id):
            raise CommandError("errors.not_found", what="schedule", id=schedule_id)
        await ctx.reply(ctx.t("scheduled.cancelled", id=schedule_id))
        return
    if action in {"reschedule", "move"}:
        schedule_id = _int_arg(ctx, 0)
        when_raw = ctx.arg(1)
        if not when_raw:
            raise CommandError("errors.missing_argument", usage=ctx.usage_text())
        try:
            when = parse_when(when_raw, tz_for(ctx))
        except TimeParseError as exc:
            raise CommandError("errors.invalid_time", detail=escape(str(exc)), example="tomorrow 09:00") from exc
        if not await ctx.store.reschedule(schedule_id, when):
            raise CommandError("errors.not_found", what="schedule", id=schedule_id)
        await ctx.reply(ctx.t("scheduled.moved", id=schedule_id, when=escape(format_datetime(when, tz_for(ctx), ctx.language))))
        return
    raise CommandError("errors.invalid_choice", field="action", choices="list, cancel, reschedule")


# ---------------------------------------------------------------------------
# Fight Mode
# ---------------------------------------------------------------------------
#: Words that turn a normal ``.fight`` into the repeating loop.
LOOP_WORDS = frozenset({"instant", "repeat", "repeated", "loop", "spam", "nonstop", "non-stop",
                        "پیوسته", "تکراری", "مداوم", "بی‌وقفه"})
STOP_WORDS = frozenset({"stop", "halt", "enough", "بسه", "بس", "کافیه", "تمام"})


def _fight_keywords(words: list[str]) -> dict[str, Any]:
    """Sort free-form arguments into style / intensity / mode.

    ``fight instant repeated brutal`` has no subcommand at all — the parser
    demotes the first unknown token into the arguments — so the words have to be
    read as a bag rather than a position.
    """
    found: dict[str, Any] = {"loop": False, "interval": None, "stop": False, "leftovers": []}
    index = 0
    while index < len(words):
        word = words[index].strip().lower()
        index += 1
        if not word:
            continue
        if word in LOOP_WORDS:
            found["loop"] = True
            continue
        if word in STOP_WORDS:
            found["stop"] = True
            continue
        if word in {"every", "هر"} and index < len(words):
            found["interval"] = words[index]
            index += 1
            continue
        if word in fight.STYLE_ALIASES:
            found["style"] = word
            continue
        if word in fight.INTENSITY_ALIASES:
            found["intensity"] = word
            continue
        if word.isdigit():
            found["interval"] = word
            continue
        found["leftovers"].append(word)
    return found


async def cmd_fight(ctx: CommandContext) -> None:
    runtime = ctx.runtime
    action = (ctx.sub or "").lower()
    words = [word for word in ctx.args if word.strip()]

    if action in {"styles", "help"}:
        rows = [ui.item(style, ctx.t(f"fight.style_{style}", default=style)) for style in fight.STYLES]
        intensities = " · ".join(
            escape(ctx.t(f"fight.intensity_{level}", default=level)) for level in fight.INTENSITIES
        )
        await ctx.reply(
            "\n\n".join(
                [
                    ui.title("⚔", ctx.t("fight.styles_header")),
                    ui.panel(rows),
                    ui.note(ctx.t("fight.intensities_line", levels=intensities)),
                    ui.note(ctx.t("fight.capacity_line", count=fight.capacity())),
                    ui.footer(ctx.t("fight.styles_hint", prefix=runtime.prefix)),
                ]
            )
        )
        return

    if action == "stop" or (words and words[0].lower() in STOP_WORDS):
        stopped = await runtime.fight_loop.stop(ctx.chat_id)
        await ctx.reply_ok(
            ctx.t("fight.loop_stopped" if stopped else "fight.loop_none")
        )
        return

    if action == "send":
        drafted = await ctx.store.get_chat_setting(ctx.chat_id, "fight.draft", None)
        if not drafted:
            raise CommandError("fight.no_draft", prefix=ctx.runtime.prefix)
        text = str(drafted)
        await runtime.sender.send(ctx.chat_id, escape(text))
        await ctx.store.delete_chat_setting(ctx.chat_id, "fight.draft")
        await ctx.reply_ok(ctx.t("fight.sent"))
        return

    # Everything after the command name, in both the subcommand and args slots.
    keywords = _fight_keywords([action, *words] if action else words)

    reply = await ctx.reply_message()
    if reply is None:
        raise CommandError("errors.reply_required", prefix=runtime.prefix, name="fight")

    source = reply_text(reply)
    if not source.strip():
        raise CommandError("errors.no_text")
    if contains_harassment(source):
        raise CommandError("fight.unsafe_target")

    language = ctx.flag("lang") if ctx.flag("lang") in {"en", "fa"} else detect_language(source).resolve(ctx.language)
    style = fight.normalize_style(keywords.get("style") or action, language)
    intensity = fight.normalize_intensity(
        ctx.flag("intensity") or keywords.get("intensity") or "medium"
    )
    hook = extract_hook(source, language)

    if keywords["loop"]:
        await _start_loop(
            ctx,
            hook=hook,
            source=source,
            style=style,
            intensity=intensity,
            language=language,
            interval_word=keywords.get("interval"),
        )
        return

    result = None
    if runtime.ai.available and (ctx.has_flag("ai") or await ctx.store.get_bool("fight.use_ai", False)):
        from ..content import FIGHT_SYSTEM_PROMPT

        try:
            answer = await runtime.ai.complete(
                ctx.t("fight.ai_prompt", style=style, message=truncate(" ".join(source.split()), 400)),
                system=FIGHT_SYSTEM_PROMPT.format(language_name=ctx.t("fight.language_name", default=language)),
                max_tokens=120,
                temperature=0.9,
            )
            if not contains_harassment(answer):
                result = _ComebackLike(answer.strip(), style, language, intensity, "ai")
        except AIError as exc:
            runtime.logger.warning("fight: AI failed (%s), using offline templates", exc.kind)

    if result is None:
        result = build_offline_comeback(
            hook=hook, style=style, intensity=intensity, language=language, source_text=source
        )

    await ctx.store.set_chat_setting(ctx.chat_id, "fight.draft", result.text)
    facts = [
        f"{ui.bold(ctx.t('fight.style'))} {escape(ctx.t(f'fight.style_{result.style}', default=result.style))}",
        f"{ui.bold(ctx.t('fight.intensity'))} {escape(fight.INTENSITY_LABELS.get(language, fight.INTENSITY_LABELS['en']).get(intensity, intensity))}",
        f"{ui.bold(ctx.t('fight.source'))} {ui.note(escape(result.source))}",
    ]
    blocks = [
        ui.title("⚔", ctx.t("fight.header")),
        ui.note(ctx.t("fight.their_line")),
        ui.quote_card(" ".join(source.split())[:280], ""),
        ui.note(ctx.t("fight.your_line")),
        ui.quote_card(result.text, ""),
        ui.panel(facts),
    ]
    if intensity in {"intense", "unhinged"}:
        # The escalation has to be visible, not just advertised: at this level
        # the card ends with a terminal readout, monospaced and dry.
        blocks.append(
            ui.code_block(
                [
                    f"target  : {truncate(' '.join(source.split()), 48)}",
                    f"style   : {style}",
                    f"verdict : {'annihilated' if intensity == 'unhinged' else 'dismantled'}",
                    f"damage  : {'██████████' if intensity == 'unhinged' else '██████▱▱▱▱'}",
                ]
            )
        )
    blocks.append(ui.footer(ctx.t("fight.preview_hint", prefix=runtime.prefix)))
    await ctx.reply("\n\n".join(blocks))


async def _start_loop(
    ctx: CommandContext,
    *,
    hook: str,
    source: str,
    style: str,
    intensity: str,
    language: str,
    interval_word: Any,
) -> None:
    """Start the repeating stream and answer with one card, once."""
    from ..services.fight_loop import DEFAULT_INTERVAL, MIN_INTERVAL

    interval = DEFAULT_INTERVAL
    if interval_word:
        try:
            interval = max(MIN_INTERVAL, float(str(interval_word).rstrip("s")))
        except (TypeError, ValueError):
            interval = DEFAULT_INTERVAL

    state = await ctx.runtime.fight_loop.start(
        ctx.chat_id,
        hook=hook,
        style=style,
        intensity=intensity,
        language=language,
        interval=interval,
        source_text=source,
    )
    facts = [
        f"{ui.bold(ctx.t('fight.style'))} {escape(ctx.t(f'fight.style_{state.style}', default=state.style))}",
        f"{ui.bold(ctx.t('fight.intensity'))} {escape(fight.INTENSITY_LABELS.get(language, fight.INTENSITY_LABELS['en']).get(state.intensity, state.intensity))}",
        f"{ui.bold(ctx.t('fight.loop_interval'))} {ui.code(f'{state.interval:.0f}s')}",
    ]
    await ctx.reply(
        "\n\n".join(
            [
                ui.title("⚔", ctx.t("fight.loop_header")),
                ui.note(ctx.t("fight.their_line")),
                ui.quote_card(" ".join(source.split())[:280], ""),
                ui.panel(facts),
                ui.footer(ctx.t("fight.loop_hint", prefix=ctx.runtime.prefix)),
            ]
        )
    )


class _ComebackLike:
    def __init__(self, text: str, style: str, language: str, intensity: str, source: str) -> None:
        self.text = text
        self.style = style
        self.language = language
        self.intensity = intensity
        self.source = source


# ---------------------------------------------------------------------------
# Reply helper / suggestions
# ---------------------------------------------------------------------------
SUGGESTION_TONES = ("friendly", "formal", "funny", "supportive", "direct")
_OFFLINE_SUGGESTIONS: dict[str, dict[str, tuple[str, ...]]] = {
    "en": {
        "friendly": ("Sounds good to me ◡", "Thanks for the message — let's do it!", "Got it, appreciate you telling me."),
        "formal": ("Thank you for the update. I will follow up shortly.", "Noted; I will review and reply in detail.", "Received — I will get back to you today."),
        "funny": ("Bold of you to assume I read that before replying. (I did.)", "I have consulted my inner committee. We approve.", "Reply loading… 99%… done."),
        "supportive": ("That sounds heavy — I'm here if you want to talk.", "You handled that better than most people would.", "Take your time; no rush on my side."),
        "direct": ("Yes, I agree.", "No, not this time.", "Let's decide by tomorrow."),
    },
    "fa": {
        "friendly": ("حتماً، بریم جلو ◡", "ممنون که گفتی — عالیه!", "دریافت شد، ممنون."),
        "formal": ("از به‌روزرسانی شما سپاسگزارم؛ به‌زودی پیگیری می‌کنم.", "دریافت شد و در اولین فرصت پاسخ می‌دهم.", "ممنون، امروز بررسی می‌کنم."),
        "funny": ("جسارت کردی که فکر کنی قبل از جواب دادن خوندم. (خوندم.)", "با کمیته داخلی مشورت کردم؛ تأیید شد.", "در حال آماده‌سازی پاسخ… ۹۹٪… تمام."),
        "supportive": ("سنگین به نظر می‌رسه — اگر خواستی حرف بزنیم، هستم.", "از پسش بهتر از خیلی‌ها براومدی.", "عجله نکن؛ از سمت من فشاری نیست."),
        "direct": ("بله، موافقم.", "نه، این بار نه.", "تا فردا تصمیم بگیریم."),
    },
}


async def cmd_suggest(ctx: CommandContext) -> None:
    reply = await require_reply(ctx)
    source = reply_text(reply)
    if not source.strip():
        raise CommandError("errors.no_text")
    language = detect_language(source).resolve(ctx.language)
    tone = (ctx.sub or "friendly").lower()
    if tone not in SUGGESTION_TONES:
        raise CommandError("errors.invalid_choice", field="tone", choices=", ".join(SUGGESTION_TONES))

    options: list[str] = []
    if ctx.runtime.ai.available and ctx.has_flag("ai"):
        try:
            answer = await ctx.runtime.ai.complete(
                ctx.t("suggest.ai_prompt", text=truncate(source, 1200), tone=tone),
                max_tokens=200,
                temperature=0.85,
            )
            options = [line.strip(" -•\t") for line in answer.splitlines() if line.strip()][:3]
        except AIError as exc:
            ctx.runtime.logger.warning("suggest: AI failed (%s)", exc.kind)
    if not options:
        options = list(_OFFLINE_SUGGESTIONS.get(language, _OFFLINE_SUGGESTIONS["en"])[tone])

    await ctx.reply(
        "\n".join(
            [
                bold(ctx.t("suggest.header", tone=escape(tone), language=escape(language))),
                pre("\n".join(f"{index}. {option}" for index, option in enumerate(options, 1))),
                ctx.t("suggest.send_hint", prefix=ctx.runtime.prefix),
            ]
        )
    )


def _int_arg(ctx: CommandContext, index: int) -> int:
    raw = ctx.arg(index)
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise CommandError("errors.not_a_number", field="id") from exc


def register(runtime: Any) -> None:
    runtime.registry.register_many(
        [
            Command("autoreply", cmd_autoreply, category="messaging", aliases=("ar",), usage="[list|add|on|off|remove|clear]", changes_state=True, subcommands=('list', 'ls', 'add', 'on', 'off', 'remove', 'delete', 'rm', 'enable', 'disable', 'clear')),
            Command("away", cmd_away, category="messaging", usage="[on <text>|off|status]", changes_state=True, subcommands=('on', 'off', 'status', 'show')),
            Command("template", cmd_template, category="messaging", usage="[list|add|show|send|delete]", changes_state=True, subcommands=('list', 'ls', 'add', 'show', 'preview', 'send', 'delete', 'rm')),
            Command("schedule", cmd_schedule, category="messaging", usage="<when> <text> [--to chat] [--repeat daily]", changes_state=True),
            Command("scheduled", cmd_scheduled, category="messaging", usage="[list|cancel|reschedule]", changes_state=True, subcommands=('list', 'ls', 'cancel', 'delete', 'rm', 'reschedule', 'move')),
            Command("fight", cmd_fight, category="fun", usage="[style|intensity|instant|stop|send|styles]", requires_reply=False, changes_state=True, subcommands=("send", "styles", "help", "stop", "instant", "repeat", "repeated", "loop", "nonstop", "every", "low", "medium", "high", "intense", "unhinged", "ملایم", "متوسط", "شدید", "سنگین", "دیوانه", "blunt", "brutal", "clever", "direct", "dramatic", "dry", "fun", "harsh", "playful", "sarcasm", "sarcastic", "savage", "smart", "احساسی", "بازیگوش", "بی‌رحم", "خشن", "خشک", "دراماتیک", "رک", "زیرکانه", "شوخ", "صریح", "طعنه", "هوشمند", "وحشی", "کنایه")),
            Command("suggest", cmd_suggest, category="messaging", aliases=("reply", "draft"), usage="[tone]", requires_reply=False, subcommands=('friendly', 'formal', 'funny', 'supportive', 'direct')),
        ]
    )
