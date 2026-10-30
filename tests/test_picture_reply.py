"""The two things the owner asked for that a text-only reply path cannot do.

1. **A photo.** They send a picture and tag her; she must actually look at it.
2. **Telegram's reply button.** Pointing at her message must put that message in
   front of the model — otherwise "why?" is a question about nothing — and her
   answer must itself be a reply, so the next tap continues the thread.

Both are tested on the ambient path, the one the owner uses.
"""

from __future__ import annotations

from conftest import FakeDocument, FakeEvent, FakeMedia, FakeMessage

from havoc.event_engine import EventType, normalize_message
from havoc.utils.parsing import parse_command

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 256


class StubProvider:
    """Records what the model was asked, answers with one scripted line."""

    model = "stub-model"
    available = True
    last_max_tokens = 0

    def __init__(self, reply: str = "i looked. **cute** (¬‿¬)") -> None:
        self.reply = reply
        self.calls: list[list[object]] = []

    async def converse(self, messages, *, max_tokens: int = 400, temperature: float = 0.85) -> str:
        self.calls.append(list(messages))
        self.last_max_tokens = max_tokens
        return self.reply


class PhotoClient:
    """A client that downloads any photo as a real PNG."""

    def __init__(self, payload: bytes = PNG) -> None:
        self.payload = payload
        self.sent: list[tuple[object, str, dict]] = []
        self.downloads = 0

    async def download_media(self, message, file=None, **kwargs):
        self.downloads += 1
        file.write(self.payload)
        return file

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return FakeMessage(id=5000 + len(self.sent), chat_id=int(chat_id), text=text, out=True)

    async def send_file(self, chat_id, file, **kwargs):
        self.sent.append((chat_id, kwargs.get("caption", ""), {**kwargs, "file": file}))
        return FakeMessage(id=5000 + len(self.sent), text="", out=True)


async def _enable(runtime) -> None:
    await runtime.store.set_feature("persona", True)
    await runtime.feature_manager.refresh()


def _event(text: str, *, message: FakeMessage, chat_id: int = 42):
    return normalize_message(message, self_id=42, prefix=".", event_type=EventType.OUTGOING)


def _photo_message(*, text: str = "", reply: FakeMessage | None = None) -> FakeMessage:
    media = FakeMedia(document=FakeDocument(file_name="cat.jpg"))
    media.photo = object()
    return FakeMessage(id=901, chat_id=42, sender_id=42, text=text, out=True, media=media, reply=reply)


def _her_message(text: str = "do you even lift") -> FakeMessage:
    return FakeMessage(id=700, chat_id=42, sender_id=42, text=text, out=True, is_group=False)


def _picture_part(messages) -> dict | None:
    """The image part inside whatever was sent to the model."""
    for message in messages:
        content = getattr(message, "content", None)
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    return part
    return None


def _all_text(messages) -> str:
    pieces: list[str] = []
    for message in messages:
        content = getattr(message, "content", None)
        if isinstance(content, str):
            pieces.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    pieces.append(str(part.get("text", "")))
    return "\n".join(pieces)


# ---------------------------------------------------------------------------
# A picture
# ---------------------------------------------------------------------------
async def test_a_photo_with_no_caption_still_gets_looked_at(runtime) -> None:
    await _enable(runtime)
    runtime.ai = StubProvider()
    runtime.client = PhotoClient()
    runtime.sender.client = runtime.client
    runtime.persona_replies._last_at = None

    await runtime.persona_replies.on_self_message(_event("", message=_photo_message()))

    part = _picture_part(runtime.ai.calls[-1])
    assert part is not None, "the photo never reached the model"
    assert part["image_url"]["url"].startswith("data:image/png;base64,")
    assert runtime.client.downloads == 1


async def test_a_photo_with_a_caption_carries_both(runtime) -> None:
    await _enable(runtime)
    runtime.ai = StubProvider()
    runtime.client = PhotoClient()
    runtime.sender.client = runtime.client
    runtime.persona_replies._last_at = None

    await runtime.persona_replies.on_self_message(
        _event("what is this", message=_photo_message(text="what is this"))
    )

    prompt = _all_text(runtime.ai.calls[-1])
    assert "what is this" in prompt
    assert _picture_part(runtime.ai.calls[-1]) is not None


async def test_a_picture_that_is_not_an_image_does_not_crash_the_reply(runtime) -> None:
    await _enable(runtime)
    runtime.ai = StubProvider()
    runtime.client = PhotoClient(b"this is a zip file, not a cat")
    runtime.sender.client = runtime.client
    runtime.persona_replies._last_at = None

    await runtime.persona_replies.on_self_message(
        _event("look", message=_photo_message(text="look"))
    )

    assert _picture_part(runtime.ai.calls[-1]) is None
    assert "look" in _all_text(runtime.ai.calls[-1])
    assert runtime.client.sent, "she must still answer in words"


async def test_a_voice_note_is_not_downloaded(runtime) -> None:
    await _enable(runtime)
    runtime.ai = StubProvider()
    runtime.client = PhotoClient()
    runtime.sender.client = runtime.client

    media = FakeMedia(document=FakeDocument(file_name="voice.ogg"))
    media.voice = object()
    message = FakeMessage(id=902, chat_id=42, sender_id=42, text="listen", out=True, media=media)
    await runtime.persona_replies.on_self_message(_event("listen", message=message))

    assert runtime.client.downloads == 0


async def test_the_command_also_looks_at_a_photo(runtime) -> None:
    """.yura on a picture must not be the one path that ignores it."""
    await _enable(runtime)
    runtime.ai = StubProvider()
    runtime.client = PhotoClient()
    runtime.sender.client = runtime.client

    message = _photo_message(text=".yura what is this")
    message.id = 520
    message.chat_id = 100
    parsed = parse_command(".yura what is this", runtime.prefix)
    assert parsed is not None
    await runtime.dispatcher.handle(FakeEvent(message, chat_id=100, out=True), message, parsed, "en")

    assert _picture_part(runtime.ai.calls[-1]) is not None
# ---------------------------------------------------------------------------
# Telegram's reply button
# ---------------------------------------------------------------------------
async def test_replying_to_her_message_puts_it_in_front_of_the_model(runtime) -> None:
    await _enable(runtime)
    runtime.ai = StubProvider()
    runtime.client = PhotoClient()
    runtime.sender.client = runtime.client
    runtime.persona_replies._last_at = None

    message = FakeMessage(
        id=903, chat_id=42, sender_id=42, text="y", out=True, reply=_her_message()
    )
    await runtime.persona_replies.on_self_message(_event("y", message=message))

    prompt = _all_text(runtime.ai.calls[-1])
    assert "do you even lift" in prompt, "the quoted message was dropped"
    assert "y" in prompt


async def test_her_answer_is_a_reply_to_the_message_it_answers(runtime) -> None:
    await _enable(runtime)
    runtime.ai = StubProvider()
    runtime.client = PhotoClient()
    runtime.sender.client = runtime.client
    runtime.persona_replies._last_at = None

    message = FakeMessage(
        id=903, chat_id=42, sender_id=42, text="y", out=True, reply=_her_message()
    )
    await runtime.persona_replies.on_self_message(_event("y", message=message))

    assert runtime.client.sent
    chat_id, text, kwargs = runtime.client.sent[-1]
    assert kwargs.get("reply_to") == 903, "her answer must thread under the question"
    assert "<b>cute</b>" in text, "the model's markdown must arrive as formatting"


async def test_a_reply_to_a_third_party_does_not_leak_their_words(runtime) -> None:
    """The quote is folded in only when the owner pointed at their own thread."""
    await _enable(runtime)
    runtime.ai = StubProvider()
    runtime.client = PhotoClient()
    runtime.sender.client = runtime.client
    runtime.persona_replies._last_at = None

    stranger = FakeMessage(id=704, chat_id=42, sender_id=999, text="my private secret", out=False)
    message = FakeMessage(id=904, chat_id=42, sender_id=42, text="hm", out=True, reply=stranger)
    await runtime.persona_replies.on_self_message(_event("hm", message=message))

    prompt = _all_text(runtime.ai.calls[-1])
    assert "my private secret" not in prompt
    assert "hm" in prompt


async def test_a_reply_to_a_photo_is_described_as_a_photo(runtime) -> None:
    await _enable(runtime)
    runtime.ai = StubProvider()
    runtime.client = PhotoClient()
    runtime.sender.client = runtime.client
    runtime.persona_replies._last_at = None

    hers = FakeMessage(id=705, chat_id=42, sender_id=42, text="", out=True)
    hers.media = FakeMedia(photo=object())
    message = FakeMessage(id=905, chat_id=42, sender_id=42, text="and this", out=True, reply=hers)
    await runtime.persona_replies.on_self_message(_event("and this", message=message))

    prompt = _all_text(runtime.ai.calls[-1])
    assert "a picture" in prompt.lower()
    assert "and this" in prompt


async def test_her_own_line_coming_back_is_still_ignored(runtime) -> None:
    """The loop guard must survive reply-threading."""
    await _enable(runtime)
    runtime.ai = StubProvider()
    runtime.client = PhotoClient()
    runtime.sender.client = runtime.client
    runtime.persona_replies._last_at = None

    her = _her_message("mrrp. you called?")
    runtime.yura_chat._sent.setdefault(42, {})["mrrp. you called?"] = None
    message = FakeMessage(id=906, chat_id=42, sender_id=42, text="mrrp. you called?", out=True, reply=her)
    await runtime.persona_replies.on_self_message(_event("mrrp. you called?", message=message))

    assert runtime.ai.calls == []
