"""Her own identity: the username, the photo, and what she can actually do.

The interesting cases are the refusals: a username Telegram would reject must be
rejected *before* a request goes out, and a photo that is too big must never be
downloaded. Both are checked by watching the fake client, not by reading text.
"""

from __future__ import annotations

import pytest
from conftest import FakeMedia, FakeMessage, invoke

from havoc.features.identity_tools import MAX_PHOTO_BYTES, USERNAME_RULE


def _said(sent) -> str:
    return "\n".join(message.text for message in sent)


async def test_username_with_no_argument_reports_the_current_one(runtime) -> None:
    runtime.telegram.me.username = "yura_live"
    sent = await invoke(runtime, ".username")
    assert "@yura_live" in _said(sent)


async def test_username_with_none_says_none(runtime) -> None:
    runtime.telegram.me.username = None
    sent = await invoke(runtime, ".username")
    assert "none" in _said(sent).lower()


async def test_username_change_sends_one_update_request(runtime) -> None:
    sent = await invoke(runtime, ".username yura_1704")
    assert "yura_1704" in _said(sent)
    assert len(runtime.client.raw_requests) == 1
    request = runtime.client.raw_requests[0]
    assert type(request).__name__ == "UpdateUsernameRequest"
    assert request.username == "yura_1704"
    assert runtime.telegram.me.username == "yura_1704"


async def test_username_removal_sends_none(runtime) -> None:
    runtime.telegram.me.username = "yura_live"
    sent = await invoke(runtime, ".username off")
    assert "removed" in _said(sent).lower()
    request = runtime.client.raw_requests[-1]
    assert request.username is None
    assert runtime.telegram.me.username is None


@pytest.mark.parametrize(
    "bad",
    ["yura", "1yura_17", "yura-17", "yu", "a" * 33, "yura 17"],
)
async def test_a_username_telegram_would_reject_never_leaves_the_machine(runtime, bad) -> None:
    sent = await invoke(runtime, f".username {bad}")
    assert "Telegram" in _said(sent)
    assert runtime.client.raw_requests == [], "a doomed request was still sent"


@pytest.mark.parametrize("good", ["yura_1704", "Yura_17", "a1234"])
async def test_a_legal_username_matches_telegrams_own_rule(runtime, good) -> None:
    assert USERNAME_RULE.match(good)
    await invoke(runtime, f".username {good}")
    assert runtime.client.raw_requests, "the request never went out"


async def test_photo_without_a_reply_explains_what_to_do(runtime) -> None:
    sent = await invoke(runtime, ".photo --yes")
    assert ".photo" in _said(sent)
    assert runtime.client.uploads == []


async def test_photo_of_a_plain_message_is_refused(runtime) -> None:
    target = FakeMessage(id=77, text="just words")
    sent = await invoke(runtime, ".photo --yes", reply=target)
    assert "no picture" in _said(sent).lower()
    assert runtime.client.uploads == []


async def test_photo_too_big_is_refused_before_downloading(runtime) -> None:
    target = FakeMessage(
        id=78, media=FakeMedia(document=_Doc(MAX_PHOTO_BYTES + 1))
    )
    sent = await invoke(runtime, ".photo --yes", reply=target)
    assert "10 MB" in _said(sent)
    assert runtime.client.uploads == []


async def test_photo_asks_before_it_changes_her_face(runtime) -> None:
    target = FakeMessage(id=76, media=FakeMedia(document=_Doc(2048)))
    sent = await invoke(runtime, ".photo", reply=target)
    assert "confirm" in _said(sent).lower()
    assert runtime.client.uploads == []


async def test_photo_uploads_the_reply_and_records_it(runtime) -> None:
    target = FakeMessage(id=79, media=FakeMedia(document=_Doc(2048)))
    sent = await invoke(runtime, ".photo --yes", reply=target)
    assert "face" in _said(sent).lower()
    assert runtime.client.uploads, "nothing was uploaded"
    history = await runtime.store.profile_history(limit=5)
    assert any(row["field"] == "photo" for row in history), "the change was not recorded"
    assert runtime.client.raw_requests[-1].__class__.__name__ == "UploadProfilePhotoRequest"


async def test_abilities_counts_the_live_registry(runtime) -> None:
    sent = await invoke(runtime, ".abilities")
    text = _said(sent)
    live = len(runtime.registry.visible())
    assert str(live) in text
    assert "tools" in text.lower()
    # Categories come from the registry, so a new family shows up for free.
    assert "media" in text


async def test_abilities_mentions_tool_names(runtime) -> None:
    sent = await invoke(runtime, ".abilities")
    text = _said(sent)
    for tool in runtime.actions.tools:
        assert tool.name in text


class _Doc:
    """Only the size attribute ``.photo`` reads."""

    def __init__(self, size: int) -> None:
        self.size = size
        self.attributes = []
