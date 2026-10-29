"""``.settings`` — a menu you can answer with a number.

Most of the bot is a command with arguments to remember. Settings are the one
place where remembering things is the wrong shape: you want to *see* what is on,
then change it. So ``.settings`` prints a numbered card, and the next message you
send — just a digit — picks a row. Each row then offers its own numbered choices.

The conversation lives in the existing ``interactive_sessions`` table (the same
one confirmations use), so it expires on its own after ten minutes and never
blocks a real command: typing ``.help`` while a menu is open still works.

Feature areas: 7 (feature switches), 96 (settings UI).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .. import ai_models, ui
from ..command_registry import Command, CommandContext
from ..utils.formatting import escape

#: How long a half-finished menu stays open before it is forgotten.
FLOW = "settings"
STEP_ROOT = "root"

BACK_WORDS = {"0", "back", "بازگشت", "قبلی"}
CLOSE_WORDS = {"x", "q", "exit", "close", "بستن", "خروج", "لغو"}


@dataclass(frozen=True)
class Choice:
    """One selectable value: what to show, and what to do when picked."""

    label_key: str
    apply: Callable[[Menu, str, int | None], Awaitable[str]]
    label: str = ""
    args: dict[str, Any] | None = None


@dataclass(frozen=True)
class Item:
    """One row of the root menu."""

    key: str
    icon: str
    title_key: str
    value: Callable[[Menu, str, int | None], Awaitable[str]]
    choices: tuple[Choice, ...] = ()
    #: Free-text rows ask for a typed value instead of offering choices.
    free_text: bool = False
    hint_key: str = ""


class Menu:
    """State machine behind ``.settings``. One instance per runtime."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.items: tuple[Item, ...] = self._build()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    async def root_card(self, language: str, chat_id: int | None = None) -> str:
        runtime = self.runtime
        rows: list[str] = []
        for index, item in enumerate(self.items, 1):
            value = await item.value(self, language, chat_id)
            rows.append(
                f"{ui.code(str(index))} {item.icon} {ui.bold(runtime.t(item.title_key, language))} "
                f"{ui.note(ui.DOT)} {escape(value)}"
            )
        return "\n\n".join(
            [
                ui.title("⚙", runtime.t("settings.title", language)),
                ui.note(runtime.t("settings.subtitle", language)),
                ui.panel(rows),
                ui.footer(runtime.t("settings.footer", language, prefix=runtime.prefix)),
            ]
        )

    async def item_card(self, item: Item, language: str, chat_id: int | None = None) -> str:
        runtime = self.runtime
        value = await item.value(self, language, chat_id)
        blocks = [
            ui.title(item.icon, runtime.t(item.title_key, language)),
            ui.note(runtime.t("settings.current", language, value=escape(value))),
        ]
        if item.free_text:
            blocks.append(ui.panel([runtime.t(item.hint_key or "settings.type_value", language)]))
        else:
            rows = [
                f"{ui.code(str(index))} {runtime.t(choice.label_key, language)}"
                + (f" {ui.note(ui.DOT)} {escape(choice.label)}" if choice.label else "")
                for index, choice in enumerate(item.choices, 1)
            ]
            blocks.append(ui.panel(rows))
        blocks.append(ui.footer(runtime.t("settings.item_footer", language)))
        return "\n\n".join(blocks)

    # ------------------------------------------------------------------
    # Flow
    # ------------------------------------------------------------------
    async def open(self, event: Any, message: Any, language: str, chat_id: int | None = None) -> None:
        """Show the root menu and remember that we are waiting for a digit."""
        user_id = getattr(event, "sender_id", None) or getattr(message, "sender_id", None)
        if user_id:
            await self.runtime.store.set_interactive(int(user_id), FLOW, STEP_ROOT, {})
        await self.runtime.sender.respond(event, await self.root_card(language, chat_id), reply=True, dry_run=False)

    async def handle(self, event: Any, message: Any, text: str, language: str, session: Any) -> bool:
        """Consume one reply from the owner. Returns True when it was ours."""
        runtime = self.runtime
        user_id = getattr(event, "sender_id", None) or getattr(message, "sender_id", None)
        chat_id = getattr(event, "chat_id", None) or getattr(message, "chat_id", None)
        normalized = text.strip().lower()
        if normalized in CLOSE_WORDS:
            if user_id:
                await runtime.store.clear_interactive(int(user_id))
            await runtime.sender.respond(event, runtime.t("settings.closed", language), reply=True, dry_run=False)
            return True

        step = session["step"] or STEP_ROOT

        if normalized in BACK_WORDS or step == STEP_ROOT:
            if normalized in BACK_WORDS:
                await self._show_root(event, language, user_id, chat_id)
                return True
            choice = self._pick(self.items, normalized)
            if choice is None:
                # Not a number we offered: leave the menu intact and let the
                # message be handled as ordinary chat.
                return False
            item = self.items[choice - 1]
            if user_id:
                await runtime.store.set_interactive(int(user_id), FLOW, item.key, {})
            await runtime.sender.respond(event, await self.item_card(item, language, chat_id), reply=True, dry_run=False)
            return True

        item = next((candidate for candidate in self.items if candidate.key == step), None)
        if item is None:
            if user_id:
                await runtime.store.clear_interactive(int(user_id))
            return False

        if item.free_text:
            message_text = await self._apply_free_text(item, text, language)
            await runtime.sender.respond(event, message_text, reply=True, dry_run=False)
            await self._show_root(event, language, user_id, chat_id)
            return True

        choice = self._pick(item.choices, normalized)
        if choice is None:
            await runtime.sender.respond(event, runtime.t("settings.bad_choice", language), reply=True, dry_run=False)
            return True
        result = await item.choices[choice - 1].apply(self, language, chat_id)
        await runtime.sender.respond(event, result, reply=True, dry_run=False)
        await self._show_root(event, language, user_id, chat_id)
        return True

    async def _show_root(
        self, event: Any, language: str, user_id: Any, chat_id: int | None = None
    ) -> None:
        if user_id:
            await self.runtime.store.set_interactive(int(user_id), FLOW, STEP_ROOT, {})
        await self.runtime.sender.respond(event, await self.root_card(language, chat_id), reply=True, dry_run=False)

    async def _apply_free_text(self, item: Item, text: str, language: str) -> str:
        runtime = self.runtime
        if item.key == "prefix":
            try:
                applied = await runtime.set_prefix(text.strip() or None)
            except ValueError as exc:
                return runtime.t("errors.invalid_prefix", language, detail=escape(str(exc)))
            return runtime.t("settings.changed", language, value=escape(applied))
        if item.key == "quiet_hours":
            return await self._set_quiet_hours(text.strip(), language)
        return runtime.t("settings.bad_choice", language)

    async def _set_quiet_hours(self, raw: str, language: str) -> str:
        runtime = self.runtime
        if raw.lower() in {"off", "none", "خاموش", "هیچ"}:
            await runtime.store.delete_setting("quiet_hours")
            return runtime.t("settings.quiet_off", language)
        parts = [part for part in raw.replace("—", "-").replace("–", "-").split("-") if part.strip()]
        if len(parts) != 2:
            return runtime.t("settings.quiet_format", language, prefix=runtime.prefix)
        start, end = (part.strip() for part in parts)
        for stamp in (start, end):
            if not _is_hhmm(stamp):
                return runtime.t("settings.quiet_format", language, prefix=runtime.prefix)
        await runtime.store.set_setting(
            "quiet_hours", {"start": start, "end": end, "timezone": runtime.config.timezone}
        )
        return runtime.t("settings.quiet_set", language, start=escape(start), end=escape(end))

    @staticmethod
    def _pick(options: tuple[Any, ...], normalized: str) -> int | None:
        if normalized.isdigit():
            number = int(normalized)
            if 1 <= number <= len(options):
                return number
        return None

    # ------------------------------------------------------------------
    # The menu itself
    # ------------------------------------------------------------------
    def _build(self) -> tuple[Item, ...]:
        return (
            Item(
                key="language",
                icon="⌖",
                title_key="settings.language",
                value=_language_value,
                choices=(
                    Choice("settings.lang_en", _set_language("en")),
                    Choice("settings.lang_fa", _set_language("fa")),
                    Choice("settings.lang_auto", _set_language("auto")),
                ),
            ),
            Item(
                key="chat_language",
                icon="❝",
                title_key="settings.chat_language",
                value=_chat_language_value,
                choices=(
                    Choice("settings.lang_auto", _set_chat_language("auto")),
                    Choice("settings.lang_en", _set_chat_language("en")),
                    Choice("settings.lang_fa", _set_chat_language("fa")),
                ),
            ),
            Item(
                key="prefix",
                icon="⌘",
                title_key="settings.prefix",
                value=_prefix_value,
                free_text=True,
                hint_key="settings.prefix_hint",
            ),
            Item(
                key="dry_run",
                icon="⚗",
                title_key="settings.dry_run",
                value=_dry_run_value,
                choices=(
                    Choice("settings.turn_on", _set_dry_run(True)),
                    Choice("settings.turn_off", _set_dry_run(False)),
                ),
            ),
            Item(
                key="quiet_hours",
                icon="☾",
                title_key="settings.quiet_hours",
                value=_quiet_value,
                free_text=True,
                hint_key="settings.quiet_hint",
            ),
            Item(
                key="ai",
                icon="◈",
                title_key="settings.ai",
                value=_ai_value,
                # Built once, from the catalogue: `auto` first, then every model
                # that is actually available on the free plan, then the two
                # housekeeping actions. `.ai model <name>` reaches the rest.
                choices=(
                    Choice("settings.ai_auto", _set_ai_model("auto")),
                    *(
                        Choice("", _set_ai_model(spec.key), label=f"{spec.icon} {spec.label}")
                        for spec in ai_models.available_models()
                    ),
                    Choice("settings.ai_test", _ai_test),
                    Choice("settings.ai_reset", _ai_reset),
                ),
            ),
            Item(
                key="persona",
                icon="♡",
                title_key="settings.persona",
                value=_feature_value("persona"),
                choices=(
                    Choice("settings.turn_on", _set_feature("persona", True)),
                    Choice("settings.turn_off", _set_feature("persona", False)),
                ),
            ),
            Item(
                key="poetry",
                icon="❦",
                title_key="settings.poetry",
                value=_feature_value("poetry"),
                choices=(
                    Choice("settings.turn_on", _set_feature("poetry", True)),
                    Choice("settings.turn_off", _set_feature("poetry", False)),
                ),
            ),
            Item(
                key="roasts",
                icon="✦",
                title_key="settings.roasts",
                value=_feature_value("roasts"),
                choices=(
                    Choice("settings.turn_on", _set_feature("roasts", True)),
                    Choice("settings.turn_off", _set_feature("roasts", False)),
                ),
            ),
        )


# ---------------------------------------------------------------------------
# Value getters
# ---------------------------------------------------------------------------
async def _language_value(menu: Menu, language: str, chat_id: int | None = None) -> str:
    runtime = menu.runtime
    stored = await runtime.store.get_language("global", "") or "auto"
    return runtime.t(f"settings.lang_{'auto' if stored == 'auto' else stored}", language)


async def _chat_language_value(menu: Menu, language: str, chat_id: int | None = None) -> str:
    runtime = menu.runtime
    stored = await runtime.store.get_language("chat", str(chat_id or 0)) or "auto"
    return runtime.t(f"settings.lang_{'auto' if stored == 'auto' else stored}", language)


async def _prefix_value(menu: Menu, language: str, chat_id: int | None = None) -> str:
    return menu.runtime.prefix


async def _dry_run_value(menu: Menu, language: str, chat_id: int | None = None) -> str:
    active = await menu.runtime.permissions.dry_run_active()
    return menu.runtime.t("settings.state_on" if active else "settings.state_off", language)


async def _quiet_value(menu: Menu, language: str, chat_id: int | None = None) -> str:
    config = await menu.runtime.permissions.quiet_hours_config(None)
    if not config:
        return menu.runtime.t("settings.state_off", language)
    return f"{escape(str(config.get('start', '—')))} → {escape(str(config.get('end', '—')))}"


async def _ai_value(menu: Menu, language: str, chat_id: int | None = None) -> str:
    """Say which model is speaking and how many keys are left — not just "on"."""
    provider = menu.runtime.ai
    selection = getattr(provider, "selection", None)
    credentials = getattr(provider, "credentials", None)
    if credentials is not None and not credentials:
        return menu.runtime.t("settings.state_off", language)
    if selection is None or selection == "auto":
        spec = None
    else:
        spec = ai_models.find(str(selection))
    label = spec.label if spec is not None else menu.runtime.t("settings.ai_auto", language)
    keys = len(credentials) if credentials is not None else 1
    count = menu.runtime.t("settings.ai_keys_n", language, count=keys)
    return f"{escape(label)} {ui.note('·')} {ui.note(escape(count))}"


def _feature_value(name: str) -> Callable[[Menu, str, int | None], Awaitable[str]]:
    async def getter(menu: Menu, language: str, chat_id: int | None = None) -> str:
        enabled = await menu.runtime.store.feature_enabled(name)
        return menu.runtime.t("settings.state_on" if enabled else "settings.state_off", language)

    return getter


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------
def _set_language(code_value: str) -> Callable[[Menu, str, int | None], Awaitable[str]]:
    async def apply(menu: Menu, language: str, chat_id: int | None = None) -> str:
        await menu.runtime.store.set_language("global", "", code_value)
        return menu.runtime.t("settings.changed", language, value=escape(code_value))

    return apply


def _set_chat_language(code_value: str) -> Callable[[Menu, str, int | None], Awaitable[str]]:
    async def apply(menu: Menu, language: str, chat_id: int | None = None) -> str:
        await menu.runtime.store.set_language("chat", str(chat_id or 0), code_value)
        return menu.runtime.t("settings.changed", language, value=escape(code_value))

    return apply


def _set_dry_run(enabled: bool) -> Callable[[Menu, str, int | None], Awaitable[str]]:
    async def apply(menu: Menu, language: str, chat_id: int | None = None) -> str:
        runtime = menu.runtime
        await runtime.permissions.set_dry_run(enabled)
        runtime.sender.dry_run = enabled
        return runtime.t(
            "settings.dry_run_changed",
            language,
            state=runtime.t("settings.state_on" if enabled else "settings.state_off", language),
        )

    return apply


def _set_ai_model(reference: str) -> Callable[[Menu, str, int | None], Awaitable[str]]:
    async def apply(menu: Menu, language: str, chat_id: int | None = None) -> str:
        runtime = menu.runtime
        select = getattr(runtime.ai, "select", None)
        if select is None:
            return runtime.t("ai.single_provider", language, model=escape(str(getattr(runtime.ai, "model", "—"))))
        chosen = await select(reference)
        if chosen is None:
            return runtime.t("settings.ai_auto_changed", language)
        return runtime.t("settings.ai_model_changed", language, model=escape(chosen.label))

    return apply


def _ai_test(menu: Menu, language: str, chat_id: int | None = None) -> Awaitable[str]:
    async def apply(_menu: Menu, language: str, chat_id: int | None = None) -> str:
        runtime = menu.runtime
        probe = getattr(runtime.ai, "probe", None)
        if probe is None:
            return runtime.t("ai.single_provider", language, model=escape(str(getattr(runtime.ai, "model", "—"))))
        worked, detail = await probe()
        if worked:
            return runtime.t(
                "ai.test_ok",
                language=language,
                detail=escape(str(detail)[:120]),
                key=escape(str(getattr(runtime.ai, "active_label", "") or "—")),
            )
        return runtime.t("ai.test_failed", language=language, detail=escape(str(detail)[:200]))

    return apply()


def _ai_reset(menu: Menu, language: str, chat_id: int | None = None) -> Awaitable[str]:
    async def apply(_menu: Menu, language: str, chat_id: int | None = None) -> str:
        runtime = menu.runtime
        reset = getattr(runtime.ai, "reset_health", None)
        if reset is None:
            return runtime.t("settings.state_off", language)
        await reset()
        return runtime.t("ai.reset_done", language)

    return apply()


def _set_feature(name: str, enabled: bool) -> Callable[[Menu, str, int | None], Awaitable[str]]:
    async def apply(menu: Menu, language: str, chat_id: int | None = None) -> str:
        runtime = menu.runtime
        try:
            await runtime.feature_manager.set_enabled(name, enabled)
        except ValueError as exc:
            return runtime.t("errors.feature_blocked", language, feature=escape(name), reason=escape(str(exc)))
        return runtime.t(
            "settings.feature_changed",
            language,
            feature=escape(f"`{name}`"),
            state=runtime.t("settings.state_on" if enabled else "settings.state_off", language),
        )

    return apply


def _is_hhmm(value: str) -> bool:
    if ":" not in value:
        return False
    hour, _, minute = value.partition(":")
    return hour.strip().isdigit() and minute.strip().isdigit() and 0 <= int(hour) <= 23 and 0 <= int(minute) <= 59


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------
async def cmd_settings(ctx: CommandContext) -> None:
    await ctx.runtime.settings_menu.open(ctx.event, ctx.message, ctx.language, ctx.chat_id)


def register(runtime: Any) -> None:
    runtime.settings_menu = Menu(runtime)
    runtime.registry.register(
        Command(
            "settings",
            cmd_settings,
            category="core",
            aliases=("menu", "prefs", "تنظیمات"),
            usage="",
            parameters=("Answer the card with a number; 0 goes back, x closes.",),
        )
    )
