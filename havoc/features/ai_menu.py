"""``.ai`` — every provider, every key, every model, on one card.

The owner asked for this the way they asked for ``.settings``: a numbered card
you answer with a digit. So the same shape is used here — one card that shows the
models with their budgets, one that shows the keys with their health, and a digit
to pick either.

Why the numbers matter
----------------------
Two providers on free plans are not interchangeable: Google gives 500 requests a
day on its Lite flashes and 14,400 on Gemma, while OpenRouter's free plan gives
50 **for the whole account**. Without the numbers on screen, "the AI is broken"
and "the AI has spent today's allowance" look identical from inside the chat.

Every mark is a typographic glyph (see :mod:`havoc.ai_models`), never a colour
emoji, and every row is one line so the card survives a narrow phone.

Commands
--------
``.ai``                    the card
``.ai model 3``            pin the third model in the list (``auto`` to un-pin)
``.ai key``                the keys, with health and today's spend
``.ai key add AQ.…``       add a Google key (or ``sk-or-v1-…`` for OpenRouter)
``.ai key rm gg-2``        forget a key added from the chat
``.ai test``               one real question through the rotation
``.ai reset``              clear cooldowns and today's counters
"""

from __future__ import annotations

from typing import Any

from .. import ai_models, ui
from ..command_registry import Command, CommandContext
from ..services.ai_router import AIRouter
from ..utils.formatting import escape
from ..utils.text import truncate

#: How many models the card lists. Twelve Google rows plus the gateway fit one
#: screen on a phone, which is the point of the card.
MAX_MODEL_ROWS = 14

#: The interactive flow `.ai` opens, so a bare digit picks a model exactly the
#: way a digit picks a row in `.settings`.
FLOW = "ai"
STEP_ROOT = "root"
BACK_WORDS = {"0", "back", "بازگشت", "قبلی"}
CLOSE_WORDS = {"x", "q", "exit", "close", "بستن", "خروج", "لغو"}
KEYS_WORD = {"k", "key", "keys", "کلید", "کلیدها"}
TEST_WORD = {"t", "test", "آزمون", "تست"}


def _router(ctx: CommandContext) -> AIRouter | None:
    provider = ctx.runtime.ai
    return provider if isinstance(provider, AIRouter) else None


def _state_mark(row: dict[str, Any]) -> str:
    """One glyph for a key: live, cooling, retired, or never used."""
    if row["disabled"]:
        return "✕"
    if row["cooling"]:
        return "◔"
    return "◉" if row["successes"] else "○"


def _spend(row: dict[str, Any]) -> str:
    used = sum(int(value or 0) for value in row["used"].values())
    return f"{used} today"


async def _model_card(ctx: CommandContext, router: AIRouter) -> str:
    """The models, grouped by provider, each with its remaining budget."""
    runtime = ctx.runtime
    selected = router.selected_spec()
    rows: list[str] = []
    index = 0
    numbered = {spec.key: position for position, spec in enumerate(listed_models(), 1)}
    for provider in ("google", "openrouter"):
        specs = [spec for spec in listed_models() if spec.provider == provider]
        if not specs:
            continue
        keys = [row for row in router.status() if row["provider"] == provider]
        budget = sum(row["budget"] for row in keys)
        rows.append(
            f"{ai_models.PROVIDER_ICONS[provider]} "
            f"{ui.bold(ai_models.PROVIDER_LABELS[provider])} "
            f"{ui.note(f'· {len(keys)} key(s) · {budget:,} left today')}"
        )
        for spec in specs[:MAX_MODEL_ROWS]:
            index = numbered[spec.key]
            used = 0
            for row in keys:
                used = max(used, int(row["used"].get(spec.api_id) or 0))
            mark = "◈" if selected is not None and selected.key == spec.key else spec.icon
            if not spec.available:
                rows.append(
                    f"{ui.code(str(index))} {mark} {ui.note(escape(spec.label))} "
                    f"{ui.note(escape(spec.note or 'unavailable'))}"
                )
                continue
            left = spec.budget(used)
            used_label = f"{left:,} left" if spec.rpd else "unmetered"
            rows.append(
                f"{ui.code(str(index))} {mark} {escape(spec.label)} "
                f"{ui.note('·')} {ui.note(escape(ai_models.format_limits(spec)))} "
                f"{ui.note(escape('·'))} {ui.note(escape(used_label))}"
            )
        rows.append("")
    if len(rows) > 1 and rows[-1] == "":
        rows.pop()

    current = router.selected_spec()
    if current is not None:
        now = ctx.t("ai.current_model", model=escape(current.label), key=escape(router.active_label or "—"))
    else:
        now = ctx.t("ai.current_auto", key=escape(router.active_label or "—"))
    return "\n\n".join(
        [
            ui.title("◈", ctx.t("ai.models_title")),
            ui.note(ctx.t("ai.models_subtitle")),
            ui.panel(rows),
            ui.note(now),
            ui.footer(ctx.t("ai.models_footer", prefix=runtime.prefix)),
        ]
    )


async def _key_card(ctx: CommandContext, router: AIRouter) -> str:
    runtime = ctx.runtime
    rows: list[str] = []
    for index, row in enumerate(router.status(), 1):
        detail = f"{row['hint']} · {_spend(row)}"
        if row["successes"]:
            detail += f" · {row['successes']} ok"
        if row["error"] and (row["disabled"] or row["cooling"]):
            detail += f" · {truncate(row['error'], 70)}"
        rows.append(
            f"{ui.code(str(index))} {_state_mark(row)} {ui.bold(escape(row['label']))} "
            f"{ui.note(ai_models.PROVIDER_ICONS.get(row['provider'], '◇'))} "
            f"{ui.note(escape(row['provider']))}\n"
            f"   {ui.note(escape(detail))}"
        )
    if not rows:
        rows.append(ctx.t("ai.no_keys"))
    return "\n\n".join(
        [
            ui.title("⚿", ctx.t("ai.keys_title")),
            ui.note(ctx.t("ai.keys_subtitle")),
            ui.panel(rows),
            ui.note(ctx.t("ai.keys_marks")),
            ui.footer(ctx.t("ai.keys_footer", prefix=runtime.prefix)),
        ]
    )


def listed_models() -> tuple[ai_models.ModelSpec, ...]:
    """The order the card prints — shared with the digit handler, deliberately."""
    rows: list[ai_models.ModelSpec] = []
    for provider in ("google", "openrouter"):
        rows.extend(spec for spec in ai_models.catalogue() if spec.provider == provider)
    return tuple(rows)


def _number_to_spec(number: int) -> ai_models.ModelSpec | None:
    """The same order the card printed, so ``.ai model 3`` means row 3."""
    rows = listed_models()
    if 1 <= number <= len(rows):
        return rows[number - 1]
    return None


async def _model_command(ctx: CommandContext, reference: str, router: AIRouter) -> str:
    if not reference:
        return await _model_card(ctx, router)
    if reference.isdigit():
        spec = _number_to_spec(int(reference))
        if spec is None:
            return ctx.t("ai.model_unknown", reference=escape(reference))
        reference = spec.key
    if reference.strip().lower() == "auto":
        await router.select("auto")
        return ctx.t("ai.model_auto")
    known = ai_models.find(reference)
    if known is None:
        return ctx.t("ai.model_unknown", reference=escape(reference))
    if not known.available:
        # Listed, and honest about why it cannot speak.
        return ctx.t("ai.model_unavailable", model=escape(known.label), note=escape(known.note or "—"))
    chosen = await router.select(reference)
    if chosen is None:
        return ctx.t("ai.model_unknown", reference=escape(reference))
    return ctx.t(
        "ai.model_set",
        model=escape(chosen.label),
        limits=escape(ai_models.format_limits(chosen)),
    )


async def _key_command(ctx: CommandContext, words: list[str], router: AIRouter) -> str:
    runtime = ctx.runtime
    action = words[0].lower() if words else ""
    if action in {"add", "set", "new"} and len(words) >= 2:
        value = " ".join(words[1:]).strip()
        credential = await router.add_key("auto", value)
        try:
            await runtime.sender.delete(ctx.chat_id, ctx.message.id)
        except Exception as exc:  # noqa: BLE001 - a failed delete must not lose the key
            runtime.logger.warning("ai: could not delete the key message: %s", exc)
        worked, detail = await router.probe()
        note = (
            ctx.t("ai.key_verified", detail=escape(truncate(detail, 90)))
            if worked
            else ctx.t("ai.key_failed", detail=escape(truncate(detail, 140)))
        )
        return "\n\n".join(
            [
                ctx.t(
                    "ai.key_added",
                    label=escape(credential.label),
                    provider=escape(ai_models.PROVIDER_LABELS.get(credential.provider, credential.provider)),
                ),
                ui.note(note),
            ]
        )
    if action in {"rm", "remove", "del", "delete"} and len(words) >= 2:
        removed = await router.retire(words[1])
        return ctx.t("ai.key_removed", label=escape(words[1])) if removed else ctx.t(
            "ai.key_not_found", label=escape(words[1])
        )
    return await _key_card(ctx, router)


async def cmd_ai(ctx: CommandContext) -> None:
    """``.ai`` — the AI card, its model picker and its key ring."""
    router = _router(ctx)
    if router is None:
        # A single-provider setup still deserves an answer, not an error.
        provider = ctx.runtime.ai
        await ctx.reply(
            "\n\n".join(
                [
                    ui.title("◈", ctx.t("ai.models_title")),
                    ui.note(ctx.t("ai.single_provider", model=escape(str(getattr(provider, "model", "—"))))),
                    ui.footer(ctx.t("ai.models_footer", prefix=ctx.runtime.prefix)),
                ]
            )
        )
        return

    # The parser hands back the first word after the command as `ctx.sub` and the
    # remainder as `ctx.args`; glue them back so `.ai model gemma` and
    # `.ai model gemma,` mean the same thing.
    words = [word for word in ((ctx.sub, *ctx.args)) if word and word.strip()]
    action = words[0].lower() if words else ""
    rest = words[1:]

    if action in {"model", "models", "pick", "use"}:
        await ctx.reply(await _model_command(ctx, " ".join(rest).strip(), router))
        return
    if action in {"key", "keys", "token", "tokens"}:
        await ctx.reply(await _key_command(ctx, rest, router))
        return
    if action in {"test", "probe", "check"}:
        worked, detail = await router.probe()
        lines = [ui.title("⌁", ctx.t("ai.test_title"))]
        if worked:
            lines.append(ctx.t("ai.test_ok", detail=escape(truncate(detail, 120)), key=escape(router.active_label or "—")))
        else:
            lines.append(ctx.t("ai.test_failed", detail=escape(truncate(detail, 200))))
        for attempt in router.last_attempts:
            marker = "◉" if attempt.ok else "◌"
            lines.append(
                ui.note(
                    escape(
                        f"{marker} {attempt.label} · {attempt.model}"
                        + (f" · {truncate(attempt.detail, 90)}" if attempt.detail else "")
                    )
                )
            )
        await ctx.reply("\n\n".join(lines))
        return
    if action in {"reset", "clear"}:
        await router.reset_health()
        await ctx.reply(ctx.t("ai.reset_done"))
        return
    if action in {"on", "off"}:
        await ctx.reply(ui.note(ctx.t("ai.no_switch", prefix=ctx.runtime.prefix)))
        return

    # A bare `.ai` opens the flow: the card, and the next digit picks a model.
    menu = getattr(ctx.runtime, "ai_menu", None)
    if menu is not None:
        await menu.open(ctx.event, ctx.message, ctx.language, ctx.chat_id)
        return
    await ctx.reply(await _model_card(ctx, router))




class AIMenu:
    """The state machine behind a bare ``.ai``: digits pick, keys are one letter.

    Telegram has no buttons here, so the card is answered the way ``.settings``
    is answered — with a number. Keeping the numbering in one function
    (:func:`listed_models`) is what makes "row 3" mean the same model on the card
    and in the handler.
    """

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    async def open(self, event: Any, message: Any, language: str, chat_id: int | None = None) -> None:
        router = self.runtime.ai
        if isinstance(router, AIRouter):
            card = await _model_card(_Context(self.runtime, language), router)
        else:
            card = self.runtime.t(
                "ai.single_provider",
                language,
                model=escape(str(getattr(router, "model", "—"))),
            )
        await self._remember(event, message)
        await self.runtime.sender.respond(event, card, reply=True, dry_run=False)

    async def _remember(self, event: Any, message: Any, step: str = STEP_ROOT) -> None:
        user_id = getattr(event, "sender_id", None) or getattr(message, "sender_id", None)
        if user_id:
            await self.runtime.store.set_interactive(int(user_id), FLOW, step, {})

    async def handle(self, event: Any, message: Any, text: str, language: str, session: Any) -> bool:
        runtime = self.runtime
        normalized = text.strip().lower()
        user_id = getattr(event, "sender_id", None) or getattr(message, "sender_id", None)
        router = runtime.ai

        if normalized in CLOSE_WORDS:
            if user_id:
                await runtime.store.clear_interactive(int(user_id))
            await runtime.sender.respond(event, runtime.t("settings.closed", language), reply=True, dry_run=False)
            return True
        if not isinstance(router, AIRouter):
            if user_id:
                await runtime.store.clear_interactive(int(user_id))
            return False
        context = _Context(runtime, language)

        if normalized in BACK_WORDS:
            await self._remember(event, message)
            await runtime.sender.respond(
                event, await _model_card(context, router), reply=True, dry_run=False
            )
            return True
        if normalized in KEYS_WORD:
            await self._remember(event, message)
            await runtime.sender.respond(
                event, await _key_card(context, router), reply=True, dry_run=False
            )
            return True
        if normalized in TEST_WORD:
            await self._remember(event, message)
            worked, detail = await router.probe()
            key = router.active_label or "—"
            body = (
                runtime.t("ai.test_ok", language, detail=escape(truncate(detail, 120)), key=escape(key))
                if worked
                else runtime.t("ai.test_failed", language, detail=escape(truncate(detail, 200)))
            )
            await runtime.sender.respond(
                event, ui.title("⌁", runtime.t("ai.test_title", language)) + "\n\n" + body, reply=True, dry_run=False
            )
            return True
        if normalized.isdigit():
            chosen = _number_to_spec(int(normalized))
            if chosen is None:
                await runtime.sender.respond(
                    event,
                    runtime.t("ai.model_unknown", language, reference=escape(text.strip())),
                    reply=True,
                    dry_run=False,
                )
                return True
            if not chosen.available:
                await runtime.sender.respond(
                    event,
                    runtime.t("ai.model_unavailable", language, model=escape(chosen.label), note=escape(chosen.note or "—")),
                    reply=True,
                    dry_run=False,
                )
                await self._remember(event, message)
                return True
            await router.select(chosen.key)
            await runtime.sender.respond(
                event,
                runtime.t(
                    "ai.model_set",
                    language,
                    model=escape(chosen.label),
                    limits=escape(ai_models.format_limits(chosen)),
                ),
                reply=True,
                dry_run=False,
            )
            await self._remember(event, message)
            return True
        # Anything else is ordinary chat: close quietly and let the router route it.
        if user_id:
            await runtime.store.clear_interactive(int(user_id))
        return False


class _Context:
    """The two fields the card renderers need, for the flow and for a command."""

    def __init__(self, runtime: Any, language: str) -> None:
        self.runtime = runtime
        self.language = language

    def t(self, message_key: str, **kwargs: Any) -> str:
        """`runtime.t(key, language, …)` with the language already filled in."""
        kwargs.pop("language", None)
        return self.runtime.t(message_key, self.language, **kwargs)


def register(runtime: Any) -> None:
    runtime.ai_menu = AIMenu(runtime)
    runtime.registry.register(
        Command(
            "ai",
            cmd_ai,
            category="ai",
            aliases=("models", "model", "keys"),
            usage="[model <n|name|auto> | key [add <key>|rm <label>] | test | reset]",
            subcommands=("model", "models", "key", "keys", "test", "probe", "reset", "clear", "use", "pick"),
        )
    )
