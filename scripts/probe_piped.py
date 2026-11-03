#!/usr/bin/env python3
"""Test public Piped APIs with a public YouTube video, without credentials."""
import json
import subprocess
import tempfile
import urllib.request
from pathlib import Path

APIS = ("https://pipedapi.kavin.rocks", "https://api.piped.private.coffee", "https://pipedapi.adminforge.de", "https://pipedapi.leptons.xyz")

for api in APIS:
    try:
        req = urllib.request.Request(f"{api}/streams/wDchsz8nmbo", headers={"User-Agent": "YuraMediaProbe/1.0"})
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.load(response)
        videos = [s for s in data.get("videoStreams", []) if not s.get("videoOnly") and s.get("url")]
        if not videos:
            print(f"::notice::{api}: no combined video/audio stream", flush=True)
            continue
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "video"
            with urllib.request.urlopen(videos[0]["url"], timeout=45) as response, path.open("wb") as out:
                size = 0
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > 50 * 1024 * 1024:
                        raise ValueError("stream exceeded limit")
                    out.write(chunk)
            result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(path)], capture_output=True, text=True, timeout=20, check=False)
            streams = json.loads(result.stdout).get("streams", []) if result.returncode == 0 else []
            kinds = {stream.get("codec_type") for stream in streams}
            if {"audio", "video"} <= kinds:
                print(f"::notice::{api}: downloaded valid combined stream ({size} bytes)", flush=True)
                raise SystemExit(0)
            print(f"::notice::{api}: no valid combined stream", flush=True)
    except Exception as exc:
        print(f"::notice::{api}: {type(exc).__name__}", flush=True)
print("::error::No Piped API produced a valid combined video/audio stream", flush=True)
raise SystemExit(1)
