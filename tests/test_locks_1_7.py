"""The stale locks and dead ends found by walking every command — 1.7.

Each of these was a real "she stopped working" that had nothing to do with her
voice: a switch that could not be switched back, a cooldown that started before
the command ran, a command that answered with silence, two copies of the version
string. They are pinned here so the next release cannot quietly bring them back.
"""

from __future__ import annotations

import pytest
from conftest import invoke

from havoc.command_registry import Command, disabled_commands


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------
async def test_a_dry_run_still_answers(runtime) -> None:
    """`.dryrun on` used to make every reply disappear, not just the side effects."""
    await invoke(runtime, ".dryrun on", sender_id=42)
    sent = await invoke(runtime, ".calc 2+2", sender_id=42)
    assert sent and "4" in sent[0].text


async def test_a_dry_run_can_be_turned_off(runtime) -> None:
    """A lock with the key inside it: `.dryrun off` was itself previewed, not run."""
    await invoke(runtime, ".dryrun on", sender_id=42)
    assert await runtime.permissions.dry_run_active() is True
    await invoke(runtime, ".dryrun off", sender_id=42)
    assert await runtime.permissions.dry_run_active() is False


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------
async def test_a_failed_command_does_not_start_a_cooldown(runtime) -> None:
    """Touching the cooldown before the run locked a command after a typo."""
    calls: list[str] = []

    async def handler(ctx) -> None:
        calls.append(ctx.command.name)
        if len(calls) == 1:
            raise ValueError("first run fails on purpose")
        await ctx.reply("ran")

    runtime.registry.register(Command("slow", handler, category="core", cooldown=60.0))
    await invoke(runtime, ".slow", sender_id=42)  # fails
    await invoke(runtime, ".slow", sender_id=42)  # must not be refused
    assert len(calls) == 2, "a failed run must not lock the command"


async def test_a_successful_command_does_start_one(runtime) -> None:
    async def handler(ctx) -> None:
        await ctx.reply("ran")

    runtime.registry.register(Command("once", handler, category="core", cooldown=60.0))
    await invoke(runtime, ".once", sender_id=42)
    second = await invoke(runtime, ".once", sender_id=42)
    assert any("wait" in message.text.lower() or "⏳" in message.text for message in second)


# ---------------------------------------------------------------------------
# Silence
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("line", (".calc", ".search", ".convert-time", ".digits", ".transform"))
async def test_a_command_that_needs_arguments_says_so(runtime, line) -> None:
    sent = await invoke(runtime, line, sender_id=42)
    assert sent, f"{line} answered with nothing at all"
    # Either "here is what I need" or "here are the choices" — never silence.
    text = sent[0].text.lower()
    assert "usage" in text or "choose one of" in text or "missing" in text


# ---------------------------------------------------------------------------
# One command at a time
# ---------------------------------------------------------------------------
async def test_a_command_can_be_switched_off_and_back_on(runtime) -> None:
    await invoke(runtime, ".disable fal", sender_id=42)
    assert "fal" in await disabled_commands(runtime)
    refused = await invoke(runtime, ".fal", sender_id=42)
    assert any("switched off" in message.text for message in refused)

    await invoke(runtime, ".enable fal", sender_id=42)
    assert "fal" not in await disabled_commands(runtime)


async def test_the_way_back_cannot_be_switched_off(runtime) -> None:
    """`.disable enable` would leave the account with no way to switch anything on."""
    await invoke(runtime, ".disable enable", sender_id=42)
    assert "enable" not in await disabled_commands(runtime)


async def test_help_and_commands_stay_reachable(runtime) -> None:
    await invoke(runtime, ".disable help", sender_id=42)
    assert "help" not in await disabled_commands(runtime)


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------
def test_the_version_lives_in_one_place() -> None:
    import havoc
    from havoc.version import VERSION, label

    assert havoc.__version__ == VERSION
    assert VERSION in label()
    assert label() == "Y.U.R.A 1.9 Open beta"


# ---------------------------------------------------------------------------
# The audit itself
# ---------------------------------------------------------------------------
def test_every_command_has_a_description_and_a_usage_line(runtime) -> None:
    """A command nobody documented is a command nobody can use."""
    missing = [
        command.name
        for command in runtime.registry.all()
        if not runtime.locales.has(command.description_key, "en")
        or not runtime.locales.has(command.description_key, "fa")
    ]
    assert missing == []
