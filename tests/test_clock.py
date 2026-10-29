"""The profile clock: one update a minute, and the name really changes.

The owner asked for a clock that keeps up. So these tests measure the *cadence* —
the delay the job asks the scheduler for — rather than only that `.clock on` sets a
switch, and they push five simulated minutes through the job to prove the name
follows the time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from havoc.features import profile as profile_module


class FakeMe:
    first_name = "old"


class FakeTelegram:
    def __init__(self) -> None:
        # Per instance: a class attribute here would let one test's profile name
        # leak into the next one's.
        self.me = FakeMe()


class RecordingClient:
    """Only the one call the clock makes."""

    def __init__(self) -> None:
        self.names: list[str] = []

    async def __call__(self, request) -> None:  # noqa: D401 - Telethon-shaped call
        self.names.append(request.first_name)


class RecordingScheduler:
    def __init__(self) -> None:
        self.intervals: list[float] = []

    async def ensure_periodic(self, kind: str, interval: float, payload=None) -> int:
        self.intervals.append(interval)
        return 1


@pytest.fixture
def clock_rig(runtime, monkeypatch):
    """A runtime whose clock sees a frozen, movable 'now'."""
    runtime.telegram = FakeTelegram()
    runtime.client = RecordingClient()
    runtime.scheduler = RecordingScheduler()
    moment = {"now": datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)}

    monkeypatch.setattr(profile_module, "utc_now", lambda: moment["now"])

    def move(seconds: float) -> None:
        moment["now"] = moment["now"] + timedelta(seconds=seconds)

    return runtime, moment, move


async def _enable(runtime) -> None:
    await runtime.store.set_setting("clock.enabled", True)
    await runtime.store.set_setting("clock.template", "◷ {time}")
    await runtime.store.set_setting("clock.timezone", "UTC")


async def test_the_first_tick_sets_the_name_to_the_current_minute(clock_rig) -> None:
    runtime, moment, move = clock_rig
    await _enable(runtime)
    await profile_module.clock_job(runtime, None)
    assert runtime.client.names == ["◷ 12:00"]


async def test_the_name_follows_the_clock_every_minute(clock_rig) -> None:
    runtime, moment, move = clock_rig
    await _enable(runtime)
    await profile_module.clock_job(runtime, None)
    seen = list(runtime.client.names)
    for _ in range(5):
        move(60)
        await profile_module.clock_job(runtime, None)
        seen.extend(runtime.client.names)
    # One update per minute, each with the minute that had just started.
    assert runtime.client.names == ["◷ 12:00", "◷ 12:01", "◷ 12:02", "◷ 12:03", "◷ 12:04", "◷ 12:05"]
    assert len(runtime.client.names) == len(set(runtime.client.names))


async def test_it_does_not_re_apply_a_name_that_is_already_there(clock_rig) -> None:
    runtime, moment, move = clock_rig
    await _enable(runtime)
    await profile_module.clock_job(runtime, None)
    move(20)  # still the same minute
    await profile_module.clock_job(runtime, None)
    assert runtime.client.names == ["◷ 12:00"], "an unchanged clock name must not hit the API again"


async def test_the_next_tick_is_aimed_at_the_minute_boundary(clock_rig) -> None:
    runtime, moment, move = clock_rig
    await _enable(runtime)
    move(37)  # 12:00:37 — 23 seconds to go
    await profile_module.clock_job(runtime, None)
    interval = runtime.scheduler.intervals[-1]
    # Either the exact remainder, or a full minute when it is too short for the
    # scheduler's 30-second floor. Never "a minute from now", which drifts.
    assert interval in {23.0, profile_module.CLOCK_INTERVAL}
    assert interval <= profile_module.CLOCK_INTERVAL


async def test_the_cadence_never_drifts_past_a_minute(clock_rig) -> None:
    runtime, moment, move = clock_rig
    await _enable(runtime)
    for _ in range(30):
        await profile_module.clock_job(runtime, None)
        move(runtime.scheduler.intervals[-1])
    assert all(0 < interval <= profile_module.CLOCK_INTERVAL for interval in runtime.scheduler.intervals)


async def test_a_template_error_is_recorded_and_does_not_kill_the_clock(clock_rig) -> None:
    runtime, moment, move = clock_rig
    await _enable(runtime)
    await runtime.store.set_setting("clock.template", "◷ {nonsense}")
    await profile_module.clock_job(runtime, None)
    assert runtime.client.names == []
    # It still reschedules, so fixing the template is enough to recover.
    assert runtime.scheduler.intervals


async def test_switching_it_off_stops_the_updates(clock_rig) -> None:
    runtime, moment, move = clock_rig
    await _enable(runtime)
    await profile_module.clock_job(runtime, None)
    await runtime.store.set_setting("clock.enabled", False)
    move(60)
    await profile_module.clock_job(runtime, None)
    assert runtime.client.names == ["◷ 12:00"]


def test_the_interval_is_one_minute() -> None:
    assert profile_module.CLOCK_INTERVAL == 60.0
