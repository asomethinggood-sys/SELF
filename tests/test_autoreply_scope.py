"""An auto reply knows *where* it may speak.

"Reply to anyone who DMs me" is the request. Before 1.7 the only scopes were one
chat or every chat, so a rule written for his inbox also answered in every group he
is in — which is the opposite of discreet. The scope column makes the sentence mean
what it says, and these tests run a real rule through the real engine.
"""

from __future__ import annotations

from conftest import invoke

from havoc.event_engine import EventType, NormalizedEvent
from havoc.services.self_console import run as console


async def _rule(runtime, line: str = "autoreply add --type any --private => he's not here"):
    result = await console(
        runtime, line, chat_id=100, sender_id=7542695649, language="en", is_private=True
    )
    assert result.ok, result.note
    await invoke(runtime, ".autoreply on", sender_id=7542695649)
    return (await runtime.store.list_autoreplies())[0]


async def test_a_private_rule_answers_a_dm(runtime) -> None:
    row = await _rule(runtime)
    assert row["scope"] == "private" and row["match_type"] == "any"
    event = NormalizedEvent(
        type=EventType.MESSAGE,
        chat_id=555,
        message_id=7,
        sender_id=999,
        text="hello?",
        is_private=True,
        is_group=False,
    )
    await runtime.automation._autoreply(event)
    sent = [m for m in runtime.client.sent if m.chat_id == 555]
    assert sent and "not here" in sent[-1].text


async def test_the_same_rule_says_nothing_in_a_group(runtime) -> None:
    await _rule(runtime)
    event = NormalizedEvent(
        type=EventType.MESSAGE,
        chat_id=777,
        message_id=8,
        sender_id=123,
        text="hello?",
        is_private=False,
        is_group=True,
    )
    await runtime.automation._autoreply(event)
    assert not [m for m in runtime.client.sent if m.chat_id == 777]


async def test_a_group_rule_says_nothing_in_a_dm(runtime) -> None:
    await _rule(runtime, "autoreply add --type any --group => only in groups")
    event = NormalizedEvent(
        type=EventType.MESSAGE,
        chat_id=556,
        message_id=9,
        sender_id=999,
        text="hello?",
        is_private=True,
        is_group=False,
    )
    await runtime.automation._autoreply(event)
    assert not [m for m in runtime.client.sent if m.chat_id == 556]


async def test_no_pattern_is_needed_when_the_scope_is_the_trigger(runtime) -> None:
    row = await _rule(runtime)
    assert row["pattern"] == "*"
    assert row["response"] == "he's not here"


async def test_a_new_standing_instruction_replaces_the_old_one(runtime) -> None:
    """"If anyone messages me say X" then "…say Y" is a correction, not two rules
    that both fire and race."""
    await _rule(runtime, "autoreply add --type any --private => first answer")
    await _rule(runtime, "autoreply add --type any --private => second answer")
    rows = await runtime.store.list_autoreplies()
    enabled = [row for row in rows if row["enabled"]]
    assert len(enabled) == 1
    assert enabled[0]["response"] == "second answer"
    assert len(rows) == 2, "the superseded rule should be kept, disabled, for the record"


async def test_adding_the_same_rule_twice_changes_nothing(runtime) -> None:
    await _rule(runtime, "autoreply add --type any --private => same words")
    result = await console(
        runtime,
        "autoreply add --type any --private => same words",
        chat_id=100,
        sender_id=7542695649,
        language="en",
        is_private=True,
    )
    assert result.ok
    rows = await runtime.store.list_autoreplies()
    assert len(rows) == 1
