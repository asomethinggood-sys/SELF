"""Concurrency smoke test.

Telegram delivers bursts: a message, an edit, and a scheduled reminder can land
in the same tick. Every command runs on the same event loop but interleaves at
``await`` points, and SQLite writes go through one connection, so this is where
locking and re-entrancy bugs hide.
"""

from __future__ import annotations

import asyncio
import random

from conftest import invoke

BURST = (
    ".remind 10m water",
    ".bookmark save note hello",
    ".todo add task one",
    ".note add idea",
    ".calc 2+2*3",
    ".help",
    ".status",
    ".bookmarks",
    ".reminders",
    ".features",
    ".health",
    ".settings",
    ".time",
    ".stats",
)


async def test_concurrent_burst_does_not_raise(runtime) -> None:
    rng = random.Random(1234)
    texts = [rng.choice(BURST) for _ in range(200)]
    chats = [rng.choice([100, 200, 300]) for _ in texts]

    # If any handler leaks an exception it reaches us here, because invoke()
    # drives the same dispatcher the client uses.
    await _gather_all(runtime, texts, chats)

    assert [entry for entry in runtime.health.errors] == []


async def test_concurrent_bookmarks_are_all_stored(runtime) -> None:
    texts = [f".bookmark save note item {index}" for index in range(60)]
    await _gather_all(runtime, texts, [100] * len(texts))
    bookmarks = await runtime.store.list_bookmarks(limit=500)
    assert len(bookmarks) >= 60


async def _gather_all(runtime, texts, chats) -> None:
    await asyncio.gather(
        *(invoke(runtime, text, chat_id=chat) for text, chat in zip(texts, chats, strict=True))
    )
