"""Banter: ``.roast`` — the reply-only, non-cringe roaster.

A roast is always *about a message*. Without one there is no target, and the
command says so instead of inventing a person to insult. The tone is gen-z but
specific: every line mocks the take, never the body, identity, family or
anything private. That rule is written into the templates and repeated in the
optional AI prompt, so enabling AI cannot quietly make the bot mean.

Feature areas: 27 (offline content), 67 (fight-mode safety), 68.
"""

from __future__ import annotations

from typing import Any

from .. import content, ui
from ..command_registry import Command, CommandContext, CommandError
from ..language_detection import detect_language
from ..utils.formatting import escape
from ..utils.text import truncate

_SPICE_ALIASES = {
    "mild": "mild",
    "low": "mild",
    "آرام": "mild",
    "medium": "medium",
    "mid": "medium",
    "معمولی": "medium",
    "spicy": "spicy",
    "high": "spicy",
    "تند": "spicy",
}


def _source_text(ctx: CommandContext, reply: Any) -> tuple[str, str]:
    """Return ``(text, kind)`` for whatever this roast is aimed at."""
    if reply is not None:
        text = getattr(reply, "raw_text", None) or getattr(reply, "message", None) or ""
        if str(text).strip():
            return str(text), "reply"
    typed = " ".join(ctx.args).strip()
    if typed:
        return typed, "text"
    raise CommandError("roast.no_target", prefix=ctx.runtime.prefix)


async def cmd_roast(ctx: CommandContext) -> None:
    runtime = ctx.runtime
    reply = await ctx.reply_message()
    source, kind = _source_text(ctx, reply)

    if content.contains_harassment(source):
        raise CommandError("fight.unsafe_target")

    language = (
        ctx.flag("lang")
        if ctx.flag("lang") in {"en", "fa"}
        else detect_language(source).resolve(ctx.language)
    )
    # Flags in this bot are booleans; a *value* rides as the first word.
    spice = _SPICE_ALIASES.get((ctx.sub or "").lower())
    if spice is None:
        spice = next((name for name in ("spicy", "mild") if ctx.has_flag(name)), "medium")

    hook = content.extract_hook(source, language)
    line: str | None = None

    if runtime.ai.available and ctx.has_flag("ai"):
        from ..services.ai_provider import AIError

        try:
            answer = await runtime.ai.complete(
                ctx.t(
                    "roast.ai_prompt",
                    message=truncate(" ".join(source.split()), 400),
                    spice=spice,
                ),
                system=content.ROAST_SYSTEM_PROMPT.format(
                    language_name=ctx.t("fight.language_name", default=language)
                ),
                max_tokens=90,
                temperature=0.95,
            )
            candidate = answer.strip().strip('"“”').strip()
            if candidate and not content.contains_harassment(candidate) and len(candidate) <= 240:
                line = candidate
        except AIError as exc:
            runtime.logger.warning("roast: AI failed (%s), using offline lines", exc.kind)

    offline = line is None
    if line is None:
        line = content.random_roast(language, hook)

    blocks = [ui.quote_card(line, "")]
    meta = [f"◎ {ui.note(escape(truncate(' '.join(source.split()), 70)))}"] if kind == "reply" else []
    meta.append(
        f"{ui.bold(ctx.t('roast.spice'))} {ui.code(spice)} {ui.note(ui.DOT)} "
        f"{ui.bold(ctx.t('roast.mode'))} {ui.code(ctx.t('roast.offline') if offline else 'ai')}"
    )
    blocks.append(ui.panel(meta))
    await ctx.reply("\n\n".join(blocks))


def register(runtime: Any) -> None:
    runtime.registry.register(
        Command(
            "roast",
            cmd_roast,
            category="fun",
            feature="roasts",
            requires_feature=True,
            usage="[mild|medium|spicy] [text] [--ai]",
            subcommands=("mild", "medium", "spicy", "آرام", "معمولی", "تند"),
            parameters=(
                "mild | medium | spicy — how much bite, default medium",
                "text — what to roast when not replying to a message",
                "--ai — let the model write the line (needs an AI key)",
            ),
            aliases=("burn",),
        )
    )
