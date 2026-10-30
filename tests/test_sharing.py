"""Polls and places — the two shapes that are not a sentence or a file.

A poll is checked by looking at the object that reached the client: a text
imitation would pass a "did she say poll?" test and fail a real one, so the test
inspects the ``InputMediaPoll`` itself. A location is checked the same way, and
an out-of-range point must never be sent at all.
"""

from __future__ import annotations

import pytest
from conftest import invoke

GROUP = -1001234567890


def _said(sent) -> str:
    return "\n".join(message.text for message in sent)


def _media_requests(runtime) -> list:
    """Everything that reached ``send_file``, i.e. the media that really went out."""
    return [row.kwargs.get("file") for row in runtime.client.sent if "file" in row.kwargs]


async def test_poll_posts_a_real_poll(runtime) -> None:
    sent = await invoke(runtime, ".poll tea or coffee | tea | coffee", chat_id=GROUP)
    assert "poll" in _said(sent).lower()
    media = _media_requests(runtime)
    assert media, "nothing reached send_file"
    from telethon.tl.types import InputMediaPoll

    poll = media[-1]
    assert isinstance(poll, InputMediaPoll)
    assert poll.poll.question.text == "tea or coffee"
    assert [answer.text.text for answer in poll.poll.answers] == ["tea", "coffee"]
    # The same serialisation the client performs: a poll that cannot be written
    # to bytes would fail on the wire, not in a test.
    assert poll._bytes()


async def test_poll_without_options_is_refused(runtime) -> None:
    sent = await invoke(runtime, ".poll tea or coffee", chat_id=GROUP)
    assert "|" in _said(sent)
    assert _media_requests(runtime) == []


async def test_poll_with_one_option_is_refused(runtime) -> None:
    sent = await invoke(runtime, ".poll where | here", chat_id=GROUP)
    assert "|" in _said(sent)
    assert _media_requests(runtime) == []


async def test_poll_caps_at_ten_options(runtime) -> None:
    body = "pick | " + " | ".join(f"option {index}" for index in range(14))
    await invoke(runtime, f".poll {body}", chat_id=GROUP)
    media = _media_requests(runtime)
    assert len(media[-1].poll.answers) == 10


async def test_poll_question_is_counted_in_the_answer(runtime) -> None:
    sent = await invoke(runtime, ".poll dinner | pasta | rice | bread", chat_id=GROUP)
    assert "3" in _said(sent)
    assert "dinner" in _said(sent)


async def test_poll_is_recorded_in_the_outbox(runtime) -> None:
    await invoke(runtime, ".poll one | a | b", chat_id=GROUP)
    rows = await runtime.store.outbox(limit=5)
    assert any(row["kind"] == "poll" for row in rows)


async def test_geo_sends_a_point(runtime) -> None:
    sent = await invoke(runtime, ".geo 35.6892, 51.3890", chat_id=GROUP)
    assert "location" in _said(sent).lower() or "35.689" in _said(sent)
    from telethon.tl.types import InputMediaGeoPoint

    point = _media_requests(runtime)[-1]
    assert isinstance(point, InputMediaGeoPoint)
    assert point.geo_point.lat == pytest.approx(35.6892)
    assert point.geo_point.long == pytest.approx(51.3890)
    assert point._bytes()


async def test_geo_accepts_a_space_instead_of_a_comma(runtime) -> None:
    await invoke(runtime, ".geo 59.91 10.75", chat_id=GROUP)
    point = _media_requests(runtime)[-1]
    assert point.geo_point.lat == pytest.approx(59.91)


@pytest.mark.parametrize("bad", ["91, 0", "0, 181", "-90.5, 10", "nowhere"])
async def test_geo_refuses_a_point_that_is_not_on_earth(runtime, bad) -> None:
    await invoke(runtime, f".geo {bad}", chat_id=GROUP)
    assert _media_requests(runtime) == [], "an impossible point was still sent"


async def test_geo_without_coordinates_asks_for_them(runtime) -> None:
    sent = await invoke(runtime, ".geo", chat_id=GROUP)
    assert ".geo" in _said(sent)
    assert _media_requests(runtime) == []


# ---------------------------------------------------------------------------
# .pic still owns file sending, and still answers to .sendfile
# ---------------------------------------------------------------------------
async def test_sendfile_is_still_an_alias_of_pic(runtime) -> None:
    command = runtime.registry.resolve("sendfile")
    assert command is not None and command.name == "pic"


async def test_photo_belongs_to_her_face_not_to_file_sending(runtime) -> None:
    command = runtime.registry.resolve("photo")
    assert command is not None and command.name == "photo"
