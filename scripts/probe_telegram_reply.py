#!/usr/bin/env python3
"""Opt-in real-account reply probe in Saved Messages; removes its own messages."""
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


async def main() -> int:
    config, _ = load_config()
    logger = logging.getLogger("reply-probe")
    logger.addHandler(logging.NullHandler())
    logger.setLevel(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix="yura-reply-probe-") as temp:
        root = Path(temp)
        copied = root / "test.db"
        if config.database_path.exists():
            with sqlite3.connect(f"file:{config.database_path}?mode=ro", uri=True) as source, sqlite3.connect(copied) as target:
                source.backup(target)
        config = config.with_overrides(database_path=copied, data_dir=root, log_dir=root, backup_dir=root / "backups")
        runtime = await Runtime.create(config, logger)
        telegram = TelegramService(config, logger)
        made: list[int] = []
        info = None
        try:
            info = await telegram.authorize(interactive=False)
            runtime.attach_client(telegram, telegram.client, info)
            runtime.permissions.set_self_id(info.user_id)
            runtime.load_features()
            client = telegram.client
            raw_send = client.send_message
            results: list[Any] = []

            async def send(*args: Any, **kwargs: Any) -> Any:
                message = await raw_send(*args, **kwargs)
                if getattr(message, "id", None):
                    made.append(message.id)
                    results.append(message)
                return message

            client.send_message = send
            previous = await runtime.sender.send(info.user_id, "Reply test: I am here.", dry_run=False)
            if not getattr(previous, "message", None):
                raise RuntimeError("probe message was not sent")
            incoming = await client.send_message(info.user_id, "What did you mean?", reply_to=previous.message.id)
            before = len(results)
            event = SimpleNamespace(message=incoming, chat_id=info.user_id, id=incoming.id,
                                    sender_id=info.user_id, out=True, is_private=True)
            await asyncio.wait_for(runtime.handlers.on_outgoing(event), timeout=120)
            if len(results) <= before:
                print(f"::error::No reply; skip={runtime.persona_replies.last_skip}; handler_errors={runtime.handlers.handler_errors}")
                for sub in runtime.event_engine.subscriptions:
                    if sub.failures:
                        print(f"::error::Subscriber {sub.name}: {sub.last_error}")
                return 1
            response = await client.get_messages(info.user_id, ids=results[-1].id)
            if not response or not getattr(response, "text", ""):
                print("::error::Reply was not present in Telegram")
                return 1
            print(f"::notice::Reply to a bot message delivered through Telegram; persona_enabled={await runtime.store.feature_enabled('persona')}")
            return 0
        finally:
            if telegram.client and info and made:
                try:
                    await telegram.client.delete_messages(info.user_id, list(set(made)))
                except Exception:
                    print("::warning::Could not remove every reply test message")
            await runtime.shutdown()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:
        print(f"::error::Reply probe failed: {type(exc).__name__}: {str(exc)[:200]}")
        sys.exit(1)
