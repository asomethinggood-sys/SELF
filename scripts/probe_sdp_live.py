#!/usr/bin/env python3
"""Run upstream SDP's actual download functions against public sample URLs.

The upstream checkout is supplied as argv[1]; no Telegram account is used.
"""
import asyncio
import os
import sys
import time
import urllib.request
from pathlib import Path

root = Path(sys.argv[1]).resolve()
sys.path[:0] = [str(root), str(root / "tests")]
from helper import load_bot  # noqa: E402


async def main():
    bot = load_bot(BOT_LANG="en", POT_PROVIDER_URL="http://127.0.0.1:4416")
    samples = (
        ("YouTube video", "https://www.youtube.com/watch?v=wDchsz8nmbo"),
        ("Pinterest video", "https://www.pinterest.com/pin/385691155608427004/"),
        ("Pinterest image", "https://www.pinterest.com/pin/543809723775636824/"),
    )
    for label, url in samples:
        started = time.monotonic()
        path = None
        try:
            path, reason = await asyncio.wait_for(bot.ytdlp_download(url, 360), timeout=120)
            if path:
                valid = await asyncio.wait_for(bot.validate_media(path, deep=False), timeout=90)
                print(f"::notice::{label}: SDP yt-dlp produced {path.stat().st_size} bytes, validation={valid} in {time.monotonic()-started:.1f}s", flush=True)
            else:
                photos = await asyncio.wait_for(bot.ytdlp_photos(url), timeout=70)
                print(f"::warning::{label}: SDP yt-dlp failed ({reason}); photo URLs={len(photos)}", flush=True)
                if label == "Pinterest image" and photos:
                    photo_url = photos[0] if isinstance(photos[0], str) else photos[0].get("url", "")
                    def read_photo(url=photo_url):
                        with urllib.request.urlopen(url, timeout=30) as response:
                            return response.read(50 * 1024 * 1024 + 1)
                    picture = await asyncio.to_thread(read_photo)
                    print(f"::notice::SDP Pinterest photo: {len(picture)} bytes, JPEG={picture.startswith(bytes.fromhex('ffd8ff'))}", flush=True)
        except Exception as exc:
            print(f"::warning::{label}: {type(exc).__name__}", flush=True)
        finally:
            if path:
                bot.cleanup(path)
    try:
        query = await asyncio.wait_for(bot.resolve_track_title("https://open.spotify.com/track/0Id4mflvticjdKDRkyEkWZ"), timeout=30)
        print(f"::notice::Spotify public metadata lookup: {'resolved' if query else 'failed'}", flush=True)
        if query:
            path, reason = await asyncio.wait_for(bot.ytdlp_audio("ytsearch1:" + query), timeout=120)
            if path:
                print(f"::notice::Spotify YouTube match: {path.stat().st_size} bytes (not Spotify audio)", flush=True)
                bot.cleanup(path)
            else:
                print(f"::warning::Spotify YouTube match failed: {reason}", flush=True)
    except Exception as exc:
        print(f"::warning::Spotify matching: {type(exc).__name__}", flush=True)


if __name__ == "__main__":
    os.environ.pop("BOT_TOKEN", None)
    asyncio.run(main())
