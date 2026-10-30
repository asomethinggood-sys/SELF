"""Pictures the owner sends, turned into something a model can look at.

Two jobs, both small:

* **Fetch the bytes.** Telethon will download a photo into a file-like object,
  and the result has to be bounded — a 12 MP photo is several megabytes, and a
  request body that size is a timeout waiting to happen. The image is capped and
  the format sniffed from the bytes rather than trusted from the file name.
* **Describe it in the shape the chat APIs expect.** An ``image_url`` part with a
  ``data:`` URL is accepted by OpenRouter and by Google's OpenAI-compatible
  endpoint, so one shape serves every model this project can reach.

Why base64 and not a URL: Telegram's file links expire and are not fetchable by
a third party without a bot token, so the model would receive a dead link.

Feature area: 70 (optional providers), 96 (presentation).
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from typing import Any

#: Telegram compresses a photo to a JPEG before it reaches us; 4 MB is generous
#: for that and still small enough to send inside a JSON body without a timeout.
MAX_IMAGE_BYTES = 4 * 1024 * 1024
#: Guards against a document that claims to be an image and is not.
IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),  # checked further below: RIFF….WEBP
    (b"BM", "image/bmp"),
)

#: Media types worth trying to look at. A video or a voice note is not.
IMAGE_MEDIA_TYPES = frozenset({"photo", "document", "sticker", "animation"})
#: Telethon wraps a voice note, an audio file and a video in a *document*, so the
#: media type alone cannot rule them out — these attributes can.
NOT_A_PICTURE: tuple[str, ...] = ("voice", "audio", "video", "video_note", "gif")


def looks_like_picture(media: Any) -> bool:
    """True when the media container is worth downloading as an image."""
    if media is None:
        return False
    return not any(getattr(media, attribute, None) is not None for attribute in NOT_A_PICTURE)


@dataclass(frozen=True)
class Picture:
    """Bytes plus the mime type the bytes actually are."""

    data: bytes
    mime: str = "image/jpeg"

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def data_url(self) -> str:
        return f"data:{self.mime};base64,{base64.b64encode(self.data).decode('ascii')}"

    def part(self) -> dict[str, Any]:
        """The OpenAI-shaped content part for this picture."""
        return {"type": "image_url", "image_url": {"url": self.data_url}}


def sniff_mime(data: bytes) -> str | None:
    """The image type those bytes really are, or ``None`` when they are not one."""
    head = data[:16]
    for magic, mime in IMAGE_MAGIC:
        if head.startswith(magic):
            if magic == b"RIFF" and head[8:12] != b"WEBP":
                continue
            return mime
    return None


def make_picture(data: bytes, *, mime: str | None = None) -> Picture | None:
    """A :class:`Picture` for image bytes, or ``None`` if this is not an image.

    ``mime`` is only a hint: the bytes decide. Anything that is not recognisably
    an image, or is larger than :data:`MAX_IMAGE_BYTES`, is refused here instead
    of at the provider, where the error would look like a broken key.
    """
    if not data or len(data) > MAX_IMAGE_BYTES:
        return None
    detected = sniff_mime(data)
    if detected is None:
        return None
    if mime and mime.startswith("image/") and mime != detected:
        # A PNG named .jpg is still a PNG; the bytes win.
        return Picture(data, detected)
    return Picture(data, detected)


async def download(client: Any, message: Any, *, limit: int = MAX_IMAGE_BYTES) -> Picture | None:
    """Download the image attached to ``message``.

    Returns ``None`` for anything that is not an image or cannot be fetched —
    a photo the owner sent that we cannot read is a reason to answer in words,
    not a reason to crash the reply path.
    """
    if client is None or message is None:
        return None
    buffer = io.BytesIO()
    try:
        result = await client.download_media(message, file=buffer)
    except Exception:  # noqa: BLE001 - a missing photo must not lose the answer
        return None
    data = result if isinstance(result, (bytes, bytearray)) else buffer.getvalue()
    return make_picture(bytes(data or b""))
