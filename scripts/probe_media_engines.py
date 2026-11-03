#!/usr/bin/env python3
"""Opt-in live comparison of downloader candidates on the hosted runner.

This talks to real public sites but never touches Telegram or real credentials.
Failures are recorded per candidate rather than presented as working features.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

YOUTUBE = "https://www.youtube.com/watch?v=wDchsz8nmbo"
PINTEREST = "https://www.pinterest.com/pin/385691155608427004/"
PINTEREST_IMAGE = "https://www.pinterest.com/pin/1900024838646360/"
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


def main() -> int:
    if not all(shutil.which(tool) for tool in ("yt-dlp", "gallery-dl", "spotdl")):
        print("::error::Candidate tools are not installed")
        return 1
    with tempfile.TemporaryDirectory(prefix="yura-media-probe-") as temp:
        root = Path(temp)
        results = {
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
            "spotify_spotdl": attempt(
                "spotify_spotdl", ["spotdl", "download", SPOTIFY, "--output", "{title}.{output-ext}"], root / "spotify", 150,
            ),
        }
        print("candidate results: " + json.dumps(results, sort_keys=True), flush=True)
        # The job must remain red if every candidate fails, not turn warnings green.
        return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
