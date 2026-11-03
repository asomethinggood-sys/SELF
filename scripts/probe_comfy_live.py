#!/usr/bin/env python3
"""Opt-in: run the bot's real generator over the hosted tunnel, without Telegram."""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from havoc.services.imagegen import ImageGenerator  # noqa: E402
from havoc.services.service_directory import ServiceDirectory  # noqa: E402


async def main() -> int:
    directory = ServiceDirectory.from_env()
    await asyncio.to_thread(directory.pull_now)
    endpoint = directory.resolve("comfy", force=True)
    if not endpoint.ok:
        print(f"::error::Picture service unavailable: {endpoint.state}")
        return 1
    generator = ImageGenerator.from_env(directory=directory)
    if generator is None:
        print("::error::Image generator not configured")
        return 1
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="yura-comfy-live-") as temp:
        try:
            path = await generator.generate("a blue kite flying over the sea", outdir=Path(temp))
            data = path.read_bytes()
        except Exception as exc:
            print(f"::error::Picture generation failed: {type(exc).__name__}: {str(exc)[:200]}")
            return 1
        if len(data) < 1024 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
            print("::error::The service did not return a valid PNG")
            return 1
        print(f"::notice::Picture generated through hosted tunnel: {len(data)} bytes in {time.monotonic() - started:.1f}s; run {endpoint.run_id}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
