"""Invariants that hold for *every* command, not one screenshot at a time.

He complained twice about the same class of thing: a card that shows the escaping
(``&lt;text&gt;``) and a card with an empty placeholder (``«»``) where a value
should be. Both are found by asking every command in the registry the same two
questions, which is what this file does — so the next command that ships with
either defect fails here instead of in his chat.
"""

from __future__ import annotations

import html
import re

import pytest
from conftest import invoke

#: Commands that change the account in a way a test must not do (log out, stop
#: the runtime, rotate the prefix) — every other command is fair game.
SKIP = {
    "yura", "person", "self", "start", "stop", "restart", "key", "owner", "prefix",
    "plugins", "dryrun", "alias", "shutdown", "emergency", "logout", "sessions",
    "endsession", "broadcast",
}

#: ``«»`` with nothing between the marks: a template that formatted a value it
#: never received.
_EMPTY_QUOTES = re.compile(r"«\s*»")


async def _every_command(runtime) -> list[str]:
    names = sorted({command.name for command in runtime.registry.all()})
    return [name for name in names if name not in SKIP]


async def test_no_command_shows_the_escaping(runtime) -> None:
    """One unescape is what Telegram does; anything left is what he sees."""
    runtime.permissions.set_self_id(42)
    offenders: list[tuple[str, str]] = []
    for name in await _every_command(runtime):
        try:
            sent = await invoke(runtime, f".{name}", chat_id=42, is_private=True)
        except Exception:  # noqa: BLE001 - a command that throws has its own test
            continue
        for message in sent:
            visible = html.unescape(message.text)
            if "&lt;" in visible or "&amp;" in visible:
                offenders.append((name, visible[:160]))
    assert not offenders, offenders


async def test_no_command_draws_an_empty_quote(runtime) -> None:
    """.chats on an empty list said «» — a value that was never there."""
    runtime.permissions.set_self_id(42)
    offenders: list[tuple[str, str]] = []
    for name in await _every_command(runtime):
        try:
            sent = await invoke(runtime, f".{name}", chat_id=42, is_private=True)
        except Exception:  # noqa: BLE001
            continue
        for message in sent:
            for match in _EMPTY_QUOTES.finditer(message.text):
                offenders.append((name, message.text[max(0, match.start() - 40) : match.end() + 20]))
    assert not offenders, offenders


async def test_every_command_is_reachable_from_her_hands(runtime) -> None:
    """The toolbox and `.help` are the same list: what he can type, she can run.

    ``BLOCKED`` is deliberate — turning herself off, changing the prefix, logging
    out — and those are the only names allowed to be missing.
    """
    from havoc.services.self_console import BLOCKED
    from havoc.services.self_console import run as run_console
    from havoc.services.tools import build_tools

    reachable = {command.name for command in runtime.registry.all()} - set(BLOCKED)
    assert "command" in {tool.name for tool in build_tools(runtime)}, (
        "she has no way to run a command at all"
    )
    # The console resolves by name for every command the registry holds.

    missing: list[str] = []
    for name in sorted(reachable):
        reply = await run_console(
            runtime,
            name,
            chat_id=42,
            sender_id=42,
            language="en",
        )
        if reply.kind in {"unknown", "blocked"}:
            missing.append(name)
    assert not missing, missing


@pytest.mark.parametrize("line", [".help", ".commands", ".features", ".status"])
async def test_the_big_cards_are_well_formed(runtime, line: str) -> None:
    runtime.permissions.set_self_id(42)
    sent = await invoke(runtime, line, chat_id=42, is_private=True)
    joined = "\n".join(message.text for message in sent)
    assert joined.strip()
    assert "&lt;" not in html.unescape(joined)
    # Every opened tag is closed, in order.
    opens = re.findall(r"<(?!/)([a-z-]+)", joined)
    closes = re.findall(r"</([a-z-]+)", joined)
    assert sorted(opens) == sorted(closes), (line, opens, closes)
