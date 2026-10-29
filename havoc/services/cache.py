"""Bounded in-memory caches.

Telegram userbots need short-lived state (cooldowns, dedupe keys, recent chat
metadata) that must not grow without limit. :class:`TTLCache` combines a TTL
with a hard size cap and evicts the oldest entry first, so a busy account
cannot exhaust memory (feature areas 40, 89).

The cache is intentionally synchronous: operations never block, so it is safe
to call from event handlers.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

T = TypeVar("T")


@dataclass
class _Entry(Generic[T]):
    value: T
    expires_at: float


class TTLCache(Generic[T]):
    def __init__(self, maxsize: int = 1024, ttl: float = 300.0, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.maxsize = max(1, maxsize)
        self.ttl = ttl
        self._data: OrderedDict[str, _Entry[T]] = OrderedDict()
        self._clock = clock
        self.hits = 0
        self.misses = 0

    # ------------------------------------------------------------------
    def _purge_expired(self) -> None:
        now = self._clock()
        expired = [key for key, entry in self._data.items() if entry.expires_at <= now]
        for key in expired:
            self._data.pop(key, None)

    def get(self, key: str, default: T | None = None) -> T | None:
        entry = self._data.get(key)
        if entry is None:
            self.misses += 1
            return default
        if entry.expires_at <= self._clock():
            self._data.pop(key, None)
            self.misses += 1
            return default
        self._data.move_to_end(key)
        self.hits += 1
        return entry.value

    def set(self, key: str, value: T, ttl: float | None = None) -> None:
        self._purge_expired()
        self._data[key] = _Entry(value, self._clock() + (self.ttl if ttl is None else ttl))
        self._data.move_to_end(key)
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)

    def pop(self, key: str, default: T | None = None) -> T | None:
        entry = self._data.pop(key, None)
        return default if entry is None else entry.value

    def contains(self, key: str) -> bool:
        return self.get(key, None) is not None

    def clear(self) -> None:
        self._data.clear()

    def prune(self) -> int:
        before = len(self._data)
        self._purge_expired()
        return before - len(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __iter__(self) -> Iterator[str]:
        return iter(list(self._data))

    @property
    def stats(self) -> dict[str, int]:
        return {"size": len(self._data), "maxsize": self.maxsize, "hits": self.hits, "misses": self.misses}


class CooldownTracker:
    """Per-key cooldown gate used by auto-replies, reactions and triggers.

    A cooldown is stored as the *last fire timestamp*, not a boolean, so the
    remaining time can be reported ("try again in 12s") instead of a vague
    refusal.
    """

    def __init__(self, maxsize: int = 4096, clock: Callable[[], float] = time.monotonic) -> None:
        self._last: OrderedDict[str, float] = OrderedDict()
        self.maxsize = maxsize
        self._clock = clock

    def remaining(self, key: str, cooldown: float) -> float:
        last = self._last.get(key)
        if last is None:
            return 0.0
        elapsed = self._clock() - last
        return max(0.0, cooldown - elapsed)

    def allowed(self, key: str, cooldown: float) -> bool:
        return self.remaining(key, cooldown) <= 0

    def touch(self, key: str) -> None:
        self._last[key] = self._clock()
        self._last.move_to_end(key)
        while len(self._last) > self.maxsize:
            self._last.popitem(last=False)

    def clear(self) -> None:
        self._last.clear()


def dedupe_key(*parts: Any) -> str:
    """Stable short key for duplicate-event detection."""
    joined = "|".join(str(part) for part in parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:24]


class RateCounter:
    """Sliding-window counter (used by the outbound rate limiter)."""

    def __init__(self, limit: int, window_seconds: float = 60.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = max(1, limit)
        self.window = window_seconds
        self._events: list[float] = []
        self._clock = clock

    def _trim(self) -> None:
        cutoff = self._clock() - self.window
        self._events = [stamp for stamp in self._events if stamp > cutoff]

    def remaining(self) -> int:
        self._trim()
        return max(0, self.limit - len(self._events))

    def try_acquire(self) -> bool:
        self._trim()
        if len(self._events) >= self.limit:
            return False
        self._events.append(self._clock())
        return True

    def retry_after(self) -> float:
        self._trim()
        if len(self._events) < self.limit:
            return 0.0
        return max(0.0, self.window - (self._clock() - self._events[0]))
