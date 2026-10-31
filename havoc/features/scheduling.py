"""Reminders, task management, quiet hours and calendar overviews.

Handlers registered here are the *delivery* side of the scheduler: the
scheduler owns timing and duplicate prevention, this module owns wording and
localization (spec §4: detection/scheduling separate from action).

Feature areas: 51, 52, 55, 56, 57, 58, 59, 60.
"""

from __future__ import annotations

import re
from datetime import UTC, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .. import ui
from ..command_registry import Command, CommandContext, CommandError
from ..utils.formatting import bold, code, escape, key_value_block, pre
from ..utils.parsing import paginate
from ..utils.security import redact_text
from ..utils.text import truncate
from ..utils.time import (
    TimeParseError,
    day_bounds,
    format_datetime,
    humanize_delta,
    iso,
    parse_clock,
    parse_iso,
    parse_recurrence,
    parse_when,
    utc_now,
    week_bounds,
)
from .helpers import parse_page_arg, tz_for


# ---------------------------------------------------------------------------
# Delivery handlers (registered with the scheduler)
# ---------------------------------------------------------------------------
async def deliver_reminder(runtime: Any, row: Any) -> None:
    chat_id = int(row["chat_id"])
    language = await runtime.resolve_language(row["text"], chat_id=chat_id)
    text = runtime.t(
        "reminder.delivered",
        language,
        text=escape(truncate(row["text"], 3000)),
        id=row["id"],
        prefix=runtime.prefix,
    )
    recurrence = row["recurrence"]
    if recurrence:
        next_at = parse_iso(row["remind_at"])
        if next_at is not None:
            text += "\n" + runtime.t(
                "reminder.next_run",
                language,
                when=escape(format_datetime(next_at, row["timezone"] or runtime.config.timezone, language)),
            )
    await runtime.sender.send(chat_id, text)


async def deliver_schedule(runtime: Any, row: Any) -> None:
    chat_id = int(row["chat_id"])
    await runtime.sender.send(chat_id, row["text"])


async def report_scheduler_problem(runtime: Any, kind: str, info: dict[str, Any], exc: BaseException) -> None:
    # Health first: diagnostics must know even if the notification fails.
    runtime.health.record_error(f"scheduler:{info.get('scope') or kind}", exc)
    language = runtime.config.default_language
    row = info.get("row") or {}
    scope = info.get("scope") or kind
    detail = redact_text(str(exc))[:200]
    text = runtime.t(
        "scheduler.problem",
        language,
        scope=escape(str(scope)),
        id=row.get("id", "—"),
        detail=escape(detail),
        prefix=runtime.prefix,
    )
    await runtime.notify_owner(text)


# ---------------------------------------------------------------------------
# Reminder commands
# ---------------------------------------------------------------------------
def _strip_tags(text: str) -> str:
    """Card lines are plain words; a locale that formats inside one is flattened."""
    return str(text).replace("<i>", "").replace("</i>", "").replace("<b>", "").replace("</b>", "")


async def cmd_remind(ctx: CommandContext) -> None:
    if not ctx.args:
        raise CommandError("errors.missing_argument", usage=ctx.usage_text())
    when_raw = ctx.arg(0)
    body = " ".join(ctx.args[1:]).strip()
    if not body:
        raise CommandError("remind.usage_hint", prefix=ctx.runtime.prefix)
    tz = tz_for(ctx)
    try:
        when = parse_when(when_raw, tz)
    except TimeParseError as exc:
        raise CommandError("errors.invalid_time", detail=escape(str(exc)), example="30m | tomorrow 09:00") from exc
    if when <= utc_now():
        # A bare clock time that has already gone by is *tomorrow's* — nobody
        # types "09:00" at eight in the evening meaning this morning.
        bare = re.fullmatch(r"(?i)\s*(?:at\s*)?(\d{1,2}:\d{2})\s*", when_raw or "")
        rolled = None
        if bare:
            try:
                rolled = parse_when(f"tomorrow {bare.group(1)}", tz)
            except TimeParseError:
                rolled = None
        if rolled is None or rolled <= utc_now():
            raise CommandError("errors.time_in_past", when=escape(format_datetime(when, tz, ctx.language)))
        when = rolled

    recurrence = parse_recurrence(ctx.flag("repeat") or ctx.flag("every") or None)
    if ctx.flag("repeat") and not recurrence:
        raise CommandError("errors.invalid_recurrence", value=escape(ctx.flag("repeat")))

    reminder_id = await ctx.store.add_reminder(
        ctx.chat_id,
        body,
        when,
        recurrence=recurrence,
        timezone_name=tz,
        created_by=getattr(ctx.event, "sender_id", None),
        meta={"language": ctx.language},
    )
    # The card, not the log line: this is the moment the owner is looking for an
    # answer, and "◷ Reminder #4 set for …" reads like a database row.
    lines = [
        ctx.t("remind.card_line", text=escape(truncate(body, 200))),
        _strip_tags(
            ctx.t(
                "remind.card_note",
                when=escape(format_datetime(when, tz, ctx.language)),
                relative=escape(humanize_delta(when - utc_now(), ctx.language)),
            )
        ),
    ]
    if recurrence:
        lines.append(ctx.t("remind.card_repeat", spec=escape(recurrence)))
    await ctx.reply(
        ui.card(
            ctx.t("remind.card_title", when=escape(format_datetime(when, tz, ctx.language))),
            lines,
            icon="◷",
            footer=ctx.t("remind.card_id", id=reminder_id),
        )
    )
    ctx.runtime.metrics.count("reminders_created")


async def cmd_reminders(ctx: CommandContext) -> None:
    action = (ctx.sub or "list").lower()
    if action in {"list", "ls"}:
        page = parse_page_arg(ctx.arg(0))
        rows = await ctx.store.list_reminders(status="pending", limit=200)
        items, total_pages = paginate(rows, page, 10)
        tz = tz_for(ctx)
        header = ui.title("◷", ctx.t("reminders.header", count=len(rows), page=page, pages=total_pages))
        if not rows:
            await ctx.reply(
                "\n\n".join([header, ui.note(ctx.t("reminders.empty", prefix=ctx.runtime.prefix))])
            )
            return
        rows_out: list[str] = []
        for row in items:
            when = parse_iso(row["remind_at"])
            stamp = format_datetime(when, tz, ctx.language) if when else "—"
            repeat = f" ↻ {ui.note(escape(str(row['recurrence'])))}" if row["recurrence"] else ""
            rows_out.append(
                f"{code('#' + str(row['id']))} {ui.bold(escape(stamp))}{repeat}\n"
                f"  {escape(truncate(row['text'], 80))}"
            )
        blocks = [header, ui.panel(rows_out)]
        if total_pages > 1:
            blocks.append(ui.footer(ctx.t("reminders.page_hint", prefix=ctx.runtime.prefix)))
        await ctx.reply("\n\n".join(blocks))
        return

    if action in {"done", "complete"}:
        reminder_id = _int(ctx.arg(0))
        row = await ctx.store.get_reminder(reminder_id)
        if row is None:
            raise CommandError("errors.not_found", what="reminder", id=reminder_id)
        if not await ctx.store.complete_reminder(reminder_id):
            raise CommandError("errors.not_found", what="pending reminder", id=reminder_id)
        await ctx.reply(ctx.t("reminders.completed", id=reminder_id))
        return

    if action in {"cancel", "delete", "rm"}:
        reminder_id = _int(ctx.arg(0))
        if not await ctx.store.cancel_reminder(reminder_id):
            raise CommandError("errors.not_found", what="pending reminder", id=reminder_id)
        await ctx.reply(ctx.t("reminders.cancelled", id=reminder_id))
        return

    if action in {"edit", "move"}:
        reminder_id = _int(ctx.arg(0))
        when_raw = ctx.arg(1)
        if not when_raw:
            raise CommandError("errors.missing_argument", usage=ctx.usage_text())
        row = await ctx.store.get_reminder(reminder_id)
        if row is None or row["status"] != "pending":
            raise CommandError("errors.not_found", what="pending reminder", id=reminder_id)
        try:
            when = parse_when(when_raw, tz_for(ctx))
        except TimeParseError as exc:
            raise CommandError("errors.invalid_time", detail=escape(str(exc)), example="in 2h | tomorrow 08:00") from exc
        await ctx.store.update_reminder(reminder_id, remind_at=when)
        await ctx.reply(ctx.t("reminders.moved", id=reminder_id, when=escape(format_datetime(when, tz_for(ctx), ctx.language))))
        return

    if action in {"missed", "held"}:
        rows = await ctx.store.db.fetch_all(
            "SELECT * FROM reminders WHERE status IN ('missed','held') ORDER BY remind_at DESC LIMIT 30"
        )
        if not rows:
            await ctx.reply(ctx.t("reminders.none_missed"))
            return
        lines = [ui.title("⚠", ctx.t("reminders.missed_header"))]
        entries: list[str] = []
        for row in rows:
            when = parse_iso(row["remind_at"])
            entries.append(
                f"{code('#' + str(row['id']))} {ui.note(escape(str(row['status'])))} "
                f"{escape(truncate(row['text'], 70))}\n"
                f"  {ui.note(escape(format_datetime(when, tz_for(ctx), ctx.language) if when else '—'))}"
            )
        if entries:
            lines.append(ui.panel(entries))
        lines.append(ui.footer(ctx.t("reminders.release_hint", prefix=ctx.runtime.prefix)))
        await ctx.reply("\n\n".join(line for line in lines if line.strip()))
        return

    if action in {"release", "revive"}:
        reminder_id = _int(ctx.arg(0))
        row = await ctx.store.get_reminder(reminder_id)
        if row is None or row["status"] not in {"missed", "held"}:
            raise CommandError("errors.not_found", what="missed reminder", id=reminder_id)
        when = utc_now() + timedelta(minutes=1)
        await ctx.store.db.execute(
            "UPDATE reminders SET status = 'pending', remind_at = ? WHERE id = ?", (iso(when), reminder_id)
        )
        await ctx.reply(ctx.t("reminders.released", id=reminder_id, when=escape(format_datetime(when, tz_for(ctx), ctx.language))))
        return

    raise CommandError("errors.invalid_choice", field="action", choices="list, done, cancel, edit, missed, release")


def _int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise CommandError("errors.not_a_number", field="id") from exc


# ---------------------------------------------------------------------------
# Task manager
# ---------------------------------------------------------------------------
async def cmd_tasks(ctx: CommandContext) -> None:
    action = (ctx.sub or "list").lower()
    scheduler = ctx.runtime.scheduler
    if action in {"list", "ls"}:
        items = await scheduler.pending(limit=30)
        tz = tz_for(ctx)
        header = ui.title("▦", ctx.t("tasks.header", count=len(items)))
        if not items:
            await ctx.reply("\n\n".join([header, ui.note(ctx.t("tasks.empty"))]))
            return
        rows_out: list[str] = []
        for item in items:
            when = parse_iso(item["when"])
            rows_out.append(
                f"{ui.icon(item['type'])} {code(escape(str(item['type'])))} "
                f"{ui.bold(escape(format_datetime(when, tz, ctx.language) if when else '—'))}\n"
                f"  {escape(truncate(item['text'], 60))}"
            )
        await ctx.reply(
            "\n\n".join([header, ui.panel(rows_out), ui.footer(ctx.t("tasks.hint", prefix=ctx.runtime.prefix))])
        )
        return

    if action in {"cancel", "rm", "delete"}:
        target = _int(ctx.arg(0))
        kind = ctx.arg(1).lower() or "reminder"
        if kind.startswith("sched"):
            ok = await ctx.store.cancel_schedule(target)
        elif kind.startswith("job"):
            changed = await ctx.store.db.execute(
                "UPDATE jobs SET status = 'cancelled' WHERE id = ? AND status = 'pending'", (target,)
            )
            ok = changed > 0
        else:
            ok = await ctx.store.cancel_reminder(target)
        if not ok:
            raise CommandError("errors.not_found", what="task", id=target)
        await ctx.reply(ctx.t("tasks.cancelled", id=target))
        return

    if action == "info":
        target = _int(ctx.arg(0))
        row = await ctx.store.get_reminder(target) or await ctx.store.get_schedule(target)
        if row is None:
            raise CommandError("errors.not_found", what="task", id=target)
        await ctx.reply(pre("\n".join(f"{key}: {row[key]}" for key in row.keys())))
        return

    raise CommandError("errors.invalid_choice", field="action", choices="list, cancel, info")


# ---------------------------------------------------------------------------
# Quiet hours
# ---------------------------------------------------------------------------
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
_SPAN = re.compile(
    # "4h", "for 4 hours", "تا ۴ ساعت" — and the Persian unit-first "ساعت ۴".
    r"^\s*(?:(?:for|next|تا|به\s*مدت)\s*)?"
    r"(?:(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>s|sec|secs|second|seconds|m|min|mins|minute|minutes|"
    r"h|hour|hours|d|day|days|ثانیه|دقیقه|ساعت|روز)"
    r"|(?P<unit2>ساعت|دقیقه|روز|ثانیه)\s*(?P<amount2>\d+(?:\.\d+)?))\s*$",
    re.I,
)
_SPAN_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "ثانیه": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60, "دقیقه": 60,
    "h": 3600, "hour": 3600, "hours": 3600, "ساعت": 3600,
    "d": 86400, "day": 86400, "days": 86400, "روز": 86400,
}


def _span(text: str) -> tuple[int, str]:
    """``4h`` → ``(14400, '4h')``; anything else is not a span."""
    raw = " ".join(str(text or "").translate(_DIGITS).split())
    match = _SPAN.match(raw)
    if not match:
        return 0, ""
    unit = (match.group("unit") or match.group("unit2") or "").lower()
    amount = match.group("amount") or match.group("amount2") or "0"
    seconds = int(float(amount) * _SPAN_UNITS.get(unit, 0))
    return (seconds, raw) if 60 <= seconds <= 366 * 86400 else (0, "")


async def cmd_quiet_hours(ctx: CommandContext) -> None:
    action = (ctx.sub or "status").lower()
    scope_chat = ctx.has_flag("chat")
    if action == "off":
        if scope_chat:
            await ctx.store.delete_chat_setting(ctx.chat_id, "quiet_hours")
        else:
            await ctx.store.delete_setting("quiet_hours")
        await ctx.reply(
            ui.card(
                ctx.t("quiet.off_title"),
                [ctx.t("quiet.off_line")],
                footer=ctx.t("quiet.scope_chat" if scope_chat else "quiet.scope_global"),
            )
        )
        return
    if action in {"on", "set"}:
        start, end = ctx.arg(0), ctx.arg(1)
        # "ساکت شو", "be quiet for four hours": a window measured from now,
        # because that is the sentence he actually says. The span can arrive in
        # one word ("4h") or two ("2 ساعت"), because the parser splits on spaces.
        if start:
            seconds, _ = _span(start)
            if not seconds:
                seconds, _ = _span(f"{start} {end}".strip())
            if seconds:
                tz = tz_for(ctx)
                begin = utc_now().astimezone(ZoneInfo(tz) if tz else UTC)
                finish = begin + timedelta(seconds=seconds)
                start, end = begin.strftime("%H:%M"), finish.strftime("%H:%M")
        if not start and not end:
            # "quiet hours on" with no window means the night, which is what the
            # sentence has always meant. An error here was a dead end.
            start, end = "23:00", "07:00"
        if not start or not end:
            raise CommandError("errors.missing_argument", usage=ctx.usage_text())
        try:
            parse_clock(start)
            parse_clock(end)
        except TimeParseError as exc:
            raise CommandError("errors.invalid_time", detail=escape(str(exc)), example="23:00 07:00") from exc
        payload = {"start": start, "end": end, "timezone": tz_for(ctx)}
        if scope_chat:
            await ctx.store.set_chat_setting(ctx.chat_id, "quiet_hours", payload)
        else:
            await ctx.store.set_setting("quiet_hours", payload)
        await ctx.reply(
            ui.card(
                ctx.t("quiet.set_title"),
                [
                    ctx.t("quiet.set_line", start=start, end=end),
                    ctx.t("quiet.scope_line", scope=ctx.t("quiet.scope_chat" if scope_chat else "quiet.scope_global")),
                ],
                footer=escape(payload["timezone"]),
            )
        )
        return
    config = await ctx.runtime.permissions.quiet_hours_config(ctx.chat_id if scope_chat else None)
    active = await ctx.runtime.permissions.in_quiet_hours(ctx.chat_id)
    if not config:
        await ctx.reply(
            ui.card(
                ctx.t("quiet.header"),
                [ctx.t("quiet.status_off", prefix=ctx.runtime.prefix)],
            )
        )
        return
    await ctx.reply(
        "\n".join(
            [
                bold(ctx.t("quiet.header")),
                key_value_block(
                    [
                        (ctx.t("quiet.start"), config.get("start")),
                        (ctx.t("quiet.end"), config.get("end")),
                        (ctx.t("quiet.timezone"), config.get("timezone")),
                        (ctx.t("quiet.active_now"), ctx.t("common.yes" if active else "common.no")),
                    ]
                ),
            ]
        )
    )


# ---------------------------------------------------------------------------
# Agenda (day/week overview)
# ---------------------------------------------------------------------------
async def cmd_agenda(ctx: CommandContext) -> None:
    scope = (ctx.sub or "today").lower()
    tz = tz_for(ctx)
    today = utc_now().astimezone(safe_zone_shim(tz)).date()
    if scope in {"week", "هفته"}:
        start, end = week_bounds(today, tz)
        title_key = "agenda.week_header"
    else:
        start, end = day_bounds(today, tz)
        title_key = "agenda.day_header"

    reminders = await ctx.store.db.fetch_all(
        "SELECT * FROM reminders WHERE status = 'pending' AND remind_at >= ? AND remind_at < ? ORDER BY remind_at",
        (iso(start), iso(end)),
    )
    schedules = await ctx.store.db.fetch_all(
        "SELECT * FROM schedules WHERE enabled = 1 AND run_at >= ? AND run_at < ? ORDER BY run_at",
        (iso(start), iso(end)),
    )
    header = ui.title("▦", ctx.t(title_key, date=escape(format_datetime(start, tz, ctx.language))))
    if not reminders and not schedules:
        await ctx.reply("\n\n".join([header, ui.note(ctx.t("agenda.empty"))]))
        return
    entries: list[str] = []
    for row in reminders:
        when = parse_iso(row["remind_at"])
        entries.append(
            f"{ui.icon('reminder')} {ui.bold(escape(format_datetime(when, tz, ctx.language) if when else '—'))}\n"
            f"  {escape(truncate(row['text'], 70))}"
        )
    for row in schedules:
        when = parse_iso(row["run_at"])
        entries.append(
            f"✉ {ui.bold(escape(format_datetime(when, tz, ctx.language) if when else '—'))}\n"
            f"  {escape(truncate(row['text'], 70))} {ui.note('→ ' + str(row['chat_id']))}"
        )
    await ctx.reply("\n\n".join([header, ui.panel(entries)]))


def safe_zone_shim(tz: str):
    from ..utils.time import safe_zone

    return safe_zone(tz)


def register(runtime: Any) -> None:
    runtime.scheduler.on_reminder = lambda row: deliver_reminder(runtime, row)
    runtime.scheduler.on_schedule = lambda row: deliver_schedule(runtime, row)
    runtime.scheduler.on_error = lambda kind, info, exc: report_scheduler_problem(runtime, kind, info, exc)

    runtime.registry.register_many(
        [
            Command("remind", cmd_remind, category="scheduling", aliases=("reminder", "یادآوری"), usage="<when> <text> [--repeat daily]", changes_state=True),
            Command("reminders", cmd_reminders, category="scheduling", usage="[list|done|cancel|edit|missed|release]", changes_state=True, subcommands=('list', 'ls', 'done', 'complete', 'cancel', 'delete', 'rm', 'edit', 'move', 'missed', 'held', 'release', 'revive')),
            Command("tasks", cmd_tasks, category="scheduling", usage="[list|cancel|info]", changes_state=True, subcommands=('list', 'ls', 'cancel', 'rm', 'delete', 'info')),
            Command("quiet-hours", cmd_quiet_hours, category="scheduling", aliases=("quiet",), usage="[on <start> <end>|off|status]", changes_state=True, subcommands=('on', 'set', 'off', 'status')),
            Command("agenda", cmd_agenda, category="scheduling", aliases=("calendar",), usage="[today|week]", subcommands=('today', 'week', 'هفته')),
        ]
    )
