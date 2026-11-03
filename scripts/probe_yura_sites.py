#!/usr/bin/env python3
"""Opt-in real hosted downloader checks, without Telegram or credentials."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from havoc.services.downloader import discard, fetch, inspect, validate_file  # noqa: E402

SAMPLES = (
    ("Pinterest image", "https://www.pinterest.com/pin/543809723775636824/", "image"),
    ("Pinterest video", "https://www.pinterest.com/pin/385691155608427004/", "video"),
    ("YouTube", "https://www.youtube.com/watch?v=wDchsz8nmbo", "video"),
    ("Instagram", "https://www.instagram.com/reel/Chunk8-jurw/", "video"),
    ("TikTok", "https://www.tiktok.com/@astro_alexandra/video/7515209457870294318", "video"),
    ("Spotify match", "https://open.spotify.com/track/0Id4mflvticjdKDRkyEkWZ", "audio"),
)


async def main() -> int:
    failures = 0
    for name, url, expected in SAMPLES:
        path = None
        try:
            item = await inspect(url, timeout=75)
            if expected == "audio":
                if "not Spotify audio" not in item.source_note:
                    raise ValueError("missing source label")
                quality = next(q for q in item.qualities if q.audio_only)
            else:
                quality = next((q for q in item.qualities if 0 < q.height <= 360), item.qualities[0])
            path = await fetch(item.url, quality, max_mb=50, timeout=120)
            await validate_file(path, item, quality)
            proc = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,duration:format=duration", "-of", "json", str(path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                output, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            except (TimeoutError, asyncio.CancelledError):
                proc.kill()
                await proc.communicate()
                raise
            media = json.loads(output) if proc.returncode == 0 else {}
            streams = media.get("streams", [])
            stream_type = "video" if expected == "image" else expected
            stream = next((s for s in streams if s.get("codec_type") == stream_type), None)
            if not stream:
                raise ValueError("media did not contain the expected stream")
            if expected == "image" and stream.get("codec_name") not in {"mjpeg", "png", "webp", "gif"}:
                raise ValueError("expected an image but received another media type")
            if expected == "video":
                duration = (media.get("format") or {}).get("duration") or stream.get("duration") or 0
                if float(duration) < 0.5:
                    raise ValueError("video duration is absent or under half a second")
            print(f"::notice::{name}: validated {expected} ({stream.get('codec_name')}), {path.stat().st_size} bytes, quality {quality.label}", flush=True)
        except Exception as exc:
            failures += 1
            detail = " ".join(str(exc).split())[:250].replace("%", "%25")
            print(f"::warning::{name}: {type(exc).__name__}: {detail}", flush=True)
        finally:
            discard(path)
    if failures:
        print(f"::error::{failures} live downloader samples failed", flush=True)
    return int(bool(failures))


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
