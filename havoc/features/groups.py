"""Groups, members, invite links, folders, muting, and the devices signed in.

Every command here asks Telegram, reports what Telegram said, and refuses in a
sentence when the account is not allowed to do it. Three rules:

* **A link he did not give is not a link she follows.** ``.join`` takes the link
  and nothing else; it never guesses a chat by name.
* **Membership changes are confirmed before they happen**, because adding the
  wrong person to a group is visible to everyone in it and cannot be unsaid.
* **A read is a read.** Listing groups, folders and sessions changes nothing.

Feature areas: 44 (chats), 47 (privacy), 96 (feature switches).
"""

from __future__ import annotations

from typing import Any

from ..command_registry import Command, CommandContext, CommandError
from ..services.tg_extra import ExtrasError
from ..utils.formatting import bold, code, escape
from ..utils.text import truncate

__all__ = ["register"]


def _fail(exc: ExtrasError, *, key: str = "groups.failed") -> CommandError:
    """One shape for every raw-call refusal, so the wording stays consistent."""
    return CommandError(key, detail=escape(truncate(str(exc), 200)))


async def _named_target(ctx: CommandContext, raw: str) -> Any:
    text = (raw or "").strip()
    if not text:
        raise CommandError("groups.usage_target", prefix=ctx.runtime.prefix)
    try:
        return await ctx.runtime.reach.resolve_or_raise(text)
    except Exception as exc:  # noqa: BLE001 - ReachError carries the name
        raise CommandError("reach.unknown_target", name=escape(text)) from exc


def _seconds(raw: str) -> int:
    """``1h``, ``30m``, ``2d``, ``off``/``0`` → seconds. ``-1`` means forever."""
    text = str(raw or "").strip().lower()
    if not text:
        return 0
    if text in {"off", "unmute", "0", "stop"}:
        return 0
    if text in {"forever", "always", "∞"}:
        return 10 * 365 * 86400
    from ..services.reach import parse_duration

    seconds = parse_duration(text)
    if seconds is None:
        raise CommandError("errors.not_a_number", field=text)
    return int(seconds)


# ---------------------------------------------------------------------------
# .groups / .members
# ---------------------------------------------------------------------------
async def cmd_groups(ctx: CommandContext) -> None:
    try:
        rows = await ctx.runtime.tg.groups()
    except ExtrasError as exc:
        raise _fail(exc) from exc
    if not rows:
        await ctx.reply(ctx.t("groups.empty"))
        return
    try:
        wanted = int(ctx.arg(0) or 15)
    except ValueError:
        wanted = 15
    wanted = max(1, min(wanted, 40))
    lines = [bold(ctx.t("groups.header", count=len(rows)))]
    for row in rows[:wanted]:
        members = (
            f" · {ctx.t('groups.members_count', count=row['members'])}" if row["members"] else ""
        )
        lines.append(
            f"• {escape(row['title'])} — {code(row['id'])}{members}"
            + (f" {code('@' + row['username'])}" if row["username"] else "")
        )
    if len(rows) > wanted:
        lines.append(ctx.t("groups.more", rest=len(rows) - wanted))
    await ctx.reply("\n".join(lines))


async def cmd_members(ctx: CommandContext) -> None:
    target = await _named_target(ctx, " ".join(ctx.args))
    try:
        people = await ctx.runtime.tg.participants(target.id, limit=200)
    except ExtrasError as exc:
        raise _fail(exc, key="groups.members_failed") from exc
    if not people:
        await ctx.reply(ctx.t("groups.no_members", name=escape(target.label)))
        return
    admins = [row for row in people if row["admin"]]
    lines = [bold(ctx.t("groups.members_header", name=escape(target.label), count=len(people)))]
    if admins:
        lines.append(ctx.t("groups.admins", count=len(admins)))
        lines.extend(f"★ {escape(row['name'])} — {code(row['id'])}" for row in admins[:10])
    others = [row for row in people if not row["admin"]][:15]
    if others:
        lines.append(ctx.t("groups.everyone_else"))
        lines.extend(f"• {escape(row['name'])} — {code(row['id'])}" for row in others)
    await ctx.reply("\n".join(lines))


# ---------------------------------------------------------------------------
# .invite / .join
# ---------------------------------------------------------------------------
async def cmd_invite(ctx: CommandContext) -> None:
    target = await _named_target(ctx, " ".join(ctx.args))
    try:
        link = await ctx.runtime.tg.invite_link(target.id)
    except ExtrasError as exc:
        raise _fail(exc, key="groups.invite_failed") from exc
    await ctx.reply(
        "\n".join(
            [
                bold(ctx.t("groups.invite_header", name=escape(target.label))),
                code(link),
                ctx.t("groups.invite_note"),
            ]
        )
    )


async def cmd_join(ctx: CommandContext) -> None:
    link = " ".join(ctx.args).strip()
    if not link:
        raise CommandError("groups.usage_join", prefix=ctx.runtime.prefix)
    try:
        name = await ctx.runtime.tg.join(link)
    except ExtrasError as exc:
        raise _fail(exc, key="groups.join_failed") from exc
    await ctx.reply(ctx.t("groups.joined", name=escape(name)))


# ---------------------------------------------------------------------------
# .folder
# ---------------------------------------------------------------------------
async def cmd_folder(ctx: CommandContext) -> None:
    action = (ctx.sub or "list").lower()
    if action not in {"list", "ls", "show"}:
        raise CommandError("groups.usage_folder", prefix=ctx.runtime.prefix)
    try:
        rows = await ctx.runtime.tg.folders()
    except ExtrasError as exc:
        raise _fail(exc, key="groups.folder_failed") from exc
    if not rows:
        await ctx.reply(ctx.t("groups.no_folders"))
        return
    lines = [bold(ctx.t("groups.folders_header", count=len(rows)))]
    for row in rows:
        lines.append(
            f"• {escape(str(row['title']))} — {code(str(row['count']))} {ctx.t('groups.chats')}"
        )
    await ctx.reply("\n".join(lines))


# ---------------------------------------------------------------------------
# .mute / .unmute
# ---------------------------------------------------------------------------
async def cmd_mute(ctx: CommandContext) -> None:
    args = list(ctx.args)
    if not args:
        raise CommandError("groups.usage_mute", prefix=ctx.runtime.prefix)
    target = await _named_target(ctx, args[0])
    seconds = _seconds(args[1] if len(args) > 1 else "forever")
    try:
        await ctx.runtime.tg.mute(target.id, seconds)
    except ExtrasError as exc:
        raise _fail(exc, key="groups.mute_failed") from exc
    await ctx.reply(
        ctx.t(
            "groups.muted",
            name=escape(target.label),
            when=escape(_human(seconds, ctx)),
        )
    )


async def cmd_unmute(ctx: CommandContext) -> None:
    target = await _named_target(ctx, " ".join(ctx.args))
    try:
        await ctx.runtime.tg.mute(target.id, 0)
    except ExtrasError as exc:
        raise _fail(exc, key="groups.mute_failed") from exc
    await ctx.reply(ctx.t("groups.unmuted", name=escape(target.label)))


def _human(seconds: int, ctx: CommandContext) -> str:
    if seconds <= 0:
        return ctx.t("groups.forever_never")
    if seconds >= 365 * 86400:
        return ctx.t("common.forever", default="forever")
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


# ---------------------------------------------------------------------------
# .sessions — who is signed in
# ---------------------------------------------------------------------------
async def cmd_sessions(ctx: CommandContext) -> None:
    try:
        rows = await ctx.runtime.tg.sessions()
    except ExtrasError as exc:
        raise _fail(exc, key="groups.sessions_failed") from exc
    if not rows:
        await ctx.reply(ctx.t("groups.no_sessions"))
        return
    lines = [bold(ctx.t("groups.sessions_header", count=len(rows)))]
    for index, row in enumerate(rows, start=1):
        mark = " ●" if row["current"] else ""
        lines.append(
            f"{code(str(index))} {escape(row['device'])} · {escape(row['platform'])}"
            f" · {escape(row['country'])}{mark}"
        )
    lines.append(ctx.t("groups.sessions_hint", prefix=ctx.runtime.prefix))
    ctx.runtime.stats["sessions_last"] = [row["hash"] for row in rows]  # type: ignore[assignment]
    await ctx.reply("\n".join(lines))


async def cmd_endsession(ctx: CommandContext) -> None:
    """``.endsession 2`` — the index ``.sessions`` just printed, nothing else."""
    raw = ctx.arg(0).strip()
    if not raw.isdigit():
        raise CommandError("groups.usage_endsession", prefix=ctx.runtime.prefix)
    hashes = ctx.runtime.stats.get("sessions_last") or []
    index = int(raw)
    if not hashes or index < 1 or index > len(hashes):
        raise CommandError("groups.session_unknown", prefix=ctx.runtime.prefix)
    try:
        await ctx.runtime.tg.terminate_session(int(hashes[index - 1]))
    except ExtrasError as exc:
        raise _fail(exc, key="groups.session_failed") from exc
    await ctx.reply(ctx.t("groups.session_ended", index=index))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register(runtime: Any) -> None:
    runtime.registry.register_many(
        [
            Command(
                "groups",
                cmd_groups,
                category="chats",
                aliases=("guilds",),
                usage="[count]",
                description="Your groups, with member counts.",
                examples=(".groups", ".groups 30"),
            ),
            Command(
                "members",
                cmd_members,
                category="chats",
                usage="<chat>",
                description="Who is in a group, admins first.",
                examples=(".members lunch",),
            ),
            Command(
                "invite",
                cmd_invite,
                category="chats",
                usage="<chat>",
                description="An invite link for a chat, revocable from Telegram.",
                changes_state=True,
                examples=(".invite lunch",),
            ),
            Command(
                "join",
                cmd_join,
                category="chats",
                usage="<t.me link>",
                description="Join a chat by the link you were given.",
                changes_state=True,
            ),
            Command(
                "folder",
                cmd_folder,
                category="chats",
                aliases=("folders",),
                usage="[list]",
                description="Your Telegram folders.",
                subcommands=("list", "ls", "show"),
            ),
            Command(
                "mute",
                cmd_mute,
                category="chats",
                usage="<chat> [1h|2d|forever]",
                description="Silence a chat for a while, or forever.",
                changes_state=True,
                examples=(".mute lunch 2h",),
            ),
            Command(
                "unmute",
                cmd_unmute,
                category="chats",
                usage="<chat>",
                description="Let a chat be loud again.",
                changes_state=True,
            ),
            Command(
                "sessions",
                cmd_sessions,
                category="account",
                aliases=("devices",),
                usage="",
                description="Every device signed into this account.",
            ),
            Command(
                "endsession",
                cmd_endsession,
                category="account",
                usage="<number from .sessions>",
                description="Sign one of those devices out.",
                changes_state=True,
                needs_confirmation=True,
            ),
        ]
    )
