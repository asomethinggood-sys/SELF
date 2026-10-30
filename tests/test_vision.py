"""Pictures: reading the bytes, not the file name.

A photo the owner sends has to become a bounded, correctly typed request part.
The failure modes worth testing are the quiet ones — a document called
``photo.jpg`` that is really a zip, a 12 MP photo that would blow the request
body, a download that fails while she still owes an answer.
"""

from __future__ import annotations

import base64

import pytest

from havoc.services import vision
from havoc.services.vision import MAX_IMAGE_BYTES, Picture, download, make_picture, sniff_mime

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 128
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 128
GIF = b"GIF89a" + b"\x00" * 128
WEBP = b"RIFF" + (100).to_bytes(4, "little") + b"WEBP" + b"\x00" * 128
BMP = b"BM" + b"\x00" * 128


# ---------------------------------------------------------------------------
# Sniffing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (PNG, "image/png"),
        (JPEG, "image/jpeg"),
        (GIF, "image/gif"),
        (WEBP, "image/webp"),
        (BMP, "image/bmp"),
    ],
)
def test_it_recognises_the_formats_people_actually_send(data: bytes, expected: str) -> None:
    assert sniff_mime(data) == expected


def test_a_riff_that_is_not_a_webp_is_not_an_image() -> None:
    """A .wav file also starts with RIFF; only the brand makes it a picture."""
    wave = b"RIFF" + (100).to_bytes(4, "little") + b"WAVE" + b"\x00" * 64
    assert sniff_mime(wave) is None


@pytest.mark.parametrize(
    "data",
    [b"", b"hello world", b"PK\x03\x04zipfile", b"{\"json\": true}", b"<?xml version='1.0'?>"],
)
def test_something_that_is_not_a_picture_is_not_one(data: bytes) -> None:
    assert sniff_mime(data) is None


# ---------------------------------------------------------------------------
# Making a picture
# ---------------------------------------------------------------------------
def test_it_builds_a_picture_from_bytes() -> None:
    picture = make_picture(PNG)
    assert picture is not None
    assert picture.mime == "image/png"
    assert picture.size == len(PNG)


def test_a_wrong_name_does_not_win_over_the_bytes() -> None:
    """A PNG uploaded as ``photo.jpg`` is still a PNG."""
    picture = make_picture(PNG, mime="image/jpeg")
    assert picture is not None
    assert picture.mime == "image/png"


def test_a_matching_hint_changes_nothing() -> None:
    picture = make_picture(JPEG, mime="image/jpeg")
    assert picture is not None and picture.mime == "image/jpeg"


def test_an_oversized_picture_is_refused_here_not_at_the_provider() -> None:
    big = PNG + b"\x00" * MAX_IMAGE_BYTES
    assert make_picture(big) is None


def test_an_empty_payload_is_refused() -> None:
    assert make_picture(b"") is None


def test_a_non_image_is_refused() -> None:
    assert make_picture(b"plain text pretending to be a photo") is None


# ---------------------------------------------------------------------------
# The shape the model receives
# ---------------------------------------------------------------------------
def test_the_part_is_an_openai_style_image_url() -> None:
    part = Picture(PNG, "image/png").part()
    assert part["type"] == "image_url"
    assert part["image_url"]["url"].startswith("data:image/png;base64,")


def test_the_data_url_round_trips_to_the_original_bytes() -> None:
    url = Picture(PNG, "image/png").data_url
    payload = url.split(",", 1)[1]
    assert base64.b64decode(payload) == PNG


# ---------------------------------------------------------------------------
# Downloading
# ---------------------------------------------------------------------------
class _Client:
    """A Telethon client that fills the buffer it is handed."""

    def __init__(self, payload: bytes = PNG, *, raises: bool = False) -> None:
        self.payload = payload
        self.raises = raises
        self.calls = 0

    async def download_media(self, message, file=None, **kwargs):
        self.calls += 1
        if self.raises:
            raise RuntimeError("file reference expired")
        file.write(self.payload)
        return file


async def test_it_downloads_a_photo_into_a_picture() -> None:
    client = _Client()
    picture = await download(client, object())
    assert picture is not None and picture.data == PNG


async def test_a_failed_download_is_not_an_exception() -> None:
    assert await download(_Client(raises=True), object()) is None


async def test_a_document_that_is_not_an_image_downloads_to_nothing() -> None:
    assert await download(_Client(b"not an image at all"), object()) is None


async def test_no_client_or_no_message_is_just_nothing() -> None:
    assert await download(None, object()) is None
    assert await download(_Client(), None) is None


class _Media:
    """A Telethon media container: a document that may also be a voice note."""

    def __init__(self, **attributes: object) -> None:
        self.document = object()
        for name, value in attributes.items():
            setattr(self, name, value)


def test_a_photo_container_is_a_picture() -> None:
    assert vision.looks_like_picture(_Media()) is True


def test_a_voice_note_is_not_a_picture_even_though_it_is_a_document() -> None:
    """Telethon wraps voice notes in documents, so the document flag is not enough."""
    assert vision.looks_like_picture(_Media(voice=object())) is False


@pytest.mark.parametrize("attribute", ["audio", "video", "video_note"])
def test_other_document_media_is_not_a_picture(attribute: str) -> None:
    assert vision.looks_like_picture(_Media(**{attribute: object()})) is False


def test_no_media_is_not_a_picture() -> None:
    assert vision.looks_like_picture(None) is False


def test_video_and_voice_are_not_pictures() -> None:
    assert "photo" in vision.IMAGE_MEDIA_TYPES
    assert "video" not in vision.IMAGE_MEDIA_TYPES
    assert "voice" not in vision.IMAGE_MEDIA_TYPES
