"""Is the event loop breathing? A heartbeat that answers with numbers.

Every "the bot feels slow" report so far has had the same shape: *a mass send
runs, and ping stops answering*. The temptation is to guess (the model is slow,
Telegram is slow, SQLite is slow) and to "fix" the guess. This module exists so
nobody has to guess: one task sleeps for a known interval, measures how late it
actually woke, and keeps the distribution.

Late wake-ups mean the single-threaded loop is busy — a blocking call, a tight
loop, a thousand `await`s that never yield. Low lag with slow replies means the
loop is fine and the *wait* is elsewhere (the outbound window, the model, the
network), which is a completely different fix.

What is measured:

``lag_ms``
    How much later than asked the heartbeat woke up. This is the number the
    owner feels as "the bot is frozen".
``ticks``
    Heartbeats so far — a sanity check that the loop is still turning at all.
``p95`` / ``max``
    A window of the last 512 samples, so one cold start does not define the day.

Nothing here is expensive: one wake-up every two seconds and a deque append.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LoopLagMonitor:
    """Measures how late a periodic wake-up is, and keeps the distribution."""

    interval: float = 2.0
    window: int = 512
    #: A wake-up later than this is logged, because a frozen loop is the one
    #: failure the owner notices before any log does.
    alert_ms: float = 1500.0
    samples: deque[float] = field(default_factory=lambda: deque(maxlen=512))
    ticks: int = 0
    started_at: float = field(default_factory=time.monotonic)
    last_ms: float = 0.0
    worst_ms: float = 0.0
    alerts: int = 0
    _task: asyncio.Task[None] | None = None
    _running: bool = False

    def __post_init__(self) -> None:
        self.samples = deque(maxlen=max(32, int(self.window)))
        self.interval = max(0.25, float(self.interval))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self, logger: Any = None) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(logger), name="havoc-loop-lag")

    async def stop(self) -> None:
        self._running = False
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - teardown
                pass

    async def _loop(self, logger: Any) -> None:
        expected = time.monotonic()
        while self._running:
            expected += self.interval
            try:
                await asyncio.sleep(max(0.0, expected - time.monotonic()))
            except asyncio.CancelledError:
                raise
            lag_ms = max(0.0, (time.monotonic() - expected) * 1000.0)
            self.observe(lag_ms)
            if logger is not None and lag_ms >= self.alert_ms:
                logger.warning(
                    "event loop was blocked for %.0f ms (threshold %.0f)",
                    lag_ms,
                    self.alert_ms,
                )
            # If we fell far behind (a suspended runner, a laptop lid), re-anchor
            # instead of measuring the backlog forever.
            if lag_ms > self.interval * 1000 * 5:
                expected = time.monotonic()

    # ------------------------------------------------------------------
    # Samples
    # ------------------------------------------------------------------
    def observe(self, lag_ms: float) -> None:
        value = max(0.0, float(lag_ms))
        self.samples.append(value)
        self.ticks += 1
        self.last_ms = round(value, 1)
        if value > self.worst_ms:
            self.worst_ms = round(value, 1)
        if value >= self.alert_ms:
            self.alerts += 1

    def percentile(self, fraction: float) -> float:
        if not self.samples:
            return 0.0
        ordered = sorted(self.samples)
        index = min(len(ordered) - 1, max(0, int(round(len(ordered) * fraction)) - 1))
        return round(ordered[index], 1)

    @property
    def healthy(self) -> bool:
        """A loop that is not turning at all is the only hard failure."""
        if not self._running:
            return True
        return self.ticks > 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "ticks": self.ticks,
            "last_ms": self.last_ms,
            "p50_ms": self.percentile(0.50),
            "p95_ms": self.percentile(0.95),
            "worst_ms": self.worst_ms,
            "alerts": self.alerts,
            "interval": self.interval,
            "samples": len(self.samples),
        }

    def sentence(self, language: str = "en") -> str:
        """One line for a card: what the loop is doing, in plain words."""
        if not self.samples:
            return "not measured yet" if language == "en" else "هنوز اندازه‌گیری نشده"
        p95 = self.percentile(0.95)
        if language == "fa":
            mood = "روان" if p95 < 250 else "تحت فشار" if p95 < 1200 else "قفل‌شده"
            return f"{mood} · p95 {p95:.0f}ms · بدترین {self.worst_ms:.0f}ms"
        mood = "smooth" if p95 < 250 else "loaded" if p95 < 1200 else "blocked"
        return f"{mood} · p95 {p95:.0f}ms · worst {self.worst_ms:.0f}ms"


__all__ = ["LoopLagMonitor"]
