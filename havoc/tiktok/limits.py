"""What the account is allowed to do today, and how fast.

This module exists because of one asymmetry: the account has one life and the
code has a `git revert`. Everything here fails **closed**.

The numbers are not TikTok's — TikTok publishes none for web actions, and any
number would be a guess. They are deliberately below what a person does, because
the honest goal is not "maximize actions" but "stay boring". An account that
likes 120 videos, follows 30 people, comments on 20 and posts 6 times in a day is
already at the ceiling of what a keen human looks like; above that, the only
thing that grows is the chance of being flagged for it.

Two independent guards:

* **Daily ceiling** (:class:`Budget`) — how much in a UTC day, persisted, so a
  handover does not reset it. A run that restarts six times must not get six
  allowances.
* **Pace** (:class:`Pace`) — the gap between two writes, jittered. A fixed
  interval is itself a fingerprint; a range is not.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any

from . import SessionError, TikTokSessionStore

__all__ = ["DEFAULT_LIMITS", "MIN_GAP", "Budget", "Pace", "budget_for"]

#: Per UTC day, per account. ``HAVOC_TIKTOK_DAILY_<ACTION>`` moves one.
DEFAULT_LIMITS: dict[str, int] = {
    "like": 120,
    "comment": 20,
    "follow": 30,
    "unfollow": 30,
    "block": 10,
    "post": 6,
    "dm": 60,
    "reply": 60,
    "scroll": 400,
}

#: Minimum seconds between two writes of the same kind.
MIN_GAP: dict[str, float] = {
    "like": 2.5,
    "comment": 25.0,
    "follow": 20.0,
    "unfollow": 20.0,
    "block": 15.0,
    "post": 90.0,
    "dm": 8.0,
    "reply": 12.0,
    "scroll": 0.8,
}


def budget_for(action: str) -> int:
    """The daily ceiling for one action, honouring the environment override."""
    action = action.strip().lower()
    if action not in DEFAULT_LIMITS:
        raise KeyError(action)
    raw = os.environ.get(f"HAVOC_TIKTOK_DAILY_{action.upper()}", "").strip()
    if not raw:
        return DEFAULT_LIMITS[action]
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_LIMITS[action]
    # A typo must not make the tool unlimited; 0 is a legitimate "off switch".
    return max(0, value)


def _day(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


@dataclass
class Budget:
    """A persisted daily allowance, charged before the action, not after.

    ``store`` is the account's :class:`~havoc.tiktok.TikTokSessionStore`; the
    ledger rides along in its ``.state.json`` so a runner restart cannot mint a
    second allowance.
    """

    store: TikTokSessionStore
    _state: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._state = self.store.state()

    # -- internals ----------------------------------------------------------
    def _counts(self, now: float | None = None) -> dict[str, int]:
        day = _day(now)
        counted = self._state.get("counts")
        if not isinstance(counted, dict) or counted.get("day") != day:
            return {}
        spent = counted.get("spent")
        return dict(spent) if isinstance(spent, dict) else {}

    def _commit(self, spent: dict[str, int], now: float | None = None) -> None:
        # Only today is kept: a spending record, not an archive.
        self._state["counts"] = {"day": _day(now), "spent": spent}
        self.store.save_state(self._state)

    # -- public -------------------------------------------------------------
    def spent(self, action: str, now: float | None = None) -> int:
        return int(self._counts(now).get(action, 0))

    def remaining(self, action: str, now: float | None = None) -> int:
        return max(0, budget_for(action) - self.spent(action, now))

    def can(self, action: str, *, count: int = 1, now: float | None = None) -> bool:
        return self.remaining(action, now) >= count

    def spend(self, action: str, *, count: int = 1, now: float | None = None) -> int:
        """Charge the allowance and return what is left. Raises when it cannot.

        Charged *before* the action so a refused action never half-happens, and
        so a crash mid-action still counts against the day — the alternative
        (counting only successes) makes a retry loop free, which is precisely
        the behaviour that gets an account flagged.
        """
        action = action.strip().lower()
        if action not in DEFAULT_LIMITS:
            raise KeyError(action)
        if count < 1:
            raise ValueError("count must be at least 1")
        limit = budget_for(action)
        spent = self._counts(now)
        used = int(spent.get(action, 0))
        if used + count > limit:
            raise SessionError(
                f"{action} allowance spent for today ({used}/{limit})",
                kind="budget",
                hint=f"resets at 00:00 UTC; HAVOC_TIKTOK_DAILY_{action.upper()} raises it",
            )
        spent[action] = used + count
        self._commit(spent, now)
        return max(0, limit - spent[action])


@dataclass
class Pace:
    """Space writes apart the way a bored human would.

    ``speed`` below 1 is faster; the tests use it so a suite does not sleep for
    minutes. Production never sets it — a fast pace is the thing this class
    exists to prevent.
    """

    speed: float = 1.0
    rng: random.Random = field(default_factory=random.Random)
    _last: dict[str, float] = field(default_factory=dict)

    def seconds_for(self, action: str) -> float:
        floor = MIN_GAP.get(action, 5.0) * max(0.01, self.speed)
        # 0–80% on top, so the interval is a range and not a metronome.
        return floor * (1.0 + self.rng.random() * 0.8)

    async def wait(self, action: str) -> float:
        delay = self.seconds_for(action)
        last = self._last.get(action)
        if last is not None:
            remaining = delay - (time.monotonic() - last)
            if remaining > 0:
                await asyncio.sleep(remaining)
        self._last[action] = time.monotonic()
        return delay

    def mark(self, action: str) -> None:
        self._last[action] = time.monotonic()

    async def pause(self, low: float = 0.4, high: float = 1.6) -> None:
        await asyncio.sleep(self.rng.uniform(low, high) * max(0.01, self.speed))
