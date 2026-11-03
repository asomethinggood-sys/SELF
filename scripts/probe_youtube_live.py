#!/usr/bin/env python3
"""Exercise the real yt-dlp downloader on a hosted runner, without Telegram.

This is an opt-in network probe, not a unit test and not a Telegram delivery
check. It inspects a public sample, downloads a video, and asks ffprobe whether
there is a recognizable video stream. YouTube may reject datacenter IPs; report
that failure rather than treating it as a successful download.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import time

from havoc.services.downloader import DownloadError, discard, fetch, inspect

URL = "https://www.youtube.com/watch?v=wDchsz8nmbo"


async def main() -> int:
    start = time.monotonic()
    path = None
    try:
        if not shutil.which("ffprobe"):
            raise RuntimeError("ffprobe is not installed")
        item = await inspect(URL, timeout=90)
        video = [quality for quality in item.qualities if not quality.audio_only]
        if not video:
            raise RuntimeError("inspection returned no video qualities")
        quality = min(video, key=lambda row: row.height)
        print(f"inspected: {item.title[:100]} · {quality.label}", flush=True)
        path = await fetch(item.url, quality, max_mb=100, timeout=180)
        check = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if check.returncode:
            raise RuntimeError(f"ffprobe rejected the downloaded file: {check.stderr[:300]}")
        streams = json.loads(check.stdout).get("streams", [])
        if not any(stream.get("codec_type") == "video" for stream in streams):
            raise RuntimeError("downloaded file contains no video stream")
        size = path.stat().st_size
        print(f"::notice::YouTube download probe passed: {size} bytes, {time.monotonic() - start:.1f}s; Telegram delivery not tested")
        return 0
    except (DownloadError, OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        message = " ".join(str(exc).split())[:500]
        print(f"::error::YouTube download probe failed ({type(exc).__name__}): {message}")
        return 1
    finally:
        discard(path)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
