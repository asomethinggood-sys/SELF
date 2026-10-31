"""Forum topics: listing, creating, closing, pinning, renaming.

Telegram only allows this in a group with forums on, and says so with an error.
These tests check both halves: that the right request is built (the peer, the
topic id, the flag) and that a refusal reaches his chat as a sentence.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from conftest import invoke

GROUP = -1001234567890


def _said(sent) -> str:
    return "\n".join(message.text for message in sent)


def _last(runtime) -> object:
    return runtime.client.raw_requests[-1]


def _topics(*rows) -> SimpleNamespace:
    return SimpleNamespace(topics=list(rows))


def _topic(topic_id, title, *, closed=False, pinned=False, unread=0) -> SimpleNamespace:
    return SimpleNamespace(id=topic_id, title=title, closed=closed, pinned=pinned, unread_count=unread)


async def test_topics_lists_titles_ids_and_unread(runtime) -> None:
    runtime.client.responses["GetForumTopicsRequest"] = _topics(
        _topic(12, "Night Shift", unread=4),
        _topic(13, "Lost and found", closed=True),
    )
    sent = await invoke(runtime, ".topics", chat_id=GROUP)
    text = _said(sent)
    assert "Night Shift" in text and "12" in text and "4" in text
    assert "closed" in text.lower()
    assert _last(runtime).__class__.__name__ == "GetForumTopicsRequest"


async def test_topics_skips_the_untitled_general_topic(runtime) -> None:
    runtime.client.responses["GetForumTopicsRequest"] = _topics(
        _topic(1, None), _topic(12, "Night Shift")
    )
    sent = await invoke(runtime, ".topics", chat_id=GROUP)
    text = _said(sent)
    assert "Night Shift" in text
    assert "· 1 ·" not in text


async def test_no_topics_says_forums_may_be_off(runtime) -> None:
    runtime.client.responses["GetForumTopicsRequest"] = _topics()
    sent = await invoke(runtime, ".topics", chat_id=GROUP)
    assert "forum" in _said(sent).lower()


class MessageActionTopicCreate:
    """Telegram's own marker on the service message that opened a topic."""


async def test_new_topic_creates_and_reports_the_id(runtime) -> None:
    runtime.client.responses["CreateForumTopicRequest"] = SimpleNamespace(
        updates=[SimpleNamespace(message=SimpleNamespace(id=41, action=MessageActionTopicCreate()))]
    )
    sent = await invoke(runtime, ".topic new Night Shift", chat_id=GROUP)
    text = _said(sent)
    assert "Night Shift" in text and "41" in text
    request = _last(runtime)
    assert request.__class__.__name__ == "CreateForumTopicRequest"
    assert request.title == "Night Shift"
    assert request.random_id != 0


@pytest.mark.parametrize(
    ("line", "field", "value"),
    [
        (".topic close 12", "closed", True),
        (".topic open 12", "closed", False),
    ],
)
async def test_closing_and_opening_set_the_flag(runtime, line, field, value) -> None:
    sent = await invoke(runtime, line, chat_id=GROUP)
    assert "12" in _said(sent)
    request = _last(runtime)
    assert type(request).__name__ == "EditForumTopicRequest"
    assert request.topic_id == 12
    assert getattr(request, field) is value


async def test_pinning_and_unpinning(runtime) -> None:
    moved = await invoke(runtime, ".topic pin 12", chat_id=GROUP)
    assert "pinned" in _said(moved).lower()
    request = _last(runtime)
    assert type(request).__name__ == "UpdatePinnedForumTopicRequest"
    assert request.pinned is True
    await invoke(runtime, ".topic unpin 12", chat_id=GROUP)
    assert _last(runtime).pinned is False


async def test_renaming_sends_the_new_title(runtime) -> None:
    sent = await invoke(runtime, ".topic rename 12 Late shift", chat_id=GROUP)
    assert "12" in _said(sent)
    request = _last(runtime)
    assert request.title == "Late shift"
    assert request.topic_id == 12


async def test_rename_without_a_title_asks_for_one(runtime) -> None:
    sent = await invoke(runtime, ".topic rename 12", chat_id=GROUP)
    assert ".topic" in _said(sent)
    assert runtime.client.raw_requests == []


@pytest.mark.parametrize("line", [".topic close", ".topic pin", ".topic close twelve"])
async def test_a_topic_id_must_be_a_number(runtime, line) -> None:
    sent = await invoke(runtime, line, chat_id=GROUP)
    assert "number" in _said(sent).lower()
    assert runtime.client.raw_requests == []


async def test_an_unknown_action_explains_the_vocabulary(runtime) -> None:
    sent = await invoke(runtime, ".topic dance 12", chat_id=GROUP)
    assert "close" in _said(sent).lower()
    assert runtime.client.raw_requests == []


async def test_a_group_without_forums_reports_telegrams_refusal(runtime) -> None:
    async def refuse(self, request):
        self.raw_requests.append(request)
        raise RuntimeError("CHANNEL_FORUM_MISSING")

    original = type(runtime.client).__call__
    type(runtime.client).__call__ = refuse
    try:
        sent = await invoke(runtime, ".topics", chat_id=GROUP)
    finally:
        type(runtime.client).__call__ = original
    assert "CHANNEL_FORUM_MISSING" in _said(sent)
