"""The commands you point at her: recap, diary, faces, say, voice, poster, react.

Each one is checked for what it sends, and for what it says when the thing it
needs is missing — a command that fails silently is the bug this file exists to
prevent.
"""

from __future__ import annotations

import json

import pytest
from conftest import FakeEvent, FakeMessage, invoke, last_text

from havoc.features import yura_extras

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class _Edge:
    def __init__(self, audio: bytes = b"fake-mp3") -> None:
        self.audio = audio
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, text: str, voice: str) -> bytes:
        self.calls.append((text, voice))
        return self.audio


def _edge_engine(runtime, edge=None) -> _Edge:
    edge = edge or _Edge()
    runtime.tts = yura_extras.TextToSpeech(runtime, edge_speak=edge)
    return edge


async def _history(runtime, chat_id: int = 100) -> None:
    await runtime.memory.append(chat_id, "user", "i have been up since five")
    await runtime.memory.append(chat_id, "assistant", "go to **sleep**, kitten")
    await runtime.memory.append(chat_id, "user", "no")


# ---------------------------------------------------------------------------
# recap / diary
# ---------------------------------------------------------------------------
async def test_recap_shows_the_day_with_times(runtime) -> None:
    await _history(runtime)
    await invoke(runtime, ".yura recap")
    text = last_text(runtime)
    assert "last day" in text
    assert "up since five" in text
    assert ":" in text  # a clock time


async def test_recap_says_so_when_there_is_nothing(runtime) -> None:
    await invoke(runtime, ".yura recap")
    assert "Nothing said" in last_text(runtime)


async def test_diary_writes_a_file_and_sends_it(runtime) -> None:
    await _history(runtime)
    await invoke(runtime, ".yura diary")
    sent = [entry for entry in runtime.client.sent if "file" in entry.kwargs]
    assert sent, "no file was sent"
    path = sent[-1].kwargs["file"]
    body = open(path, encoding="utf-8").read()
    assert body.startswith("# diary")
    assert "up since five" in body


# ---------------------------------------------------------------------------
# faces
# ---------------------------------------------------------------------------
async def test_faces_lists_every_mood(runtime) -> None:
    await invoke(runtime, ".yura faces")
    text = last_text(runtime)
    for mood in yura_extras.FACE_BOOK["en"]:
        assert mood in text


async def test_a_named_mood_gets_its_own_faces(runtime) -> None:
    await invoke(runtime, ".yura faces smug")
    text = last_text(runtime)
    assert "smug" in text
    assert any(face in text for face in yura_extras.FACE_BOOK["en"]["smug"])


async def test_an_unknown_mood_names_the_real_ones(runtime) -> None:
    await invoke(runtime, ".yura faces sleepyish")
    text = last_text(runtime)
    assert "sleepyish" in text and "feral" in text


# ---------------------------------------------------------------------------
# say / voice
# ---------------------------------------------------------------------------
async def test_speak_sends_a_voice_note(runtime) -> None:
    edge = _edge_engine(runtime)
    await invoke(runtime, ".yura speak good night")
    sent = [entry for entry in runtime.client.sent if entry.kwargs.get("voice_note")]
    assert sent, "no voice note was sent"
    assert edge.calls[0][0] == "good night"
    assert sent[-1].kwargs["file"].endswith(".mp3")


async def test_say_is_always_a_conversation_and_never_a_voice_note(runtime) -> None:
    """Two words, two meanings, at every install — not depending on a package."""
    _edge_engine(runtime)
    runtime.ai = _Stub(["mrrp"])
    await invoke(runtime, ".yura say something kind")
    assert not any(entry.kwargs.get("voice_note") for entry in runtime.client.sent)
    assert "something kind" in runtime.ai.calls[-1][-1].content


async def test_a_failed_voice_names_what_is_wrong(runtime) -> None:
    class Broken(_Edge):
        async def __call__(self, text, voice):
            raise yura_extras.TextToSpeech.__init__.__globals__["TTSError"]("no route", kind="network")

    runtime.tts = yura_extras.TextToSpeech(runtime, edge_speak=Broken())
    await invoke(runtime, ".yura speak hello")
    assert "No voice" in last_text(runtime)


async def test_the_voice_key_is_stored_and_the_message_deleted(runtime) -> None:
    _edge_engine(runtime)
    message = FakeMessage(id=777, chat_id=100, sender_id=42, text=".yura voice key gsk_secret", out=True)
    event = FakeEvent(message, chat_id=100, out=True)
    await runtime.handlers._handle_outgoing(event)
    await _drain()
    stored = await runtime.store.get_setting("tts.key", None)
    assert stored and "gsk_secret" not in stored
    assert runtime.client.deleted, "the message with the key was left in the chat"


async def test_voice_status_shows_the_provider(runtime) -> None:
    _edge_engine(runtime)
    await invoke(runtime, ".yura voice")
    assert "edge" in last_text(runtime)


async def test_voice_can_switch_to_the_paid_provider(runtime) -> None:
    await invoke(runtime, ".yura voice groq")
    assert await runtime.store.get_setting("tts.provider", None) == "openai"
    await invoke(runtime, ".yura voice off")
    assert await runtime.store.get_setting("tts.provider", None) == "edge"


# ---------------------------------------------------------------------------
# poster
# ---------------------------------------------------------------------------
async def test_poster_says_so_when_none_are_built(runtime, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(yura_extras, "POSTER_DIR", tmp_path)
    monkeypatch.setattr(yura_extras, "POSTERS", tmp_path / "index.json")
    await invoke(runtime, ".yura poster")
    assert "No posters" in last_text(runtime)


async def test_poster_sends_a_real_file(runtime, monkeypatch, tmp_path) -> None:
    image = tmp_path / "001-hafez.png"
    image.write_bytes(PNG)
    (tmp_path / "index.json").write_text(
        json.dumps([{"file": image.name, "poet": "Hafez", "title": "Ghazal 1"}]), encoding="utf-8"
    )
    monkeypatch.setattr(yura_extras, "POSTER_DIR", tmp_path)
    monkeypatch.setattr(yura_extras, "POSTERS", tmp_path / "index.json")

    await invoke(runtime, ".yura poster")

    sent = [entry for entry in runtime.client.sent if "file" in entry.kwargs]
    assert sent and str(sent[-1].kwargs["file"]).endswith(".png")
    assert "Hafez" in sent[-1].text


async def test_poster_filters_by_poet(runtime, monkeypatch, tmp_path) -> None:
    for name in ("a.png", "b.png"):
        (tmp_path / name).write_bytes(PNG)
    (tmp_path / "index.json").write_text(
        json.dumps(
            [
                {"file": "a.png", "poet": "Hafez", "title": "one"},
                {"file": "b.png", "poet": "Rumi", "title": "two"},
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(yura_extras, "POSTER_DIR", tmp_path)
    monkeypatch.setattr(yura_extras, "POSTERS", tmp_path / "index.json")

    await invoke(runtime, ".yura poster rumi")

    sent = [entry for entry in runtime.client.sent if "file" in entry.kwargs]
    assert str(sent[-1].kwargs["file"]).endswith("b.png")


# ---------------------------------------------------------------------------
# caption
# ---------------------------------------------------------------------------
async def test_caption_without_a_picture_says_so(runtime) -> None:
    await invoke(runtime, ".yura caption")
    assert "not looked at a picture" in last_text(runtime)


async def test_caption_revisits_the_last_picture(runtime) -> None:
    await runtime.yura_chat.remember_picture(100, "a black cat on a keyboard")
    runtime.ai = _Stub(["it is judging your code."])
    await invoke(runtime, ".yura caption")
    assert "judging" in last_text(runtime)


# ---------------------------------------------------------------------------
# react
# ---------------------------------------------------------------------------
async def test_react_needs_a_reply(runtime) -> None:
    await invoke(runtime, ".yura react")
    assert "Reply to the message" in last_text(runtime)


async def test_react_reaches_the_message_being_replied_to(runtime) -> None:
    class Reactor:
        def __init__(self) -> None:
            self.calls: list[tuple[object, int, str]] = []
            self.sent: list[tuple[object, str, dict]] = []

        async def send_message(self, chat_id, text, **kwargs):
            self.sent.append((chat_id, text, kwargs))
            return FakeMessage(id=9000 + len(self.sent), text=text, out=True)

        async def send_reaction(self, chat_id, message_id, emoji):
            self.calls.append((chat_id, message_id, emoji))
            return True

    reactor = Reactor()
    runtime.client = reactor
    runtime.sender.client = reactor
    target = FakeMessage(id=55, chat_id=100, sender_id=42, text="something i said", out=True)

    message = FakeMessage(id=56, chat_id=100, sender_id=42, text=".yura react ❤", out=True, reply=target)
    await runtime.handlers._handle_outgoing(FakeEvent(message, chat_id=100, out=True))
    await _drain()

    assert reactor.calls and reactor.calls[0][1] == 55


# ---------------------------------------------------------------------------
# No key ever reaches the chat
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "command",
    [
        ".yura recap",
        ".yura faces",
        ".yura faces nope",
        ".yura voice",
        ".yura say",
        ".yura poster",
        ".yura caption",
        ".yura react",
    ],
)
async def test_no_command_leaks_a_locale_key(runtime, command, monkeypatch, tmp_path) -> None:
    """A missing placeholder used to fail the whole interpolation and print the key."""
    monkeypatch.setattr(yura_extras, "POSTER_DIR", tmp_path)
    monkeypatch.setattr(yura_extras, "POSTERS", tmp_path / "index.json")
    await invoke(runtime, command)
    text = last_text(runtime)
    assert not text.startswith("yura."), f"{command} printed a raw key: {text}"
    assert "{" not in text, f"{command} left a placeholder: {text}"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
class _Stub:
    model = "stub"
    available = True
    last_max_tokens = 0

    def __init__(self, replies=("ok",)) -> None:
        self.replies = list(replies)
        self.calls: list[list[object]] = []

    async def converse(self, messages, *, max_tokens: int = 400, temperature: float = 0.85) -> str:
        self.calls.append(list(messages))
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


async def _drain() -> None:
    """Let fire-and-forget tasks (the key deletion) run."""
    import asyncio

    await asyncio.sleep(0)
    await asyncio.sleep(0)
