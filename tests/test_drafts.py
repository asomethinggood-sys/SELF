"""Drafts and the archive.

The one thing these tests guard hardest is the promise a draft makes: **nothing
is sent**. After ``.draft``, the fake client must have no new messages — only a
``SaveDraftRequest`` in the raw request log.
"""

from __future__ import annotations

from types import SimpleNamespace

from conftest import invoke

GROUP = -1001234567890


def _said(sent) -> str:
    return "\n".join(message.text for message in sent)


def _names(runtime) -> list[str]:
    return [type(row).__name__ for row in runtime.client.raw_requests]


async def test_a_draft_reaches_telegram_and_sends_nothing(runtime) -> None:
    before = len(runtime.client.sent)
    sent = await invoke(runtime, ".draft 880 running late, sorry")
    text = _said(sent)
    assert "draft written" in text.lower()
    assert "nothing was sent" in text.lower()
    assert runtime.client.raw_requests[-1].__class__.__name__ == "SaveDraftRequest"
    assert runtime.client.raw_requests[-1].message == "running late, sorry"
    # The only outgoing message is her own answer.
    assert len(runtime.client.sent) - before == 1


async def test_a_draft_keeps_the_sentence_verbatim(runtime) -> None:
    await invoke(runtime, ".draft 880 Can't make it — it's raining")
    assert runtime.client.raw_requests[-1].message == "Can't make it — it's raining"


async def test_off_clears_the_draft(runtime) -> None:
    sent = await invoke(runtime, ".draft 880 off")
    assert "gone" in _said(sent).lower()
    assert runtime.client.raw_requests[-1].message == ""


async def test_a_draft_needs_a_chat(runtime) -> None:
    sent = await invoke(runtime, ".draft")
    assert ".draft" in _said(sent)
    assert runtime.client.raw_requests == []


async def test_drafts_lists_what_telegram_holds(runtime) -> None:
    runtime.client.responses["GetAllDraftsRequest"] = SimpleNamespace(
        updates=[
            SimpleNamespace(
                peer=SimpleNamespace(user_id=880, chat_id=None, channel_id=None),
                message=SimpleNamespace(message="half a thought", date=None),
            )
        ]
    )
    sent = await invoke(runtime, ".drafts")
    text = _said(sent)
    assert "half a thought" in text
    assert "880" in text


async def test_drafts_ignores_empty_drafts(runtime) -> None:
    runtime.client.responses["GetAllDraftsRequest"] = SimpleNamespace(
        updates=[
            SimpleNamespace(
                peer=SimpleNamespace(user_id=880, chat_id=None, channel_id=None),
                message=SimpleNamespace(message="", date=None),
            )
        ]
    )
    sent = await invoke(runtime, ".drafts")
    assert "no chat" in _said(sent).lower()


async def test_archive_moves_the_chat_to_folder_one(runtime) -> None:
    sent = await invoke(runtime, ".archive 880")
    assert "archive" in _said(sent).lower()
    request = runtime.client.raw_requests[-1]
    assert request.__class__.__name__ == "EditPeerFoldersRequest"
    assert request.folder_peers[0].folder_id == 1


async def test_unarchive_puts_it_back(runtime) -> None:
    sent = await invoke(runtime, ".unarchive 880")
    assert "main list" in _said(sent).lower()
    assert runtime.client.raw_requests[-1].folder_peers[0].folder_id == 0


async def test_archive_needs_a_chat(runtime) -> None:
    sent = await invoke(runtime, ".archive")
    assert ".archive" in _said(sent)
    assert runtime.client.raw_requests == []


async def test_a_refusal_is_a_sentence(runtime) -> None:
    async def refuse(self, request):
        self.raw_requests.append(request)
        raise RuntimeError("CHAT_WRITE_FORBIDDEN")

    original = type(runtime.client).__call__
    type(runtime.client).__call__ = refuse
    try:
        sent = await invoke(runtime, ".draft 880 hello")
    finally:
        type(runtime.client).__call__ = original
    assert "CHAT_WRITE_FORBIDDEN" in _said(sent)
