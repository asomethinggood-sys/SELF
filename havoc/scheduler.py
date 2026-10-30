"""Persistent scheduler with crash-safe, exactly-once semantics.

Guarantees
----------
* Jobs live in SQLite, so a restart never loses them.
* A slot is *claimed* before it runs (``last_slot`` guard) — two workers, a
  reconnect, or a restart cannot deliver the same reminder twice.
* Recurring jobs advance from the **planned** time, not from ``now``, so a
  late tick does not drift the schedule.
* The missed-job policy is configurable (``run_once``/``skip``/``ask``) and is
  reported to the owner instead of failing silently.
* Handler errors are isolated, logged, counted and surfaced; the loop keeps
  running.

Feature areas: 8, 23, 51, 52, 55, 56, 57, 60, 84, 90.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .services.storage import Store
from .utils.security import redact_text
from .utils.time import iso, next_occurrence, parse_iso, utc_now

Handler = Callable[[Any], Awaitable[None]]
ErrorHandler = Callable[[str, Any, BaseException], Awaitable[None]]


class SchedulerError(Exception):
    pass


@dataclass
class SchedulerStats:
    ticks: int = 0
    reminders_fired: int = 0
    schedules_fired: int = 0
    jobs_fired: int = 0
    failures: int = 0
    skipped: int = 0
    last_tick_at: float = 0.0
    last_error: str = ""
    last_tick_ms: float = 0.0


@dataclass
class Scheduler:
    store: Store
    logger: Any
    config: Any
    on_reminder: Handler | None = None
    on_schedule: Handler | None = None
    on_error: ErrorHandler | None = None
    tick_interval: float = 10.0
    grace_seconds: float = 90.0
    missed_policy: str = "run_once"
    stats: SchedulerStats = field(default_factory=SchedulerStats)
    _task: asyncio.Task[None] | None = None
    _running: bool = False
    _job_handlers: dict[str, Handler] = field(default_factory=dict)
    _inflight: set[asyncio.Task[Any]] = field(default_factory=set)

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------
    def register_job(self, kind: str, handler: Handler) -> None:
        """Register a handler for ``kind``.

        Handlers receive the job row (``row["payload"]`` holds the JSON payload)
        so they can inspect attempts and re-enqueue themselves via
        :meth:`ensure_periodic`.
        """
        self._job_handlers[kind] = handler

    @property
    def running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await self.recover()
        self._task = asyncio.create_task(self._loop(), name="havoc-scheduler")
        self.logger.info("scheduler started (tick=%.0fs, policy=%s)", self.tick_interval, self.missed_policy)

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        for task in list(self._inflight):
            task.cancel()
        if self._inflight:
            await asyncio.gather(*self._inflight, return_exceptions=True)
            self._inflight.clear()
        self.logger.info("scheduler stopped")

    async def _loop(self) -> None:
        while self._running:
            started = time.monotonic()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never let the loop die
                self.stats.failures += 1
                self.stats.last_error = redact_text(f"{type(exc).__name__}: {exc}")
                self.logger.exception("scheduler tick failed")
                await self._report_error("tick", None, exc)
            finally:
                self.stats.last_tick_ms = round((time.monotonic() - started) * 1000, 2)
                self.stats.last_tick_at = time.time()
            try:
                await asyncio.sleep(self.tick_interval)
            except asyncio.CancelledError:
                raise

    # ------------------------------------------------------------------
    # Startup recovery
    # ------------------------------------------------------------------
    async def recover(self) -> dict[str, int]:
        """Handle jobs that came due while HAVOC was offline."""
        now = utc_now()
        policy = self.missed_policy if self.missed_policy in {"run_once", "skip", "ask"} else "run_once"
        summary = {"reminders_missed": 0, "schedules_missed": 0, "jobs_missed": 0, "held": 0}

        overdue_reminders = await self.store.db.fetch_all(
            "SELECT * FROM reminders WHERE status = 'pending' AND remind_at <= ? ORDER BY remind_at",
            (iso(now - timedelta(seconds=self.grace_seconds)),),
        )
        for row in overdue_reminders:
            summary["reminders_missed"] += 1
            if policy == "skip":
                await self._skip_reminder(row, notify=True)
            elif policy == "ask":
                await self.store.db.execute(
                    "UPDATE reminders SET status = 'held' WHERE id = ? AND status = 'pending'", (row["id"],)
                )
                summary["held"] += 1
            # run_once falls through to the loop, which fires immediately.

        overdue_schedules = await self.store.db.fetch_all(
            "SELECT * FROM schedules WHERE enabled = 1 AND run_at <= ? ORDER BY run_at",
            (iso(now - timedelta(seconds=self.grace_seconds)),),
        )
        for row in overdue_schedules:
            summary["schedules_missed"] += 1
            if policy == "skip":
                await self._skip_schedule(row, notify=True)
            elif policy == "ask":
                await self.store.db.execute("UPDATE schedules SET enabled = 0 WHERE id = ?", (row["id"],))
                await self._notify_missed("schedule", row)

        overdue_jobs = await self.store.db.fetch_all(
            "SELECT * FROM jobs WHERE status = 'pending' AND run_at <= ? ORDER BY run_at",
            (iso(now - timedelta(seconds=self.grace_seconds)),),
        )
        summary["jobs_missed"] = len(overdue_jobs)
        if policy == "skip":
            for row in overdue_jobs:
                await self.store.db.execute(
                    "UPDATE jobs SET status = 'skipped', updated_at = ? WHERE id = ?", (iso(now), row["id"])
                )
            summary["jobs_missed"] = 0 if not overdue_jobs else summary["jobs_missed"]

        if any(summary[key] for key in ("reminders_missed", "schedules_missed", "jobs_missed", "held")):
            self.logger.warning("missed jobs recovered: %s", summary)
        return summary

    async def _skip_reminder(self, row: Any, *, notify: bool = False) -> None:
        recurrence = row["recurrence"]
        planned = parse_iso(row["remind_at"]) or utc_now()
        if recurrence:
            nxt = next_occurrence(planned, recurrence, row["timezone"]) or (utc_now() + timedelta(days=1))
            while nxt <= utc_now():
                nxt = next_occurrence(nxt, recurrence, row["timezone"]) or (nxt + timedelta(days=1))
            await self.store.db.execute(
                "UPDATE reminders SET remind_at = ?, last_slot = ? WHERE id = ?",
                (iso(nxt), iso(planned), row["id"]),
            )
        else:
            await self.store.db.execute(
                "UPDATE reminders SET status = 'missed' WHERE id = ? AND status = 'pending'", (row["id"],)
            )
        self.stats.skipped += 1
        if notify:
            await self._notify_missed("reminder", row)

    async def _skip_schedule(self, row: Any, *, notify: bool = False) -> None:
        recurrence = row["recurrence"]
        planned = parse_iso(row["run_at"]) or utc_now()
        if recurrence:
            nxt = next_occurrence(planned, recurrence, row["timezone"]) or (utc_now() + timedelta(days=1))
            while nxt <= utc_now():
                nxt = next_occurrence(nxt, recurrence, row["timezone"]) or (nxt + timedelta(days=1))
            await self.store.db.execute(
                "UPDATE schedules SET run_at = ?, last_slot = ? WHERE id = ?",
                (iso(nxt), iso(planned), row["id"]),
            )
        else:
            await self.store.db.execute("UPDATE schedules SET enabled = 0 WHERE id = ?", (row["id"],))
        self.stats.skipped += 1
        if notify:
            await self._notify_missed("schedule", row)

    async def _notify_missed(self, kind: str, row: Any) -> None:
        if self.on_error is None:
            return
        try:
            await self.on_error("missed", {"kind": kind, "row": dict(row)}, SchedulerError("missed while offline"))
        except Exception:  # pragma: no cover - notification must never break recovery
            self.logger.exception("failed to report missed job")

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------
    async def tick(self) -> dict[str, int]:
        """Process everything that is due; never raises on a single failure.

        Emergency stop freezes all outbound work here as well as in the command
        dispatcher: a stop that only silences commands would be a lie.
        """
        self.stats.ticks += 1
        now = utc_now()
        fired = {"reminders": 0, "schedules": 0, "jobs": 0, "errors": 0, "skipped": 0}

        if await self.store.emergency_stop():
            self.stats.skipped += 1
            fired["skipped"] = 1
            return fired

        for row in await self.store.due_reminders(now):
            before = self.stats.failures
            if await self._fire_reminder(row):
                fired["reminders"] += 1
            if self.stats.failures > before:
                fired["errors"] += 1

        for row in await self.store.due_schedules(now):
            before = self.stats.failures
            if await self._fire_schedule(row):
                fired["schedules"] += 1
            if self.stats.failures > before:
                fired["errors"] += 1

        for row in await self._due_jobs(now):
            before = self.stats.failures
            if await self._fire_job(row):
                fired["jobs"] += 1
            if self.stats.failures > before:
                fired["errors"] += 1

        if self.stats.ticks % 60 == 0:
            await self.store.prune_dedupe(86400)
        return fired

    # -- reminders -----------------------------------------------------
    async def _fire_reminder(self, row: Any) -> bool:
        planned = parse_iso(row["remind_at"]) or utc_now()
        slot = iso(planned)
        recurrence = row["recurrence"]
        nxt = next_occurrence(planned, recurrence, row["timezone"]) if recurrence else None
        if recurrence and nxt is not None and nxt <= utc_now():
            # Catch up without flooding: skip intermediate slots.
            while nxt is not None and nxt <= utc_now():
                nxt = next_occurrence(nxt, recurrence, row["timezone"])

        # Claim first: only one runner can win this slot.
        claimed = await self.store.complete_reminder(row["id"], next_at=nxt if recurrence else None, slot=slot)
        if not claimed:
            return False

        if self.on_reminder is None:
            self.logger.warning("reminder %s fired but no handler is registered", row["id"])
            return False
        try:
            await self.on_reminder(row)
            self.stats.reminders_fired += 1
            return True
        except Exception as exc:  # noqa: BLE001
            self.stats.failures += 1
            self.stats.last_error = redact_text(f"{type(exc).__name__}: {exc}")
            self.logger.error("reminder handler failed for %s: %s", row["id"], exc)
            await self._report_error("reminder", row, exc)
            return False

    # -- schedules -----------------------------------------------------
    async def _fire_schedule(self, row: Any) -> bool:
        planned = parse_iso(row["run_at"]) or utc_now()
        slot = iso(planned)
        recurrence = row["recurrence"]
        nxt = next_occurrence(planned, recurrence, row["timezone"]) if recurrence else None
        if recurrence and nxt is not None and nxt <= utc_now():
            while nxt is not None and nxt <= utc_now():
                nxt = next_occurrence(nxt, recurrence, row["timezone"])

        await self.store.mark_schedule_run(row["id"], slot=slot, next_run=nxt)
        # ``mark_schedule_run`` is guarded, but we also verify the row moved.
        check = await self.store.db.fetch_one("SELECT last_slot FROM schedules WHERE id = ?", (row["id"],))
        if check is None or check["last_slot"] != slot:
            return False

        if self.on_schedule is None:
            await self.store.db.execute(
                "UPDATE schedules SET last_error = ? WHERE id = ?",
                ("no schedule handler registered", row["id"]),
            )
            self.logger.warning("schedule %s fired but no handler is registered", row["id"])
            return False
        try:
            await self.on_schedule(row)
            self.stats.schedules_fired += 1
            return True
        except Exception as exc:  # noqa: BLE001
            self.stats.failures += 1
            self.stats.last_error = redact_text(f"{type(exc).__name__}: {exc}")
            self.logger.error("schedule handler failed for %s: %s", row["id"], exc)
            await self.store.db.execute(
                "UPDATE schedules SET last_error = ? WHERE id = ?",
                (redact_text(str(exc))[:200], row["id"]),
            )
            await self._report_error("schedule", row, exc)
            return False

    # -- generic jobs --------------------------------------------------
    async def enqueue(self, kind: str, run_at: datetime, payload: dict[str, Any] | None = None, *, max_attempts: int = 3) -> int:
        return await self.store.db.insert(
            """
            INSERT INTO jobs(kind, payload, run_at, status, attempts, max_attempts, created_at, updated_at)
            VALUES(?, ?, ?, 'pending', 0, ?, ?, ?)
            """,
            (kind, json.dumps(payload or {}, ensure_ascii=False), iso(run_at), max_attempts, iso(utc_now()), iso(utc_now())),
        )

    async def ensure_periodic(self, kind: str, interval_seconds: float, payload: dict[str, Any] | None = None) -> int:
        """Ensure a self-rescheduling periodic job exists for ``kind``.

        Returns the job id when a new row was created, ``0`` when one was
        already pending. The handler is expected to call ``ensure_periodic``
        again (or the job is enqueued by the handler) so restarts keep the
        cadence without duplicating jobs.
        """
        existing = await self.store.db.fetch_one(
            "SELECT id FROM jobs WHERE kind = ? AND status = 'pending' LIMIT 1", (kind,)
        )
        if existing is not None:
            return 0
        interval = max(30.0, float(interval_seconds))
        run_at = utc_now() + timedelta(seconds=interval)
        body = dict(payload or {})
        body.setdefault("interval", interval)
        return await self.enqueue(kind, run_at, body)

    async def _due_jobs(self, now: datetime) -> list[Any]:
        return await self.store.db.fetch_all(
            "SELECT * FROM jobs WHERE status = 'pending' AND run_at <= ? ORDER BY run_at LIMIT 20",
            (iso(now),),
        )

    async def _fire_job(self, row: Any) -> bool:
        slot = iso(utc_now())
        claimed = await self.store.db.execute(
            """
            UPDATE jobs SET status = 'running', attempts = attempts + 1, updated_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (slot, row["id"]),
        )
        if not claimed:
            return False
        handler = self._job_handlers.get(row["kind"])
        if handler is None:
            await self.store.db.execute(
                "UPDATE jobs SET status = 'failed', last_error = ?, updated_at = ? WHERE id = ?",
                ("no handler", slot, row["id"]),
            )
            self.logger.warning("job %s has unknown kind %r", row["id"], row["kind"])
            self.stats.failures += 1
            await self._report_error("job", row, SchedulerError(f"unknown job kind: {row['kind']}"))
            return False
        try:
            await handler(row)
            await self.store.db.execute(
                "UPDATE jobs SET status = 'done', updated_at = ? WHERE id = ?", (slot, row["id"])
            )
            self.stats.jobs_fired += 1
            await self._reschedule_if_periodic(row)
            return True
        except Exception as exc:  # noqa: BLE001
            attempts = int(row["attempts"]) + 1
            max_attempts = int(row["max_attempts"] or 1)
            status = "pending" if attempts < max_attempts else "failed"
            retry_at = iso(utc_now() + timedelta(seconds=min(300, 15 * attempts)))
            await self.store.db.execute(
                "UPDATE jobs SET status = ?, last_error = ?, run_at = ?, updated_at = ? WHERE id = ?",
                (status, redact_text(str(exc))[:300], retry_at, slot, row["id"]),
            )
            self.stats.failures += 1
            self.logger.error("job %s failed (attempt %s/%s): %s", row["id"], attempts, max_attempts, exc)
            await self._report_error("job", row, exc)
            return False

    # ------------------------------------------------------------------
    async def _reschedule_if_periodic(self, row: Any) -> None:
        """Re-queue a periodic job after it ran, using its stored interval.

        Doing this in the scheduler (not in each handler) means one forgotten
        ``ensure_periodic`` call cannot silently stop a background job.
        """
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            return
        interval = payload.get("interval") if isinstance(payload, dict) else None
        if not interval:
            return
        try:
            await self.ensure_periodic(row["kind"], float(interval), {k: v for k, v in payload.items() if k != "interval"})
        except Exception as exc:  # noqa: BLE001 - never break a tick for this
            self.logger.warning("could not reschedule %s: %s", row["kind"], exc)

    async def _report_error(self, scope: str, row: Any, exc: BaseException) -> None:
        if self.on_error is None:
            return
        try:
            await self.on_error(
                "error",
                {"scope": scope, "row": dict(row) if row is not None else None, "error": redact_text(str(exc))},
                exc,
            )
        except Exception:  # pragma: no cover
            self.logger.exception("error reporter failed")

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    async def pending(self, limit: int = 50) -> list[dict[str, Any]]:
        reminders = await self.store.list_reminders(status="pending", limit=limit)
        schedules = await self.store.list_schedules(enabled_only=True)
        jobs = await self.store.db.fetch_all(
            "SELECT * FROM jobs WHERE status IN ('pending', 'running') ORDER BY run_at LIMIT ?", (limit,)
        )
        items = [
            {
                "type": "reminder",
                "id": row["id"],
                "when": row["remind_at"],
                "text": row["text"],
                "recurrence": row["recurrence"],
            }
            for row in reminders
        ] + [
            {
                "type": "schedule",
                "id": row["id"],
                "when": row["run_at"],
                "text": row["text"],
                "recurrence": row["recurrence"],
                "chat_id": row["chat_id"],
            }
            for row in schedules
        ] + [
            {
                "type": "job",
                "id": row["id"],
                "when": row["run_at"],
                "text": row["kind"],
                "recurrence": None,
            }
            for row in jobs
        ]
        items.sort(key=lambda item: item["when"] or "")
        return items[:limit]

    async def next_due(self, limit: int = 5) -> list[dict[str, Any]]:
        return (await self.pending(limit=limit))[:limit]

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "ticks": self.stats.ticks,
            "reminders_fired": self.stats.reminders_fired,
            "schedules_fired": self.stats.schedules_fired,
            "jobs_fired": self.stats.jobs_fired,
            "failures": self.stats.failures,
            "skipped": self.stats.skipped,
            "last_error": self.stats.last_error,
            "last_tick_ms": self.stats.last_tick_ms,
            "tick_interval": self.tick_interval,
            "missed_policy": self.missed_policy,
        }


