"""His address book and his block list — reading it, adding to it, and undoing it.

Contacts are the one part of Telegram that is genuinely personal, so the shape is
deliberate:

* ``.contacts [query]`` lists names, handles and numbers, never more than asked;
* ``.contact <phone> <name>`` imports a number the way a phone does;
* ``.block <chat>`` / ``.unblock <chat>`` block and unblock — blocking asks first,
  because it silently stops someone being able to reach him;
* ``.blocklist`` says who is blocked, which is the only way to undo it later.

Feature areas: 12 (people), 47 (privacy).
"""

from __future__ import annotations

from typing import Any

from ..command_registry import Command, CommandContext, CommandError
from ..services.tg_extra import ExtrasError
from ..utils.formatting import bold, code, escape
from ..utils.text import truncate

__all__ = ["register"]

DEFAULT_LIST = 25
MAX_LIST = 200
#: E.164-ish, or the local form people actually type. Telegram normalises it.
PHONE_RULE = "0123456789+ -()"


def _extras(ctx: CommandContext) -> Any:
    extras = getattr(ctx.runtime, "tg", None)
    if extras is None:
        raise CommandError("errors.not_connected")
    return extras


def _raise(exc: ExtrasError, key: str = "contacts.failed") -> None:
    raise CommandError(key, detail=escape(str(exc)[:180])) from exc


def _limit(ctx: CommandContext, default: int = DEFAULT_LIST) -> int:
    raw = ctx.flag("limit") or ""
    try:
        wanted = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(1, min(wanted, MAX_LIST))


async def cmd_contacts(ctx: CommandContext) -> None:
    query = " ".join(ctx.args).strip().lower()
    try:
        rows = await _extras(ctx).contacts(limit=MAX_LIST)
    except ExtrasError as exc:
        _raise(exc)
        return
    if query:
        rows = [
            row
            for row in rows
            if query in str(row.get("name", "")).lower()
            or query in str(row.get("username", "")).lower()
            or query in str(row.get("phone", ""))
        ]
    if not rows:
        await ctx.reply(
            ctx.t("contacts.nothing", query=escape(query)) if query else ctx.t("contacts.empty")
        )
        return
    shown = rows[: _limit(ctx)]
    lines = [bold(ctx.t("contacts.header", count=len(shown), total=len(rows)))]
    for row in shown:
        handle = f" {code('@' + str(row['username']))}" if row.get("username") else ""
        phone = f" · {code(str(row['phone']))}" if row.get("phone") else ""
        lines.append(
            f"• {escape(display_name_text(row))}{handle}{phone} · {code(str(row.get('id')))}"
        )
    if len(rows) > len(shown):
        lines.append(ctx.t("contacts.more", left=len(rows) - len(shown), prefix=ctx.runtime.prefix))
    await ctx.reply("\n".join(lines))


def display_name_text(row: dict[str, Any]) -> str:
    return truncate(str(row.get("name") or row.get("username") or row.get("id") or "—"), 60)


async def cmd_contact(ctx: CommandContext) -> None:
    words = [word for word in ctx.args if word.strip()]
    if len(words) < 2:
        raise CommandError("contacts.usage_add", prefix=ctx.runtime.prefix)
    phone = words[0]
    if any(character not in PHONE_RULE for character in phone):
        raise CommandError("contacts.bad_phone", value=escape(truncate(phone, 30)))
    name = " ".join(words[1:]).strip()
    first, _, last = name.partition(" ")
    try:
        added = await _extras(ctx).import_contact(phone, first, last)
    except ExtrasError as exc:
        _raise(exc, "contacts.add_failed")
        return
    if not added:
        await ctx.reply(ctx.t("contacts.add_none", value=escape(truncate(phone, 30))))
        return
    person = added[0]
    await ctx.reply(ctx.t("contacts.added", name=escape(display_name_text(person))))
    if person.get("id"):
        await ctx.store.add_chat_label(int(person["id"]), str(person.get("name") or ""))


async def _block_toggle(ctx: CommandContext, *, blocked: bool) -> None:
    who = " ".join(ctx.args).strip()
    if not who:
        raise CommandError("contacts.usage_block", prefix=ctx.runtime.prefix)
    target = await ctx.runtime.reach.resolve_or_raise(who)
    chat_id = getattr(target, "id", None)
    label = escape(getattr(target, "label", "") or str(chat_id))
    try:
        await _extras(ctx).block(chat_id, blocked=blocked)
    except ExtrasError as exc:
        _raise(exc, "contacts.block_failed")
        return
    await ctx.reply(
        ctx.t("contacts.blocked" if blocked else "contacts.unblocked", name=label, id=code(str(chat_id)))
    )


async def cmd_block(ctx: CommandContext) -> None:
    await _block_toggle(ctx, blocked=True)


async def cmd_unblock(ctx: CommandContext) -> None:
    await _block_toggle(ctx, blocked=False)


async def cmd_blocklist(ctx: CommandContext) -> None:
    try:
        rows = await _extras(ctx).blocked(limit=100)
    except ExtrasError as exc:
        _raise(exc)
        return
    if not rows:
        await ctx.reply(ctx.t("contacts.blocklist_empty"))
        return
    lines = [bold(ctx.t("contacts.blocklist_header", count=len(rows)))]
    for row in rows:
        handle = f" {code('@' + str(row['username']))}" if row.get("username") else ""
        lines.append(f"• {escape(display_name_text(row))}{handle} · {code(str(row.get('id')))}")
    lines.append(ctx.t("contacts.blocklist_hint", prefix=ctx.runtime.prefix))
    await ctx.reply("\n".join(lines))


def register(runtime: Any) -> None:
    runtime.registry.register_many(
        [
            Command(
                "contacts",
                cmd_contacts,
                category="people",
                aliases=("addressbook",),
                usage="[query] [--limit n]",
                description="His address book, as Telegram has it.",
                examples=(".contacts", ".contacts sara"),
            ),
            Command(
                "contact",
                cmd_contact,
                category="people",
                aliases=("addcontact",),
                usage="<phone> <name>",
                changes_state=True,
                description="Add a number to the address book.",
                examples=(".contact +15551234567 Sara",),
            ),
            Command(
                "block",
                cmd_block,
                category="people",
                usage="<chat>",
                changes_state=True,
                needs_confirmation=True,
                description="Stop someone being able to reach this account.",
                examples=(".block @spammer",),
            ),
            Command(
                "unblock",
                cmd_unblock,
                category="people",
                usage="<chat>",
                changes_state=True,
                needs_confirmation=True,
                description="Undo a block.",
                examples=(".unblock @spammer",),
            ),
            Command(
                "blocklist",
                cmd_blocklist,
                category="people",
                aliases=("blocked",),
                usage="",
                description="Who is blocked right now.",
            ),
        ]
    )
