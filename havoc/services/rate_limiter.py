"""Centralized rate limiting and Telegram FloodWait handling.

Two layers
----------
``RateLimiter``
    Per-operation cooldowns plus a global outbound window. Feature code asks
    "may I send in chat X now?" instead of sleeping in place, which keeps
    handlers responsive and prevents accidental bursts.

``FloodGuard``
    Remembers FloodWait deadlines reported by Telegram so *all* operations
    back off, not only the one that triggered the error. This is a safety
    feature, not an evasion mechanism: HAVOC respects the wait.

Feature areas: 9, 84 (crash/reconnect recovery), 96.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .cache import CooldownTracker, RateCounter


class RateLimited(Exception):
    """Raised when an action is refused because of a cooldown/window."""

    def __init__(self, retry_after: float, scope: str = "operation") -> None:
        self.retry_after = max(0.0, retry_after)
        self.scope = scope
        super().__init__(f"rate limited ({scope}); retry in {self.retry_after:.1f}s")


@dataclass
class RateLimiter:
    outbound_limit: int = 20
    outbound_window: float = 60.0
    clock: Callable[[], float] = time.monotonic
    _cooldowns: CooldownTracker = field(default_factory=CooldownTracker)
    _outbound: RateCounter | None = None
    _flood_until: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    counters: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self._outbound is None:
            self._outbound = RateCounter(self.outbound_limit, self.outbound_window, self.clock)

    # ------------------------------------------------------------------
    # Cooldowns
    # ------------------------------------------------------------------
    def cooldown_remaining(self, key: str, seconds: float) -> float:
        return self._cooldowns.remaining(key, seconds)

    def check_cooldown(self, key: str, seconds: float) -> None:
        remaining = self.cooldown_remaining(key, seconds)
        if remaining > 0:
            raise RateLimited(remaining, scope=key)

    def touch(self, key: str) -> None:
        self._cooldowns.touch(key)

    def run(self, key: str, seconds: float) -> None:
        """Register an action and start its cooldown (call after success)."""
        self._cooldowns.touch(key)
        self.counters[key] = self.counters.get(key, 0) + 1

    # ------------------------------------------------------------------
    # Outbound window
    # ------------------------------------------------------------------
    def check_outbound(self) -> None:
        assert self._outbound is not None
        if not self._outbound.try_acquire():
            raise RateLimited(self._outbound.retry_after(), scope="outbound")

    async def acquire_outbound(self, timeout: float = 30.0) -> None:
        """Wait for a slot in the outbound window (bounded wait)."""
        assert self._outbound is not None
        # The wait bounds itself with the *real* clock: the window arithmetic
        # uses ``self.clock`` (which tests may freeze), so measuring the timeout
        # with it would loop forever.
        deadline = time.monotonic() + timeout
        while not self._outbound.try_acquire():
            if time.monotonic() >= deadline:
                raise RateLimited(self._outbound.retry_after(), scope="outbound")
            await asyncio.sleep(min(0.5, max(0.02, min(self._outbound.retry_after(), deadline - time.monotonic()))))

    # ------------------------------------------------------------------
    # FloodWait
    # ------------------------------------------------------------------
    def note_flood_wait(self, seconds: float) -> None:
        self._flood_until = max(self._flood_until, self.clock() + max(0.0, seconds))

    def flood_remaining(self) -> float:
        return max(0.0, self._flood_until - self.clock())

    def check_flood(self) -> None:
        remaining = self.flood_remaining()
        if remaining > 0:
            raise RateLimited(remaining, scope="floodwait")

    def snapshot(self) -> dict[str, float | int]:
        return {
            "cooldown_keys": len(self._cooldowns._last),  # noqa: SLF001 - diagnostics only
            "floodwait_remaining": round(self.flood_remaining(), 1),
            "outbound_remaining": self._outbound.remaining() if self._outbound else 0,
            "outbound_limit": self.outbound_limit,
        }


class FloodGuard:
    """Context-free helper that turns Telethon FloodWait errors into backoff."""

    def __init__(self, limiter: RateLimiter) -> None:
        self.limiter = limiter
        self.flood_events = 0
        self.last_wait: float = 0.0

    @staticmethod
    def _extract_seconds(exc: BaseException) -> float | None:
        seconds = getattr(exc, "seconds", None)
        if isinstance(seconds, (int, float)):
            return float(seconds)
        return None

    def handle(self, exc: BaseException) -> float | None:
        """Record a FloodWait and return the wait in seconds (or ``None``)."""
        seconds = self._extract_seconds(exc)
        if seconds is None:
            name = type(exc).__name__
            if name != "FloodWaitError":
                return None
            seconds = 0.0
        self.flood_events += 1
        self.last_wait = seconds
        self.limiter.note_flood_wait(seconds)
        return seconds


async def retry_async(
    operation: Callable[[], asyncio.Future | object],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    retry_on: tuple[type[BaseException], ...] = (OSError, asyncio.TimeoutError),
    on_retry: Callable[[int, BaseException], None] | None = None,
) -> object:
    """Bounded retry with exponential backoff (feature area 84).

    Only ``retry_on`` exceptions are retried; everything else propagates so
    genuine bugs are not masked.
    """
    last_exc: BaseException | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            result = operation()
            if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                return await result
            return result
        except retry_on as exc:
            last_exc = exc
            if attempt >= attempts:
                break
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            if on_retry:
                on_retry(attempt, exc)
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc
