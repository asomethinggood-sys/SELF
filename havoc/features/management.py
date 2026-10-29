"""Management commands: per-chat settings, aliases, backups, plugins, quick actions.

Feature areas: 71-79, 86, 88, 92, 93, 98.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from ..command_registry import Command, CommandContext, CommandError
from ..utils.formatting import bold, code, escape, key_value_block, pre
from ..utils.parsing import ParsedCommand, parse_keyword_list
from ..utils.security import ensure_within
from ..utils.time import iso, parse_duration, utc_now

BACKUP_VERSION = 1
MAX_IMPORT_BYTES = 5 * 1024 * 1024

ALLOWED_CHAT_KEYS: dict[str, str] = {
    "automation.allowed": "bool",
    "automation.denied": "bool",
    "automation.allow_private": "bool",
    "timezone": "text",
    "nickname": "text",
    "notify.keywords": "csv",
    "fight.use_ai": "bool",
    "quiet_hours": "json",
}

RESERVED_ALIAS_TARGETS = {"alias", "plugins", "backup", "stop"}


# ---------------------------------------------------------------------------
# Per-chat settings
# ---------------------------------------------------------------------------
async def cmd_chatsettings(ctx: CommandContext) -> None:
    action = (ctx.sub or "show").lower()
    if action in {"show", "list"}:
        settings = await ctx.store.chat_settings(ctx.chat_id)
        labels = await ctx.store.chat_labels(ctx.chat_id)
        language = await ctx.store.get_language("chat", str(ctx.chat_id)) or "auto"
        lines = [
            bold(ctx.t("chatsettings.header", chat=ctx.chat_id)),
            key_value_block([(ctx.t("chatsettings.language"), language), (ctx.t("chatsettings.labels"), ", ".join(labels) or "—")]),
        ]
        if settings:
            lines.append("")
            lines.append(pre("\n".join(f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in settings.items())))
        else:
            lines.append(ctx.t("chatsettings.empty"))
        lines.append("")
        lines.append(ctx.t("chatsettings.allowed_keys", keys=", ".join(sorted(ALLOWED_CHAT_KEYS))))
        lines.append(ctx.t("chatsettings.hint", prefix=ctx.runtime.prefix))
        await ctx.reply("\n".join(lines))
        return
    if action == "set":
        key = (ctx.arg(0) or "").strip()
        value_raw = " ".join(ctx.args[1:]).strip()
        if key not in ALLOWED_CHAT_KEYS or not value_raw:
            raise CommandError("errors.invalid_choice", field="key", choices=", ".join(sorted(ALLOWED_CHAT_KEYS)))
        kind = ALLOWED_CHAT_KEYS[key]
        value: Any = value_raw
        if kind == "bool":
            lowered = value_raw.lower()
            if lowered not in {"on", "off", "true", "false", "yes", "no", "1", "0"}:
                raise CommandError("errors.invalid_choice", field="value", choices="on, off")
            value = lowered in {"on", "true", "yes", "1"}
        elif kind == "csv":
            value = parse_keyword_list(value_raw)
        elif kind == "json":
            try:
                value = json.loads(value_raw)
            except json.JSONDecodeError as exc:
                raise CommandError("errors.invalid_json", detail=escape(str(exc))) from exc
        await ctx.store.set_chat_setting(ctx.chat_id, key, value)
        await ctx.reply(ctx.t("chatsettings.set", key=escape(key), value=escape(str(value))))
        return
    if action == "reset":
        key = (ctx.arg(0) or "").strip()
        if key:
            await ctx.store.delete_chat_setting(ctx.chat_id, key)
            await ctx.reply(ctx.t("chatsettings.reset_key", key=escape(key)))
        else:
            await ctx.store.delete_chat_setting(ctx.chat_id)
            await ctx.reply(ctx.t("chatsettings.reset_all"))
        return
    raise CommandError("errors.invalid_choice", field="action", choices="show, set, reset")


# ---------------------------------------------------------------------------
# Aliases
# ---------------------------------------------------------------------------
async def cmd_alias(ctx: CommandContext) -> None:
    action = (ctx.sub or "list").lower()
    registry = ctx.runtime.registry
    if action in {"list", "ls"}:
        rows = await ctx.store.list_aliases()
        if not rows:
            await ctx.reply(ctx.t("alias.empty", prefix=ctx.runtime.prefix))
            return
        lines = [bold(ctx.t("alias.header", count=len(rows)))]
        for row in rows:
            lines.append(f"• {code(ctx.runtime.prefix + row['alias'])} → {code(row['target'])}")
        await ctx.reply("\n".join(lines))
        return
    if action == "add":
        alias = (ctx.arg(0) or "").strip().lower().lstrip(ctx.runtime.prefix)
        target = " ".join(ctx.args[1:]).strip().lstrip(ctx.runtime.prefix)
        if not alias or not target:
            raise CommandError("errors.missing_argument", usage=ctx.usage_text())
        if not re.fullmatch(r"[a-z0-9_]{1,32}", alias):
            raise CommandError("errors.invalid_name", value=escape(alias))
        if alias in registry.reserved_names():
            raise CommandError("alias.reserved", name=escape(alias))
        target_name = target.split()[0].lower()
        if registry.resolve(target_name) is None:
            raise CommandError("errors.unknown_command", name=escape(target_name), prefix=ctx.runtime.prefix)
        if alias == target_name:
            raise CommandError("alias.loop")
        await ctx.store.add_alias(alias, target)
        await ctx.reply(ctx.t("alias.added", alias=escape(alias), target=escape(target)))
        return
    if action in {"remove", "delete", "rm"}:
        alias = (ctx.arg(0) or "").strip().lower().lstrip(ctx.runtime.prefix)
        if not await ctx.store.delete_alias(alias):
            raise CommandError("errors.not_found", what="alias", id=escape(alias))
        await ctx.reply(ctx.t("alias.removed", alias=escape(alias)))
        return
    if action == "clear":
        count = await ctx.store.clear_aliases()
        await ctx.reply(ctx.t("alias.cleared", count=count))
        return
    raise CommandError("errors.invalid_choice", field="action", choices="list, add, remove, clear")


# ---------------------------------------------------------------------------
# Quick actions
# ---------------------------------------------------------------------------
async def cmd_quick(ctx: CommandContext) -> None:
    actions = {"list", "ls", "add", "remove", "delete", "rm", "run"}
    # ``.quick add work .status`` edits; ``.quick work`` is shorthand for "run work".
    action = (ctx.sub or ctx.arg(0) or "list").lower()
    shorthand = ctx.sub is None and action not in actions
    if shorthand:
        action = "run"
    if action in {"list", "ls"}:
        settings = await ctx.store.all_settings()
        quick = {key[len("quick.") :]: value for key, value in settings.items() if key.startswith("quick.")}
        if not quick:
            await ctx.reply(ctx.t("quick.empty", prefix=ctx.runtime.prefix))
            return
        lines = [bold(ctx.t("quick.header", count=len(quick)))]
        for name, command in sorted(quick.items()):
            lines.append(f"• {code(name)} → {code(str(command))}")
        await ctx.reply("\n".join(lines))
        return
    if action == "add":
        name = (ctx.arg(0) or "").strip().lower()
        command = ctx.rest_raw.strip()
        if command.startswith(name):
            command = command[len(name) :].strip()
        command = command.strip()
        if not name or not command:
            raise CommandError("errors.missing_argument", usage=ctx.usage_text())
        if not re.fullmatch(r"[a-z0-9_\-]{1,24}", name):
            raise CommandError("errors.invalid_name", value=escape(name))
        if command.startswith(ctx.runtime.prefix):
            command = command[len(ctx.runtime.prefix) :]
        target = command.split()[0].lower()
        if target == "quick":
            raise CommandError("alias.loop")
        if ctx.runtime.registry.resolve(target) is None:
            raise CommandError("errors.unknown_command", name=escape(target), prefix=ctx.runtime.prefix)
        await ctx.store.set_setting(f"quick.{name}", command)
        await ctx.reply(ctx.t("quick.added", name=escape(name), command=escape(command)))
        return
    if action in {"remove", "delete", "rm"}:
        name = (ctx.arg(0) or "").strip().lower()
        await ctx.store.delete_setting(f"quick.{name}")
        await ctx.reply(ctx.t("quick.removed", name=escape(name)))
        return
    if action == "run":
        name = (ctx.arg(0) or "").strip().lower()
        command = await ctx.store.get_setting(f"quick.{name}", None)
        if not command:
            raise CommandError("errors.not_found", what="quick action", id=escape(name))
        parsed = ParsedCommand(
            prefix=ctx.runtime.prefix,
            name=str(command).split()[0].lower(),
            subcommand=str(command).split()[1].lower() if len(str(command).split()) > 1 else None,
            args=tuple(str(command).split()[2:] if len(str(command).split()) > 2 else str(command).split()[1:2] if len(str(command).split()) == 2 else ()),
            raw_args=" ".join(str(command).split()[1:]),
            raw=str(command),
        )
        await ctx.runtime.dispatcher.handle(ctx.event, ctx.message, parsed, ctx.language)
        return
    raise CommandError("errors.invalid_choice", field="action", choices="list, add, remove, run")


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------
def _backup_dir(ctx: CommandContext) -> Path:
    target = ctx.config.backup_dir
    target.mkdir(parents=True, exist_ok=True)
    return target


async def write_backup(runtime: Any, *, kind: str = "manual") -> Path:
    """Create a JSON backup atomically and rotate old files."""
    directory = runtime.config.backup_dir
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "app": "havoc",
        "version": BACKUP_VERSION,
        "kind": kind,
        "created_at": iso(utc_now()),
        "tables": await runtime.store.export_tables(),
    }
    stamp = utc_now().strftime("%Y%m%d-%H%M%S")
    final = ensure_within(directory, directory / f"havoc-backup-{stamp}.json")
    temp = final.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, final)
    await _rotate_backups(directory, runtime.config.backup_retention)
    runtime.metrics.count("backups")
    return final


async def _rotate_backups(directory: Path, keep: int) -> None:
    files = sorted(directory.glob("havoc-backup-*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for old in files[max(1, keep) :]:
        try:
            old.unlink()
        except OSError:
            continue


async def backup_job(runtime: Any, row: Any) -> None:
    path = await write_backup(runtime, kind="scheduled")
    runtime.logger.info("scheduled backup written to %s", path.name)
    interval = await runtime.store.get_setting("backup.interval_seconds", 86400)
    await runtime.scheduler.ensure_periodic("backup.auto", float(interval or 86400))


async def cmd_backup(ctx: CommandContext) -> None:
    action = (ctx.sub or "").lower()
    if not action:
        # ``.backup typo`` demotes the unknown word to a positional argument, and
        # silently exporting a file in that case is more confusing than helpful.
        if ctx.args:
            raise CommandError(
                "errors.invalid_choice", field="action", choices="export, list, import, auto"
            )
        action = "export"
    if action == "export":
        path = await write_backup(ctx.runtime, kind="manual")
        size = path.stat().st_size
        await ctx.runtime.sender.send(
            ctx.chat_id,
            ctx.t("backup.exported", name=escape(path.name), size=f"{size / 1024:.1f} KB"),
            file=str(path),
        )
        return
    if action == "list":
        directory = _backup_dir(ctx)
        files = sorted(directory.glob("havoc-backup-*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        if not files:
            await ctx.reply(ctx.t("backup.none"))
            return
        lines = [bold(ctx.t("backup.header", count=len(files)))]
        for path in files[:15]:
            lines.append(f"• {code(escape(path.name))} — {path.stat().st_size / 1024:.1f} KB")
        await ctx.reply("\n".join(lines))
        return
    if action == "import":
        directory = _backup_dir(ctx)
        name_arg = (ctx.arg(0) or "").strip()
        if name_arg:
            # Import a backup that already lives in the backup directory.
            target = ensure_within(directory, directory / Path(name_arg).name)
            if not target.is_file():
                raise CommandError("errors.not_found", what="backup", id=escape(name_arg))
            if target.stat().st_size > MAX_IMPORT_BYTES:
                raise CommandError("backup.too_large", limit=f"{MAX_IMPORT_BYTES // (1024 * 1024)} MB")
        else:
            reply = await ctx.reply_message()
            if reply is None or getattr(getattr(reply, "document", None), "size", None) is None:
                raise CommandError(
                    "backup.import_hint", prefix=ctx.runtime.prefix
                )
            size = getattr(getattr(reply, "document", None), "size", 0)
            if size and size > MAX_IMPORT_BYTES:
                raise CommandError("backup.too_large", limit=f"{MAX_IMPORT_BYTES // (1024 * 1024)} MB")
            if ctx.runtime.client is None:
                raise CommandError("errors.not_connected")
            target = ensure_within(directory, directory / safe_backup(name="import", ctx=ctx))
            try:
                await ctx.runtime.client.download_media(reply, file=str(target))
            except Exception as exc:  # noqa: BLE001
                raise CommandError("errors.send_failed", detail=escape(str(exc)[:150])) from exc
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError("backup.invalid", detail=escape(str(exc)[:150])) from exc
        if not isinstance(payload, dict) or payload.get("app") != "havoc" or not isinstance(payload.get("tables"), dict):
            raise CommandError("backup.invalid", detail="unexpected structure")
        tables = payload["tables"]
        unknown = [table for table in tables if table not in ctx.store.BACKUP_TABLES]
        if unknown:
            raise CommandError("backup.invalid", detail="unknown tables: " + ", ".join(unknown[:3]))
        counts = await ctx.store.import_tables(tables, replace=ctx.has_flag("replace"))
        await ctx.reply(
            bold(ctx.t("backup.imported"))
            + "\n"
            + pre("\n".join(f"{table}: {count}" for table, count in counts.items()))
        )
        return
    if action == "auto":
        mode = (ctx.arg(0) or "status").lower()
        if mode in {"on", "off"}:
            await ctx.store.set_setting("backup.auto", mode == "on")
            await ctx.runtime.feature_manager.set_enabled("backups", mode == "on")
            if mode == "on":
                interval_raw = ctx.arg(1) or "24h"
                try:
                    interval = parse_duration(interval_raw).total_seconds()
                except Exception as exc:  # noqa: BLE001
                    raise CommandError("errors.invalid_time", detail=escape(str(exc)), example="24h") from exc
                interval = max(3600.0, interval)
                await ctx.store.set_setting("backup.interval_seconds", interval)
                # Drop a queued job so the new interval is the one that applies.
                await ctx.store.db.execute(
                    "DELETE FROM jobs WHERE kind = 'backup.auto' AND status = 'pending'"
                )
                await ctx.runtime.scheduler.ensure_periodic("backup.auto", interval)
            await ctx.reply(ctx.t("backup.auto_changed", state=ctx.t("common.on" if mode == "on" else "common.off")))
            return
        enabled = await ctx.store.get_bool("backup.auto", False)
        interval = await ctx.store.get_setting("backup.interval_seconds", 86400)
        await ctx.reply(
            key_value_block(
                [
                    (ctx.t("backup.auto"), ctx.t("common.on" if enabled else "common.off")),
                    (ctx.t("backup.interval"), f"{int(interval) // 3600}h"),
                    (ctx.t("backup.retention"), ctx.config.backup_retention),
                ]
            )
        )
        return
    raise CommandError("errors.invalid_choice", field="action", choices="export, list, import, auto")


def safe_backup(name: str, ctx: CommandContext) -> str:
    from ..utils.security import safe_filename

    return safe_filename(f"havoc-import-{utc_now().strftime('%Y%m%d-%H%M%S')}.json", fallback=f"{name}.json")


# ---------------------------------------------------------------------------
# Plugins
# ---------------------------------------------------------------------------
async def cmd_plugins(ctx: CommandContext) -> None:
    action = (ctx.sub or "list").lower()
    if action in {"list", "ls", "status"}:
        rows = await ctx.store.list_plugins()
        loader = ctx.runtime.plugin_loader
        lines = [bold(ctx.t("plugins.header", count=len(rows)))]
        if not rows:
            lines.append(ctx.t("plugins.none", dir=str(ctx.config.plugin_dir)))
        for row in rows:
            state = "●" if row["enabled"] and not row["error"] else ("✗" if row["error"] else "⚪")
            lines.append(f"{state} {code(escape(row['name']))} v{escape(str(row['version'] or '0'))}" + (f" — {escape(row['error'][:80])}" if row["error"] else ""))
        if loader is not None:
            lines.append("")
            lines.append(ctx.t("plugins.loaded_now", count=len(loader.loaded)))
        lines.append("")
        lines.append(ctx.t("plugins.hint", dir=str(ctx.config.plugin_dir)))
        await ctx.reply("\n".join(lines))
        return
    if action in {"reload", "load"}:
        loader = ctx.runtime.plugin_loader
        if loader is None:
            raise CommandError("plugins.disabled")
        records = await loader.load_all()
        loaded = len([record for record in records if record.loaded])
        failed = len(records) - loaded
        await ctx.reply(ctx.t("plugins.reloaded", loaded=loaded, failed=failed))
        return
    if action in {"disable", "enable"}:
        name = (ctx.arg(0) or "").strip()
        if not name:
            raise CommandError("errors.missing_argument", usage=ctx.usage_text())
        await ctx.store.set_plugin_enabled(name, action == "enable")
        await ctx.reply(ctx.t("plugins.changed", name=escape(name), state=ctx.t("common.on" if action == "enable" else "common.off")))
        return
    raise CommandError("errors.invalid_choice", field="action", choices="list, reload, enable, disable")


def register(runtime: Any) -> None:
    runtime.scheduler.register_job("backup.auto", lambda row: backup_job(runtime, row))

    runtime.registry.register_many(
        [
            Command("chatsettings", cmd_chatsettings, category="management", aliases=("cs",), usage="[show|set|reset]", changes_state=True, subcommands=('show', 'list', 'set', 'reset')),
            Command("alias", cmd_alias, category="management", usage="[list|add|remove|clear]", changes_state=True, subcommands=('list', 'ls', 'add', 'remove', 'delete', 'rm', 'clear')),
            Command("quick", cmd_quick, category="management", usage="[list|add|remove|run] <name>", changes_state=True, subcommands=('list', 'ls', 'add', 'remove', 'delete', 'rm', 'run')),
            Command("backup", cmd_backup, category="management", usage="[export|list|import|auto]", changes_state=True, subcommands=('export', 'list', 'import', 'auto')),
            Command("plugins", cmd_plugins, category="management", usage="[list|reload|enable|disable]", changes_state=True, subcommands=('list', 'ls', 'status', 'reload', 'load', 'enable', 'disable')),
        ]
    )
