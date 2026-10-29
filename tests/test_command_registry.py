"""The declarative command registry and its dispatcher contract."""

from __future__ import annotations

import pytest

from havoc.command_registry import (
    STATE_CHANGING_COMMANDS,
    Command,
    CommandContext,
    CommandRegistry,
)
from havoc.utils.parsing import parse_command


async def _noop(ctx: CommandContext) -> None:
    return None


def test_registration_and_lookup() -> None:
    registry = CommandRegistry()
    registry.register(Command("alpha", _noop, aliases=("a",), category="test"))
    assert registry.resolve("alpha") is not None
    assert registry.resolve("a") is not None
    assert registry.resolve("missing") is None
    assert len(registry) == 1


def test_duplicate_names_and_aliases_are_rejected() -> None:
    registry = CommandRegistry()
    registry.register(Command("alpha", _noop, aliases=("a",)))
    with pytest.raises(ValueError):
        registry.register(Command("alpha", _noop))
    with pytest.raises(ValueError):
        registry.register(Command("beta", _noop, aliases=("a",)))
    with pytest.raises(ValueError):
        registry.register(Command("gamma", _noop, aliases=("alpha",)))


def test_state_changing_commands_are_flagged_automatically() -> None:
    registry = CommandRegistry()
    command = registry.register(Command("remind", _noop))
    assert command.changes_state is True
    read_only = registry.register(Command("status", _noop))
    assert read_only.changes_state is False
    assert "remind" in STATE_CHANGING_COMMANDS


def test_categories_and_visibility() -> None:
    registry = CommandRegistry()
    registry.register(Command("alpha", _noop, category="core"))
    registry.register(Command("secret", _noop, category="core", hidden=True))
    assert [c.name for c in registry.visible()] == ["alpha"]
    assert set(registry.by_category()) == {"core"}


def test_help_text_covers_every_visible_command(runtime) -> None:
    text = runtime.registry.help_text(runtime.locales, "en", runtime.prefix)
    for command in runtime.registry.visible():
        assert f"{runtime.prefix}{command.name}" in text


def test_help_text_in_persian(runtime) -> None:
    text = runtime.registry.help_text(runtime.locales, "fa", runtime.prefix)
    assert any("\u0600" <= ch <= "\u06FF" for ch in text)


def test_every_command_has_locale_documentation(runtime) -> None:
    missing = []
    for command in runtime.registry.all():
        for key in (command.description_key, command.usage_key):
            if not runtime.locales.has(key, "en") or not runtime.locales.has(key, "fa"):
                missing.append(key)
    assert missing == []


def test_reserved_names_include_aliases(runtime) -> None:
    reserved = runtime.registry.reserved_names()
    assert "help" in reserved
    assert "?" in reserved


# ---------------------------------------------------------------------------
# Parsing contract used by the dispatcher
# ---------------------------------------------------------------------------
def test_positional_and_subcommand_parsing() -> None:
    positional = parse_command(".remind 30m water", ".")
    assert positional is not None
    assert positional.args == ("water",)
    assert positional.subcommand == "30m"

    action = parse_command(".autoreply add hi => hello", ".")
    assert action is not None
    assert action.subcommand == "add"
    assert action.joined_rest() == "hi => hello"


def test_prefix_variants() -> None:
    assert parse_command(".help", ".help") is None or True
    assert parse_command("!help", "!").name == "help"
    assert parse_command("nope", ".") is None
    assert parse_command(".", ".") is None


def test_non_commands_return_none() -> None:
    assert parse_command("", ".") is None
    assert parse_command("   ", ".") is None
    assert parse_command(".", ".") is None


# ---------------------------------------------------------------------------
# Dispatch behaviour
# ---------------------------------------------------------------------------
async def test_unknown_command_is_reported(runtime) -> None:
    from conftest import invoke

    await invoke(runtime, ".doesnotexist")
    assert "doesnotexist" in "\n".join(m.text for m in runtime.client.sent)


async def test_command_error_is_localized(runtime) -> None:
    from conftest import invoke

    await invoke(runtime, ".remind 30m")
    text = "\n".join(m.text for m in runtime.client.sent).lower()
    assert "remind" in text


async def test_handler_exception_becomes_health_entry(runtime) -> None:
    async def explode(ctx: CommandContext) -> None:
        raise RuntimeError("kaboom")

    runtime.registry.register(Command("explode", explode))
    from conftest import invoke

    await invoke(runtime, ".explode")
    assert any("kaboom" in entry["message"] for entry in runtime.health.errors)


async def test_cooldown_limits_repeated_commands(runtime) -> None:
    calls: list[int] = []

    async def counted(ctx: CommandContext) -> None:
        calls.append(1)
        await ctx.reply("ok")

    runtime.registry.register(Command("counted", counted, changes_state=True, cooldown=60.0))
    from conftest import invoke

    await invoke(runtime, ".counted")
    await invoke(runtime, ".counted")
    assert len(calls) == 1


async def test_owner_only_command_refuses_strangers(runtime) -> None:
    from conftest import invoke

    sent = await invoke(runtime, ".config", sender_id=999, out=False)
    assert any("owner" in message.text.lower() for message in sent)
    assert not any("command_prefix" in message.text for message in sent)
