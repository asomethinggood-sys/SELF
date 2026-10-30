"""Core commands: help, status, feature switches, language, diagnostics.

These commands are always available, including while the emergency stop is
active, because they are the tools needed to *recover* from a bad state.

Feature areas: 2, 4, 7, 10, 81, 93, 96, 97, 100.
"""

from __future__ import annotations

import time
from typing import Any

from .. import ui
from ..command_registry import (
    Command,
    CommandContext,
    CommandError,
    disabled_commands,
    set_command_disabled,
)
from ..localization import normalize_language_code
from ..utils.formatting import bold, code, escape, key_value_block, pre
from ..version import STAGE, VERSION


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------
async def cmd_help(ctx: CommandContext) -> None:
    registry = ctx.runtime.registry
    query = ctx.arg(0).lstrip(ctx.runtime.prefix).strip()
    if not query:
        features = await ctx.runtime.feature_manager.refresh()
        # The index is longer than one Telegram message; the registry decides
        # where to cut, between categories rather than mid-line.
        chunks = registry.help_sections(
            ctx.runtime.locales,
            ctx.language,
            ctx.runtime.prefix,
            features,
            disabled=await disabled_commands(ctx.runtime),
        )
        for index, chunk in enumerate(chunks):
            await ctx.reply(chunk, reply=index == 0)
        return

    command = registry.resolve(query)
    if command is not None:
        features = await ctx.runtime.feature_manager.refresh()
        await ctx.reply(
            registry.command_detail(
                command, ctx.runtime.locales, ctx.language, ctx.runtime.prefix, features
            )
        )
        return

    matches = registry.search(query)
    if matches:
        lines = [ctx.t("help.search_results", query=escape(query))]
        lines.extend(
            f"  {code(ctx.runtime.prefix + item.name)} — {escape(ctx.t(item.description_key, default=item.name))}"
            for item in matches[:10]
        )
        await ctx.reply("\n".join(lines))
        return
    await ctx.reply(ctx.t("help.not_found", query=escape(query), prefix=ctx.runtime.prefix))


async def cmd_commands(ctx: CommandContext) -> None:
    commands = ctx.runtime.registry.visible()
    page = max(1, int(ctx.arg(0)) if ctx.arg(0).isdigit() else 1)
    per_page = 40
    total_pages = max(1, (len(commands) + per_page - 1) // per_page)
    page = min(page, total_pages)
    chunk = commands[(page - 1) * per_page : page * per_page]
    blocks = [ui.title("▤", ctx.t("commands.header", count=len(commands), page=page, pages=total_pages))]
    grouped: dict[str, list[str]] = {}
    for command in chunk:
        icon = ui.CATEGORY_ICONS.get(command.category, "◇")
        description = ctx.t(command.description_key, default=command.name)
        grouped.setdefault(icon, []).append(ui.item(ctx.runtime.prefix + command.name, description))
    for rows in grouped.values():
        blocks.append(ui.panel(rows))
    if total_pages > 1:
        blocks.append(ui.footer(ctx.t("commands.page_hint", prefix=ctx.runtime.prefix)))
    await ctx.reply("\n\n".join(blocks))


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
async def cmd_status(ctx: CommandContext) -> None:
    runtime = ctx.runtime
    features = await runtime.feature_manager.refresh()
    enabled = [name for name, on in features.items() if on]
    pending = await runtime.scheduler.pending(limit=100)
    db_health = await runtime.health.database_health()
    pair = [
        (ctx.t("status.version"), VERSION),
        (ctx.t("status.uptime"), runtime.health.uptime_human),
        (ctx.t("status.connected"), ctx.t("common.yes" if runtime.health.connected else "common.no")),
        (ctx.t("status.prefix"), ctx.runtime.prefix),
        (ctx.t("status.language"), runtime.locales.language_name(await runtime.resolve_language(chat_id=ctx.chat_id), ctx.language)),
        (ctx.t("status.features"), f"{len(enabled)}/{len(features)}"),
        (ctx.t("status.pending_jobs"), len(pending)),
        (ctx.t("status.database"), f"{'ok' if db_health['ok'] else 'error'} ({db_health['latency_ms']} ms)"),
        (ctx.t("status.dry_run"), ctx.t("common.on" if await runtime.permissions.dry_run_active() else "common.off")),
        (ctx.t("status.emergency_stop"), ctx.t("common.active" if await runtime.store.emergency_stop() else "common.inactive")),
        (ctx.t("status.errors"), len(runtime.health.errors)),
        (ctx.t("status.reconnects"), runtime.health.reconnects),
    ]
    health, settings = pair[:8], pair[8:]
    await ctx.reply(
        "\n\n".join(
            [
                ui.title("◫", ctx.t("status.header").replace("◫", "").strip()),
                ui.panel(f"{ui.bold(k)} {ui.note(ui.DOT)} {escape(v)}" for k, v in health),
                ui.panel(f"{ui.bold(k)} {ui.note(ui.DOT)} {escape(v)}" for k, v in settings),
            ]
        )
    )


async def cmd_ping(ctx: CommandContext) -> None:
    started = time.monotonic()
    db_started = time.monotonic()
    await ctx.store.db.scalar("SELECT 1")
    db_ms = round((time.monotonic() - db_started) * 1000, 2)
    await ctx.reply(ctx.t("ping.result", db_ms=f"{db_ms}", total_ms=f"{(time.monotonic() - started) * 1000:.0f}"))
    ctx.runtime.metrics.count("ping")


async def cmd_diagnostics(ctx: CommandContext) -> None:
    runtime = ctx.runtime
    snapshot = await runtime.snapshot()
    events = snapshot.get("event_engine", {}).get("metrics", {})
    scheduler = snapshot.get("scheduler", {})
    limiter = snapshot.get("rate_limiter", {})
    subs = snapshot.get("event_engine", {}).get("subscribers", [])
    failing = [s for s in subs if s.get("failures")]
    lines = [
        bold(ctx.t("diagnostics.header")),
        "",
        bold(ctx.t("diagnostics.events")),
        f"  {ctx.t('diagnostics.processed')}: {events.get('processed', 0)}",
        f"  {ctx.t('diagnostics.duplicates')}: {events.get('dropped_duplicates', 0)}",
        f"  {ctx.t('diagnostics.subscriber_errors')}: {events.get('subscriber_errors', 0)}",
        f"  {ctx.t('diagnostics.latency')}: {events.get('p95_latency_ms', 0)} ms",
        "",
        bold(ctx.t("diagnostics.scheduler")),
        f"  {ctx.t('diagnostics.ticks')}: {scheduler.get('ticks', 0)}",
        f"  {ctx.t('diagnostics.jobs_fired')}: {scheduler.get('reminders_fired', 0)} + {scheduler.get('schedules_fired', 0)} + {scheduler.get('jobs_fired', 0)}",
        f"  {ctx.t('diagnostics.failures')}: {scheduler.get('failures', 0)}",
        "",
        bold(ctx.t("diagnostics.limiter")),
        f"  {ctx.t('diagnostics.outbound')}: {limiter.get('outbound_remaining', 0)}/{limiter.get('outbound_limit', 0)}",
        f"  {ctx.t('diagnostics.floodwait')}: {limiter.get('floodwait_remaining', 0)}s",
        "",
        bold(ctx.t("diagnostics.integrations")),
        f"  AI: {'available' if runtime.ai.available else 'disabled'}",
        f"  {ctx.t('diagnostics.translation')}: {'available' if runtime.translator.available else 'disabled'}",
    ]
    if failing:
        lines.append("")
        lines.append(bold(ctx.t("diagnostics.failing_subscribers")))
        lines.extend(f"  {escape(item['name'])} ({item['failures']})" for item in failing)
    errors = runtime.health.render_errors(limit=3)
    if errors:
        lines.append("")
        lines.append(bold(ctx.t("diagnostics.recent_errors")))
        lines.append(pre(errors))
    await ctx.reply("\n".join(lines))


# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------
async def cmd_features(ctx: CommandContext) -> None:
    manager = ctx.runtime.feature_manager
    await manager.refresh()
    state_map = await manager.refresh()
    on = sum(1 for value in state_map.values() if value)
    total = len(manager.features)
    blocks = [
        ui.title("⚙", ctx.t("features.header")),
        ui.note(f"{ui.meter(on, total)} {on}/{total} {ctx.t('common.on')}"),
    ]
    for group, features in manager.by_group().items():
        rows: list[str] = []
        for feature in features:
            state = await manager.is_available(feature.name)
            marker = "●" if state.enabled else "⚪"
            title = ctx.t(feature.title, default=feature.name)
            row = f"{marker} {code(feature.name)} {ui.note(ui.DOT)} {escape(title)}"
            if state.missing:
                row += f" {ui.note(ctx.t('features.missing', deps=', '.join(state.missing)))}"
            rows.append(row)
        blocks.append(
            ui.section(ui.CATEGORY_ICONS.get(group, "◆"), ctx.t(f"feature_group.{group}", default=group.title()), rows)
        )
    blocks.append(ui.footer(ctx.t("features.hint", prefix=ctx.runtime.prefix)))
    await ctx.reply("\n\n".join(blocks))


async def cmd_feature(ctx: CommandContext) -> None:
    manager = ctx.runtime.feature_manager
    if not ctx.args:
        await cmd_features(ctx)
        return
    name = ctx.arg(0).lower()
    action = ctx.arg(1).lower() or "toggle"
    if manager.get(name) is None:
        raise CommandError("errors.unknown_feature", name=escape(name), prefix=ctx.runtime.prefix)
    if action not in {"on", "off", "toggle", "enable", "disable"}:
        raise CommandError("errors.invalid_choice", field="state", choices="on, off")
    try:
        if action in {"on", "enable"}:
            await manager.set_enabled(name, True)
            enabled = True
        elif action in {"off", "disable"}:
            await manager.set_enabled(name, False)
            enabled = False
        else:
            enabled = await manager.toggle(name)
    except ValueError as exc:
        raise CommandError("errors.missing_dependencies", detail=escape(str(exc))) from exc
    await ctx.reply(ctx.t("features.changed", feature=escape(name), state=ctx.t("common.on" if enabled else "common.off")))


# ---------------------------------------------------------------------------
# One command at a time
# ---------------------------------------------------------------------------
async def cmd_disable(ctx: CommandContext) -> None:
    """`.disable fal` — stop serving one command without touching its family."""
    await _switch_command(ctx, disabled=True)


async def cmd_enable(ctx: CommandContext) -> None:
    """`.enable fal` — the way back, and it works even for itself."""
    await _switch_command(ctx, disabled=False)


async def _switch_command(ctx: CommandContext, *, disabled: bool) -> None:
    runtime = ctx.runtime
    name = ctx.arg(0).lower().lstrip(".")
    if not name:
        current = await disabled_commands(runtime)
        listing = ", ".join(f"{runtime.prefix}{entry}" for entry in sorted(current)) or ctx.t("common.none")
        await ctx.reply(ctx.t("commands.disabled_list", list=escape(listing), prefix=runtime.prefix))
        return
    command = runtime.registry.resolve(name)
    if command is None:
        raise CommandError("errors.unknown_command", name=escape(name), prefix=runtime.prefix)
    if command.name in {"enable", "disable", "help", "commands"} and disabled:
        # Switching off the way back is how an account locks itself out.
        raise CommandError("errors.cannot_disable", name=escape(command.name))
    await set_command_disabled(runtime, command.name, disabled)
    await ctx.reply(
        ctx.t(
            "commands.disabled_one" if disabled else "commands.enabled_one",
            name=escape(command.name),
            prefix=runtime.prefix,
        )
    )


# ---------------------------------------------------------------------------
# Emergency stop
# ---------------------------------------------------------------------------
async def cmd_stop(ctx: CommandContext) -> None:
    runtime = ctx.runtime
    action = (ctx.sub or ctx.arg(0) or "").lower()
    if action in {"resume", "off", "start", "ادامه"}:
        await runtime.feature_manager.resume()
        await ctx.reply(ctx.t("stop.resumed"))
        return
    if action in {"status", "state", "وضعیت"}:
        stopped = await runtime.store.emergency_stop()
        await ctx.reply(ctx.t("stop.status", state=ctx.t("common.active" if stopped else "common.inactive")))
        return
    if action and action not in {"on", "stop", "halt", "توقف"}:
        raise CommandError("errors.invalid_choice", field="action", choices="on, resume, status")
    await runtime.feature_manager.stop_all()
    await ctx.reply(ctx.t("stop.activated", prefix=runtime.prefix))


# ---------------------------------------------------------------------------
# Language
# ---------------------------------------------------------------------------
async def cmd_lang(ctx: CommandContext) -> None:
    runtime = ctx.runtime
    scope = "global"
    value = ctx.sub or (ctx.arg(0) or None)
    if value in {"chat", "user", "global"}:
        scope = value
        value = ctx.rest_arg(0) or ctx.arg(1) or None
    if value in (None, "status", "وضعیت"):
        prefs = {f"{row['scope']}:{row['scope_id']}": row["language"] for row in await runtime.store.language_prefs()}
        lines = [
            bold(ctx.t("lang.status_header")),
            key_value_block(
                [
                    (ctx.t("lang.default"), runtime.locales.language_name(runtime.config.default_language, ctx.language)),
                    (ctx.t("lang.global"), runtime.locales.language_name(prefs.get("global:", "auto"), ctx.language)),
                    (ctx.t("lang.chat"), runtime.locales.language_name(prefs.get(f"chat:{ctx.chat_id}", "auto"), ctx.language)),
                    (ctx.t("lang.detected"), runtime.locales.language_name(await runtime.resolve_language(ctx.event.text or "", chat_id=ctx.chat_id), ctx.language)),
                ]
            ),
        ]
        await ctx.reply("\n".join(lines))
        return

    normalized = normalize_language_code(value, fallback="")
    if normalized not in {"en", "fa", "auto"}:
        raise CommandError("errors.invalid_language", value=escape(value), choices="en, fa, auto")
    scope_id = "" if scope == "global" else str(ctx.chat_id if scope == "chat" else getattr(ctx.event, "sender_id", ""))
    await runtime.store.set_language(scope, scope_id, normalized)
    display = runtime.locales.language_name(normalized, ctx.language) if normalized != "auto" else ctx.t("lang.auto")
    scope_label = ctx.t(f"lang.scope_{scope}")
    await ctx.reply(ctx.t("lang.changed", lang=escape(display), scope=escape(scope_label)))


# ---------------------------------------------------------------------------
# Configuration / diagnostics helpers
# ---------------------------------------------------------------------------
async def cmd_config(ctx: CommandContext) -> None:
    runtime = ctx.runtime
    redacted = runtime.config.redacted()
    keys = [
        "command_prefix", "default_language", "timezone", "dry_run", "debug", "log_level",
        "database_path", "backup_retention", "outbound_rate_limit", "missed_job_policy",
        "ai_provider", "ai_model", "translate_provider", "plugins_enabled",
        "automation_allowlist", "automation_denylist", "owner_ids", "session_path",
    ]
    pairs = [(key, redacted.get(key)) for key in keys if key in redacted]
    secrets_state = [
        (
            ctx.t("config.api_hash"),
            ctx.t("config.masked") if runtime.config.api_hash else ctx.t("config.unset"),
        ),
        (ctx.t("config.api_credentials"), ctx.t("common.yes" if runtime.config.has_credentials else "common.no")),
        (ctx.t("config.session_string"), ctx.t("common.yes" if runtime.config.session_string else "common.no")),
        (ctx.t("config.ai_key"), ctx.t("common.yes" if runtime.config.ai_api_key else "common.no")),
    ]
    runtime_pairs = [
        (ctx.t("config.stored_prefix"), runtime.prefix),
        (ctx.t("config.chat_language"), await runtime.store.get_language("chat", str(ctx.chat_id)) or "auto"),
        (ctx.t("config.quiet_hours"), await runtime.permissions.quiet_hours_config(ctx.chat_id) or "—"),
    ]
    text = "\n".join(
        [
            bold(ctx.t("config.header")),
            key_value_block(pairs),
            "",
            bold(ctx.t("config.secrets")),
            key_value_block(secrets_state),
            "",
            bold(ctx.t("config.runtime")),
            key_value_block(runtime_pairs),
            "",
            ctx.t("config.note", prefix=code(ctx.runtime.prefix)),
        ]
    )
    await ctx.reply(text)


async def cmd_selfcheck(ctx: CommandContext) -> None:
    results = await ctx.runtime.self_check()
    lines = [bold(ctx.t("selfcheck.header"))]
    for result in results:
        lines.append(result.render())
    failed = [r for r in results if not r.ok and r.severity == "error"]
    lines.append("")
    lines.append(
        ctx.t("selfcheck.result_ok") if not failed else ctx.t("selfcheck.result_failed", count=len(failed))
    )
    await ctx.reply("\n".join(lines))


async def cmd_about(ctx: CommandContext) -> None:
    facts = [
        (ctx.t("about.commands"), len(ctx.runtime.registry)),
        (ctx.t("about.features"), len(ctx.runtime.feature_manager.features)),
        (ctx.t("about.languages"), ", ".join(ctx.runtime.locales.languages)),
        (ctx.t("about.prefix"), ctx.runtime.prefix),
    ]
    await ctx.reply(
        "\n\n".join(
            [
                ui.title("❥", ctx.t("about.title", version=VERSION, stage=STAGE)),
                ui.note(ctx.t("about.summary")),
                ui.panel(f"{ui.bold(k)} {ui.note(ui.DOT)} {escape(v)}" for k, v in facts),
                ui.footer(ctx.t("about.license")),
            ]
        )
    )


async def cmd_owner(ctx: CommandContext) -> None:
    """Who owns her. Only an owner can change this, which is the point of it."""
    prefix = ctx.runtime.prefix
    permissions = ctx.runtime.permissions
    action = (ctx.sub or "").lower() or ("list" if not ctx.args else ctx.args[0].lower())
    words = list(ctx.args)
    if action in {"add", "allow", "grant"}:
        handle = next((word for word in words if word.startswith("@") or word.lower() != action), "")
        handle = handle.lstrip("@").lower()
        if not handle:
            await ctx.reply(ctx.t("owner.usage", prefix=prefix))
            return
        entity = None
        try:
            if ctx.runtime.client is not None:
                entity = await ctx.runtime.client.get_entity(f"@{handle}")
        except Exception:  # noqa: BLE001 - an unknown handle is a message, not a crash
            entity = None
        user_id = getattr(entity, "id", None)
        if user_id is None:
            await ctx.reply(ctx.t("owner.unknown"))
            return
        permissions.add_id(int(user_id), handle=handle)
        await permissions.save()
        await ctx.reply(ctx.t("owner.added", id=user_id))
        return
    if action in {"remove", "revoke", "del", "delete"}:
        target = next((word for word in words if word.lower() != action), "")
        removed = False
        if target.lstrip("-").isdigit():
            removed = permissions.remove_id(int(target))
        elif target:
            resolved = permissions.owner_ids
            for user_id in list(resolved):
                if str(user_id) == target:
                    removed = permissions.remove_id(user_id)
        if removed:
            await permissions.save()
            await ctx.reply(ctx.t("owner.removed", id=escape(target)))
        else:
            await ctx.reply(ctx.t("owner.usage", prefix=prefix))
        return
    rows = []
    for user_id in sorted(permissions.owner_ids):
        note = ctx.t("owner.you") if user_id == permissions.self_id else ctx.t("owner.id_note")
        for handle, resolved in getattr(permissions, "handles_resolved", {}).items():
            if resolved == user_id:
                note = f"@{handle} · " + ctx.t("owner.handle_note")
        rows.append(ui.fact_line("◇", f"{user_id} · {note}"))
    body = "\n".join(rows) if rows else ui.note(ctx.t("owner.none"))
    await ctx.reply(ui.card(ctx.t("owner.header").lstrip("⟡ ").strip(), (), icon="⟡") + "\n" + body)


async def cmd_id(ctx: CommandContext) -> None:
    pairs = [
        (ctx.t("id.chat"), ctx.chat_id),
        (ctx.t("id.message"), getattr(ctx.message, "id", "—")),
        (ctx.t("id.sender"), getattr(ctx.event, "sender_id", "—")),
        (ctx.t("id.type"), ctx.t("id.private" if ctx.is_private else "id.group")),
    ]
    reply = await ctx.reply_message()
    if reply is not None:
        pairs.append((ctx.t("id.reply_message"), getattr(reply, "id", "—")))
        pairs.append((ctx.t("id.reply_sender"), getattr(reply, "sender_id", "—")))
    await ctx.reply(key_value_block(pairs))


async def cmd_prefix(ctx: CommandContext) -> None:
    runtime = ctx.runtime
    value = ctx.arg(0)
    if not value or value in {"show", "status"}:
        await ctx.reply(ctx.t("prefix.current", prefix=code(runtime.prefix)))
        return
    if value in {"reset", "default"}:
        await runtime.set_prefix(None)
        await ctx.reply(ctx.t("prefix.reset", prefix=code(runtime.prefix)))
        return
    try:
        applied = await runtime.set_prefix(value)
    except ValueError as exc:
        raise CommandError("errors.invalid_prefix", detail=escape(str(exc))) from exc
    await ctx.reply(ctx.t("prefix.changed", prefix=code(applied)))


async def cmd_dryrun(ctx: CommandContext) -> None:
    runtime = ctx.runtime
    action = (ctx.sub or ctx.arg(0) or "status").lower()
    if action in {"on", "off"}:
        enabled = action == "on"
        await runtime.permissions.set_dry_run(enabled)
        runtime.sender.dry_run = enabled
        # The acknowledgement must reach the user even while dry-run is on,
        # otherwise "dryrun on" looks like the command silently failed.
        await runtime.sender.respond(
            ctx.event,
            ctx.t("dryrun.changed", state=ctx.t("common.on" if enabled else "common.off")),
            dry_run=False,
        )
        return
    state = await runtime.permissions.dry_run_active()
    await ctx.reply(
        ctx.t("dryrun.status", state=ctx.t("common.on" if state else "common.off")) + "\n" + ctx.t("dryrun.explain")
    )


def register(runtime: Any) -> None:
    runtime.registry.register_many(
        [
            Command(
                "owner",
                cmd_owner,
                category="management",
                usage="[list | add @handle | remove <id>]",
                subcommands=("list", "add", "allow", "remove", "revoke", "who"),
                changes_state=True,
            ),
            Command("help", cmd_help, category="core", aliases=("?",), usage="[command]", examples=("help", "help remind")),
            Command("commands", cmd_commands, category="core", usage="[page]"),
            Command("status", cmd_status, category="core"),
            Command("ping", cmd_ping, category="core"),
            Command("diagnostics", cmd_diagnostics, category="core", aliases=("diag",)),
            Command("features", cmd_features, category="core"),
            Command("feature", cmd_feature, category="core", usage="<name> <on|off>", changes_state=True),
            Command("disable", cmd_disable, category="management", aliases=("switch-off",), usage="[command]", changes_state=True, subcommands=()),
            Command("enable", cmd_enable, category="management", aliases=("switch-on",), usage="[command]", changes_state=True, subcommands=()),
            Command("stop", cmd_stop, category="core", usage="[on|resume|status]", changes_state=True, subcommands=('on', 'resume', 'off', 'start', 'status', 'state', 'stop', 'halt', 'توقف', 'ادامه', 'وضعیت')),
            Command("lang", cmd_lang, category="core", aliases=("language", "زبان"), usage="[en|fa|auto|chat|user|status]", changes_state=True, subcommands=("chat", "user", "global", "en", "fa", "auto", "status", "وضعیت")),
            Command("config", cmd_config, category="management"),
            Command("selfcheck", cmd_selfcheck, category="management", aliases=("check",)),
            Command("about", cmd_about, category="core"),
            Command("id", cmd_id, category="core", aliases=("ids",)),
            Command("prefix", cmd_prefix, category="management", usage="<new|reset>", changes_state=True, needs_confirmation=True),
            Command("dryrun", cmd_dryrun, category="core", usage="[on|off|status]", changes_state=True, subcommands=('on', 'off', 'status')),
        ]
    )
