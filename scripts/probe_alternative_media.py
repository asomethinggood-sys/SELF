#!/usr/bin/env python3
"""Opt-in real-network comparison; no Telegram session or credentials needed."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

YOUTUBE = "https://www.youtube.com/watch?v=wDchsz8nmbo"
SPOTIFY = "https://open.spotify.com/track/0Id4mflvticjdKDRkyEkWZ"


def notice(message: str, *, failure: bool = False) -> None:
    text = " ".join(message.split())[:450].replace("%", "%25")
    print(f"::{'error' if failure else 'notice'}::{text}", flush=True)


def probe_cobalt() -> bool:
    start = time.monotonic()
    request = urllib.request.Request(
        "http://127.0.0.1:9000/",
        json.dumps({"url": YOUTUBE, "videoQuality": "360"}).encode(),
        {"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=75) as response:
            result = json.load(response)
        status = result.get("status")
        if status not in ("redirect", "tunnel") or not result.get("url"):
            notice(f"Cobalt returned status {status}: {result.get('error', {})}", failure=True)
            return False
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "video"
            size = 0
            with urllib.request.urlopen(result["url"], timeout=90) as response, path.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > 50 * 1024 * 1024:
                        raise ValueError("Cobalt video exceeded 50 MB probe limit")
                    output.write(chunk)
            check = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(path)],
                capture_output=True, text=True, timeout=30, check=False,
            )
            streams = json.loads(check.stdout).get("streams", []) if check.returncode == 0 else []
            if not any(stream.get("codec_type") == "video" for stream in streams):
                notice("Cobalt returned bytes but ffprobe found no video", failure=True)
                return False
            notice(f"Cobalt downloaded a video: {size} bytes in {time.monotonic() - start:.1f}s")
            return True
    except urllib.error.HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", "replace")
        notice(f"Cobalt HTTP {exc.code}: {detail[:300]}", failure=True)
        return False
    except (OSError, ValueError, subprocess.TimeoutExpired, urllib.error.URLError) as exc:
        notice(f"Cobalt probe failed: {type(exc).__name__}: {str(exc)[:250]}", failure=True)
        return False


def probe_spotify() -> bool:
    start = time.monotonic()
    with tempfile.TemporaryDirectory() as temp:
        try:
            direct = subprocess.run(
                ["yt-dlp", "--no-playlist", "--no-progress", "--socket-timeout", "15", "--retries", "0", "-f", "bestaudio/best", "-o", "source.%(ext)s", "https://soundcloud.com/elektronomia-sc/sky-high"],
                cwd=temp, capture_output=True, text=True, timeout=90,
            )
            sources = list(Path(temp).glob("source.*"))
            if direct.returncode == 0 and sources:
                notice(f"Direct SoundCloud fetch succeeded: {sources[0].stat().st_size} bytes")
            else:
                notice(f"Direct SoundCloud fetch failed: {' '.join((direct.stderr or direct.stdout).split())[-250:]}", failure=True)
            for source in sources:
                source.unlink()
            result = subprocess.run(
                ["spotdl", "download", SPOTIFY, "--audio", "soundcloud", "bandcamp", "--output", "{title}.{output-ext}"],
                cwd=temp, capture_output=True, text=True, timeout=150,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            notice(f"Spotify matching failed: {type(exc).__name__}: {str(exc)[:200]}", failure=True)
            return False
        files = [p for p in Path(temp).rglob("*") if p.is_file() and p.suffix.lower() in (".mp3", ".m4a", ".opus", ".ogg")]
        if not files:
            detail = " ".join((result.stdout + " " + result.stderr).split())[-300:]
            notice(f"Spotify matching returned no audio (exit {result.returncode}): {detail}", failure=True)
            return False
        path = files[0]
        check = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30, check=False,
        )
        streams = json.loads(check.stdout).get("streams", []) if check.returncode == 0 else []
        if result.returncode or not any(s.get("codec_type") == "audio" for s in streams):
            notice("Spotify match did not produce valid audio", failure=True)
            return False
        notice(f"Spotify match produced {path.stat().st_size} bytes in {time.monotonic() - start:.1f}s (not Spotify's own audio)")
        return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("engine", choices=("cobalt", "spotify"))
    args = parser.parse_args()
    return 0 if (probe_cobalt() if args.engine == "cobalt" else probe_spotify()) else 1


if __name__ == "__main__":
    sys.exit(main())
