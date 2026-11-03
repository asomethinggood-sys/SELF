"""The downloader: a link is an order, and the order has a shape.

Everything here drives the real runtime — the same subscriber the account uses,
the same claims store, the same sender — because the bug this feature exists to
avoid is behavioural: a video arrives and a paragraph arrives with it. A unit
test on ``Quality.selector`` would not have caught that.

The network is faked at the boundary (:mod:`havoc.services.downloader`), never
inside the feature: ``inspect`` and ``fetch`` are the two calls that leave the
machine, so they are the two that are replaced. Everything between them —
gating, numbering, claiming, sending, cleanup — runs for real.
"""

from __future__ import annotations

import html

import pytest
from conftest import FakeMessage, sent_text

from havoc.services import downloader as service
from havoc.services.downloader import DownloadError, Item, Quality

YOUTUBE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
OWN = 42


def _item(url: str = YOUTUBE) -> Item:
    return Item(
        url=url,
        site="youtube",
        title="a very loud song",
        uploader="someone",
        duration=213,
        thumbnail="https://img.example/thumb.jpg",
        qualities=[
            Quality(index=1, label="1080p", height=1080, filesize=180 * 1024 * 1024),
            Quality(index=2, label="720p", height=720, filesize=64 * 1024 * 1024),
            Quality(index=3, label="sound only", audio_only=True, filesize=4 * 1024 * 1024),
        ],
    )


@pytest.fixture()
def fake_media(monkeypatch):
    """The two calls that leave the machine, and nothing else."""
    state = {"inspected": [], "fetched": [], "thumb": True}

    async def inspect(url, *, timeout=None):
        state["inspected"].append(url)
        return _item(url)

    async def fetch(url, quality, *, max_mb=0, timeout=None):
        state["fetched"].append((url, quality.label, quality.audio_only))
        path = f"/tmp/fake-download-{quality.index}.mp4"
        return path  # type: ignore[return-value]

    async def thumb(url, *, timeout=None):
        return b"\x89PNG\r\n\x1a\n" if state["thumb"] else b""

    monkeypatch.setattr(service, "installed", lambda: True)
    monkeypatch.setattr(service, "inspect", inspect)
    monkeypatch.setattr(service, "fetch", fetch)
    monkeypatch.setattr(service, "thumbnail", thumb)
    monkeypatch.setattr(service, "discard", lambda path: state.setdefault("discarded", []).append(path))
    return state


async def _enable(runtime) -> None:
    await runtime.store.set_feature("downloader", True)
    await runtime.feature_manager.refresh()
    runtime.permissions.set_self_id(OWN)


_NEXT = {"id": 900}


async def _send(
    runtime,
    text: str,
    *,
    sender: int = OWN,
    chat_id: int = OWN,
    is_private: bool = False,
    out: bool = False,
) -> None:
    # A fresh id per message: claims are keyed by (chat, message), so reusing one
    # would make the second message look like one already handled.
    _NEXT["id"] += 1
    message = FakeMessage(
        id=_NEXT["id"],
        chat_id=chat_id,
        sender_id=sender,
        text=text,
        out=out,
        is_private=is_private,
        is_group=not is_private,
    )
    from havoc.event_engine import normalize_message

    event = normalize_message(message, self_id=OWN, prefix=".")
    await runtime.event_engine.emit(event)


def _plain(runtime) -> str:
    return html.unescape(sent_text(runtime))


# ---------------------------------------------------------------------------
# The gate: what counts as an order
# ---------------------------------------------------------------------------
def test_a_supported_link_is_recognised_and_a_sentence_with_one_is_not() -> None:
    assert service.site_of(YOUTUBE) == "youtube"
    assert service.site_of("https://youtu.be/dQw4w9WgXcQ") == "youtube"
    assert service.site_of("https://www.tiktok.com/@x/video/123") == "tiktok"
    assert service.site_of("https://www.instagram.com/reel/Cabc123/") == "instagram"
    assert service.site_of("https://example.com/watch") == ""

    assert service.is_link(YOUTUBE)
    # A link inside a sentence is something he is saying, not something he wants.
    assert not service.is_link(f"look at this {YOUTUBE} it is awful")
    assert service.find_link(f"look at this {YOUTUBE} it is awful") == YOUTUBE


# ---------------------------------------------------------------------------
# The exchange: picture, then title, then a number
# ---------------------------------------------------------------------------
async def test_a_link_answers_with_a_picture_and_a_menu_not_a_paragraph(runtime, fake_media) -> None:
    await _enable(runtime)
    await _send(runtime, YOUTUBE)

    files = [message for message in runtime.client.sent if "file" in message.kwargs]
    assert fake_media["inspected"] == [YOUTUBE]
    # The thumbnail is the first thing that arrives, as media.
    assert files, "no picture was sent"
    assert files[0].kwargs["file"] == b"\x89PNG\r\n\x1a\n"

    text = _plain(runtime)
    assert "a very loud song" in text
    # The ladder comes from the site, and the numbers are the only way to answer.
    for expected in ("1080p", "720p", "sound only"):
        assert expected in text
    assert "1" in text and "3" in text
    # The working notice is cleaned up: what stays in the chat is the menu.
    assert runtime.client.deleted, "the 'looking at the link' message was left behind"


async def test_the_menu_is_numbered_by_the_site_and_never_invents_a_quality() -> None:
    info = {
        "formats": [
            {"height": 1080, "vcodec": "avc1", "acodec": "none", "filesize": 200_000_000},
            {"height": 720, "vcodec": "avc1", "acodec": "none", "filesize": 90_000_000},
            {"height": 480, "vcodec": "avc1", "acodec": "none", "filesize": 40_000_000},
            {"vcodec": "none", "acodec": "mp4a", "filesize": 4_000_000},
        ]
    }
    rows = service.build_qualities(info)
    assert [row.label for row in rows] == ["1080p", "720p", "480p", "sound only"]
    assert [row.index for row in rows] == [1, 2, 3, 4]
    assert rows[-1].audio_only and rows[-1].selector == "bestaudio/best"
    assert rows[0].selector.startswith("best[height<=1080]")

    # One quality means one row plus the sound, not a menu of fake choices.
    single = service.build_qualities({"formats": [{"height": 360, "vcodec": "avc1"}]})
    assert [row.label for row in single] == ["360p", "sound only"]


async def test_a_number_downloads_and_sends_the_file_then_forgets_the_menu(runtime, fake_media) -> None:
    await _enable(runtime)
    await _send(runtime, YOUTUBE)
    state = runtime.downloader
    assert state.sessions

    before = len(runtime.client.sent)
    await _send(runtime, "2")

    assert fake_media["fetched"] == [(YOUTUBE, "720p", False)]
    delivered = runtime.client.sent[before:]
    assert delivered, "nothing came back after the pick"
    media = [message for message in delivered if "file" in message.kwargs]
    assert media, "the pick did not produce a file"
    assert media[-1].kwargs["file"].endswith("fake-download-2.mp4")
    # The menu is gone: an open session would let a stray number download twice.
    assert not state.sessions
    # ...and the temporary directory is not left behind.
    assert fake_media["discarded"]


async def test_sound_only_is_its_own_choice(runtime, fake_media) -> None:
    await _enable(runtime)
    await _send(runtime, YOUTUBE)
    await _send(runtime, "3")
    assert fake_media["fetched"] == [(YOUTUBE, "sound only", True)]


async def test_zero_cancels_and_fetches_nothing(runtime, fake_media) -> None:
    await _enable(runtime)
    await _send(runtime, YOUTUBE)
    before = len(runtime.client.sent)
    await _send(runtime, "0")

    assert fake_media["fetched"] == []
    assert not runtime.downloader.sessions
    assert "nothing was fetched" in _plain(runtime)
    assert len(runtime.client.sent) > before


async def test_a_number_off_the_menu_does_not_download(runtime, fake_media) -> None:
    await _enable(runtime)
    await _send(runtime, YOUTUBE)
    await _send(runtime, "9")

    assert fake_media["fetched"] == []
    assert runtime.downloader.sessions, "a wrong number must not lose the menu"
    assert "not on the menu" in _plain(runtime)


# ---------------------------------------------------------------------------
# The gates that keep it his
# ---------------------------------------------------------------------------
async def test_a_link_from_a_stranger_downloads_nothing(runtime, fake_media) -> None:
    await _enable(runtime)
    await _send(runtime, YOUTUBE, sender=999, chat_id=999)
    assert fake_media["inspected"] == []
    assert not runtime.downloader.sessions


async def test_a_stranger_cannot_answer_the_owners_menu(runtime, fake_media) -> None:
    """A private chat has two people in it; only one of them ordered the video.

    The link path was already gated, but the menu was answered by whoever spoke
    next in that chat — so the other participant could pick a quality, spend the
    owner's bandwidth and account, and have the file land in the conversation.
    """
    await _enable(runtime)
    await _send(runtime, YOUTUBE, chat_id=999, is_private=True)
    assert fake_media["inspected"] == [YOUTUBE]
    assert 999 in runtime.downloader.sessions, "the owner was not offered a menu"

    await _send(runtime, "1", sender=999, chat_id=999, is_private=True)
    assert fake_media["fetched"] == [], "a stranger's number started a download"
    assert runtime.downloader.refused == 1
    assert 999 in runtime.downloader.sessions, "refusing must not consume the menu"

    # His own answer to his own menu still downloads, and still says nothing.
    await _send(runtime, "1", chat_id=999, is_private=True)
    assert fake_media["fetched"] == [(YOUTUBE, "1080p", False)]


async def test_a_stranger_cannot_cancel_the_owners_menu(runtime, fake_media) -> None:
    """Cancelling is an action on somebody else's order, not a remark."""
    await _enable(runtime)
    await _send(runtime, YOUTUBE, chat_id=999, is_private=True)

    await _send(runtime, "cancel", sender=999, chat_id=999, is_private=True)
    assert 999 in runtime.downloader.sessions, "a stranger cancelled the owner's download"
    assert fake_media["fetched"] == []

    await _send(runtime, "لغو", sender=999, chat_id=999, is_private=True)
    assert 999 in runtime.downloader.sessions, "the Persian cancel word got through"

    await _send(runtime, "3", chat_id=999, is_private=True)
    assert fake_media["fetched"] == [(YOUTUBE, "sound only", True)]


async def test_a_sender_that_cannot_be_read_is_not_the_owner(runtime, fake_media) -> None:
    """A missing ``sender_id`` means *this account wrote it* — only if it did.

    The subscriber listens for outgoing events as well as incoming ones, so the
    gate used to answer "unknown sender" with "yes, that is the owner". An
    incoming message this shape cannot attribute is a stranger.
    """
    await _enable(runtime)
    await _send(runtime, YOUTUBE, sender=None, chat_id=OWN)
    assert fake_media["inspected"] == [], "an unattributed message ordered a download"

    await _send(runtime, YOUTUBE, sender=None, chat_id=OWN, out=True)
    assert fake_media["inspected"] == [YOUTUBE], "his own outgoing message was refused"


def test_target_of_reads_the_outgoing_flag_from_every_shape() -> None:
    """The command path hands over a wrapper, the subscriber a normalized event."""
    from havoc.event_engine import normalize_message
    from havoc.features.downloader import target_of

    wrapped = normalize_message(
        FakeMessage(id=1, chat_id=2, text="x", out=True), self_id=OWN, prefix="."
    )
    assert target_of(wrapped).is_outgoing is True
    assert target_of(wrapped).is_outgoing is not None

    bare = FakeMessage(id=1, chat_id=2, text="x", out=True)
    assert target_of(bare).is_outgoing is True, "a raw message spells it `out`"

    incoming = normalize_message(
        FakeMessage(id=1, chat_id=2, text="x", out=False), self_id=OWN, prefix="."
    )
    assert target_of(incoming).is_outgoing is False


async def test_the_feature_switch_is_a_real_switch(runtime, fake_media) -> None:
    await runtime.store.set_feature("downloader", False)
    await runtime.feature_manager.refresh()
    runtime.permissions.set_self_id(OWN)
    await _send(runtime, YOUTUBE)
    assert fake_media["inspected"] == []


async def test_an_emergency_stop_stops_it(runtime, fake_media) -> None:
    await _enable(runtime)
    await runtime.store.set_emergency_stop(True)
    await _send(runtime, YOUTUBE)
    assert fake_media["inspected"] == []


# ---------------------------------------------------------------------------
# The thing the owner actually asked for: no comment on the video
# ---------------------------------------------------------------------------
async def test_the_link_never_reaches_the_model(runtime, fake_media) -> None:
    """The whole point: a video, and no paragraph about the video."""
    from test_persona import StubProvider

    await _enable(runtime)
    await runtime.store.set_feature("persona", True)
    await runtime.feature_manager.refresh()
    provider = StubProvider(["i would have commented on that video"])
    runtime.ai = provider

    await _send(runtime, YOUTUBE)
    await _send(runtime, "1")

    assert provider.calls == [], "the model was asked about a video she already sent"
    first = _NEXT["id"] - 1
    assert runtime.claims.claimed(OWN, first), (
        "the message was not claimed, so anything could answer it later"
    )


async def test_a_failed_download_says_so_and_keeps_the_menu(runtime, fake_media, monkeypatch) -> None:
    async def explode(url, quality, *, max_mb=0, timeout=None):
        raise DownloadError("the site refused the link", kind="unavailable")

    monkeypatch.setattr(service, "fetch", explode)
    await _enable(runtime)
    await _send(runtime, YOUTUBE)
    await _send(runtime, "1")

    text = _plain(runtime)
    assert "did not come down" in text
    assert "the site refused the link" in text
    # The session survives an honest failure: a different number can work.
    assert runtime.downloader.sessions
    assert not runtime.downloader.sessions[OWN].busy


async def test_a_missing_downloader_says_so_instead_of_crashing(runtime, monkeypatch) -> None:
    monkeypatch.setattr(service, "installed", lambda: False)
    await _enable(runtime)
    await _send(runtime, YOUTUBE)
    text = _plain(runtime)
    assert "havoc-userbot[download]" in text
    assert not runtime.downloader.sessions


# ---------------------------------------------------------------------------
# The command in front of it
# ---------------------------------------------------------------------------
async def test_the_command_reaches_the_same_flow(runtime, fake_media) -> None:
    from conftest import invoke

    await _enable(runtime)
    await invoke(runtime, f".download {YOUTUBE}")
    assert fake_media["inspected"] == [YOUTUBE]
    assert "a very loud song" in _plain(runtime)


async def test_the_info_card_is_honest_about_what_is_missing(runtime, monkeypatch) -> None:
    from conftest import invoke

    monkeypatch.setattr(service, "installed", lambda: False)
    await invoke(runtime, ".download info")
    text = _plain(runtime)
    assert "downloader" in text.lower()
    assert "havoc-userbot[download]" in text

