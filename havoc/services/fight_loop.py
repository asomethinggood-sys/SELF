"""``fight instant repeated brutal`` — the loop the owner asked for.

What it does
------------
Starts an :mod:`asyncio` task that posts a new composed line into one chat every
few seconds, until told to stop. "Instant" means the first line goes out before
the command's own confirmation card, and the rest follow without waiting for
another command.

What keeps it safe
------------------
* **One loop per chat.** Starting again replaces the old one instead of stacking
  a second stream on top of the first.
* **It stops itself.** A hard ceiling (:data:`MAX_LINES`) and a wall-clock limit
  (:data:`MAX_MINUTES`) mean a forgotten loop cannot run until the process ends.
* **It obeys the brake.** Every iteration checks the emergency stop and the
  per-chat permission decision, so ``.stop`` (or quiet hours, or a denylisted
  chat) ends it within one tick.
* **It only ever writes where it was started.** No fan-out, no other chats, no
  retargeting: the chat id is fixed when the loop is created.

The lines come from :mod:`havoc.fight`, which is offline, local, and has no
network path at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any

from .. import fight
from ..utils.formatting import escape
from ..utils.time import utc_now

#: Seconds between lines. The owner said "instant"; a Telegram userbot that
#: ignores FloodWait gets throttled into silence, so "instant" is as fast as a
#: chat can actually take it.
DEFAULT_INTERVAL = 6.0
MIN_INTERVAL = 3.0
#: Absolute ceiling. A loop that nobody stopped ends on its own.
MAX_LINES = 600
MAX_MINUTES = 45.0


@dataclass
class LoopState:
    chat_id: int
    style: str
    intensity: str
    language: str
    interval: float
    sent: int = 0
    started_at: float = field(default_factory=time.monotonic)
    task: asyncio.Task | None = None
    stop_reason: str = ""

    def summary(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "style": self.style,
            "intensity": self.intensity,
            "language": self.language,
            "interval": self.interval,
            "sent": self.sent,
            "running": self.task is not None and not self.task.done(),
            "stop_reason": self.stop_reason,
            "started_at": utc_now().isoformat(),
        }


class FightLoop:
    """Owns at most one running loop per chat."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.logger = runtime.logger
        self._loops: dict[int, LoopState] = {}
        self._recent: dict[int, dict[str, tuple[str, ...]]] = {}
        self.started = 0

    # -- queries -------------------------------------------------------
    def active(self, chat_id: int) -> bool:
        state = self._loops.get(chat_id)
        return bool(state and state.task and not state.task.done())

    def state(self, chat_id: int) -> LoopState | None:
        return self._loops.get(chat_id)

    def running(self) -> list[LoopState]:
        return [state for state in self._loops.values() if self.active(state.chat_id)]

    # -- control -------------------------------------------------------
    async def start(
        self,
        chat_id: int,
        *,
        hook: str,
        style: str = "clever",
        intensity: str = "unhinged",
        language: str = "en",
        interval: float = DEFAULT_INTERVAL,
        source_text: str = "",
    ) -> LoopState:
        await self.stop(chat_id, reason="restarted")
        state = LoopState(
            chat_id=int(chat_id),
            style=fight.normalize_style(style, language),
            intensity=fight.normalize_intensity(intensity),
            language=language if language in {"en", "fa"} else "en",
            interval=max(MIN_INTERVAL, min(float(interval), 120.0)),
        )
        state.task = asyncio.create_task(
            self._run(state, hook=hook, source_text=source_text),
            name=f"fight-loop-{chat_id}",
        )
        self._loops[state.chat_id] = state
        self.started += 1
        self.logger.info(
            "fight loop started in %s (%s/%s, every %.0fs)",
            chat_id, state.style, state.intensity, state.interval,
        )
        return state

    async def stop(self, chat_id: int, *, reason: str = "stopped by owner") -> bool:
        state = self._loops.get(int(chat_id))
        if state is None or state.task is None:
            return False
        if not state.task.done():
            state.stop_reason = reason
            state.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await state.task
        else:
            state.stop_reason = state.stop_reason or reason
        return True

    async def stop_all(self, *, reason: str = "shutdown") -> int:
        count = 0
        for chat_id in list(self._loops):
            if await self.stop(chat_id, reason=reason):
                count += 1
        return count

    # -- the loop ------------------------------------------------------
    async def _run(self, state: LoopState, *, hook: str, source_text: str) -> None:
        deadline = state.started_at + MAX_MINUTES * 60
        seen: set[str] = set()
        try:
            while state.sent < MAX_LINES and time.monotonic() < deadline:
                permissions = self.runtime.permissions
                if await self.runtime.store.emergency_stop():
                    state.stop_reason = "emergency_stop"
                    return
                if await permissions.dry_run_active():
                    state.stop_reason = "dry_run"
                    return
                if await permissions.in_quiet_hours(state.chat_id):
                    state.stop_reason = "quiet_hours"
                    return
                scope = await permissions.automation_scope(state.chat_id, is_private=True)
                if not scope.allowed:
                    state.stop_reason = scope.code
                    return

                line = self._next_line(state, hook=hook, source_text=source_text, seen=seen)
                if line is None:
                    state.stop_reason = "lines_exhausted"
                    return
                try:
                    await self.runtime.sender.send(state.chat_id, escape(line), reply_to=None)
                except Exception as exc:  # noqa: BLE001 - one failed send must not kill the loop
                    self.logger.warning("fight loop send failed: %s", exc)
                    state.stop_reason = f"send_error:{type(exc).__name__}"
                    return
                state.sent += 1
                await asyncio.sleep(state.interval)
            state.stop_reason = state.stop_reason or "limit_reached"
        except asyncio.CancelledError:
            raise
        finally:
            if not state.stop_reason:
                state.stop_reason = "finished"

    def _next_line(
        self, state: LoopState, *, hook: str, source_text: str, seen: set[str]
    ) -> str | None:
        """A line this loop has not used yet, or None when it truly ran out."""
        recent = self._recent.setdefault(state.chat_id, {"opener": (), "middle": (), "closer": ()})
        for _attempt in range(40):
            opener, middle, closer = fight.compose_parts(
                hook,
                style=state.style,
                intensity=state.intensity,
                language=state.language,
                avoid=recent,
            )
            line = " ".join(part for part in (opener, middle, closer) if part)
            if line in seen:
                continue
            seen.add(line)
            recent["opener"] = (*recent["opener"][-3:], opener)
            recent["middle"] = (*recent["middle"][-3:], middle)
            recent["closer"] = (*recent["closer"][-2:], closer) if closer else recent["closer"]
            return line
        # 40 collisions in a row means the composer is repeating itself, which
        # only happens if the pools were gutted; stop rather than spam one line.
        return None
