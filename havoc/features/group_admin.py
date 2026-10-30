"""Running a group: the title, the description, slow mode, and who may stay.

Every one of these is something only an administrator can do, and Telegram is the
one that says no — the answer here is Telegram's refusal, in his chat, not a
guess. The two that remove someone from a place (``.kick`` and ``.leave``) ask
first; ``.ban`` does too, because it is the one that keeps them out.

* ``.settitle <text>`` / ``.setdesc <text>`` — rename, re-describe;
* ``.slowmode <30s|5m|off>`` — throttle or un-throttle a supergroup;
* ``.kick <person>`` / ``.ban <person>`` / ``.unban <person>`` — one person;
* ``.promote <person>`` / ``.demote <person>`` — admin rights, the boring safe set;
* ``.leave [chat]`` — leave the group standing in front of him (or a named one).

``.promote`` grants exactly the rights named by flags and nothing else; a bot that
silently makes someone a full admin is how groups get taken over.

Feature areas: 12 (groups), 96 (feature switches).
"""

from __future__ import annotations

import re
from typing import Any

from ..command_registry import Command, CommandContext, CommandError
from ..services.tg_extra import ExtrasError
from ..utils.formatting import bold, escape
from ..utils.text import truncate

__all__ = ["register"]

MAX_TITLE = 128
MAX_ABOUT = 255
#: The rights ``.promote`` will grant when asked. Anything beyond this list is
#: not reachable from a flag, on purpose.
PROMOTE_FLAGS = {
    "delete": "delete_messages",
    "pin": "pin_messages",
    "invite": "invite_users",
    "ban": "ban_users",
    "manage": "manage_call",
}
_DURATION = re.compile(r"^(?P<value>\d+)\s*(?P<unit>s|sec|m|min|h|hour)?$", re.I)


def _extras(ctx: CommandContext) -> Any:
    extras = getattr(ctx.runtime, "tg", None)
    if extras is None:
        raise CommandError("errors.not_connected")
    return extras


def _fail(exc: ExtrasError, key: str = "admin.failed") -> None:
    raise CommandError(key, detail=escape(str(exc)[:180])) from exc


def _chat(ctx: CommandContext, raw: str = "") -> Any:
    """The chat he named, or the one he is standing in."""
    return raw.strip() or ctx.chat_id


def _seconds(raw: str) -> int:
    """``30``, ``30s``, ``5m``, ``2h``, ``off`` → seconds."""
    text = (raw or "").strip().lower()
    if text in {"off", "0", "none", "stop"}:
        return 0
    match = _DURATION.match(text)
    if not match:
        raise CommandError("admin.bad_duration", value=escape(truncate(raw, 20)))
    value = int(match.group("value"))
    unit = (match.group("unit") or "s").lower()
    factor = 3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1
    return min(value * factor, 86400)


async def cmd_settitle(ctx: CommandContext) -> None:
    title = " ".join(ctx.args).strip()
    if not title:
        raise CommandError("admin.usage_settitle", prefix=ctx.runtime.prefix)
    target = _chat(ctx)
    try:
        await _extras(ctx).set_title(target, title[:MAX_TITLE])
    except ExtrasError as exc:
        _fail(exc, "admin.title_failed")
        return
    await ctx.reply(ctx.t("admin.title_set", title=escape(truncate(title, MAX_TITLE))))


async def cmd_setdesc(ctx: CommandContext) -> None:
    about = (ctx.rest_raw or " ".join(ctx.args)).strip()
    if not about:
        raise CommandError("admin.usage_setdesc", prefix=ctx.runtime.prefix)
    target = _chat(ctx)
    try:
        await _extras(ctx).set_about(target, about[:MAX_ABOUT])
    except ExtrasError as exc:
        _fail(exc, "admin.desc_failed")
        return
    await ctx.reply(ctx.t("admin.desc_set", count=len(about)))


async def cmd_slowmode(ctx: CommandContext) -> None:
    raw = ctx.arg(0) or ctx.flag("to") or ""
    if not raw:
        raise CommandError("admin.usage_slowmode", prefix=ctx.runtime.prefix)
    seconds = _seconds(raw)
    try:
        applied = await _extras(ctx).slowmode(ctx.chat_id, seconds)
    except ExtrasError as exc:
        _fail(exc, "admin.slowmode_failed")
        return
    if not applied:
        await ctx.reply(ctx.t("admin.slowmode_off"))
        return
    await ctx.reply(ctx.t("admin.slowmode_set", seconds=applied))


async def _person(ctx: CommandContext) -> tuple[Any, str, int]:
    who = " ".join(ctx.args).strip()
    if not who:
        raise CommandError("admin.usage_person", prefix=ctx.runtime.prefix)
    target = await ctx.runtime.reach.resolve_or_raise(who)
    person = getattr(target, "id", None)
    label = escape(getattr(target, "label", "") or str(person))
    return person, label, int(person or 0)


async def _call_people(ctx: CommandContext, method: str, key: str, **kwargs: Any) -> None:
    person, label, _ = await _person(ctx)
    client = ctx.runtime.client
    caller = getattr(client, method, None)
    if caller is None:
        raise CommandError("errors.unsupported", detail=escape(method))
    try:
        await caller(ctx.chat_id, person, **kwargs)
    except Exception as exc:  # noqa: BLE001 - Telegram's refusal is the message
        raise CommandError(key, name=label, detail=escape(str(exc)[:180])) from exc
    await ctx.reply(ctx.t(key.replace("_failed", "_done"), name=label))


async def cmd_kick(ctx: CommandContext) -> None:
    await _call_people(ctx, "kick_participant", "admin.kick_failed")


async def cmd_ban(ctx: CommandContext) -> None:
    await _call_people(ctx, "edit_permissions", "admin.ban_failed", view_messages=False)


async def cmd_unban(ctx: CommandContext) -> None:
    await _call_people(ctx, "edit_permissions", "admin.unban_failed", view_messages=True)


async def cmd_promote(ctx: CommandContext) -> None:
    person, label, _ = await _person(ctx)
    granted = [right for flag, right in PROMOTE_FLAGS.items() if ctx.has_flag(flag)]
    if not granted:
        raise CommandError("admin.promote_needs_rights", flags=escape(", ".join("--" + f for f in PROMOTE_FLAGS)))
    client = ctx.runtime.client
    caller = getattr(client, "edit_admin", None)
    if caller is None:
        raise CommandError("errors.unsupported", detail=escape("edit_admin"))
    rights = {right: True for right in granted}
    try:
        await caller(ctx.chat_id, person, is_admin=True, **rights)
    except Exception as exc:  # noqa: BLE001
        raise CommandError("admin.promote_failed", name=label, detail=escape(str(exc)[:180])) from exc
    await ctx.reply(
        "\n".join(
            [
                bold(ctx.t("admin.promote_done", name=label)),
                "◦ " + escape(", ".join(right.replace("_", " ") for right in granted)),
            ]
        )
    )


async def cmd_demote(ctx: CommandContext) -> None:
    person, label, _ = await _person(ctx)
    client = ctx.runtime.client
    caller = getattr(client, "edit_admin", None)
    if caller is None:
        raise CommandError("errors.unsupported", detail=escape("edit_admin"))
    try:
        await caller(ctx.chat_id, person, is_admin=False)
    except Exception as exc:  # noqa: BLE001
        raise CommandError("admin.demote_failed", name=label, detail=escape(str(exc)[:180])) from exc
    await ctx.reply(ctx.t("admin.demote_done", name=label))


async def cmd_leave(ctx: CommandContext) -> None:
    raw = " ".join(ctx.args).strip()
    target = _chat(ctx, raw)
    label = escape(raw or str(target))
    try:
        await _extras(ctx).leave(target)
    except ExtrasError as exc:
        _fail(exc, "admin.leave_failed")
        return
    await ctx.reply(ctx.t("admin.left", name=label))


def register(runtime: Any) -> None:
    runtime.registry.register_many(
        [
            Command(
                "settitle",
                cmd_settitle,
                category="admin",
                aliases=("retitle",),
                usage="<text>",
                changes_state=True,
                description="Rename the group you are standing in.",
                examples=(".settitle Night Shift",),
            ),
            Command(
                "setdesc",
                cmd_setdesc,
                category="admin",
                aliases=("setabout",),
                usage="<text>",
                changes_state=True,
                description="Rewrite the group description.",
                examples=(".setdesc quiet after midnight",),
            ),
            Command(
                "slowmode",
                cmd_slowmode,
                category="admin",
                usage="<30s|5m|off>",
                changes_state=True,
                description="Throttle how often people may post.",
                examples=(".slowmode 30s", ".slowmode off"),
            ),
            Command(
                "kick",
                cmd_kick,
                category="admin",
                usage="<person>",
                changes_state=True,
                needs_confirmation=True,
                description="Remove someone from the group; they may come back.",
                examples=(".kick @loud",),
            ),
            Command(
                "ban",
                cmd_ban,
                category="admin",
                usage="<person>",
                changes_state=True,
                needs_confirmation=True,
                description="Remove someone and keep them out.",
                examples=(".ban @loud",),
            ),
            Command(
                "unban",
                cmd_unban,
                category="admin",
                usage="<person>",
                changes_state=True,
                description="Let a banned person back in.",
                examples=(".unban @loud",),
            ),
            Command(
                "promote",
                cmd_promote,
                category="admin",
                usage="<person> [--delete] [--pin] [--invite] [--ban] [--manage]",
                changes_state=True,
                needs_confirmation=True,
                description="Make someone an admin with exactly the rights you name.",
                examples=(".promote @sara --delete --pin",),
            ),
            Command(
                "demote",
                cmd_demote,
                category="admin",
                usage="<person>",
                changes_state=True,
                needs_confirmation=True,
                description="Take admin rights away.",
                examples=(".demote @sara",),
            ),
            Command(
                "leave",
                cmd_leave,
                category="admin",
                usage="[chat]",
                changes_state=True,
                needs_confirmation=True,
                description="Leave a group, this one or the one you name.",
                examples=(".leave", ".leave @noise"),
            ),
        ]
    )
