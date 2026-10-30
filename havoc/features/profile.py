"""Profile tools: live clock, dynamic bio, presets, counters and history.

Safety model
------------
* Changes are conservative: a 1-minute update interval for the clock, a
  10-minute cadence for bio rotation, and a 60-second limiter on any profile
  edit so Telegram never sees a burst.
* The original name/bio is snapshotted before HAVOC's first change, and
  ``.profile restore`` puts it back.
* ``.profile preview`` shows exactly what would change before anything is
  applied; ``.profile history`` records every change with a reason.
* Everything defaults to OFF and obeys dry-run mode.

Feature areas: 11-20, 15, 17, 18, 19, 20, 92.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..command_registry import Command, CommandContext, CommandError
from ..utils.formatting import bold, code, escape, key_value_block, pre
from ..utils.security import redact_text
from ..utils.time import (
    TimeParseError,
    format_datetime,
    next_occurrence,
    parse_iso,
    parse_recurrence,
    parse_when,
    safe_zone,
    utc_now,
)
from ..utils.validation import (
    PROFILE_BIO_LIMIT,
    PROFILE_NAME_LIMIT,
    validate_profile_bio,
    validate_profile_name,
)
from .helpers import tz_for

CLOCK_INTERVAL = 60.0  # one minute: the profile keeps up with the answer, not the other way round
BIO_INTERVAL = 600.0
PROFILE_COOLDOWN = 60.0


# ---------------------------------------------------------------------------
# Client helpers
# ---------------------------------------------------------------------------
async def _me(ctx: CommandContext) -> Any:
    telegram = ctx.runtime.telegram
    if telegram is not None and getattr(telegram, "me", None) is not None:
        return telegram.me
    if ctx.runtime.client is not None:
        try:
            return await ctx.runtime.client.get_me()
        except Exception:  # noqa: BLE001
            return None
    return None


def _full_name(me: Any) -> str:
    parts = [getattr(me, "first_name", None) or "", getattr(me, "last_name", None) or ""]
    return " ".join(part for part in parts if part).strip()


async def _ensure_snapshot(ctx: CommandContext, me: Any) -> None:
    """Store the *original* profile values exactly once."""
    if await ctx.store.get_profile_snapshot("first_name") is None:
        await ctx.store.save_profile_snapshot("first_name", getattr(me, "first_name", None))
    if await ctx.store.get_profile_snapshot("last_name") is None:
        await ctx.store.save_profile_snapshot("last_name", getattr(me, "last_name", None))
    if await ctx.store.get_profile_snapshot("bio") is None:
        try:
            from telethon.tl.functions.users import GetFullUserRequest

            full = await ctx.runtime.client(GetFullUserRequest("me"))
            about = getattr(getattr(full, "full_user", None), "about", None)
            await ctx.store.save_profile_snapshot("bio", about)
        except Exception:  # noqa: BLE001 - optional, older accounts may fail
            await ctx.store.save_profile_snapshot("bio", None)


async def _apply_profile(
    ctx: CommandContext,
    *,
    first_name: str | None = None,
    last_name: str | None = None,
    bio: str | None = None,
    reason: str = "command",
) -> dict[str, str | None]:
    """Apply a profile change with validation, cooldown and history."""
    runtime = ctx.runtime
    if runtime.client is None:
        raise CommandError("errors.not_connected")
    me = await _me(ctx)
    if me is None:
        raise CommandError("errors.not_connected")
    await _ensure_snapshot(ctx, me)

    remaining = runtime.limiter.cooldown_remaining("profile.update", PROFILE_COOLDOWN)
    if remaining > 0:
        raise CommandError("errors.cooldown", seconds=f"{remaining:.0f}")

    if first_name is not None:
        first_name = validate_profile_name(first_name)
    if bio is not None:
        bio = validate_profile_bio(bio)

    from telethon.tl.functions.account import UpdateProfileRequest

    try:
        await runtime.client(UpdateProfileRequest(first_name=first_name, last_name=last_name, about=bio))
    except Exception as exc:  # noqa: BLE001
        runtime.record_error("profile.update", exc)
        raise CommandError("errors.profile_update_failed", detail=escape(redact_text(str(exc))[:150])) from exc

    runtime.limiter.touch("profile.update")
    if first_name is not None:
        await ctx.store.add_profile_history("first_name", _full_name(me), first_name, reason)
        me.first_name = first_name
    if last_name is not None:
        await ctx.store.add_profile_history("last_name", getattr(me, "last_name", None), last_name, reason)
        me.last_name = last_name
    if bio is not None:
        previous = await ctx.store.get_setting("profile.bio_last")
        await ctx.store.add_profile_history("bio", previous, bio, reason)
        # Telegram does not echo the bio back, so HAVOC remembers what it set.
        await ctx.store.set_setting("profile.bio_last", bio)
        try:
            me.about = bio
        except Exception:  # noqa: BLE001 - Telethon objects are usually writable
            pass
    runtime.metrics.count("profile_updates")
    return {"first_name": first_name, "last_name": last_name, "bio": bio}


class _JobContext:
    """Minimal context so job handlers can reuse command helpers."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.store = runtime.store


async def preset_job(runtime: Any, row: Any) -> None:
    """Apply a profile preset that was scheduled earlier.

    The job carries the preset name plus the recurrence spec so it can queue
    its own successor — schedules survive restarts because the row lives in
    the database, not in memory.
    """
    try:
        payload = json.loads(row["payload"] or "{}")
    except (TypeError, ValueError):
        payload = {}
    name = str(payload.get("preset") or "").lower()
    language = str(payload.get("language") or runtime.config.default_language)
    repeat = payload.get("repeat")
    tz = str(payload.get("timezone") or runtime.config.timezone)
    if not await runtime.store.feature_enabled("profile_scheduler"):
        return
    preset = await runtime.store.get_profile_preset(name)
    if preset is None:
        await runtime.notify_owner(runtime.t("preset.schedule_missing", language, name=name))
        return
    context = _JobContext(runtime)
    try:
        await _apply_profile(
            context,
            first_name=preset["first_name"],
            last_name=preset["last_name"],
            bio=preset["bio"],
            reason=f"preset-schedule:{name}",
        )
    except CommandError as exc:
        runtime.health.record_error("profile.preset_job", exc)
        return
    await runtime.notify_owner(runtime.t("preset.schedule_applied", language, name=escape(name)))
    if repeat:
        nxt = next_occurrence(utc_now(), str(repeat), tz)
        if nxt is not None:
            await runtime.scheduler.enqueue("profile.preset", nxt, {**payload, "preset": name})


# ---------------------------------------------------------------------------
# Clock
# ---------------------------------------------------------------------------
CLOCK_FORMATS = {
    "time": "%H:%M",
    "hm": "%H:%M",
    "full": "%Y-%m-%d %H:%M",
    "date": "%Y-%m-%d",
}


def _render_clock(template: str, timezone_name: str) -> str:
    local = utc_now().astimezone(safe_zone(timezone_name))
    return template.format(
        time=local.strftime("%H:%M"),
        date=local.strftime("%Y-%m-%d"),
        weekday=local.strftime("%a"),
        tz=local.tzname() or timezone_name,
    )


def _next_tick_delay() -> float:
    """Seconds until the next minute boundary, never less than half a minute.

    Scheduling the clock ``CLOCK_INTERVAL`` seconds after it last ran lets it
    drift: a job that takes a moment plus a ten-second scheduler tick makes the
    name change later and later within the minute, and after an hour it is two
    minutes behind the clock on the wall. Aiming at the boundary instead keeps
    the name changing as the minute does.
    """
    remainder = utc_now().timestamp() % CLOCK_INTERVAL
    delay = CLOCK_INTERVAL - remainder
    # `ensure_periodic` floors the interval at 30 s; asking for less would be
    # silently rounded, so ask for what we will get.
    return delay if delay >= CLOCK_INTERVAL / 2 else CLOCK_INTERVAL


async def clock_job(runtime: Any, row: Any) -> None:
    """Periodic profile-name clock (re-enqueues itself)."""
    enabled = await runtime.store.get_bool("clock.enabled", False)
    if not enabled or runtime.client is None:
        return
    template = str(await runtime.store.get_setting("clock.template", "◷ {time}"))
    zone_name = str(await runtime.store.get_setting("clock.timezone", runtime.config.timezone))
    try:
        rendered = _render_clock(template, zone_name)
    except (KeyError, ValueError) as exc:
        # A broken template must not stop the clock: it is reported, and the job
        # keeps its cadence so fixing the template is enough to recover.
        runtime.health.record_error("profile.clock", f"template error: {exc}")
        rendered = None
    rendered = rendered[:PROFILE_NAME_LIMIT] if rendered else None
    me = getattr(runtime.telegram, "me", None) if runtime.telegram else None
    current = getattr(me, "first_name", None) if me is not None else None
    if rendered is not None and rendered != current:
        from telethon.tl.functions.account import UpdateProfileRequest

        try:
            await runtime.client(UpdateProfileRequest(first_name=rendered))
            await runtime.store.add_profile_history("first_name", current, rendered, "clock")
            if me is not None:
                me.first_name = rendered
        except Exception as exc:  # noqa: BLE001
            runtime.health.record_error("profile.clock", exc)
    await runtime.scheduler.ensure_periodic("profile.clock", _next_tick_delay())


async def cmd_clock(ctx: CommandContext) -> None:
    action = (ctx.sub or "status").lower()
    if action == "on":
        zone_name = ctx.arg(0) or tz_for(ctx)
        try:
            safe_zone(zone_name)
        except Exception as exc:  # noqa: BLE001
            raise CommandError("errors.invalid_timezone", value=escape(zone_name)) from exc
        fmt = ctx.flag("format") or ctx.arg(1)
        template = CLOCK_FORMATS.get(fmt or "time", "◷ {time}").replace("{time}", "{time}")
        if fmt and fmt in CLOCK_FORMATS:
            if fmt == "full":
                template = "◷ {date} {time}"
            elif fmt == "date":
                template = "▦ {date}"
            else:
                template = "◷ {time}"
        else:
            template = fmt or "◷ {time}"
        if "{time}" not in template and "{date}" not in template:
            raise CommandError("clock.invalid_template", time="{time}", date="{date}")
        me = await _me(ctx)
        if me is not None:
            await _ensure_snapshot(ctx, me)
        await ctx.store.set_setting("clock.enabled", True)
        await ctx.store.set_setting("clock.timezone", zone_name)
        await ctx.store.set_setting("clock.template", template)
        await ctx.runtime.feature_manager.set_enabled("clock", True)
        await ctx.runtime.scheduler.ensure_periodic("profile.clock", CLOCK_INTERVAL)
        await ctx.reply(ctx.t("clock.enabled", template=escape(template), tz=escape(zone_name)))
        return
    if action == "off":
        await ctx.store.set_setting("clock.enabled", False)
        await ctx.runtime.feature_manager.set_enabled("clock", False)
        await ctx.reply(ctx.t("clock.disabled", prefix=ctx.runtime.prefix))
        return
    if action in {"status", "state"}:
        enabled = await ctx.store.get_bool("clock.enabled", False)
        template = await ctx.store.get_setting("clock.template", "◷ {time}")
        zone_name = await ctx.store.get_setting("clock.timezone", ctx.config.timezone)
        preview = _render_clock(str(template), str(zone_name)) if enabled else "—"
        await ctx.reply(
            bold(ctx.t("clock.header"))
            + "\n"
            + key_value_block(
                [
                    (ctx.t("clock.state"), ctx.t("common.on" if enabled else "common.off")),
                    (ctx.t("clock.template"), template),
                    (ctx.t("clock.timezone"), zone_name),
                    (ctx.t("clock.preview"), preview),
                ]
            )
        )
        return
    raise CommandError("errors.invalid_choice", field="action", choices="on, off, status")


# ---------------------------------------------------------------------------
# Bio + rotation
# ---------------------------------------------------------------------------
async def bio_job(runtime: Any, row: Any) -> None:
    enabled = await runtime.store.get_bool("bio.rotation", False)
    if not enabled or runtime.client is None:
        return
    bios = await runtime.store.get_setting("bio.pool", []) or []
    if not isinstance(bios, list) or not bios:
        return
    index = int(await runtime.store.get_setting("bio.index", 0) or 0) % len(bios)
    text = _resolve_placeholders(str(bios[index]), await _counter_values(runtime))[:PROFILE_BIO_LIMIT]
    from telethon.tl.functions.account import UpdateProfileRequest

    try:
        await runtime.client(UpdateProfileRequest(about=text))
        await runtime.store.add_profile_history("bio", None, text, "rotation")
        await runtime.store.set_setting("bio.index", (index + 1) % len(bios))
    except Exception as exc:  # noqa: BLE001
        runtime.health.record_error("profile.bio", exc)
    await runtime.scheduler.ensure_periodic("profile.bio", BIO_INTERVAL)


async def _counter_values(runtime: Any) -> dict[str, int]:
    rows = await runtime.store.db.fetch_all("SELECT name, value FROM counters")
    return {row["name"]: row["value"] for row in rows}


def _resolve_placeholders(text: str, counters: dict[str, int]) -> str:
    def replace(match: re.Match[str]) -> str:
        return str(counters.get(match.group(1), 0))

    return re.sub(r"\{c:([A-Za-z0-9_\-]{1,32})\}", replace, text)


async def cmd_bio(ctx: CommandContext) -> None:
    action = (ctx.sub or "show").lower()
    if action in {"show", "status"}:
        me = await _me(ctx)
        current = getattr(me, "about", None) if me is not None else None
        if not current:
            current = await ctx.store.get_setting("profile.bio_last")
        rotation = await ctx.store.get_bool("bio.rotation", False)
        pool = await ctx.store.get_setting("bio.pool", []) or []
        await ctx.reply(
            bold(ctx.t("bio.header"))
            + "\n"
            + key_value_block(
                [
                    (ctx.t("bio.current"), current or "—"),
                    (ctx.t("bio.rotation"), ctx.t("common.on" if rotation else "common.off")),
                    (ctx.t("bio.pool_size"), len(pool) if isinstance(pool, list) else 0),
                    (ctx.t("bio.limit"), PROFILE_BIO_LIMIT),
                ]
            )
            + "\n\n"
            + ctx.t("bio.hint", prefix=ctx.runtime.prefix)
        )
        return
    if action == "set":
        text = ctx.rest_raw.strip()
        if not text:
            raise CommandError("errors.missing_argument", usage=ctx.usage_text())
        bio = validate_profile_bio(text)
        await _apply_profile(ctx, bio=bio, reason="bio set")
        await ctx.reply(ctx.t("bio.updated", text=escape(bio)))
        return
    if action in {"add", "pool"}:
        text = ctx.rest_raw.strip()
        if not text:
            raise CommandError("errors.missing_argument", usage=ctx.usage_text())
        validate_profile_bio(text)
        pool = await ctx.store.get_setting("bio.pool", []) or []
        if not isinstance(pool, list):
            pool = []
        if len(pool) >= 20:
            raise CommandError("errors.too_many", field="bio rotation entries", limit=20)
        pool.append(text)
        await ctx.store.set_setting("bio.pool", pool)
        await ctx.reply(ctx.t("bio.added", count=len(pool)))
        return
    if action == "clear":
        await ctx.store.set_setting("bio.pool", [])
        await ctx.reply(ctx.t("bio.pool_cleared"))
        return
    if action == "rotation":
        mode = (ctx.arg(0) or "").lower()
        if mode in {"on", "off"}:
            await ctx.store.set_setting("bio.rotation", mode == "on")
            await ctx.runtime.feature_manager.set_enabled("bio_rotation", mode == "on")
            if mode == "on":
                await ctx.runtime.scheduler.ensure_periodic("profile.bio", BIO_INTERVAL)
            await ctx.reply(ctx.t("bio.rotation_changed", state=ctx.t("common.on" if mode == "on" else "common.off")))
            return
        raise CommandError("errors.invalid_choice", field="action", choices="on, off")
    if action == "preview":
        pool = await ctx.store.get_setting("bio.pool", []) or []
        if not pool:
            raise CommandError("bio.pool_empty", prefix=ctx.runtime.prefix)
        counters = await _counter_values(ctx.runtime)
        index = int(await ctx.store.get_setting("bio.index", 0) or 0) % len(pool)
        preview = _resolve_placeholders(str(pool[index]), counters)
        await ctx.reply(bold(ctx.t("bio.preview_header")) + "\n" + pre(preview))
        return
    if action == "reset":
        original = await ctx.store.get_profile_snapshot("bio")
        await _apply_profile(ctx, bio=original or "", reason="bio reset")
        await ctx.reply(ctx.t("bio.restored", text=escape(original or "—")))
        return
    raise CommandError("errors.invalid_choice", field="action", choices="show, set, add, rotation, preview, reset, clear")


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------
async def cmd_preset(ctx: CommandContext) -> None:
    action = (ctx.sub or "list").lower()
    if action == "list":
        presets = await ctx.store.list_profile_presets()
        if not presets:
            await ctx.reply(ctx.t("preset.empty", prefix=ctx.runtime.prefix))
            return
        lines = [bold(ctx.t("preset.header", count=len(presets)))]
        for row in presets:
            lines.append(f"• {code(escape(row['name']))} — {escape(row['first_name'] or '—')} / {escape((row['bio'] or '—')[:40])}")
        await ctx.reply("\n".join(lines))
        return
    if action in {"create", "save"}:
        name = (ctx.arg(0) or "").strip().lower()
        if not name:
            raise CommandError("errors.missing_argument", usage=ctx.usage_text())
        me = await _me(ctx)
        # ``create work`` snapshots the current profile; positional words after
        # the name override it (first positional = first name, rest = bio).
        positional = list(ctx.args[1:])
        first = ctx.flag("name") or (positional.pop(0) if positional else (getattr(me, "first_name", None) if me else None))
        last = ctx.flag("last") or (getattr(me, "last_name", None) if me else None)
        bio = ctx.flag("bio") or (" ".join(positional).strip() or None)
        await ctx.store.add_profile_preset(name, first_name=first, last_name=last, bio=bio)
        await ctx.reply(ctx.t("preset.created", name=escape(name)))
        return
    if action == "apply":
        name = (ctx.arg(0) or "").strip().lower()
        row = await ctx.store.get_profile_preset(name)
        if row is None:
            raise CommandError("errors.not_found", what="preset", id=escape(name))
        if not ctx.has_flag("yes"):
            await ctx.reply(ctx.t("preset.confirm", name=escape(name), prefix=ctx.runtime.prefix))
            return
        await _apply_profile(ctx, first_name=row["first_name"], last_name=row["last_name"], bio=row["bio"], reason=f"preset:{name}")
        await ctx.reply(ctx.t("preset.applied", name=escape(name)))
        return
    if action in {"delete", "rm"}:
        name = (ctx.arg(0) or "").strip().lower()
        if not await ctx.store.delete_profile_preset(name):
            raise CommandError("errors.not_found", what="preset", id=escape(name))
        await ctx.reply(ctx.t("preset.deleted", name=escape(name)))
        return
    if action in {"preview", "show"}:
        name = (ctx.arg(0) or "").strip().lower()
        row = await ctx.store.get_profile_preset(name)
        if row is None:
            raise CommandError("errors.not_found", what="preset", id=escape(name))
        await ctx.reply(
            bold(ctx.t("preset.preview_header", name=escape(name)))
            + "\n"
            + key_value_block(
                [
                    (ctx.t("preset.first_name"), row["first_name"] or "—"),
                    (ctx.t("preset.last_name"), row["last_name"] or "—"),
                    (ctx.t("preset.bio"), row["bio"] or "—"),
                ]
            )
        )
        return
    if action == "schedule":
        name = (ctx.arg(0) or "").strip().lower()
        when_raw = ctx.arg(1)
        if not name or not when_raw:
            raise CommandError("errors.missing_argument", usage=ctx.usage_text())
        row = await ctx.store.get_profile_preset(name)
        if row is None:
            raise CommandError("errors.not_found", what="preset", id=escape(name))
        tz = tz_for(ctx)
        try:
            when = parse_when(when_raw, tz)
        except TimeParseError as exc:
            raise CommandError("errors.invalid_time", detail=escape(str(exc)), example="tomorrow 09:00") from exc
        if when <= utc_now():
            raise CommandError("errors.time_in_past", when=escape(format_datetime(when, tz, ctx.language)))
        repeat = parse_recurrence(ctx.flag("repeat") or None)
        if ctx.flag("repeat") and not repeat:
            raise CommandError("errors.invalid_recurrence", value=escape(ctx.flag("repeat")))
        # Scheduling a preset implies the feature; enabling it again is a no-op.
        if not await ctx.store.feature_enabled("profile_scheduler"):
            await ctx.runtime.feature_manager.set_enabled("profile_scheduler", True)
        await ctx.runtime.scheduler.enqueue(
            "profile.preset",
            when,
            {
                "preset": name,
                "repeat": repeat,
                "timezone": tz,
                "language": ctx.language,
                "chat_id": ctx.chat_id,
            },
        )
        await ctx.reply(
            ctx.t(
                "preset.scheduled",
                name=escape(name),
                when=escape(format_datetime(when, tz, ctx.language)),
            )
        )
        return
    if action in {"scheduled", "jobs"}:
        rows = await ctx.store.db.fetch_all(
            "SELECT * FROM jobs WHERE kind = 'profile.preset' AND status = 'pending' ORDER BY run_at"
        )
        if not rows:
            await ctx.reply(ctx.t("preset.schedule_empty", prefix=ctx.runtime.prefix))
            return
        lines = [bold(ctx.t("preset.schedule_header", count=len(rows)))]
        tz = tz_for(ctx)
        for row in rows:
            data = json.loads(row["payload"] or "{}")
            when = parse_iso(row["run_at"])
            lines.append(
                f"#{row['id']} {code(escape(format_datetime(when, tz, ctx.language) if when else '—'))} — "
                f"{escape(str(data.get('preset', '—')))}"
            )
        await ctx.reply("\n".join(lines))
        return
    if action in {"unschedule", "cancel-schedule"}:
        try:
            job_id = int(ctx.arg(0))
        except (TypeError, ValueError) as exc:
            raise CommandError("errors.not_a_number", field="id") from exc
        removed = await ctx.store.db.execute(
            "DELETE FROM jobs WHERE id = ? AND kind = 'profile.preset' AND status = 'pending'", (job_id,)
        )
        if not removed:
            raise CommandError("errors.not_found", what="scheduled preset", id=job_id)
        await ctx.reply(ctx.t("preset.schedule_cancelled", id=job_id))
        return
    raise CommandError(
        "errors.invalid_choice",
        field="action",
        choices="list, create, apply, preview, delete, schedule, scheduled",
    )


# ---------------------------------------------------------------------------
# Profile overview / restore / history
# ---------------------------------------------------------------------------
async def cmd_profile(ctx: CommandContext) -> None:
    action = (ctx.sub or "show").lower()
    if action in {"show", "preview"}:
        me = await _me(ctx)
        if me is None:
            raise CommandError("errors.not_connected")
        first = getattr(me, "first_name", None) or ""
        last = getattr(me, "last_name", None) or ""
        proposed = ctx.rest_raw.strip()
        snapshot_first = await ctx.store.get_profile_snapshot("first_name")
        lines = [
            bold(ctx.t("profile.header")),
            key_value_block(
                [
                    (ctx.t("profile.name"), f"{first} {last}".strip() or "—"),
                    (ctx.t("profile.username"), f"@{me.username}" if getattr(me, "username", None) else "—"),
                    (ctx.t("profile.id"), getattr(me, "id", "—")),
                    (ctx.t("profile.original_name"), snapshot_first or "—"),
                ]
            ),
        ]
        if proposed:
            lines.append("")
            lines.append(bold(ctx.t("profile.preview_header")))
            lines.append(code(proposed[:PROFILE_NAME_LIMIT]))
            lines.append(ctx.t("profile.preview_hint", prefix=ctx.runtime.prefix))
        await ctx.reply("\n".join(lines))
        return
    if action == "restore":
        first = await ctx.store.get_profile_snapshot("first_name")
        last = await ctx.store.get_profile_snapshot("last_name")
        bio = await ctx.store.get_profile_snapshot("bio")
        if first is None and bio is None:
            raise CommandError("errors.no_snapshot")
        await _apply_profile(ctx, first_name=first or "Yura", last_name=last, bio=bio or "", reason="restore")
        await ctx.store.set_setting("clock.enabled", False)
        await ctx.store.set_setting("bio.rotation", False)
        await ctx.reply(ctx.t("profile.restored"))
        return
    if action == "history":
        rows = await ctx.store.profile_history(limit=10)
        if not rows:
            await ctx.reply(ctx.t("profile.no_history"))
            return
        lines = [bold(ctx.t("profile.history_header"))]
        for row in rows:
            lines.append(
                f"• {escape(row['created_at'])} — {code(escape(row['field']))}: "
                f"{escape(str(row['new_value'])[:40] if row['new_value'] else '—')} ({escape(row['reason'] or '')})"
            )
        await ctx.reply("\n".join(lines))
        return
    if action == "name":
        new_name = ctx.rest_raw.strip()
        if not new_name:
            raise CommandError("errors.missing_argument", usage=ctx.usage_text())
        await _apply_profile(ctx, first_name=new_name, reason="profile name")
        await ctx.reply(ctx.t("profile.name_updated", name=escape(new_name)))
        return
    raise CommandError("errors.invalid_choice", field="action", choices="show, preview, restore, history, name")


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------
async def cmd_counter(ctx: CommandContext) -> None:
    action = (ctx.sub or "list").lower()
    if action in {"list", "show"}:
        rows = await ctx.store.db.fetch_all("SELECT name, value, updated_at FROM counters ORDER BY name")
        if not rows:
            await ctx.reply(ctx.t("counter.empty", prefix=ctx.runtime.prefix))
            return
        lines = [bold(ctx.t("counter.header", count=len(rows)))]
        for row in rows:
            lines.append(f"• {code(escape(row['name']))}: {row['value']}")
        lines.append("")
        lines.append(ctx.t("counter.bio_hint"))
        await ctx.reply("\n".join(lines))
        return
    name = (ctx.arg(0) or "").strip()
    if not name:
        raise CommandError("errors.missing_argument", usage=ctx.usage_text())
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,32}", name):
        raise CommandError("errors.invalid_name", value=escape(name))
    if action == "inc":
        by = 1
        if ctx.arg(1):
            try:
                by = int(ctx.arg(1))
            except ValueError as exc:
                raise CommandError("errors.not_a_number", field="amount") from exc
            if not 1 <= abs(by) <= 1_000_000:
                raise CommandError("errors.too_large", field="amount", maximum=1_000_000)
        value = await ctx.store.increment_counter(name, by)
        await ctx.reply(ctx.t("counter.incremented", name=escape(name), value=value))
        return
    if action == "reset":
        await ctx.store.reset_counter(name)
        await ctx.reply(ctx.t("counter.reset", name=escape(name)))
        return
    if action == "delete":
        await ctx.store.db.execute("DELETE FROM counters WHERE name = ?", (name,))
        await ctx.reply(ctx.t("counter.deleted", name=escape(name)))
        return
    raise CommandError("errors.invalid_choice", field="action", choices="list, inc, reset, delete")


def register(runtime: Any) -> None:
    runtime.scheduler.register_job("profile.preset", lambda row: preset_job(runtime, row))
    runtime.scheduler.register_job("profile.clock", lambda row: clock_job(runtime, row))
    runtime.scheduler.register_job("profile.bio", lambda row: bio_job(runtime, row))

    runtime.registry.register_many(
        [
            Command("clock", cmd_clock, category="profile", usage="[on <tz>|off|status]", changes_state=True, subcommands=('on', 'off', 'status', 'state')),
            Command("bio", cmd_bio, category="profile", usage="[show|set|add|rotation|preview|reset]", changes_state=True, subcommands=('show', 'status', 'set', 'add', 'pool', 'rotation', 'preview', 'reset', 'clear')),
            Command(
                "preset",
                cmd_preset,
                category="profile",
                usage="[list|create|apply|preview|delete|schedule|scheduled|unschedule]",
                changes_state=True,
                subcommands=(
                    "list", "create", "save", "apply", "preview", "show", "delete", "rm",
                    "schedule", "scheduled", "jobs", "unschedule", "cancel-schedule",
                ),
            ),
            Command("profile", cmd_profile, category="profile", usage="[show|preview|restore|history|name]", changes_state=True, subcommands=('show', 'preview', 'restore', 'history', 'name')),
            Command("counter", cmd_counter, category="profile", aliases=("counters",), usage="[list|inc|reset|delete] <name>", changes_state=True, subcommands=('list', 'show', 'inc', 'reset', 'delete')),
        ]
    )
