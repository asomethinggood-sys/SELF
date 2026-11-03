#!/usr/bin/env python3
"""Opt-in: exercise the real router and upload public samples to Saved Messages.

Run only before the account's userbot starts, using its existing session. The
probe copies its database, records only messages it creates, and removes those
messages when done. It never prints session data or media URLs.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from havoc.client import TelegramService  # noqa: E402
from havoc.config import load_config  # noqa: E402
from havoc.runtime import Runtime  # noqa: E402

SAMPLES = (
    ("Pinterest image", "https://www.pinterest.com/pin/543809723775636824/"),
    ("Pinterest video", "https://www.pinterest.com/pin/385691155608427004/"),
)


async def main() -> int:
    config, _ = load_config()
    logger = logging.getLogger("live-downloader-probe")
    logger.addHandler(logging.NullHandler())
    logger.setLevel(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix="yura-live-probe-") as temp:
        root = Path(temp)
        original = config.database_path
        copied = root / "test.db"
        if original.exists():
            with sqlite3.connect(f"file:{original}?mode=ro", uri=True) as source, sqlite3.connect(copied) as target:
                source.backup(target)
        config = config.with_overrides(database_path=copied, data_dir=root, log_dir=root, backup_dir=root / "backups", ai_provider="none", dry_run=False)
        runtime = await Runtime.create(config, logger)
        telegram = TelegramService(config, logger)
        made: list[int] = []
        results: list[Any] = []
        try:
            info = await telegram.authorize(interactive=False)
            runtime.attach_client(telegram, telegram.client, info)
            runtime.permissions.set_self_id(info.user_id)
            runtime.load_features()
            if not await runtime.feature_manager.is_enabled("downloader"):
                print("::error::Downloader is disabled in this account's database", flush=True)
                return 1
            # Record Telegram's actual returned message objects without changing
            # sender behaviour. Delete only these IDs, never unrelated messages.
            client = telegram.client
            original_send = client.send_message
            original_file = client.send_file

            async def send(*args: Any, **kwargs: Any) -> Any:
                result = await original_send(*args, **kwargs)
                if getattr(result, "id", None):
                    made.append(result.id)
                return result

            async def send_file(*args: Any, **kwargs: Any) -> Any:
                result = await original_file(*args, **kwargs)
                for msg in result if isinstance(result, list) else [result]:
                    if getattr(msg, "id", None):
                        made.append(msg.id)
                        results.append(msg)
                return result

            client.send_message = send
            client.send_file = send_file
            passed = 0
            for label, url in SAMPLES:
                before = len(results)
                initial = await client.send_message(info.user_id, url)
                await asyncio.wait_for(runtime.handlers.on_outgoing(SimpleNamespace(message=initial)), timeout=90)
                session = runtime.downloader.sessions.get(info.user_id)
                if not session:
                    print(f"::error::{label}: link did not produce a quality menu", flush=True)
                    continue
                pick = await client.send_message(info.user_id, "1")
                await asyncio.wait_for(runtime.handlers.on_outgoing(SimpleNamespace(message=pick)), timeout=180)
                if runtime.downloader.delivered <= passed or len(results) <= before:
                    print(f"::error::{label}: media was not delivered", flush=True)
                    continue
                delivered = results[-1]
                remote = await client.get_messages(info.user_id, ids=delivered.id)
                if remote is None or getattr(remote, "media", None) is None:
                    print(f"::error::{label}: Telegram did not return the uploaded media", flush=True)
                    continue
                passed += 1
                print(f"::notice::{label}: router, quality selection and Telegram upload verified", flush=True)
            return 0 if passed == len(SAMPLES) else 1
        finally:
            if telegram.client and made:
                try:
                    await telegram.client.delete_messages(info.user_id, list(set(made)))
                except Exception:
                    print("::warning::Could not remove every test message from Saved Messages", flush=True)
            await runtime.shutdown()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:
        print(f"::error::Telegram downloader probe failed: {type(exc).__name__}", flush=True)
        sys.exit(1)
