"""The new tools, run the way the agent runs them.

A tool is only a tool if it reaches Telegram. These call ``build_tools`` and the
``run`` coroutine directly — no model, no prompt — so a failure here is a real
failure, not a mood.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from conftest import FakeUser

from havoc.services.actions import Turn
from havoc.services.tools import build_tools

GROUP = -1001234567890


def _turn(chat_id: int = GROUP) -> Turn:
    return Turn(chat_id=chat_id, actor_id=42, language="en", is_owner=True)


def _tools(runtime) -> dict:
    return {tool.name: tool for tool in build_tools(runtime)}


def _media(runtime) -> list:
    return [row.kwargs.get("file") for row in runtime.client.sent if "file" in row.kwargs]


async def test_the_toolbox_carries_the_new_tools(runtime) -> None:
    names = set(_tools(runtime))
    assert {"send_poll", "send_location", "contacts"} <= names


async def test_send_poll_posts_a_serialisable_poll(runtime) -> None:
    result = await _tools(runtime)["send_poll"].run(
        _turn(), {"question": "tea or coffee", "options": "tea | coffee"}
    )
    assert result.ok, result.detail
    from telethon.tl.types import InputMediaPoll

    poll = _media(runtime)[-1]
    assert isinstance(poll, InputMediaPoll)
    assert poll.poll.question.text == "tea or coffee"
    assert [answer.text.text for answer in poll.poll.answers] == ["tea", "coffee"]
    assert poll._bytes()


async def test_send_poll_with_one_option_is_refused(runtime) -> None:
    result = await _tools(runtime)["send_poll"].run(
        _turn(), {"question": "where", "options": "here"}
    )
    assert not result.ok
    assert _media(runtime) == []


async def test_send_location_sends_a_real_point(runtime) -> None:
    result = await _tools(runtime)["send_location"].run(_turn(), {"lat": "35.6892", "lon": "51.3890"})
    assert result.ok, result.detail
    from telethon.tl.types import InputMediaGeoPoint

    point = _media(runtime)[-1]
    assert isinstance(point, InputMediaGeoPoint)
    assert point.geo_point.lat == pytest.approx(35.6892)


async def test_send_location_refuses_nonsense(runtime) -> None:
    result = await _tools(runtime)["send_location"].run(_turn(), {"lat": "nowhere", "lon": "0"})
    assert not result.ok
    assert _media(runtime) == []


async def test_contacts_tool_lists_and_filters(runtime) -> None:
    runtime.client.responses["GetContactsRequest"] = SimpleNamespace(
        users=[FakeUser(501, "Sara", "Ahmadi", "sara_a"), FakeUser(502, "Reza")]
    )
    tools = _tools(runtime)
    everything = await tools["contacts"].run(_turn(), {})
    assert everything.ok
    assert "Sara" in everything.note and "Reza" in everything.note
    filtered = await tools["contacts"].run(_turn(), {"about": "sara"})
    assert "Sara" in filtered.note
    assert "Reza" not in filtered.note


async def test_contacts_tool_is_honest_when_nothing_matches(runtime) -> None:
    runtime.client.responses["GetContactsRequest"] = SimpleNamespace(users=[FakeUser(501, "Sara")])
    result = await _tools(runtime)["contacts"].run(_turn(), {"about": "zzz"})
    assert result.ok
    assert "no contact matched" in result.note.lower()
    assert "contact" in " ".join(result.lines).lower()
