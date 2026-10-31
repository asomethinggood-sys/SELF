"""What Telegram lets other people see, and whether the door has a second lock.

Both commands are **read-only on purpose**. Reading the settings is what tells him
whether his number is public; changing them is one tap in Telegram's own Privacy
screen, and a bot that can silently rewrite who may see his phone number is a
worse risk than the convenience is worth. ``.security`` reports whether the
two-step lock is on and what the hint says — never the password, never a change.

* ``.privacy`` — last seen, photo, number, calls, invites, added-by-phone, birthday;
* ``.security`` — two-step lock, hint, recovery email, pending reset.

Feature areas: 47 (privacy), 17 (security posture).
"""

from __future__ import annotations

from typing import Any

from ..command_registry import Command, CommandContext, CommandError
from ..services.tg_extra import ExtrasError
from ..utils.formatting import bold, escape

__all__ = ["register"]


def _extras(ctx: CommandContext) -> Any:
    extras = getattr(ctx.runtime, "tg", None)
    if extras is None:
        raise CommandError("errors.not_connected")
    return extras


async def cmd_privacy(ctx: CommandContext) -> None:
    try:
        rows = await _extras(ctx).privacy()
    except ExtrasError as exc:
        raise CommandError("privacy.failed", detail=escape(str(exc)[:180])) from exc
    if not rows:
        await ctx.reply(ctx.t("privacy.empty"))
        return
    lines = [bold(ctx.t("privacy.header"))]
    for row in rows:
        lines.append(
            f"• {escape(ctx.t(row['label']))} — {escape(ctx.t('privacy.' + row['value']))}"
        )
    lines.append(ctx.t("privacy.hint"))
    await ctx.reply("\n".join(lines))


async def cmd_security(ctx: CommandContext) -> None:
    try:
        state = await _extras(ctx).security()
    except ExtrasError as exc:
        raise CommandError("privacy.failed", detail=escape(str(exc)[:180])) from exc
    lines = [bold(ctx.t("privacy.security_header"))]
    lines.append(
        ctx.t("privacy.two_step_on") if state.get("two_factor") else ctx.t("privacy.two_step_off")
    )
    hint = str(state.get("hint") or "").strip()
    if hint:
        lines.append(ctx.t("privacy.hint_line", hint=escape(hint)))
    lines.append(
        ctx.t("privacy.recovery_yes") if state.get("recovery") else ctx.t("privacy.recovery_no")
    )
    if state.get("pending_reset"):
        lines.append(ctx.t("privacy.pending_reset"))
    lines.append(ctx.t("privacy.security_hint", prefix=ctx.runtime.prefix))
    await ctx.reply("\n".join(lines))


def register(runtime: Any) -> None:
    runtime.registry.register_many(
        [
            Command(
                "privacy",
                cmd_privacy,
                category="account",
                usage="",
                description="What other people can see: last seen, photo, number, calls.",
                examples=(".privacy",),
            ),
            Command(
                "security",
                cmd_security,
                category="account",
                usage="",
                description="Whether the two-step lock is on, and its hint.",
                examples=(".security",),
            ),
        ]
    )
