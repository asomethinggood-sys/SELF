#!/usr/bin/env python3
"""One opt-in Telegram delivery of an already-rendered synthetic SDXL sample.

Executed before the account's regular bot starts, using its existing session;
never connects a second copy of that session on a separate runner. The owner
explicitly requested a Telegram experiment. This does not install a draw command.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from havoc.client import TelegramService  # noqa: E402
from havoc.config import load_config  # noqa: E402

OWNER_ID = 7542695649
SAMPLE = ROOT / ".runtime" / "sdxl-telegram-sample" / "bf16.png"
SHA256_PREFIX = "9a556685c201"


async def main() -> None:
    image = SAMPLE.read_bytes()
    if not image.startswith(b"\x89PNG\r\n\x1a\n") or hashlib.sha256(image).hexdigest()[:12] != SHA256_PREFIX:
        raise ValueError("the experimental sample is missing or does not match the verified image")
    config, _ = load_config()
    logger = logging.getLogger("sdxl-telegram-sample")
    logger.addHandler(logging.NullHandler())
    logger.setLevel(logging.CRITICAL)
    telegram = TelegramService(config, logger)
    try:
        account = await telegram.authorize(interactive=False)
        client = telegram.client
        if account.user_id == OWNER_ID:
            recipient = "me"
        else:
            recipient = await client.get_entity("edited_by_nyx")
            if getattr(recipient, "id", None) != OWNER_ID:
                raise ValueError("owner identity did not match; refusing to send")
        message = await client.send_file(
            recipient,
            str(SAMPLE),
            caption="🧪 Experimental DreamShaper XL Lightning sample · lighthouse at sunrise\nThis is a test image, not a replacement for .draw.",
            force_document=False,
        )
        if not getattr(message, "id", None):
            raise RuntimeError("Telegram did not return a message ID")
        fetched = await client.get_messages(recipient, ids=message.id)
        if not fetched or not getattr(fetched, "photo", None):
            raise RuntimeError("Telegram did not confirm the uploaded photo")
        print("::notice::Experimental BF16 sample delivered to the verified owner's Telegram and retrieved as a photo.", flush=True)
    finally:
        await telegram.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(asyncio.wait_for(main(), timeout=90))
    except Exception as exc:
        # No session strings, usernames, prompts, URLs or exception contents in logs.
        print(f"::error::Experimental Telegram sample failed: {type(exc).__name__}", flush=True)
        raise SystemExit(1) from None
