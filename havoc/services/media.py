"""Resolving and validating media before anything is sent.

The bot may only send files the owner explicitly names. That sounds obvious and
is exactly where userbots get people into trouble, so the rules are explicit
and tested:

* **Local paths** must exist, be regular files, have an extension Telegram can
  handle, and stay under a size limit. Session files, databases, key material
  and dotfiles are refused even if the owner asks — a userbot that will send
  ``.session`` on request is a session-stealing tool one typo away.
* **URLs** must be http(s), without embedded credentials, and must not point at
  the machine the bot runs on (localhost, ``*.local``, ``*.internal``, any
  private, loopback, link-local or otherwise reserved IP). That stops the bot
  being used as a way to read internal services into a chat.
* **Nothing is downloaded by this module.** Telegram fetches a URL itself, so a
  hostile URL cannot make *this* process connect anywhere.
* Nothing here reads file contents, so secrets cannot leak through an error
  message either.

Feature area: 45 (media handling).
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

MAX_BYTES = 50 * 1024 * 1024  # Telegram's bot-API ceiling; generous for a userbot
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_SOURCES = 10  # one album
MAX_URL_LENGTH = 2048

#: Extension -> what Telegram will treat it as.
PHOTO_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp", ".heic", ".heif", ".tif", ".tiff"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".3gp"})
AUDIO_EXTENSIONS = frozenset({".mp3", ".ogg", ".oga", ".opus", ".flac", ".wav", ".m4a", ".aac"})
ANIMATION_EXTENSIONS = frozenset({".gif", ".webp"})
STICKER_EXTENSIONS = frozenset({".tgs", ".webp"})
DOCUMENT_EXTENSIONS = frozenset(
    {
        ".pdf", ".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml", ".log",
        ".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz", ".doc", ".docx", ".xls",
        ".xlsx", ".ppt", ".pptx", ".epub", ".rtf", ".srt", ".vtt", ".apk",
    }
)
ALLOWED_EXTENSIONS = (
    PHOTO_EXTENSIONS | VIDEO_EXTENSIONS | AUDIO_EXTENSIONS | ANIMATION_EXTENSIONS | STICKER_EXTENSIONS | DOCUMENT_EXTENSIONS
)

#: Files whose *name* makes them secrets even without an extension.
BLOCKED_NAMES = frozenset({".env", ".netrc", ".git-credentials", ".npmrc", ".pypirc", "id_rsa", "id_ed25519"})

#: Never sendable, whatever the extension says.
BLOCKED_EXTENSIONS = frozenset({".session", ".session-journal", ".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3", ".env", ".pem", ".key", ".p12", ".pfx", ".keystore"})

#: Hostnames that mean "the machine we are running on".
BLOCKED_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "metadata.google.internal", "instance-data"})


class MediaError(Exception):
    """Raised with a translation key plus optional detail for the owner."""

    def __init__(self, key: str, detail: str = "") -> None:
        self.key = key
        self.detail = detail
        super().__init__(key)


@dataclass(frozen=True)
class Media:
    """One validated thing to send."""

    value: str  # path as given, or the URL
    kind: str  # "photo" | "video" | "audio" | "animation" | "sticker" | "document" | "url"
    name: str = ""
    size: int = 0
    is_url: bool = False
    is_photo_limit: bool = False

    @property
    def label(self) -> str:
        return self.name or self.value

    def human_size(self) -> str:
        if not self.size:
            return "—"
        units = ("B", "KB", "MB", "GB")
        value = float(self.size)
        for unit in units:
            if value < 1024 or unit == units[-1]:
                return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024
        return f"{self.size} B"  # pragma: no cover - unreachable


def classify(extension: str) -> str:
    ext = extension.lower()
    if ext in BLOCKED_EXTENSIONS:
        raise MediaError("media.blocked_type", detail=ext)
    if ext in PHOTO_EXTENSIONS:
        return "photo" if ext not in ANIMATION_EXTENSIONS else "animation"
    if ext in ANIMATION_EXTENSIONS:
        return "animation"
    if ext in STICKER_EXTENSIONS:
        return "sticker"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    if ext in DOCUMENT_EXTENSIONS:
        return "document"
    raise MediaError("media.unsupported_type", detail=ext or "?")


def _is_public_host(host: str) -> bool:
    """False for anything that could address the runner's own network."""
    if not host:
        return False
    name = host.strip("[]").lower().rstrip(".")
    if name in BLOCKED_HOSTNAMES or name.endswith((".local", ".internal", ".localhost", ".localdomain")):
        return False
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return bool(re.fullmatch(r"[a-z0-9.\-]+", name)) and "." in name
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def validate_url(raw: str) -> str:
    if len(raw) > MAX_URL_LENGTH:
        raise MediaError("media.url_too_long")
    parts = urlsplit(raw)
    if parts.scheme not in {"http", "https"}:
        raise MediaError("media.bad_url", detail="scheme")
    if parts.username or parts.password:
        raise MediaError("media.bad_url", detail="credentials")
    if not _is_public_host(parts.hostname or ""):
        raise MediaError("media.bad_url", detail="host")
    return raw


def resolve(raw: str, *, max_bytes: int = MAX_BYTES, allow_urls: bool = True) -> Media:
    """Validate a single path or URL and describe what it is."""
    token = (raw or "").strip().strip('"').strip("'")
    if not token:
        raise MediaError("errors.missing_argument", detail="source")

    if token.lower().startswith(("http://", "https://")):
        if not allow_urls:
            raise MediaError("media.urls_disabled")
        return Media(value=validate_url(token), kind="url", is_url=True)

    if "\x00" in token:
        raise MediaError("media.not_found", detail="?")
    path = Path(os.path.expanduser(token))
    try:
        exists = path.exists()
    except OSError as exc:  # e.g. embedded null, absurd length
        raise MediaError("media.not_found", detail=type(exc).__name__) from exc
    if not exists:
        raise MediaError("media.not_found", detail=path.name or "?")
    if path.is_dir():
        raise MediaError("media.is_directory", detail=path.name)
    if not path.is_file():
        raise MediaError("media.not_a_file", detail=path.name)

    if path.name.lower() in BLOCKED_NAMES:
        raise MediaError("media.blocked_type", detail=path.name)
    extension = path.suffix
    if not extension:
        raise MediaError("media.unsupported_type", detail="?")
    kind = classify(extension)
    try:
        stat = path.stat()
    except OSError as exc:
        raise MediaError("media.not_found", detail=path.name) from exc
    if stat.st_size == 0:
        raise MediaError("media.empty_file", detail=path.name)
    limit = MAX_PHOTO_BYTES if kind in {"photo", "animation", "sticker"} else max_bytes
    if stat.st_size > limit:
        raise MediaError("media.too_large", detail=f"{path.name} ({stat.st_size} B)")
    return Media(
        value=str(path),
        kind=kind,
        name=path.name,
        size=stat.st_size,
        is_photo_limit=kind in {"photo", "animation", "sticker"},
    )


def resolve_many(raws: list[str], *, max_bytes: int = MAX_BYTES, allow_urls: bool = True) -> list[Media]:
    """Validate a list; every entry must pass, so nothing is half-sent."""
    if not raws:
        raise MediaError("errors.missing_argument", detail="source")
    if len(raws) > MAX_SOURCES:
        raise MediaError("media.too_many", detail=str(MAX_SOURCES))
    return [resolve(raw, max_bytes=max_bytes, allow_urls=allow_urls) for raw in raws]


def as_telegram_input(media: list[Media]) -> object:
    """What to hand to ``client.send_file``: one item or an album."""
    values = [item.value for item in media]
    return values[0] if len(values) == 1 else values


def summary(media: list[Media]) -> str:
    """A one-line description: ``photo sunset.jpg (1.2 MB)``."""
    return ", ".join(f"{item.kind} {item.label} ({item.human_size()})" for item in media)
