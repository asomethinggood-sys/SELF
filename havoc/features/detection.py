"""Detection configuration: keyword watches, triggers, reactions, rules.

Detection never implies permission to act (spec §4). These commands only
*declare* rules; the subscribers in :mod:`havoc.features.automation` enforce
feature switches, chat allowlists, cooldowns, quiet hours and dry-run.

Feature areas: 29, 31-40, 73, 75, 80.
"""

from __future__ import annotations

import re
from typing import Any

from ..command_registry import Command, CommandContext, CommandError
from ..utils.formatting import bold, code, escape, key_value_block
from ..utils.text import truncate
from ..utils.validation import validate_emoji, validate_regex

MATCH_TYPES = ("contains", "exact", "startswith", "endswith", "regex", "any")
ACTION_TYPES = ("notify", "react", "template", "translate")
TRIGGER_EVENTS = ("message", "edit", "media", "url", "mention", "reply")


async def detect_status_text(ctx: CommandContext) -> str:
    runtime = ctx.runtime
    engine = runtime.event_engine
    metrics = engine.metrics.snapshot()
    watches = await ctx.store.list_watches(enabled_only=False)
    triggers = await ctx.store.list_triggers(enabled_only=False)
    reactions = await ctx.store.list_reaction_rules(enabled_only=False)
    state = await runtime.features.refresh()
    pairs = [
        (ctx.t("detect.processed"), metrics["processed"]),
        (ctx.t("detect.duplicates"), metrics["dropped_duplicates"]),
        (ctx.t("detect.errors"), metrics["subscriber_errors"]),
        (ctx.t("detect.bursts"), metrics["bursts"]),
        (ctx.t("detect.queue"), engine.snapshot()["queue_size"]),
        (ctx.t("detect.watches"), len(watches)),
        (ctx.t("detect.triggers"), len(triggers)),
        (ctx.t("detect.reactions"), len(reactions)),
        (ctx.t("detect.active_subscribers"), len(engine.subscriptions)),
    ]
    flags = [
        (ctx.t(f"feature.{name}.title", default=name), ctx.t("common.on" if on else "common.off"))
        for name, on in state.items()
        if name in {"watches", "triggers", "reactions", "notifications", "autoreply", "away", "auto_translate", "reminder_intent"}
    ]
    lines = [
        bold(ctx.t("detect.status_header")),
        key_value_block(pairs),
        "",
        bold(ctx.t("detect.feature_state")),
        key_value_block(flags),
        "",
        bold(ctx.t("detect.subscribers")),
    ]
    for sub in engine.subscriptions:
        state = "ok" if not sub.failures else ctx.t("detect.sub_failures", count=sub.failures)
        types = ", ".join(sorted(event.value for event in sub.event_types)) if sub.event_types else "all"
        lines.append(f"• {code(sub.name)} — {escape(types)} ({state})")
    return "\n".join(lines)


async def cmd_watch(ctx: CommandContext) -> None:
    action = (ctx.sub or "list").lower()
    if action in {"list", "ls"}:
        rows = await ctx.store.list_watches(enabled_only=False)
        if not rows:
            await ctx.reply(ctx.t("watch.empty", prefix=ctx.runtime.prefix))
            return
        lines = [bold(ctx.t("watch.header", count=len(rows)))]
        for row in rows:
            state = "●" if row["enabled"] else "⚪"
            scope = ctx.t("watch.scope_all") if row["scope_chat_id"] is None else str(row["scope_chat_id"])
            lines.append(f"{state} #{row['id']} {code(escape(row['keyword']))} — {escape(scope)}")
        await ctx.reply("\n".join(lines))
        return
    if action == "add":
        keyword = " ".join(ctx.args).strip()
        if not keyword:
            raise CommandError("errors.missing_argument", usage=ctx.usage_text())
        if len(keyword) > 100:
            raise CommandError("errors.too_long", field="keyword", limit=100)
        scope = ctx.chat_id if ctx.has_flag("chat") else None
        watch_id = await ctx.store.add_watch(keyword, scope_chat_id=scope, notify_chat_id=None, language=ctx.flag("lang") or "auto")
        await ctx.reply(
            ctx.t("watch.added", id=watch_id, keyword=escape(keyword))
            + "\n"
            + ctx.t("watch.enable_hint", prefix=ctx.runtime.prefix)
        )
        return
    if action in {"remove", "delete", "rm"}:
        watch_id = _int_arg(ctx, 0)
        if not await ctx.store.delete_watch(watch_id):
            raise CommandError("errors.not_found", what="watch", id=watch_id)
        await ctx.reply(ctx.t("watch.removed", id=watch_id))
        return
    if action in {"on", "off"}:
        await ctx.runtime.feature_manager.set_enabled("watches", action == "on")
        if action == "on":
            await ctx.runtime.feature_manager.set_enabled("notifications", True)
        await ctx.reply(ctx.t("watch.toggled", state=ctx.t("common.on" if action == "on" else "common.off")))
        return
    raise CommandError("errors.invalid_choice", field="action", choices="list, add, remove, on, off")


async def cmd_trigger(ctx: CommandContext) -> None:
    action = (ctx.sub or "list").lower()
    store = ctx.store
    if action in {"list", "ls"}:
        rows = await store.list_triggers(enabled_only=False)
        if not rows:
            await ctx.reply(ctx.t("trigger.empty", prefix=ctx.runtime.prefix))
            return
        lines = [bold(ctx.t("trigger.header", count=len(rows)))]
        for row in rows:
            state = "●" if row["enabled"] else "⚪"
            lines.append(
                f"{state} {code(escape(row['name']))} [{escape(row['event_type'])}/{escape(row['match_type'])}] "
                f"{escape(truncate(row['pattern'], 30))} → {escape(row['action_type'])}"
            )
        await ctx.reply("\n".join(lines))
        return
    if action == "add":
        name = (ctx.arg(0) or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9_\-]{1,32}", name):
            raise CommandError("errors.invalid_name", value=escape(name))
        if not ctx.raw_args:
            raise CommandError("errors.missing_argument", usage=ctx.usage_text())
        # syntax: .trigger add name "pattern" action payload
        parts = _split_trigger_body(ctx.rest_raw, name)
        if not parts:
            raise CommandError("trigger.usage_hint", prefix=ctx.runtime.prefix)
        pattern = parts[0]
        # The action is optional: a trigger that only notifies is the safe default.
        action_type = parts[1].lower() if len(parts) > 1 else "notify"
        payload = parts[2] if len(parts) > 2 else None
        match_type = (ctx.flag("match") or "contains").lower()
        event_name = (ctx.flag("event") or "message").lower()
        if match_type not in MATCH_TYPES:
            raise CommandError("errors.invalid_choice", field="match", choices=", ".join(MATCH_TYPES))
        if event_name not in TRIGGER_EVENTS:
            raise CommandError("errors.invalid_choice", field="event", choices=", ".join(TRIGGER_EVENTS))
        if action_type not in ACTION_TYPES:
            raise CommandError("errors.invalid_choice", field="action", choices=", ".join(ACTION_TYPES))
        if match_type == "regex":
            try:
                validate_regex(pattern)
            except Exception as exc:  # noqa: BLE001
                raise CommandError("errors.invalid_regex", detail=escape(str(exc))) from exc
        if action_type == "react":
            if not payload:
                raise CommandError("errors.missing_argument", usage=".trigger add name pattern react ⌣")
            validate_emoji(payload)
        if action_type == "template" and not payload:
            raise CommandError("errors.missing_argument", usage=".trigger add name pattern template <template>")
        scope = ctx.chat_id if ctx.has_flag("chat") else None
        await store.add_trigger(
            name,
            pattern,
            event_type=event_name,
            match_type=match_type,
            action_type=action_type,
            payload=payload,
            scope_chat_id=scope,
            language=ctx.flag("lang") or "auto",
        )
        await ctx.reply(
            ctx.t("trigger.added", name=escape(name))
            + "\n"
            + ctx.t("trigger.enable_hint", prefix=ctx.runtime.prefix)
        )
        return
    if action == "test":
        name = (ctx.arg(0) or "").strip().lower()
        text = " ".join(ctx.args[1:]).strip()
        if not name or not text:
            raise CommandError("errors.missing_argument", usage=ctx.usage_text())
        rows = await store.list_triggers(enabled_only=False)
        row = next((item for item in rows if item["name"] == name), None)
        if row is None:
            raise CommandError("errors.not_found", what="trigger", id=escape(name))
        matched = _matches(text, row["pattern"], row["match_type"])
        await ctx.reply(
            "\n".join(
                [
                    bold(ctx.t("trigger.test_header", name=escape(name))),
                    key_value_block(
                        [
                            (ctx.t("trigger.matched"), ctx.t("common.yes" if matched else "common.no")),
                            (ctx.t("trigger.pattern"), escape(row["pattern"])),
                            (ctx.t("trigger.action"), escape(row["action_type"])),
                            (ctx.t("trigger.payload"), escape(str(row["payload"] or "—"))),
                        ]
                    ),
                ]
            )
        )
        return
    if action in {"on", "off"}:
        name = (ctx.arg(0) or "").strip().lower()
        row = next((item for item in await store.list_triggers(enabled_only=False) if item["name"] == name), None)
        if row is None:
            raise CommandError("errors.not_found", what="trigger", id=escape(name))
        await store.set_trigger_enabled(row["id"], action == "on")
        await ctx.reply(ctx.t("trigger.changed", name=escape(name), state=ctx.t("common.on" if action == "on" else "common.off")))
        return
    if action in {"remove", "delete", "rm"}:
        name = (ctx.arg(0) or "").strip().lower()
        row = next((item for item in await store.list_triggers(enabled_only=False) if item["name"] == name), None)
        if row is None:
            raise CommandError("errors.not_found", what="trigger", id=escape(name))
        await store.delete_trigger(row["id"])
        await ctx.reply(ctx.t("trigger.removed", name=escape(name)))
        return
    raise CommandError("errors.invalid_choice", field="action", choices="list, add, test, on, off, remove")


def _split_trigger_body(raw: str, name: str) -> list[str]:
    body = raw[len(name) :].strip() if raw.startswith(name) else raw
    parts: list[str] = []
    buffer = ""
    in_quotes = False
    for char in body:
        if char == '"':
            in_quotes = not in_quotes
            continue
        if char.isspace() and not in_quotes:
            if buffer:
                parts.append(buffer)
                buffer = ""
            continue
        buffer += char
    if buffer:
        parts.append(buffer)
    return parts


def _matches(text: str, pattern: str, match_type: str) -> bool:
    haystack = text.lower()
    needle = pattern.lower()
    if match_type == "any":
        return True
    if match_type == "exact":
        return haystack.strip() == needle.strip()
    if match_type == "startswith":
        return haystack.startswith(needle)
    if match_type == "endswith":
        return haystack.endswith(needle)
    if match_type == "regex":
        try:
            return re.search(pattern, text, re.IGNORECASE) is not None
        except re.error:
            return False
    return needle in haystack


async def cmd_react(ctx: CommandContext) -> None:
    action = (ctx.sub or "list").lower()
    if action in {"list", "ls"}:
        rows = await ctx.store.list_reaction_rules(enabled_only=False)
        if not rows:
            await ctx.reply(ctx.t("react.empty", prefix=ctx.runtime.prefix))
            return
        lines = [bold(ctx.t("react.header", count=len(rows)))]
        for row in rows:
            state = "●" if row["enabled"] else "⚪"
            lines.append(
                f"{state} #{row['id']} {code(escape(row['pattern']))} → {escape(row['emoji'])} "
                f"({row['cooldown']}s)"
            )
        await ctx.reply("\n".join(lines))
        return
    if action == "add":
        raw = " ".join(ctx.args).strip()
        if "=>" not in raw:
            raise CommandError("react.usage_hint", prefix=ctx.runtime.prefix)
        pattern, _, emoji_raw = raw.partition("=>")
        pattern = pattern.strip()
        emoji_raw = emoji_raw.strip()
        if not pattern or not emoji_raw:
            raise CommandError("react.usage_hint", prefix=ctx.runtime.prefix)
        emoji = validate_emoji(emoji_raw)
        try:
            cooldown = int(ctx.flag("cooldown") or 60)
        except ValueError as exc:
            raise CommandError("errors.not_a_number", field="cooldown") from exc
        scope = ctx.chat_id if ctx.has_flag("chat") else None
        rule_id = await ctx.store.add_reaction_rule(pattern, emoji, match_type=(ctx.flag("match") or "contains"), scope_chat_id=scope, cooldown=cooldown)
        await ctx.reply(
            ctx.t("react.added", id=rule_id, emoji=escape(emoji))
            + "\n"
            + ctx.t("react.enable_hint", prefix=ctx.runtime.prefix)
        )
        return
    if action in {"remove", "delete", "rm"}:
        rule_id = _int_arg(ctx, 0)
        if not await ctx.store.delete_reaction_rule(rule_id):
            raise CommandError("errors.not_found", what="reaction rule", id=rule_id)
        await ctx.reply(ctx.t("react.removed", id=rule_id))
        return
    if action in {"on", "off"}:
        await ctx.runtime.feature_manager.set_enabled("reactions", action == "on")
        await ctx.reply(ctx.t("react.toggled", state=ctx.t("common.on" if action == "on" else "common.off")))
        return
    raise CommandError("errors.invalid_choice", field="action", choices="list, add, remove, on, off")


async def cmd_rules(ctx: CommandContext) -> None:
    runtime = ctx.runtime
    features = await runtime.feature_manager.refresh()
    watches = len(await ctx.store.list_watches(enabled_only=False))
    triggers = len(await ctx.store.list_triggers(enabled_only=False))
    reactions = len(await ctx.store.list_reaction_rules(enabled_only=False))
    autoreplies = len(await ctx.store.list_autoreplies())
    templates = len(await ctx.store.list_templates())
    pairs = [
        (ctx.t("rules.autoreply"), f"{autoreplies} ({ctx.t('common.on' if features.get('autoreply') else 'common.off')})"),
        (ctx.t("rules.watches"), f"{watches} ({ctx.t('common.on' if features.get('watches') else 'common.off')})"),
        (ctx.t("rules.triggers"), f"{triggers} ({ctx.t('common.on' if features.get('triggers') else 'common.off')})"),
        (ctx.t("rules.reactions"), f"{reactions} ({ctx.t('common.on' if features.get('reactions') else 'common.off')})"),
        (ctx.t("rules.templates"), templates),
        (ctx.t("rules.emergency"), ctx.t("common.active" if await runtime.store.emergency_stop() else "common.inactive")),
        (ctx.t("rules.dry_run"), ctx.t("common.on" if await runtime.permissions.dry_run_active() else "common.off")),
        (ctx.t("rules.quiet_hours"), ctx.t("common.active" if await runtime.permissions.in_quiet_hours(ctx.chat_id) else "common.inactive")),
    ]
    await ctx.reply(
        bold(ctx.t("rules.header"))
        + "\n"
        + key_value_block(pairs)
        + "\n\n"
        + ctx.t("rules.hint", prefix=runtime.prefix)
    )


def _int_arg(ctx: CommandContext, index: int) -> int:
    try:
        return int(ctx.arg(index))
    except (TypeError, ValueError) as exc:
        raise CommandError("errors.not_a_number", field="id") from exc


def register(runtime: Any) -> None:
    runtime.registry.register_many(
        [
            Command("watch", cmd_watch, category="detection", usage="[list|add|remove|on|off]", changes_state=True, subcommands=('list', 'ls', 'add', 'remove', 'delete', 'rm', 'on', 'off')),
            Command("trigger", cmd_trigger, category="detection", usage="[list|add|test|on|off|remove]", changes_state=True, subcommands=('list', 'ls', 'add', 'test', 'on', 'off', 'remove', 'delete', 'rm')),
            Command("react", cmd_react, category="detection", usage="[list|add|remove|on|off]", changes_state=True, subcommands=('list', 'ls', 'add', 'remove', 'delete', 'rm', 'on', 'off')),
            Command("rules", cmd_rules, category="detection"),
        ]
    )
