#!/usr/bin/env python3
"""Opt-in live comparison of downloader candidates on the hosted runner.

This talks to real public sites but never touches Telegram or real credentials.
Failures are recorded per candidate rather than presented as working features.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

YOUTUBE = "https://www.youtube.com/watch?v=wDchsz8nmbo"
PINTEREST = "https://www.pinterest.com/pin/385691155608427004/"
PINTEREST_IMAGE = "https://www.pinterest.com/pin/543809723775636824/"
INSTAGRAM = "https://www.instagram.com/reel/Chunk8-jurw/"
SPOTIFY = "https://open.spotify.com/track/7h3OzIhoGymiEBYDcqEtwc"


def attempt(name: str, args: list[str], directory: Path, timeout: int = 120) -> bool:
    start = time.monotonic()
    directory.mkdir()
    try:
        result = subprocess.run(args, cwd=directory, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"::warning::{name}: {type(exc).__name__}: {str(exc)[:200]}", flush=True)
        return False
    files = [p for p in directory.rglob("*") if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
    elapsed = time.monotonic() - start
    if result.returncode == 0 and files:
        largest = max(files, key=lambda path: path.stat().st_size)
        print(f"::notice::{name}: downloaded {largest.stat().st_size} bytes in {elapsed:.1f}s; file={largest.suffix}", flush=True)
        return True
    detail = " ".join((result.stderr or result.stdout).split())[-400:]
    print(f"::warning::{name}: no file (exit {result.returncode}, {elapsed:.1f}s): {detail}", flush=True)
    return False


async def attempt_yura_pinterest(name: str, url: str, suffix: str) -> bool:
    from havoc.services.downloader import discard, fetch, inspect

    path = None
    start = time.monotonic()
    try:
        item = await inspect(url)
        quality = item.quality(1)
        if item.site != "pinterest" or quality is None:
            raise ValueError("Pinterest item or quality missing")
        path = await fetch(item.url, quality, max_mb=30, timeout=90)
        if path.suffix.lower() != suffix or path.stat().st_size == 0:
            raise ValueError(f"unexpected media: {path.suffix}")
        print(f"::notice::{name}: Yura fetched {path.stat().st_size} bytes ({suffix}) in {time.monotonic() - start:.1f}s", flush=True)
        return True
    except Exception as exc:  # noqa: BLE001 - a probe reports real failures
        print(f"::warning::{name}: {type(exc).__name__}: {' '.join(str(exc).split())[:300]}", flush=True)
        return False
    finally:
        discard(path)


def inspect_gallery(name: str, url: str) -> None:
    """Record only safe metadata shapes, not cookies or private URLs."""
    try:
        result = subprocess.run(
            ["gallery-dl", "--config-ignore", "-j", url], capture_output=True, text=True, timeout=60
        )
        entries = json.loads(result.stdout) if result.returncode == 0 else []
        shapes = []
        for item in entries:
            if isinstance(item, list) and len(item) >= 3 and isinstance(item[-1], dict):
                data = item[-1]
                shapes.append({k: data.get(k) for k in ("extension", "filename", "title", "id", "width", "height") if k in data})
        print(f"::notice::{name} metadata: {json.dumps(shapes[:3], ensure_ascii=True)[:600]}", flush=True)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        print(f"::warning::{name} metadata unavailable: {type(exc).__name__}", flush=True)


def main() -> int:
    if not all(shutil.which(tool) for tool in ("yt-dlp", "gallery-dl", "spotdl")):
        print("::error::Candidate tools are not installed")
        return 1
    inspect_gallery("pinterest video", PINTEREST)
    inspect_gallery("pinterest image", PINTEREST_IMAGE)
    with tempfile.TemporaryDirectory(prefix="yura-media-probe-") as temp:
        root = Path(temp)
        results = {
            "yura_pinterest_video": asyncio.run(attempt_yura_pinterest("yura_pinterest_video", PINTEREST, ".mp4")),
            "yura_pinterest_image": asyncio.run(attempt_yura_pinterest("yura_pinterest_image", PINTEREST_IMAGE, ".jpg")),
            "youtube_node_ejs_pot": attempt(
                "youtube_node_ejs_pot",
                ["yt-dlp", "--no-playlist", "--no-progress", "--no-warnings", "--socket-timeout", "15", "--retries", "0", "--js-runtimes", "node", "--extractor-args", "youtube:player_client=mweb,android_vr", "-f", "best[height<=360]/best", "-o", "sample.%(ext)s", YOUTUBE],
                root / "youtube", 120,
            ),
            "pinterest_video_gallery_dl": attempt(
                "pinterest_video_gallery_dl", ["gallery-dl", "--config-ignore", "-d", ".", PINTEREST], root / "pinterest-video", 90,
            ),
            "pinterest_image_gallery_dl": attempt(
                "pinterest_image_gallery_dl", ["gallery-dl", "--config-ignore", "-d", ".", PINTEREST_IMAGE], root / "pinterest-image", 90,
            ),
            "instagram_ytdlp": attempt(
                "instagram_ytdlp", ["yt-dlp", "--no-playlist", "--no-progress", "--socket-timeout", "15", "--retries", "0", "-o", "sample.%(ext)s", INSTAGRAM], root / "instagram", 90,
            ),
            "instagram_gallery_dl": attempt(
                "instagram_gallery_dl", ["gallery-dl", "--config-ignore", "--retries", "0", "-d", ".", INSTAGRAM], root / "instagram-gallery", 90,
            ),
            "spotify_spotdl": attempt(
                "spotify_spotdl", ["spotdl", "download", SPOTIFY, "--output", "{title}.{output-ext}"], root / "spotify", 150,
            ),
        }
        print("candidate results: " + json.dumps(results, sort_keys=True), flush=True)
        # The job must remain red if every candidate fails, not turn warnings green.
        return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
