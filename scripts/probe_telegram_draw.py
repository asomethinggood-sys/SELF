#!/usr/bin/env python3
"""Opt-in live drawing probe: invoke the real outgoing handler, then remove its messages."""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from havoc.client import TelegramService  # noqa: E402
from havoc.config import load_config  # noqa: E402
from havoc.runtime import Runtime  # noqa: E402


async def main() -> int:
    # Both bots use one CPU server; avoid queueing both test renders at once.
    if os.environ.get("DRAW_PROBE_ACCOUNT") == "her":
        await asyncio.sleep(250)
    config, _ = load_config()
    logger = logging.getLogger("live-draw-probe")
    logger.addHandler(logging.NullHandler())
    logger.setLevel(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix="yura-draw-probe-") as temp:
        root = Path(temp)
        copied = root / "test.db"
        if config.database_path.exists():
            with sqlite3.connect(f"file:{config.database_path}?mode=ro", uri=True) as source, sqlite3.connect(copied) as target:
                source.backup(target)
        config = config.with_overrides(database_path=copied, data_dir=root, log_dir=root, backup_dir=root / "backups", ai_provider="none", dry_run=False)
        runtime = await Runtime.create(config, logger)
        telegram = TelegramService(config, logger)
        made: list[int] = []
        uploads: list[Any] = []
        replies: list[Any] = []
        info = None
        try:
            info = await telegram.authorize(interactive=False)
            runtime.attach_client(telegram, telegram.client, info)
            runtime.permissions.set_self_id(info.user_id)
            runtime.load_features()
            await asyncio.to_thread(runtime.services.pull_now)
            endpoint = runtime.services.resolve("comfy", force=True)
            print(f"::notice::Directory state: {endpoint.state}; run {endpoint.run_id}", flush=True)
            client = telegram.client
            original_send, original_file = client.send_message, client.send_file

            async def send(*args: Any, **kwargs: Any) -> Any:
                result = await original_send(*args, **kwargs)
                if getattr(result, "id", None):
                    made.append(result.id)
                    replies.append(result)
                return result

            async def send_file(*args: Any, **kwargs: Any) -> Any:
                result = await original_file(*args, **kwargs)
                for msg in result if isinstance(result, list) else [result]:
                    if getattr(msg, "id", None):
                        made.append(msg.id)
                        uploads.append(msg)
                return result

            client.send_message, client.send_file = send, send_file
            alias = ".imagegen" if os.environ.get("DRAW_PROBE_ACCOUNT") == "her" else ".draw"
            message = await client.send_message(info.user_id, f"{alias} a small red lighthouse by the sea")
            event = SimpleNamespace(
                message=message, chat_id=info.user_id, id=message.id,
                sender_id=info.user_id, out=True, is_private=True,
            )
            started = time.monotonic()
            await asyncio.wait_for(runtime.handlers.on_outgoing(event), timeout=700)
            if not uploads:
                print(f"::error::{alias}: no Telegram upload; generator error: {getattr(runtime.image_gen, 'last_error', '')}", flush=True)
                for error in runtime.health.errors:
                    print(f"::notice::Handler error: {error['source']} {error['message']}", flush=True)
                for reply in replies[1:]:
                    current = await client.get_messages(info.user_id, ids=reply.id)
                    if current and getattr(current, "text", ""):
                        print(f"::notice::Bot response: {current.text[:350]}", flush=True)
                return 1
            remote = await client.get_messages(info.user_id, ids=uploads[-1].id)
            if not remote or not getattr(remote, "media", None):
                print(f"::error::{alias}: Telegram could not retrieve the uploaded media", flush=True)
                return 1
            print(f"::notice::{alias}: real generation and Telegram upload verified in {time.monotonic()-started:.1f}s", flush=True)
            return 0
        finally:
            if telegram.client and info and made:
                try:
                    await telegram.client.delete_messages(info.user_id, list(set(made)))
                except Exception:
                    print("::warning::Could not remove every draw probe message", flush=True)
            await runtime.shutdown()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:
        print(f"::error::Draw probe failed: {type(exc).__name__}: {str(exc)[:200]}", flush=True)
        sys.exit(1)
