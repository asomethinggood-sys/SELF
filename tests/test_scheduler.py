"""Scheduler: reminders, scheduled messages, periodic jobs, missed runs."""

from __future__ import annotations

from datetime import timedelta

from conftest import invoke

from havoc.utils.time import iso, utc_now


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------
async def test_due_reminder_is_delivered_once(runtime) -> None:
    when = utc_now() - timedelta(seconds=1)
    reminder_id = await runtime.store.add_reminder(300, "stand up", when)
    fired = await runtime.scheduler.tick()
    assert fired["reminders"] == 1
    texts = [m.text for m in runtime.client.sent if m.chat_id == 300]
    assert any("stand up" in text for text in texts)
    row = await runtime.store.get_reminder(reminder_id)
    assert row["status"] == "done"
    # A second tick must not deliver it again.
    assert (await runtime.scheduler.tick())["reminders"] == 0


async def test_future_reminder_is_not_fired(runtime) -> None:
    await runtime.store.add_reminder(300, "later", utc_now() + timedelta(hours=1))
    assert (await runtime.scheduler.tick())["reminders"] == 0
    assert [m for m in runtime.client.sent if m.chat_id == 300] == []


async def test_recurring_reminder_reschedules(runtime) -> None:
    when = utc_now() - timedelta(seconds=1)
    reminder_id = await runtime.store.add_reminder(301, "hydrate", when, recurrence="daily")
    await runtime.scheduler.tick()
    row = await runtime.store.get_reminder(reminder_id)
    assert row["status"] == "pending"
    assert row["remind_at"] > iso(when)


async def test_reminder_respects_emergency_stop(runtime) -> None:
    await runtime.store.add_reminder(302, "should not fire", utc_now() - timedelta(seconds=1))
    await runtime.store.set_emergency_stop(True)
    await runtime.scheduler.tick()
    assert [m for m in runtime.client.sent if m.chat_id == 302] == []


# ---------------------------------------------------------------------------
# Scheduled messages
# ---------------------------------------------------------------------------
async def test_scheduled_message_is_sent_to_target_chat(runtime) -> None:
    await runtime.store.add_schedule(303, "ping at nine", utc_now() - timedelta(seconds=1))
    fired = await runtime.scheduler.tick()
    assert fired["schedules"] == 1
    assert [m.text for m in runtime.client.sent if m.chat_id == 303] == ["ping at nine"]


async def test_one_off_schedule_is_disabled_after_sending(runtime) -> None:
    schedule_id = await runtime.store.add_schedule(304, "once", utc_now() - timedelta(seconds=1))
    await runtime.scheduler.tick()
    row = await runtime.store.get_schedule(schedule_id)
    assert row["enabled"] == 0
    assert (await runtime.scheduler.tick())["schedules"] == 0


async def test_failed_send_is_recorded_not_raised(runtime) -> None:
    class Boom(Exception):
        pass

    schedule_id = await runtime.store.add_schedule(305, "explode", utc_now() - timedelta(seconds=1))
    runtime.client.fail_with = Boom("network down")
    result = await runtime.scheduler.tick()
    assert result["errors"] >= 1
    row = await runtime.store.get_schedule(schedule_id)
    assert row["last_error"]
    runtime.client.fail_with = None


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
async def test_custom_job_runs(runtime) -> None:
    calls: list[dict] = []

    async def handler(row) -> None:
        calls.append(dict(row))

    runtime.scheduler.register_job("test.job", handler)
    await runtime.scheduler.enqueue("test.job", utc_now() - timedelta(seconds=1), {"value": 1})
    fired = await runtime.scheduler.tick()
    assert fired["jobs"] == 1
    assert calls and calls[0]["kind"] == "test.job"
    assert "value" in calls[0]["payload"]


async def test_unregistered_job_is_reported(runtime) -> None:
    await runtime.scheduler.enqueue("missing.job", utc_now() - timedelta(seconds=1))
    result = await runtime.scheduler.tick()
    assert result["jobs"] == 0
    row = await runtime.store.db.fetch_one("SELECT * FROM jobs WHERE kind = 'missing.job'")
    assert row["status"] == "failed"
    # The failure is surfaced to the health dashboard, not only to the log.
    assert any("unknown job kind" in entry["message"] for entry in runtime.health.errors)


async def test_periodic_job_is_rescheduled(runtime) -> None:
    calls: list[int] = []

    async def handler(row) -> None:
        calls.append(1)

    runtime.scheduler.register_job("test.periodic", handler)
    job_id = await runtime.scheduler.ensure_periodic("test.periodic", 60)
    row = await runtime.store.db.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    assert row["kind"] == "test.periodic"
    # Move it into the past, tick, and confirm it is queued again.
    await runtime.store.db.execute(
        "UPDATE jobs SET run_at = ?, status = 'pending' WHERE id = ?",
        (iso(utc_now() - timedelta(seconds=1)), job_id),
    )
    await runtime.scheduler.tick()
    assert calls == [1]
    again = await runtime.store.db.fetch_one(
        "SELECT * FROM jobs WHERE kind = 'test.periodic' AND status = 'pending'"
    )
    assert again is not None


async def test_pending_lists_due_work(runtime) -> None:
    await runtime.store.add_reminder(306, "a", utc_now() + timedelta(minutes=5))
    await runtime.store.add_schedule(306, "b", utc_now() + timedelta(minutes=10))
    pending = await runtime.scheduler.pending()
    kinds = {item["type"] for item in pending}
    assert {"reminder", "schedule"} <= kinds


async def test_snapshot_reports_state(runtime) -> None:
    snapshot = runtime.scheduler.snapshot()
    assert snapshot["running"] is False
    assert "reminders_fired" in snapshot and "tick_interval" in snapshot


async def test_start_and_stop(runtime) -> None:
    await runtime.scheduler.start()
    assert runtime.scheduler.running is True
    await runtime.scheduler.stop()
    assert runtime.scheduler.running is False


# ---------------------------------------------------------------------------
# Recovery on startup
# ---------------------------------------------------------------------------
async def test_recover_reports_missed_work(runtime) -> None:
    await runtime.store.add_reminder(307, "missed", utc_now() - timedelta(hours=2))
    result = await runtime.scheduler.recover()
    assert result["reminders_missed"] >= 1


async def test_tasks_command_shows_pending_work(runtime) -> None:
    await runtime.store.add_reminder(308, "visible task", utc_now() + timedelta(minutes=5))
    await invoke(runtime, ".tasks")
    assert "visible task" in "\n".join(m.text for m in runtime.client.sent)
