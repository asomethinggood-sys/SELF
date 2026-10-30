"""Pictures the owner sends.

A photo without a caption used to be answered with a greeting, which reads as a
bot that cannot see. Now it is a question, and a photo that cannot be read is a
reason to answer in words rather than a reason to crash the reply path.
"""

from __future__ import annotations

import base64
import io
import struct
import zlib

from conftest import FakeClient, FakeEvent, FakeMedia, FakeMessage, FakeUser, last_text

from havoc.services import vision


class StubProvider:
    """A provider that records what it was asked and answers once."""

    model = "stub-model"
    available = True

    def __init__(self, reply: str = "that is a cat, daddaaa") -> None:
        self.reply = reply
        self.calls: list[list[object]] = []

    async def converse(self, messages, *, max_tokens: int = 400, temperature: float = 0.85) -> str:
        self.calls.append(list(messages))
        return self.reply


def _png(width: int = 2, height: int = 2) -> bytes:
    """A real, minimal PNG — the sniffer looks at the bytes, so they must be real."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = b"".join(b"\x00" + b"\x7f\x30\x90" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


PNG = _png()
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def _all_text(messages) -> str:
    """Every string in a conversation, flattened — pictures included."""

    parts: list[str] = []
    for message in messages:
        content = getattr(message, "content", message)
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    parts.extend(str(value) for value in item.values())
                else:
                    parts.append(str(item))
    return "\n".join(parts)


def _picture_part(messages) -> dict | None:
    for message in messages:
        content = getattr(message, "content", None)
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "image_url":
                    return item
    return None


# ---------------------------------------------------------------------------
# Reading the bytes
# ---------------------------------------------------------------------------
def test_the_sniffer_reads_what_the_bytes_are() -> None:
    assert vision.sniff_mime(PNG) == "image/png"
    assert vision.sniff_mime(JPG) == "image/jpeg"
    assert vision.sniff_mime(b"GIF89a" + b"\x00" * 20) == "image/gif"
    assert vision.sniff_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert vision.sniff_mime(b"RIFF\x00\x00\x00\x00WAVEfmt ") is None
    assert vision.sniff_mime(b"not an image at all") is None
    assert vision.sniff_mime(b"") is None


def test_a_picture_carries_its_real_type_not_its_name() -> None:
    picture = vision.make_picture(PNG, mime="image/jpeg")
    assert picture is not None
    assert picture.mime == "image/png"
    assert picture.size == len(PNG)


def test_something_that_is_not_an_image_is_not_a_picture() -> None:
    assert vision.make_picture(b"%PDF-1.7\n" + b"\x00" * 40) is None
    assert vision.make_picture(b"") is None


def test_an_enormous_image_is_refused_before_it_is_sent() -> None:
    too_big = PNG + b"\x00" * (vision.MAX_IMAGE_BYTES + 1)
    assert vision.make_picture(too_big) is None


def test_the_part_is_openai_shaped_and_base64() -> None:
    picture = vision.make_picture(PNG)
    assert picture is not None
    part = picture.part()
    assert part["type"] == "image_url"
    url = part["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == PNG


# ---------------------------------------------------------------------------
# Finding the picture on a message
# ---------------------------------------------------------------------------
def test_a_voice_note_is_not_a_picture() -> None:
    class Voice(FakeMedia):
        voice = object()

    class Video(FakeMedia):
        video = object()

    assert not vision.looks_like_picture(Voice(document=object()))
    assert not vision.looks_like_picture(Video(document=object()))


def test_a_photo_is_a_picture() -> None:
    assert vision.looks_like_picture(FakeMedia(photo=object()))
    assert not vision.looks_like_picture(None)


class PictureClient(FakeClient):
    """A real fake client whose downloads return the bytes it was given."""

    def __init__(self, payload: bytes | Exception) -> None:
        super().__init__()
        self.payload = payload
        self.downloads = 0

    async def download_media(self, message, file=None, **kwargs):
        self.downloads += 1
        if isinstance(self.payload, Exception):
            raise self.payload
        if file is not None:
            file.write(self.payload)
            return file
        return self.payload


async def test_a_downloaded_photo_becomes_a_picture() -> None:
    client = PictureClient(PNG)
    picture = await vision.download(client, FakeMessage(id=1, media=FakeMedia(photo=object())))
    assert picture is not None and picture.mime == "image/png"
    assert client.downloads == 1


async def test_a_download_that_fails_returns_nothing() -> None:
    client = PictureClient(RuntimeError("FLOOD_WAIT_900"))
    assert await vision.download(client, FakeMessage(id=1)) is None
    assert await vision.download(None, FakeMessage(id=1)) is None


async def test_a_document_that_is_really_a_pdf_is_not_looked_at() -> None:
    client = PictureClient(b"%PDF-1.7\n" + b"\x00" * 100)
    assert await vision.download(client, FakeMessage(id=1)) is None


# ---------------------------------------------------------------------------
# What she does with it
# ---------------------------------------------------------------------------
async def _yura(runtime, client: PictureClient, provider: StubProvider):
    await runtime.store.set_feature("persona", True)
    await runtime.feature_manager.refresh()
    runtime.ai = provider
    runtime.client = client
    runtime.sender.client = client
    runtime.permissions.set_self_id(42)
    return runtime.persona_replies


def _photo_event(text: str, *, message_id: int = 901, chat_id: int = 42, out: bool = True):
    message = FakeMessage(
        id=message_id,
        chat_id=chat_id,
        sender_id=42,
        text=text,
        out=out,
        is_private=True,
        media=FakeMedia(photo=object()),
    )
    from havoc.event_engine import EventType, normalize_message

    return normalize_message(message, self_id=42, prefix=".", event_type=EventType.OUTGOING)


async def test_a_photo_reaches_the_model_as_a_picture(runtime) -> None:
    provider = StubProvider()
    client = PictureClient(PNG)
    replies = await _yura(runtime, client, provider)

    await replies.on_self_message(_photo_event("what is this"))

    assert provider.calls, "the model was never asked"
    part = _picture_part(provider.calls[-1])
    assert part is not None, "the photo was not sent to the model"
    assert part["image_url"]["url"].startswith("data:image/png;base64,")
    assert "what is this" in _all_text(provider.calls[-1])
    assert client.sent, "she looked at it and said nothing"


async def test_a_photo_with_no_words_is_still_a_question(runtime) -> None:
    provider = StubProvider()
    replies = await _yura(runtime, PictureClient(PNG), provider)

    await replies.on_self_message(_photo_event(""))

    assert provider.calls, "a captionless photo was ignored"
    assert _picture_part(provider.calls[-1]) is not None


async def test_an_unreadable_attachment_is_answered_in_words(runtime) -> None:
    """Not every document is a picture; refusing to look must not refuse to talk."""
    provider = StubProvider("that is a pdf, not a picture")
    replies = await _yura(runtime, PictureClient(b"%PDF-1.7\n" + b"\x00" * 80), provider)

    await replies.on_self_message(_photo_event("what is this"))

    assert client_sent(replies), "she went silent instead of answering in words"
    assert _picture_part(provider.calls[-1] if provider.calls else []) is None


def client_sent(replies) -> bool:
    client = replies.runtime.client
    return bool(getattr(client, "sent", []))


async def test_what_she_said_about_the_picture_can_be_revisited(runtime) -> None:
    provider = StubProvider("a ginger cat on a keyboard")
    replies = await _yura(runtime, PictureClient(PNG), provider)

    await replies.on_self_message(_photo_event("who is this"))

    stored = await runtime.yura_chat.last_picture(42)
    assert stored is not None
    assert "ginger cat" in stored["text"]


# ---------------------------------------------------------------------------
# Threading: the reply button continues the conversation
# ---------------------------------------------------------------------------
async def test_her_answer_is_threaded_under_the_message(runtime) -> None:
    provider = StubProvider("hello again")
    client = PictureClient(PNG)
    replies = await _yura(runtime, client, provider)

    await replies.on_self_message(_photo_event("what is this", message_id=905))

    assert client.sent[-1].kwargs.get("reply_to") == 905


async def test_the_message_she_replied_to_reaches_the_prompt(runtime) -> None:
    provider = StubProvider("yes, that one")
    client = PictureClient(PNG)
    replies = await _yura(runtime, client, provider)

    earlier = FakeMessage(id=777, chat_id=42, sender_id=42, text="look at this cat", out=True)
    message = FakeMessage(
        id=906,
        chat_id=42,
        sender_id=42,
        text="this one?",
        out=True,
        is_private=True,
        reply=earlier,
    )
    from havoc.event_engine import EventType, normalize_message

    event = normalize_message(message, self_id=42, prefix=".", event_type=EventType.OUTGOING)
    await replies.on_self_message(event)

    prompt = _all_text(provider.calls[-1])
    assert "look at this cat" in prompt
    assert "this one?" in prompt


async def test_someone_elses_message_is_not_folded_into_the_prompt(runtime) -> None:
    """Quoting a third party would drag their words into the prompt."""
    provider = StubProvider("hm")
    replies = await _yura(runtime, PictureClient(PNG), provider)

    theirs = FakeMessage(
        id=778, chat_id=42, sender_id=999, text="my private medical results", out=False
    )
    theirs.sender = FakeUser(user_id=999, first_name="Ali")
    message = FakeMessage(
        id=907,
        chat_id=42,
        sender_id=42,
        text="look at this",
        out=True,
        is_private=True,
        reply=theirs,
    )
    from havoc.event_engine import EventType, normalize_message

    event = normalize_message(message, self_id=42, prefix=".", event_type=EventType.OUTGOING)
    await replies.on_self_message(event)

    assert "medical results" not in _all_text(provider.calls[-1])


async def test_the_picture_command_describes_the_last_picture(runtime) -> None:
    """``.yura caption`` must talk about the photo she just saw, not a new one."""
    from conftest import invoke

    provider = StubProvider("still a cat")
    replies = await _yura(runtime, PictureClient(PNG), provider)
    await replies.on_self_message(_photo_event("who is this"))

    provider.reply = "i would caption it 'employee of the month'"
    await invoke(runtime, ".yura caption", chat_id=42, is_private=True)

    text = last_text(runtime)
    assert "employee of the month" in text


def test_a_fake_event_still_exists_for_the_helpers() -> None:
    """Keeps the shared FakeEvent import honest — other tests build on it."""
    assert FakeEvent is not None
    assert io.BytesIO is not None
