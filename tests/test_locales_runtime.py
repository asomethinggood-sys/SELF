"""Every command must behave in every language, even when called badly.

These tests are intentionally shallow-but-broad: they run *every* registered
command in English and Persian with no arguments and with nonsense arguments.
That catches missing translation keys, translations whose placeholders do not
match their call sites, and unhandled exceptions from sloppy input.
"""

from __future__ import annotations

import logging

import pytest
from conftest import invoke

# Arguments chosen to exercise empty input and the "unknown subcommand" path.
ARGSETS = ("", "bogus", "bogus bogus bogus bogus")


class _Collector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - trivial
        self.messages.append(record.getMessage())


@pytest.fixture()
def logs(runtime):
    collector = _Collector()
    runtime.logger.addHandler(collector)
    runtime.logger.setLevel(logging.DEBUG)
    try:
        yield collector
    finally:
        runtime.logger.removeHandler(collector)


async def reset_global_state(runtime) -> None:
    """Undo the side effects that would gate every later command.

    ``.stop`` arms the emergency stop and ``.dryrun`` switches global preview
    mode; either one would make the rest of a sweep a no-op, which is exactly
    how a weak version of this test used to pass while missing real bugs.
    """
    await runtime.store.set_emergency_stop(False)
    await runtime.store.set_setting("dry_run", "off")


async def sweep(runtime, language: str, args: str) -> list[str]:
    """Run every command; return the names that produced no output at all."""
    silent = []
    for command in runtime.registry.all():
        text = f".{command.name} {args}".strip()
        # CommandError and friends are rendered to the user by the dispatcher;
        # anything escaping here is a bug.
        messages = await invoke(runtime, text, language=language)
        if not messages:
            silent.append(command.name)
        await reset_global_state(runtime)
    return silent


@pytest.mark.parametrize("language", ["en", "fa"])
@pytest.mark.parametrize("args", ARGSETS)
async def test_every_command_survives_bad_input(runtime, language, args):
    await sweep(runtime, language, args)


async def test_sweep_is_not_a_no_op(runtime):
    """Guard: a global gate left armed would make every sweep result meaningless."""
    silent = await sweep(runtime, "en", "")
    assert silent == [], f"commands produced no output: {silent}"


@pytest.mark.parametrize("language", ["en", "fa"])
async def test_sweep_has_no_localization_failures(runtime, logs, language):
    silent = await sweep(runtime, language, "")
    assert silent == [], f"{language}: commands produced no output: {silent}"

    failures = [
        entry for entry in runtime.health.errors if entry.get("source") == "localization"
    ]
    assert failures == [], f"{language}: {failures}"

    # A missing key is logged rather than raised, so a bare key can leak to the
    # user. The catalog must actually contain every key the sweep asked for.
    missing = [message for message in logs.messages if "missing translation key" in message]
    assert missing == [], f"{language}: {missing}"


async def test_persian_help_is_persian(runtime):
    messages = await invoke(runtime, ".help", language="fa")
    rendered = "\n".join(message.text for message in messages)
    assert any("\u0600" <= char <= "\u06ff" for char in rendered), rendered[:200]
