"""One outbound budget, two lanes, and a queue that cannot starve the chat.

The problem this file exists for, in the owner's words: *a mass task — a
thousand messages — takes fifty minutes and starves everything else; it barely
answers ping and the profile clock only updates every few minutes.*

That is not a Telegram limit and not a slow network. It is arithmetic: every
send waited at one global gate of ``20 per 60 s``, and a bulk job took every
slot in it. A thousand messages at one every three seconds is fifty minutes,
and a reply that arrives while that is running waits behind the whole queue,
because the queue was not a queue — it was a lock with no idea who was waiting.

The shape here:

* **A budget, not a lock.** Sends are paced against a token bucket refilled at
  ``per_minute``; a caller is told exactly how long to wait instead of spinning.
* **Two lanes.** ``INTERACTIVE`` is a person waiting for words (a reply, a ping,
  a reminder, a command answer). ``BULK`` is a job he started and walked away
  from. Bulk may not spend the last ``reserve`` of the budget *while* somebody
  interactive is waiting — the reserve is theirs the moment they appear.
* **No starvation in the other direction.** If nothing interactive has asked for
  ``handover`` seconds, bulk may use the reserve too, so a quiet account is not
  paying for a lane nobody is in.
* **Fairness inside a lane.** Waiting is FIFO by ticket, so two bulk jobs of
  500 do not interleave into 1000 delays for each other.

Telegram's own safety rails are unchanged: the total budget is still bounded,
FloodWait is still respected in :class:`RateLimiter`, and this class only
decides *order*, never the ceiling.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .cache import RateCounter


class Lane(StrEnum):
    """Who is waiting. Ordered by how much their silence is felt."""

    INTERACTIVE = "interactive"
    BULK = "bulk"


@dataclass
class PacerStats:
    granted: dict[str, int] = field(default_factory=lambda: {lane.value: 0 for lane in Lane})
    waits: dict[str, int] = field(default_factory=lambda: {lane.value: 0 for lane in Lane})
    #: The longest wait each lane has seen, in seconds — the number that says
    #: whether the reserve is actually doing its job.
    worst_wait: dict[str, float] = field(default_factory=lambda: {lane.value: 0.0 for lane in Lane})
    #: Rolling waits, so the diagnostics can show a p95 instead of a max that one
    #: bad minute can pin forever.
    recent: dict[str, deque[float]] = field(
        default_factory=lambda: {lane.value: deque(maxlen=200) for lane in Lane}
    )

    def observe(self, lane: Lane, waited: float) -> None:
        self.granted[lane.value] += 1
        if waited > 0.01:
            self.waits[lane.value] += 1
        self.worst_wait[lane.value] = max(self.worst_wait[lane.value], waited)
        self.recent[lane.value].append(waited)

    def p95(self, lane: Lane) -> float:
        window = sorted(self.recent[lane.value])
        if not window:
            return 0.0
        index = min(len(window) - 1, int(round(len(window) * 0.95)) - 1)
        return round(window[max(0, index)], 2)

    def snapshot(self) -> dict[str, Any]:
        return {
            "granted": dict(self.granted),
            "waits": dict(self.waits),
            "worst_wait": {key: round(value, 2) for key, value in self.worst_wait.items()},
            "p95_wait": {lane.value: self.p95(lane) for lane in Lane},
        }


@dataclass
class Pacer:
    """Two-lane, FIFO, budget-based pacer for outgoing Telegram calls."""

    per_minute: int = 30
    #: Share of the budget bulk may not touch while an interactive send waits.
    reserve: float = 0.35
    #: After this many seconds with no interactive send *granted*, bulk may run at
    #: ``per_minute - 1`` instead of ``per_minute - reserve``: a quiet account
    #: should not pay for a lane nobody is in, but the last slot is never sold.
    #: 0 disables it (the reserve is then always yours).
    handover: float = 5.0
    clock: Any = time.monotonic
    stats: PacerStats = field(default_factory=PacerStats)
    _tickets: dict[str, deque[Any]] = field(default_factory=lambda: {lane.value: deque() for lane in Lane})
    _next_ticket: int = 0
    _interactive_waiting_since: float | None = None
    #: When a person's send last actually went out. This is what "quiet" means:
    #: not "nobody is waiting this instant" — which is true even while a reply is
    #: being typed — but "nothing has been said for a while".
    _last_interactive_at: float | None = None
    #: Shared with :class:`RateLimiter`, so the budget is one number no matter
    #: which door a send came through.
    counter: RateCounter | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        self.per_minute = max(1, int(self.per_minute))
        self.reserve = min(0.9, max(0.0, float(self.reserve)))
        if self.counter is None:
            self.counter = RateCounter(self.per_minute, 60.0, self.clock)

    # ------------------------------------------------------------------
    # Budget arithmetic
    # ------------------------------------------------------------------
    @property
    def interactive_reserve(self) -> int:
        """How many of the window's slots bulk may not take while somebody waits."""
        if self.reserve <= 0:
            return 0
        return max(1, int(round(self.per_minute * self.reserve)))

    def _bulk_ceiling(self, now: float) -> int:
        """The number of slots bulk is allowed to use right now.

        Three states, in order of who matters:

        * somebody interactive is queued → bulk stops at the reserve line;
        * the account has been quiet for ``handover`` seconds → bulk may run at
          ``per_minute - 1``, which is nearly full speed but still leaves one
          slot, so the next thing a person says waits one gap, not one window;
        * otherwise → the reserve line.

        The first version of this returned ``per_minute`` whenever nobody was
        *waiting*, which is almost always: a job then drained the whole window
        and the person who spoke a second later waited up to a minute. The
        reserve has to be a floor, not a race.
        """
        if self._tickets[Lane.INTERACTIVE.value]:
            return max(0, self.per_minute - self.interactive_reserve)
        idle = self._last_interactive_at is None or (now - self._last_interactive_at) >= self.handover
        if self.handover > 0 and idle:
            return max(1, self.per_minute - 1)
        return max(0, self.per_minute - self.interactive_reserve)

    def depth(self) -> dict[str, int]:
        return {lane.value: len(self._tickets[lane.value]) for lane in Lane}

    # ------------------------------------------------------------------
    # Acquire
    # ------------------------------------------------------------------
    async def acquire(self, lane: Lane | str = Lane.INTERACTIVE, *, timeout: float = 120.0) -> float:
        """Wait for a slot in ``lane`` and return how long it took, in seconds.

        Raises :class:`TimeoutError` when ``timeout`` passes, so a caller never
        hangs forever behind a job it cannot see.
        """
        chosen = Lane(lane) if not isinstance(lane, Lane) else lane
        started = self.clock()
        wall_deadline = time.monotonic() + max(0.1, float(timeout))
        ticket = self._take_ticket(chosen)
        try:
            while True:
                if self.counter is None:  # pragma: no cover - __post_init__ fills it
                    raise RuntimeError("pacer has no counter")
                admitted = self._admit(chosen, ticket)
                if admitted and self.counter.try_acquire():
                    if chosen is Lane.INTERACTIVE:
                        self._last_interactive_at = self.clock()
                    waited = self.clock() - started
                    # Remove the ticket we were holding before leaving.
                    self._drop_ticket(chosen, ticket)
                    self.stats.observe(chosen, waited)
                    return waited
                remaining = self.counter.retry_after()
                if time.monotonic() >= wall_deadline:
                    self._drop_ticket(chosen, ticket)
                    raise TimeoutError(f"no outbound slot in {timeout:.0f}s")
                await asyncio.sleep(min(0.5, max(0.01, remaining if remaining else 0.02)))
        finally:
            self._drop_ticket(chosen, ticket)
            self._refresh_interactive()

    # ------------------------------------------------------------------
    # Tickets (FIFO inside a lane)
    # ------------------------------------------------------------------
    def _take_ticket(self, lane: Lane) -> int:
        self._next_ticket += 1
        ticket = self._next_ticket
        self._tickets[lane.value].append(ticket)
        if lane is Lane.INTERACTIVE:
            if self._interactive_waiting_since is None:
                self._interactive_waiting_since = self.clock()
        return ticket

    def _drop_ticket(self, lane: Lane, ticket: int) -> None:
        queue = self._tickets[lane.value]
        try:
            queue.remove(ticket)
        except ValueError:  # pragma: no cover - already dropped
            pass

    def _refresh_interactive(self) -> None:
        if not self._tickets[Lane.INTERACTIVE.value]:
            self._interactive_waiting_since = None

    def _admit(self, lane: Lane, ticket: int) -> bool:
        """Is it this ticket's turn, and may its lane spend a slot?"""
        now = self.clock()
        if lane is Lane.BULK:
            # Bulk always yields to a waiting interactive send, whatever the
            # budget says: the person staring at the chat is the point.
            if self._tickets[Lane.INTERACTIVE.value]:
                return False
            if self.counter is not None and self.counter.used() >= self._bulk_ceiling(now):
                return False
        queue = self._tickets[lane.value]
        return bool(queue) and queue[0] == ticket

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        counter = self.counter
        return {
            "per_minute": self.per_minute,
            "reserve": round(self.reserve, 2),
            "depth": self.depth(),
            "used_in_window": counter.used() if counter else 0,
            "retry_after": round(counter.retry_after(), 2) if counter else 0.0,
            **self.stats.snapshot(),
        }


class pacing:
    """Context manager that makes every send inside it take the bulk lane.

    Threading a ``lane=`` keyword through forty call sites is how a codebase ends
    up with a bulk send that forgot to declare itself. The lane is ambient while
    a job runs, so ``sender.send(...)`` inherits it without changing shape —
    and a nested interactive send (progress update to the owner) can override it
    with ``pacing(Lane.INTERACTIVE)``.
    """

    def __init__(self, lane: Lane | str) -> None:
        self.lane = Lane(lane) if not isinstance(lane, Lane) else lane
        self._token: Any = None

    def __enter__(self) -> Lane:
        self._token = current_lane.set(self.lane)
        return self.lane

    def __exit__(self, *_exc: Any) -> None:
        current_lane.reset(self._token)

#: The lane a send takes when nobody said otherwise. A reply to a person is the
#: default because that is the one whose silence is noticed.
current_lane: ContextVar[Lane] = ContextVar("havoc_lane", default=Lane.INTERACTIVE)


def lane_now() -> Lane:
    return current_lane.get()


__all__ = ["Lane", "Pacer", "PacerStats", "pacing", "current_lane", "lane_now"]
