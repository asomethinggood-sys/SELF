"""``.exportchat`` — a conversation written to a file and sent back.

The file is the deliverable, so the tests read it: oldest line first, names and
timestamps present, media reduced to a label, and nothing fetched from Telegram
except the messages themselves.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from conftest import FakeMessage, FakeUser, invoke

GROUP = -1001234567890


def _said(sent) -> str:
    return "\n".join(message.text for message in sent)


def _history(runtime) -> list[FakeMessage]:
    """Three messages, newest first — the order Telegram returns them in."""
    return [
        FakeMessage(id=31, text="third", sender=FakeUser(880, "Sara"), date=datetime(2026, 5, 2, 9, 0, tzinfo=UTC)),
        FakeMessage(id=30, text="second", sender=FakeUser(42, "Owner"), date=datetime(2026, 5, 1, 9, 0, tzinfo=UTC)),
        FakeMessage(id=29, text="first", sender=FakeUser(880, "Sara"), date=datetime(2026, 5, 1, 8, 0, tzinfo=UTC)),
    ]


def _file(runtime) -> str:
    rows = [row for row in runtime.client.sent if "file" in row.kwargs]
    assert rows, "no file was sent"
    return str(rows[-1].kwargs["file"])


async def test_exportchat_writes_a_transcript_oldest_first(runtime) -> None:
    runtime.client.history = _history(runtime)
    sent = await invoke(runtime, ".exportchat 880 10")
    assert "transcript" in _said(sent).lower()
    path = _file(runtime)
    text = open(path, encoding="utf-8").read()
    assert text.index("first") < text.index("second") < text.index("third")
    assert "Sara" in text and "2026-05-01" in text


async def test_exportchat_marks_media_instead_of_downloading_it(runtime) -> None:
    from conftest import FakeDocument

    runtime.client.history = [
        FakeMessage(id=40, text="", sender=FakeUser(880, "Sara"), media=FakeDocument(file_name="id.png"))
    ]
    await invoke(runtime, ".exportchat 880")
    text = open(_file(runtime), encoding="utf-8").read()
    assert "[photo]" in text or "[document]" in text or "[file]" in text


async def test_exportchat_honours_the_count(runtime) -> None:
    runtime.client.history = _history(runtime)
    await invoke(runtime, ".exportchat 880 2")
    text = open(_file(runtime), encoding="utf-8").read()
    assert "first" not in text
    assert "second" in text and "third" in text


async def test_exportchat_sends_the_file_to_the_chat_he_asked_from(runtime) -> None:
    runtime.client.history = _history(runtime)
    await invoke(runtime, ".exportchat 880", chat_id=GROUP)
    files = [row for row in runtime.client.sent if "file" in row.kwargs]
    assert files[-1].chat_id == GROUP


async def test_exportchat_with_no_messages_says_so(runtime) -> None:
    runtime.client.history = []
    sent = await invoke(runtime, ".exportchat 880")
    assert "nothing to export" in _said(sent).lower()
    assert not any("file" in row.kwargs for row in runtime.client.sent)


async def test_exportchat_needs_a_chat(runtime) -> None:
    sent = await invoke(runtime, ".exportchat")
    assert ".exportchat" in _said(sent)
    assert runtime.client.raw_requests == []


@pytest.mark.parametrize("count", ["0", "99999", "lots"])
async def test_a_silly_count_is_clamped_not_fatal(runtime, count) -> None:
    runtime.client.history = _history(runtime)
    sent = await invoke(runtime, f".exportchat 880 {count}")
    assert _said(sent).strip()
    assert _file(runtime)
