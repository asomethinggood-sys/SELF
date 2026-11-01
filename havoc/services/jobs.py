"""Long work that does not live inside a reply, and does not lose its place.

A thousand-message job used to be a thousand rows in the scheduler, each one
awaited inside the tick. Two things followed from that, both of them the owner's
complaint:

* **the tick stopped ticking.** Sending twenty due jobs meant twenty waits at the
  outbound window, so the loop that also fires reminders and the profile clock
  was asleep for a minute at a time;
* **nothing could say how far along it was.** A job was a list of rows with no
  identity, so "cancel it" and "how much is left" had no answer.

This runner fixes both with the same idea the scheduler already uses for
reminders — *claim a slot, or skip it* — plus a cursor that survives a restart:

* a job is one row with a ``payload`` that carries its own progress
  (``cursor``, ``total``, ``sent``), written back every ``checkpoint`` sends, so
  a deploy that kills the process resumes instead of starting over;
* the work runs in a **background task** with a lane, so the scheduler's tick
  stays free and the outbound pacer can put interactive sends first;
* every job is reachable by id: ``.jobs`` lists them, ``.jobs cancel <id>``
  stops one, and the owner gets a progress line every so often instead of a
  wall of "sent" receipts.

The table is the existing ``jobs`` one (``kind``, ``payload``, ``status``); no
second source of truth.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .pacer import Lane, pacing

#: How often a running job writes its cursor back to the database. Small enough
#: that a killed process loses seconds of work, large enough that the write is
#: not itself the bottleneck.
CHECKPOINT_EVERY = 25


class JobError(Exception):
    """A job that cannot continue, with a sentence for the owner."""

    def __init__(self, message: str, *, kind: str = "failed") -> None:
        self.kind = kind
        super().__init__(message)


@dataclass
class JobProgress:
    """The live state of one job, as its handler sees it."""

    job_id: int
    kind: str
    total: int = 0
    done: int = 0
    failed: int = 0
    cursor: int = 0
    started_at: float = field(default_factory=time.monotonic)
    #: Set by ``cancel()``; the handler polls it instead of being killed mid-send,
    #: because a cancelled send leaves a half-typed message behind.
    cancelled: bool = False
    note: str = ""

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.done)

    @property
    def percent(self) -> int:
        if self.total <= 0:
            return 0
        return min(100, int(round(self.done * 100 / self.total)))

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def rate(self) -> float:
        """Items per minute, measured not promised."""
        elapsed = self.elapsed
        if elapsed < 1 or not self.done:
            return 0.0
        return self.done * 60.0 / elapsed

    def eta_seconds(self) -> float | None:
        rate = self.rate
        if rate <= 0:
            return None
        return self.remaining * 60.0 / rate

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.job_id,
            "kind": self.kind,
            "done": self.done,
            "total": self.total,
            "failed": self.failed,
            "percent": self.percent,
            "rate": round(self.rate, 1),
            "eta": self.eta_seconds(),
            "cancelled": self.cancelled,
            "note": self.note,
        }


Handler = Callable[["JobContext"], Awaitable[None]]


@dataclass
class JobContext:
    """What a handler gets: its progress, its payload, and a way to save both."""

    runner: JobRunner
    progress: JobProgress
    payload: dict[str, Any]

    # -- progress helpers ---------------------------------------------
    def advance(self, count: int = 1) -> None:
        self.progress.done += count
        self.progress.cursor = self.progress.done

    def fail(self, count: int = 1) -> None:
        self.progress.failed += count
        self.progress.done += count

    def stopped(self) -> bool:
        """True when the owner cancelled, or the process is shutting down."""
        return self.progress.cancelled or self.runner.closing

    async def checkpoint(self, *, force: bool = False) -> None:
        if self.progress.done % CHECKPOINT_EVERY and not force:
            return
        await self.runner.save(self.progress, self.payload)


@dataclass
class JobRunner:
    """Runs registered job kinds out of the ``jobs`` table, in the background."""

    store: Any
    logger: Any
    scheduler: Any = None
    #: Hard cap on jobs running at once. Two bulk sends at the same time would
    #: halve each other's pace and double the wait for everything else.
    max_running: int = 2
    handlers: dict[str, Handler] = field(default_factory=dict)
    running: dict[int, JobProgress] = field(default_factory=dict)
    _tasks: dict[int, asyncio.Task[None]] = field(default_factory=dict)
    closing: bool = False

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------
    def register(self, kind: str, handler: Handler) -> None:
        self.handlers[kind] = handler

    # ------------------------------------------------------------------
    # Lifecycle of one job
    # ------------------------------------------------------------------
    def busy(self) -> bool:
        return len(self._tasks) >= max(1, self.max_running)

    async def start(self, row: Any) -> bool:
        """Claim a scheduler row as a background job and run it. False if busy."""
        if self.closing or self.busy():
            return False
        job_id = int(row["id"])
        if job_id in self._tasks:
            return False
        kind = str(row["kind"])
        if kind not in self.handlers:
            return False
        claimed = await self.store.db.execute(
            "UPDATE jobs SET status = 'running', attempts = attempts + 1, updated_at = datetime('now') "
            "WHERE id = ? AND status = 'pending'",
            (job_id,),
        )
        if not claimed:
            return False
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        progress = JobProgress(
            job_id=job_id,
            kind=kind,
            total=int(payload.get("total") or 0),
            done=int(payload.get("sent") or payload.get("cursor") or 0),
            cursor=int(payload.get("cursor") or 0),
        )
        self.running[job_id] = progress
        self._tasks[job_id] = asyncio.create_task(
            self._run(kind, progress, payload), name=f"havoc-job-{job_id}"
        )
        return True

    async def _run(self, kind: str, progress: JobProgress, payload: dict[str, Any]) -> None:
        handler = self.handlers[kind]
        context = JobContext(runner=self, progress=progress, payload=payload)
        status, error = "done", ""
        try:
            # Every send a job makes is bulk: the pacer keeps a person's reply
            # ahead of it without the handler having to think about lanes.
            with pacing(Lane.BULK):
                await handler(context)
            if progress.cancelled:
                status = "cancelled"
        except asyncio.CancelledError:
            status = "pending"  # a shutdown is not a failure: resume after boot
            raise
        except Exception as exc:  # noqa: BLE001 - one job must not take the bot
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"[:300]
            self.logger.error("job %s (%s) failed: %s", progress.job_id, kind, exc)
        finally:
            self._tasks.pop(progress.job_id, None)
            self.running.pop(progress.job_id, None)
            try:
                await self.store.db.execute(
                    "UPDATE jobs SET status = ?, last_error = ?, updated_at = datetime('now') WHERE id = ?",
                    (status, error or None, progress.job_id),
                )
            except Exception:  # noqa: BLE001 - a dead db is reported elsewhere
                pass
            if status == "pending":
                # Leave the payload as-is so the next boot resumes from the cursor.
                await self.save(progress, payload)

    async def save(self, progress: JobProgress, payload: dict[str, Any]) -> None:
        body = dict(payload)
        body.update(
            {
                "sent": progress.done,
                "cursor": progress.cursor,
                "total": progress.total,
                "failed": progress.failed,
            }
        )
        try:
            await self.store.db.execute(
                "UPDATE jobs SET payload = ?, updated_at = datetime('now') WHERE id = ?",
                (json.dumps(body, ensure_ascii=False), progress.job_id),
            )
        except Exception:  # noqa: BLE001 - losing a checkpoint is not losing the job
            self.logger.debug("job %s could not checkpoint", progress.job_id)

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------
    def cancel(self, job_id: int) -> bool:
        """Ask a running job to stop at the next item. Returns whether it was running."""
        progress = self.running.get(job_id)
        if progress is None:
            return False
        progress.cancelled = True
        return True

    async def cancel_stored(self, job_id: int) -> bool:
        """Cancel a job that is waiting in the database rather than running."""
        changed = await self.store.db.execute(
            "UPDATE jobs SET status = 'cancelled', updated_at = datetime('now') "
            "WHERE id = ? AND status = 'pending'",
            (job_id,),
        )
        return bool(changed)

    def get(self, job_id: int) -> JobProgress | None:
        return self.running.get(job_id)

    def snapshot(self) -> list[dict[str, Any]]:
        return [progress.snapshot() for progress in self.running.values()]

    async def shutdown(self) -> None:
        """Stop accepting work and let running jobs checkpoint, not vanish."""
        self.closing = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


__all__ = ["JobContext", "JobError", "JobProgress", "JobRunner", "CHECKPOINT_EVERY"]
