"""Reading Saved Messages and mentions — one page, bounded, escaped.

The dates and the chat ids come from the same fake message objects the rest of
the suite uses, so a passing test means the live path produces the same lines.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from conftest import FakeMessage, FakeUser, invoke

GROUP = -1001234567890


def _said(sent) -> str:
    return "\n".join(message.text for message in sent)


def _message(message_id: int, text: str, *, sender=None, chat_id: int = GROUP) -> FakeMessage:
    return FakeMessage(
        id=message_id,
        chat_id=chat_id,
        text=text,
        sender=sender,
        date=datetime(2026, 5, 1, 9, 30, tzinfo=UTC),
    )


async def test_diary_shows_saved_messages_newest_first(runtime) -> None:
    runtime.client.history = [
        _message(11, "buy milk"),
        _message(10, "call the landlord", sender=FakeUser(7, "Owner")),
    ]
    sent = await invoke(runtime, ".diary")
    text = _said(sent)
    assert "saved messages" in text.lower()
    assert "buy milk" in text
    assert "call the landlord" in text
    assert "#11" in text and "#10" in text


async def test_diary_with_nothing_saved_says_so(runtime) -> None:
    runtime.client.history = []
    sent = await invoke(runtime, ".diary")
    assert "empty" in _said(sent).lower()


async def test_diary_honours_the_count(runtime) -> None:
    runtime.client.history = [_message(20 + index, f"note {index}") for index in range(5)]
    await invoke(runtime, ".diary 2")
    request = runtime.client.history
    assert len(request) == 5  # the fake keeps them all; the limit went as a kwarg
    sent = await invoke(runtime, ".diary 2")
    assert _said(sent).count("•") == 2


async def test_diary_escapes_html_in_saved_text(runtime) -> None:
    runtime.client.history = [_message(31, "<b>not bold</b> & co")]
    sent = await invoke(runtime, ".diary")
    text = _said(sent)
    assert "&lt;b&gt;not bold&lt;/b&gt; &amp; co" in text


async def test_diary_shows_media_as_a_label(runtime) -> None:
    from conftest import FakeDocument

    message = FakeMessage(
        id=44,
        chat_id=GROUP,
        text="",
        media=FakeDocument(size=2048, file_name="receipt.pdf"),
        date=datetime(2026, 5, 1, 9, 30, tzinfo=UTC),
    )
    runtime.client.history = [message]
    sent = await invoke(runtime, ".diary")
    assert "[" in _said(sent) and "]" in _said(sent)


async def test_mentions_reads_the_mention_filter(runtime) -> None:
    runtime.client.history = [
        _message(51, "@yura look at this", sender=FakeUser(9, "Sara")),
    ]
    runtime.client.dialogs = [SimpleNamespace(entity=FakeUser(9, "Sara"), id=9)]
    sent = await invoke(runtime, ".mentions")
    text = _said(sent)
    assert "mentions" in text.lower()
    assert "@yura look at this" in text
    assert "Sara" in text


async def test_mentions_walks_chats_instead_of_asking_for_a_global_search(runtime) -> None:
    """``SearchGlobal`` rejects the mention filter; per-chat requests do not."""
    calls: list[dict[str, object]] = []
    original = runtime.client.get_messages

    async def spy(entity=None, **kwargs):
        calls.append({"entity": entity, **kwargs})
        return await original(entity, **kwargs)

    runtime.client.get_messages = spy
    runtime.client.history = []
    runtime.client.dialogs = [
        SimpleNamespace(entity=FakeUser(9, "Sara"), id=9),
        SimpleNamespace(entity=FakeUser(10, "Ali"), id=10),
    ]
    await invoke(runtime, ".mentions")
    assert len(calls) == 2, "every recent chat is asked, once"
    for call in calls:
        assert type(call["filter"]).__name__ == "InputMessagesFilterMyMentions"


async def test_no_mentions_is_a_sentence(runtime) -> None:
    runtime.client.history = []
    runtime.client.dialogs = []
    sent = await invoke(runtime, ".mentions")
    assert "nobody" in _said(sent).lower()


async def test_search_understands_in_chat(runtime) -> None:
    """``.search rent in @sara`` must not search for the words "in @sara"."""
    calls: list[dict[str, object]] = []
    original = runtime.client.get_messages

    async def spy(entity=None, **kwargs):
        calls.append({"entity": entity, **kwargs})
        return await original(entity, **kwargs)

    runtime.client.get_messages = spy
    runtime.client.history = []
    await invoke(runtime, ".search rent in @sara")
    assert calls, "no search went out"
    assert calls[-1]["search"] == "rent"
