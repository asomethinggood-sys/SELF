"""What a link points at, and how to get it onto the disk.

A link in a chat is a *request*, and the request has a shape: pick a quality,
maybe just the sound, then hand over the file. This module is the part that
talks to ``yt-dlp`` (YouTube, TikTok, Instagram and everything else it knows)
and nothing else — no Telegram, no formatting, no decisions about what the
owner is allowed to ask for.

Two rules carried over from the rest of the project:

* **A download is a job, not a pause.** ``yt-dlp`` blocks, so every call here is
  ``asyncio.to_thread``. A 200 MB video must not stop the userbot from answering
  *ping* for two minutes, which is exactly what happened when a fetch ran inline.
* **Nothing is guessed.** If the site refuses, if the file is bigger than the
  ceiling, or if the format is not there, the caller gets a typed error with a
  sentence for the owner — never a half-written file that Telegram then rejects.

The library is optional at import time: a deployment without ``yt-dlp`` still
boots, and says "the downloader is not installed" instead of crashing on a link.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _ceiling() -> int:
    """The largest file worth fetching, in MB.

    A user account may send 2 GB, but a GitHub runner has ~14 GB of disk and a
    timeout, so the ceiling is smaller on purpose — and a video that arrives
    after the runner is gone is not a video at all. ``DOWNLOADER_MAX_MB`` moves
    it without a code change; a nonsense value falls back rather than crashing
    the import of the whole service.
    """
    try:
        value = int(str(os.environ.get("DOWNLOADER_MAX_MB", "")).strip() or 0)
    except ValueError:
        return 700
    if value < 10:
        return 700
    return min(value, 2000)


MAX_MB = _ceiling()
#: yt-dlp is chatty and one bad link can be slow. Both bounds are generous
#: enough for a real video and small enough that a stuck socket does not hold a
#: worker for the rest of the run.
INSPECT_TIMEOUT = 60.0
DOWNLOAD_TIMEOUT = 900.0

#: The three he named, plus the shapes they actually use in the wild (short
#: links, mobile hosts, reels). A link that is not one of these is *not* a
#: download: it is a sentence with a URL in it, and she should answer it.
SITE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "youtube",
        re.compile(
            r"^(?:https?://)?(?:www\.|m\.|music\.)?"
            r"(?:youtube\.com/(?:watch\?|shorts/|live/|embed/)|youtu\.be/)",
            re.I,
        ),
    ),
    ("tiktok", re.compile(r"^(?:https?://)?(?:www\.|m\.|vm\.|vt\.)?(?:tiktok|douyin)\.com/", re.I)),
    ("instagram", re.compile(r"^(?:https?://)?(?:www\.|m\.)?(?:instagram\.com|instagr\.am)/(?:reel|reels|p|tv)/", re.I)),
    ("pinterest", re.compile(r"^https?://(?:(?:[a-z]{2,3}|www)\.)?pinterest\.com/pin/|^https?://pin\.it/", re.I)),
    ("twitter", re.compile(r"^(?:https?://)?(?:www\.|mobile\.)?(?:twitter|x)\.com/\w+/status/", re.I)),
    ("vimeo", re.compile(r"^(?:https?://)?(?:www\.|player\.)?vimeo\.com/(?:video/)?\d+", re.I)),
    ("reddit", re.compile(r"^(?:https?://)?(?:(?:www\.|old\.)?reddit\.com/r/[^/]+/comments/|redd\.it/)", re.I)),
    # The named services in SDP's registry. Keep these host-specific: a generic
    # webpage extractor would turn an ordinary URL into an unexpected download.
    ("facebook", re.compile(r"^https?://(?:www\.|m\.|web\.)?(?:facebook\.com|fb\.watch)/", re.I)),
    ("tumblr", re.compile(r"^https?://(?:www\.|[a-z0-9-]+\.)?tumblr\.com/", re.I)),
    ("snapchat", re.compile(r"^https?://(?:www\.)?snapchat\.com/", re.I)),
    ("bluesky", re.compile(r"^https?://(?:www\.)?bsky\.app/", re.I)),
    ("dailymotion", re.compile(r"^https?://(?:www\.)?(?:dailymotion\.com|dai\.ly)/", re.I)),
    ("bilibili", re.compile(r"^https?://(?:www\.|m\.)?(?:bilibili\.(?:com|tv)|b23\.tv)/", re.I)),
    ("twitch", re.compile(r"^https?://(?:www\.|m\.|clips\.)?twitch\.tv/", re.I)),
    ("soundcloud", re.compile(r"^https?://(?:www\.|m\.)?(?:soundcloud\.com|snd\.sc)/", re.I)),
    ("bandcamp", re.compile(r"^https?://(?:www\.|[a-z0-9-]+\.)?bandcamp\.com/", re.I)),
    ("spotify", re.compile(r"^https?://open\.spotify\.com/(?:intl-[a-z]+/)?track/[a-z0-9]+(?:[/?#]|$)", re.I)),
    ("deezer", re.compile(r"^https?://(?:www\.)?deezer\.com/(?:[a-z]{2}/)?track/\d+(?:[/?#]|$)", re.I)),
)
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)


class DownloadError(Exception):
    """A fetch that failed in a way the owner should be told about."""

    def __init__(self, message: str, *, kind: str = "failed", hint: str = "") -> None:
        self.kind = kind
        self.hint = hint
        super().__init__(message)


def installed() -> bool:
    """True when ``yt-dlp`` can actually be imported in this process."""
    try:
        import yt_dlp  # noqa: F401
    except Exception:  # noqa: BLE001 - a broken install is the same as none
        return False
    return True


def has_ffmpeg() -> bool:
    """Merging a video stream with an audio one needs it; audio-only needs it too."""
    return shutil.which("ffmpeg") is not None


def site_of(url: str) -> str:
    """The site this URL belongs to, or ``""`` when it is not a download."""
    text = str(url or "").strip()
    if not text:
        return ""
    for name, pattern in SITE_PATTERNS:
        if pattern.match(text):
            return name
    return ""


def find_link(text: str) -> str:
    """The first supported link inside a message, or ``""``.

    One link per message on purpose: a message with two links is a message about
    two things, and guessing which one to fetch is how the wrong video arrives.
    """
    for match in URL_RE.finditer(str(text or "")):
        url = match.group(0).rstrip(").,;!»”\"'")
        if site_of(url):
            return url
    return ""


def is_link(text: str) -> bool:
    """True when the whole message is one supported link (and nothing else)."""
    stripped = " ".join(str(text or "").split())
    if not stripped:
        return False
    found = find_link(stripped)
    if not found:
        return False
    # "look at this https://…" is a sentence, not an order to fetch.
    return stripped in {found, f"{found}/"} or stripped.rstrip("/") == found.rstrip("/")


@dataclass
class Quality:
    """One choice on the menu: a height, or the sound on its own."""

    index: int
    label: str
    height: int = 0
    audio_only: bool = False
    #: Bytes, when the site is willing to say. 0 means "unknown until it runs".
    filesize: int = 0

    @property
    def selector(self) -> str:
        """The yt-dlp format string this row means."""
        if self.audio_only:
            return "bestaudio/best"
        # Prefer a stream that already has sound (no ffmpeg needed), then the
        # best video at this height merged with the best audio, then anything.
        return (
            f"best[height<={self.height}][acodec!=none][vcodec!=none]/"
            f"bestvideo[height<={self.height}]+bestaudio/"
            f"best[height<={self.height}]/best"
        )


@dataclass
class Item:
    """What the site said about a link, and the menu built from it."""

    url: str
    site: str
    title: str = ""
    uploader: str = ""
    duration: float = 0.0
    thumbnail: str = ""
    #: Search matches from another provider are not the source's own audio.
    source_note: str = ""
    qualities: list[Quality] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    #: Some sites hand back a whole playlist; only the first entry is offered.
    entries: int = 1

    @property
    def minutes(self) -> str:
        if not self.duration:
            return ""
        total = int(self.duration)
        return f"{total // 60}:{total % 60:02d}"

    def quality(self, number: int) -> Quality | None:
        for row in self.qualities:
            if row.index == number:
                return row
        return None


#: How many heights to offer. Five is a menu; ten is a wall.
MAX_ROWS = 5


def _heights(info: dict[str, Any]) -> list[int]:
    """The distinct video heights the site actually has, tallest first."""
    seen: set[int] = set()
    for fmt in info.get("formats") or ():
        if str(fmt.get("vcodec") or "none") == "none":
            continue
        height = fmt.get("height")
        try:
            height = int(height or 0)
        except (TypeError, ValueError):
            continue
        if 120 <= height <= 4320:
            seen.add(height)
    top = sorted(seen, reverse=True)[:MAX_ROWS]
    # Round down to the familiar steps people recognise from the site itself.
    out: list[int] = []
    for height in top:
        for step in (4320, 2160, 1440, 1080, 720, 480, 360, 240, 144):
            if height >= step and step not in out:
                out.append(step)
                break
    return sorted(set(out), reverse=True) or [720]


def _size_for(info: dict[str, Any], height: int) -> int:
    """The largest file at or below ``height`` the site reported, in bytes."""
    best = 0
    for fmt in info.get("formats") or ():
        try:
            fmt_height = int(fmt.get("height") or 0)
        except (TypeError, ValueError):
            continue
        if fmt_height and fmt_height > height:
            continue
        size = int(fmt.get("filesize") or fmt.get("filesize_approx") or 0)
        best = max(best, size)
    return best


def build_qualities(info: dict[str, Any]) -> list[Quality]:
    """The numbered menu for one item, sound-only last."""
    rows: list[Quality] = []
    index = 1
    for height in _heights(info):
        rows.append(
            Quality(
                index=index,
                label=f"{height}p",
                height=height,
                filesize=_size_for(info, height),
            )
        )
        index += 1
    rows.append(Quality(index=index, label="sound only", audio_only=True, filesize=_estimated_audio(info)))
    return rows


def _estimated_audio(info: dict[str, Any]) -> int:
    """Roughly what the audio track weighs: bitrate x duration, when known."""
    for fmt in info.get("formats") or ():
        if str(fmt.get("vcodec") or "none") != "none":
            continue
        size = int(fmt.get("filesize") or fmt.get("filesize_approx") or 0)
        if size:
            return size
    return 0


def _options(outdir: Path, *, max_mb: int = MAX_MB) -> dict[str, Any]:
    return {
        "outtmpl": str(outdir / "%(title).80B.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "max_filesize": max(1, max_mb) * 1024 * 1024,
        "restrictfilenames": False,
        "windowsfilenames": False,
        "retries": 2,
        "fragment_retries": 2,
        "socket_timeout": 30,
        # Instagram and TikTok serve some content only to a browser-shaped
        # request; without this they answer 403 and it looks like a broken bot.
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        },
    }


def _clean_title(raw: Any) -> str:
    title = " ".join(str(raw or "").split())
    return title[:180] or "untitled"


def _inspect_sync(url: str) -> dict[str, Any]:
    import yt_dlp

    with yt_dlp.YoutubeDL({**_options(Path(tempfile.gettempdir())), "skip_download": True}) as ydl:
        return ydl.extract_info(url, download=False)


async def _gallery_run(args: list[str], *, timeout: float) -> tuple[int, bytes, bytes]:
    """Run gallery-dl without a shell; terminate it on timeout or cancellation."""
    if not shutil.which("gallery-dl"):
        raise DownloadError("gallery-dl is not installed", kind="not_installed")
    process = await asyncio.create_subprocess_exec(
        "gallery-dl", "--config-ignore", "--retries", "1", "--http-timeout", "20", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.communicate()
        raise
    return process.returncode or 0, stdout, stderr


async def _inspect_pinterest(url: str, *, timeout: float) -> Item:
    try:
        # -j stops at intermediary URLs (including pin.it redirects) and emits
        # a queue entry rather than media. -J follows those entries before
        # printing metadata, just as gallery-dl's download mode does.
        status, stdout, stderr = await _gallery_run(["--range", "1", "-J", url], timeout=timeout)
    except TimeoutError as exc:
        raise DownloadError("Pinterest did not answer in time", kind="timeout") from exc
    if status:
        detail = " ".join(stderr.decode("utf-8", "replace").split())[:200]
        raise DownloadError(detail or "Pinterest refused the link", kind="unavailable")
    try:
        entries = json.loads(stdout)
    except (ValueError, UnicodeDecodeError) as exc:
        raise DownloadError("Pinterest returned unreadable metadata", kind="unavailable") from exc
    entries = entries if isinstance(entries, list) else []
    media = next(
        (entry[-1] for entry in entries if isinstance(entry, list) and len(entry) >= 3
         and entry[0] == 3 and isinstance(entry[-1], dict)),
        None,
    )
    if not media:
        raise DownloadError("Pinterest returned no downloadable media", kind="unavailable")
    title = _clean_title(media.get("title") or media.get("description") or f"Pinterest pin {media.get('id', '')}")
    return Item(
        url=url, site="pinterest", title=title,
        qualities=[Quality(index=1, label="original")],
    )


async def _music_query(url: str, site: str, *, timeout: float) -> str:
    """Read public oEmbed metadata, as SDP does; never access protected audio."""
    from .http import get_json

    endpoint = "https://open.spotify.com/oembed" if site == "spotify" else "https://api.deezer.com/oembed"
    try:
        data = await get_json(endpoint, params={"url": url}, timeout=min(timeout, 20.0))
    except Exception as exc:  # noqa: BLE001 - remote metadata providers fail independently
        raise DownloadError(f"{site.title()} track metadata is unavailable", kind="unavailable") from exc
    if not isinstance(data, dict) or not isinstance(data.get("title"), str) or not data["title"].strip():
        raise DownloadError(f"{site.title()} returned no track title", kind="unavailable")
    author = data.get("author_name") if isinstance(data.get("author_name"), str) else ""
    return " ".join((data["title"] + " " + author).split())[:300]


async def inspect(url: str, *, timeout: float = INSPECT_TIMEOUT) -> Item:
    """Ask the site what a link is; Spotify/Deezer search for a YouTube match."""
    site = site_of(url)
    if site == "pinterest":
        return await _inspect_pinterest(url, timeout=timeout)
    if not installed():
        raise DownloadError(
            "yt-dlp is not installed, so there is nothing here to download with",
            kind="not_installed",
        )
    source_note = ""
    lookup_url = url
    if site in {"spotify", "deezer"}:
        query = await _music_query(url, site, timeout=timeout)
        lookup_url = f"ytsearch1:{query}"
        source_note = f"YouTube match for {site.title()} — may differ; not {site.title()} audio"
    try:
        info = await asyncio.wait_for(asyncio.to_thread(_inspect_sync, lookup_url), timeout=timeout)
    except TimeoutError as exc:
        raise DownloadError("the site did not answer in time", kind="timeout") from exc
    except Exception as exc:  # noqa: BLE001 - yt-dlp raises a zoo of its own errors
        message = " ".join(str(exc).split())[:200]
        raise DownloadError(message or "the site refused the link", kind="unavailable") from exc
    if not isinstance(info, dict):
        raise DownloadError("the site returned nothing for that link", kind="unavailable")
    entries = info.get("entries")
    if entries is not None:
        first = next((entry for entry in entries if entry), None)
        if not isinstance(first, dict):
            raise DownloadError("no playable match was found" if source_note else "that link is a playlist with nothing playable", kind="unavailable")
        info = first
    resolved = str(info.get("webpage_url") or "")
    if source_note and site_of(resolved) != "youtube":
        raise DownloadError("no playable YouTube match was found", kind="unavailable")
    return Item(
        url=resolved or url,
        site=site or site_of(resolved),
        title=_clean_title(info.get("title")),
        uploader=" ".join(str(info.get("uploader") or info.get("channel") or "").split())[:80],
        duration=float(info.get("duration") or 0),
        thumbnail=str(info.get("thumbnail") or ""),
        source_note=source_note,
        qualities=[Quality(index=1, label="sound only", audio_only=True, filesize=_estimated_audio(info))] if source_note else build_qualities(info),
        raw={k: info.get(k) for k in ("id", "ext", "webpage_url", "channel") if info.get(k)},
    )


def _download_sync(url: str, quality: Quality, outdir: Path, *, max_mb: int) -> str:
    import yt_dlp

    options = _options(outdir, max_mb=max_mb)
    options["format"] = quality.selector
    if quality.audio_only:
        # A soundtrack, not a video with the picture turned off: Telegram shows
        # an m4a as a playable track, and the file is a fraction of the size.
        options["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "m4a", "preferredquality": "0"}
        ]
    else:
        options["merge_output_format"] = "mp4"
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
    return path


async def _fetch_pinterest(url: str, *, max_mb: int, timeout: float) -> Path:
    outdir = Path(tempfile.mkdtemp(prefix="havoc-dl-"))
    try:
        status, _, stderr = await _gallery_run(
            ["--range", "1", "--filesize-max", f"{max(1, max_mb)}M", "-D", str(outdir), url],
            timeout=timeout,
        )
        if status:
            detail = " ".join(stderr.decode("utf-8", "replace").split())[:200]
            raise DownloadError(detail or "Pinterest refused the download", kind="failed")
        files = [path for path in outdir.rglob("*") if path.is_file() and not path.name.endswith(".part")]
        if len(files) != 1:
            raise DownloadError("Pinterest returned no file within the size limit", kind="failed")
        path = files[0]
        size = path.stat().st_size
        if size == 0 or size > max(1, max_mb) * 1024 * 1024:
            raise DownloadError("the file is empty or exceeds the size limit", kind="too_big")
        return path
    except TimeoutError as exc:
        _rmtree(outdir)
        raise DownloadError("the Pinterest download took too long", kind="timeout") from exc
    except BaseException:
        _rmtree(outdir)
        raise


async def fetch(
    url: str,
    quality: Quality,
    *,
    max_mb: int = MAX_MB,
    timeout: float = DOWNLOAD_TIMEOUT,
) -> Path:
    """Download one quality into a fresh directory and return the file.

    The directory belongs to the caller (it is removed when the send is done),
    so a cancelled download cannot leave a half-file behind in ``data/``.
    """
    if site_of(url) == "pinterest":
        return await _fetch_pinterest(url, max_mb=max_mb, timeout=timeout)
    if not installed():
        raise DownloadError("yt-dlp is not installed", kind="not_installed")
    outdir = Path(tempfile.mkdtemp(prefix="havoc-dl-"))
    try:
        raw = await asyncio.wait_for(
            asyncio.to_thread(_download_sync, url, quality, outdir, max_mb=max_mb),
            timeout=timeout,
        )
    except TimeoutError as exc:
        _rmtree(outdir)
        raise DownloadError("the download took too long and was stopped", kind="timeout") from exc
    except Exception as exc:  # noqa: BLE001
        _rmtree(outdir)
        message = " ".join(str(exc).split())[:200]
        raise DownloadError(message or "the download failed", kind="failed") from exc
    path = Path(raw)
    if not path.exists():
        # A postprocessor renames the file (mp4 merge, m4a extract): take the
        # biggest thing in the directory rather than guessing the new name.
        candidates = sorted(outdir.glob("*"), key=lambda p: p.stat().st_size, reverse=True)
        if not candidates:
            _rmtree(outdir)
            raise DownloadError("nothing arrived", kind="failed")
        path = candidates[0]
    limit = max(1, max_mb) * 1024 * 1024
    size = path.stat().st_size
    if size > limit:
        _rmtree(outdir)
        raise DownloadError(
            f"the file is {size // (1024 * 1024)} MB, over the {max_mb} MB ceiling",
            kind="too_big",
        )
    return path


async def validate_file(path: Path, item: Item, quality: Quality, *, timeout: float = 30.0) -> None:
    """Reject empty or corrupt files, and still images returned for video links.

    ffprobe reads the actual downloaded bytes, not the extractor's metadata.
    Pinterest images and photo posts remain valid; reels and video URLs must
    contain real moving media. Silent videos are allowed.
    """
    if not path.is_file() or path.stat().st_size <= 0:
        raise DownloadError("the downloaded file is empty", kind="failed")
    if not shutil.which("ffprobe"):
        raise DownloadError("ffprobe is required to verify downloaded media", kind="not_installed")
    try:
        process = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,duration:format=duration",
            "-of", "json", str(path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        raise DownloadError("could not start the media verifier", kind="failed") from exc
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except (TimeoutError, asyncio.CancelledError) as exc:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.communicate()
        if isinstance(exc, TimeoutError):
            raise DownloadError("media verification timed out", kind="timeout") from exc
        raise
    try:
        data = json.loads(output) if process.returncode == 0 else {}
        streams = data.get("streams", [])
        kinds = {s.get("codec_type") for s in streams if isinstance(s, dict)}
    except (ValueError, TypeError, AttributeError) as exc:
        raise DownloadError("downloaded media could not be verified", kind="failed") from exc
    if quality.audio_only:
        if "audio" not in kinds:
            raise DownloadError("the download contains no playable audio", kind="failed")
        return
    if not kinds.intersection({"audio", "video"}):
        raise DownloadError("the downloaded media is corrupt or unsupported", kind="failed")
    motion = item.site == "youtube" or (
        item.site == "instagram" and re.search(r"/(?:reel|reels|tv)/", item.url, re.I)
    ) or (item.site == "tiktok" and "/video/" in item.url.lower())
    if motion:
        video = next((s for s in streams if isinstance(s, dict) and s.get("codec_type") == "video"), None)
        if not video:
            raise DownloadError("the download contains no video", kind="failed")
        duration = (data.get("format") or {}).get("duration") or video.get("duration")
        try:
            seconds = float(duration) if duration is not None else None
        except (ValueError, TypeError):
            seconds = None
        if (seconds is not None and seconds < 0.5) or (seconds is None and video.get("codec_name") in {"mjpeg", "png", "webp", "gif"}):
            raise DownloadError("the site returned a still image instead of a video", kind="failed")


def _rmtree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def discard(path: Path | None) -> None:
    """Delete a downloaded file and the directory that held it."""
    if path is None:
        return
    _rmtree(Path(path).parent)


async def thumbnail(url: str, *, timeout: float = 20.0) -> bytes:
    """The preview picture, or ``b""``.

    Best effort on purpose: a menu without a picture is still a menu, and the
    picture is a CDN fetch that fails for reasons that are not the owner's.
    """
    if not url:
        return b""
    from havoc.services.http import HttpError, get_bytes

    try:
        return await get_bytes(url, timeout=timeout)
    except HttpError:
        return b""
    except Exception:  # noqa: BLE001 - see the docstring
        return b""
