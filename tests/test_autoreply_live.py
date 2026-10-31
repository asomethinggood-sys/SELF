"""\"Reply to every DM I get for four hours\" — run to the wire.

The plan was already readable; the question is whether an incoming private message
is actually answered, once, in the right room, and whether the switch is off again
when the four hours are up. The harness is the real dispatcher and the real
automation subscriber, with the fake Telegram client.
"""

from __future__ import annotations

import pytest
from conftest import FakeMessage, incoming, invoke

from havoc.services import intent as intent_module
from havoc.services.plans import build_plan

OWNER = 42
STRANGER = 999
ROOM = 555


@pytest.fixture
async def wired(runtime):
    await runtime.store.set_feature("automation", True)
    await runtime.feature_manager.refresh()
    return runtime


async def _turn_it_on(runtime) -> None:
    found = intent_module.detect("reply to every direct message i get with he is busy lil bro")
    assert found is not None
    for line in found.lines:
        await invoke(runtime, f".{line}")


async def test_an_incoming_dm_gets_exactly_one_answer(wired) -> None:
    await _turn_it_on(wired)
    before = len(wired.client.sent)
    await incoming(
        wired,
        FakeMessage(id=901, chat_id=ROOM, sender_id=STRANGER, text="hey, you around?", is_private=True),
    )
    answers = [row for row in wired.client.sent[before:] if "busy" in row.text]
    assert len(answers) == 1, [row.text for row in wired.client.sent[before:]]
    assert "he is busy lil bro" in answers[0].text
    assert int(answers[0].chat_id) == ROOM


async def test_a_group_message_is_not_auto_answered(wired) -> None:
    await _turn_it_on(wired)
    before = len(wired.client.sent)
    await incoming(
        wired,
        FakeMessage(id=902, chat_id=-1001234567890, sender_id=STRANGER, text="hey all", is_group=True),
    )
    assert not [row for row in wired.client.sent[before:] if "busy" in row.text]


async def test_the_same_person_is_not_answered_twice_in_a_row(wired) -> None:
    await _turn_it_on(wired)
    before = len(wired.client.sent)
    for message_id in (903, 904):
        await incoming(
            wired,
            FakeMessage(id=message_id, chat_id=ROOM, sender_id=STRANGER, text="hello?", is_private=True),
        )
    answers = [row for row in wired.client.sent[before:] if "busy" in row.text]
    assert len(answers) == 1, "the cooldown is what keeps this from being spam"


async def test_it_switches_itself_off_after_the_four_hours(wired) -> None:
    plan = build_plan(
        "reply to every direct message i get with he is busy lil bro for 4 hours",
        intent=intent_module,
    )
    created = await wired.plans.create(plan, said="busy for four hours", owner_id=OWNER)
    assert created["steps"] == 3
    rows = await wired.db.fetch_all("SELECT action, at FROM plan_steps ORDER BY at")
    actions = [row["action"] for row in rows]
    assert actions[0].startswith("autoreply add")
    assert actions[-1] == "autoreply off"
