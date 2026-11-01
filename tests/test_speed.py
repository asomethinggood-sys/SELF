"""The two-lane pacer, the job runner and the loop-lag monitor.

These exist because of a specific complaint — *a mass send takes fifty minutes
and starves everything else* — and because the first version of the fix had bugs
that only showed up under load:

* a timeout at the outbound gate reported ``retry_after = 0``, which the sender
  reads as "not retryable", so a busy minute silently dropped a message;
* ``--seed 1234`` was parsed as a boolean flag, so the number fell back into the
  prompt and "the same picture again" drew a different one;
* ``send_bulk`` called ``advance()`` on the progress object instead of the
  context, so every bulk job died on its first message.

Each of those has a test here, written against the behaviour rather than the
implementation, so a future refactor has to keep the promise.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from havoc.services.cache import RateCounter
from havoc.services.pacer import Lane, Pacer, lane_now, pacing
from havoc.services.rate_limiter import RateLimited, RateLimiter

# ---------------------------------------------------------------------------
# Lanes
# ---------------------------------------------------------------------------


async def test_bulk_yields_the_reserve_to_a_person() -> None:
    limiter = RateLimiter(outbound_limit=6, outbound_window=60.0)
    pacer = limiter.pacer
    assert pacer is not None
    assert pacer.interactive_reserve == 2  # 35% of six, never less than one

    # Four sends from a person: the account has just spoken, so bulk stops at
    # four and the remaining two are his.
    for _ in range(4):
        await limiter.acquire_outbound(lane=Lane.INTERACTIVE)
    assert pacer._bulk_ceiling(pacer.clock()) == 4  # noqa: SLF001 - the number is the contract

    with pytest.raises(TimeoutError):
        await pacer.acquire(Lane.BULK, timeout=0.15)

    started = time.monotonic()
    await limiter.acquire_outbound(lane=Lane.INTERACTIVE)
    assert time.monotonic() - started < 0.2


async def test_a_quiet_account_hands_bulk_nearly_everything() -> None:
    pacer = Pacer(per_minute=6, handover=0.2)
    assert pacer._bulk_ceiling(pacer.clock()) == 5  # noqa: SLF001 - nobody has spoken yet
    pacer._last_interactive_at = pacer.clock() - 1.0  # noqa: SLF001 - a quiet minute
    assert pacer._bulk_ceiling(pacer.clock()) == 5  # noqa: SLF001 - still one slot kept back
    for _ in range(5):
        await pacer.acquire(Lane.BULK, timeout=1.0)
    # The sixth slot is the person's, and it is there the instant they speak.
    started = time.monotonic()
    await pacer.acquire(Lane.INTERACTIVE, timeout=1.0)
    assert time.monotonic() - started < 0.2


async def test_lane_is_ambient_inside_a_job() -> None:
    assert lane_now() is Lane.INTERACTIVE
    with pacing(Lane.BULK):
        assert lane_now() is Lane.BULK
        with pacing(Lane.INTERACTIVE):
            assert lane_now() is Lane.INTERACTIVE
        assert lane_now() is Lane.BULK
    assert lane_now() is Lane.INTERACTIVE


async def test_waiters_inside_a_lane_are_served_in_order() -> None:
    # A one-second window: the order is the subject, not the waiting.
    pacer = Pacer(per_minute=2, reserve=0.0, counter=RateCounter(2, 1.0))
    order: list[int] = []

    async def waiter(index: int) -> None:
        await pacer.acquire(Lane.BULK, timeout=5)
        order.append(index)

    tasks = [asyncio.create_task(waiter(1)), asyncio.create_task(waiter(2))]
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
    assert order == [1, 2]


async def test_the_budget_itself_never_changes() -> None:
    # Ordering, not ceiling: whatever the lanes do, the window still says no at
    # the same number it always did.
    limiter = RateLimiter(outbound_limit=3, outbound_window=60.0)
    for _ in range(3):
        await limiter.acquire_outbound(lane=Lane.INTERACTIVE)
    assert limiter.pacer.counter.used() == 3  # type: ignore[union-attr]
    with pytest.raises(RateLimited):
        await limiter.acquire_outbound(timeout=0.05)


async def test_a_timeout_still_says_how_long_to_come_back() -> None:
    """The bug: a full window plus a short timeout reported ``retry 0.0s``.

    ``0`` is falsy, so the sender treated the refusal as permanent and the message
    was lost instead of retried. A wait that ends must report the real remaining
    time (or a floor of one second), never zero.
    """
    limiter = RateLimiter(outbound_limit=1, outbound_window=60, clock=lambda: 0.0)
    await limiter.acquire_outbound()
    with pytest.raises(RateLimited) as info:
        await limiter.acquire_outbound(timeout=0.05)
    assert info.value.retry_after > 0, "a retry hint of zero reads as 'never retry'"


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


async def test_a_job_runs_in_the_background_and_checkpoints(runtime) -> None:
    """A job is a row with a cursor, not a long ``await`` inside a tick."""
    from havoc.services.jobs import JobRunner
    from havoc.utils.time import utc_now

    seen: list[int] = []

    async def handler(context) -> None:
        context.progress.total = 3
        for _ in range(3):
            context.advance()
            seen.append(context.progress.done)
            await context.checkpoint(force=True)

    runner = JobRunner(store=runtime.store, logger=runtime.logger, scheduler=runtime.scheduler)
    runner.register("demo", handler)
    job_id = await runtime.scheduler.enqueue("demo", utc_now(), {"total": 3})
    rows = await runtime.db.fetch_all("SELECT * FROM jobs WHERE id = ?", (job_id,))
    assert rows, "the job row was not written"
    assert await runner.start(rows[0]) is True
    for _ in range(400):
        if not runner.snapshot():
            break
        await asyncio.sleep(0.02)
    assert seen == [1, 2, 3]
    final = await runtime.db.fetch_all("SELECT status, payload FROM jobs WHERE id = ?", (job_id,))
    assert final[0]["status"] == "done"
    assert '"cursor": 3' in final[0]["payload"]


async def test_a_cancelled_job_keeps_its_cursor() -> None:
    from havoc.services.jobs import JobContext, JobProgress

    class FakeRunner:
        closing = False

        def __init__(self) -> None:
            self.saved: list[dict] = []

        async def save(self, progress: JobProgress, payload: dict) -> None:
            self.saved.append({"done": progress.done, "cursor": progress.cursor})

    runner = FakeRunner()
    progress = JobProgress(job_id=1, kind="demo", total=10)
    context = JobContext(runner=runner, progress=progress, payload={})
    context.advance()
    context.advance()
    progress.cancelled = True
    assert context.stopped() is True
    await context.checkpoint(force=True)
    assert runner.saved[-1]["cursor"] == 2


# ---------------------------------------------------------------------------
# Loop lag
# ---------------------------------------------------------------------------


async def test_lag_measures_a_blocked_loop() -> None:
    from havoc.services.lag import LoopLagMonitor

    # The interval has a 0.25s floor, so the stall has to be longer than that.
    monitor = LoopLagMonitor(interval=0.25)
    monitor.start()
    try:
        await asyncio.sleep(0.1)
        time.sleep(0.45)  # a blocking call: exactly what the monitor exists to catch
        await asyncio.sleep(0.4)
        assert monitor.ticks > 0
        assert monitor.worst_ms >= 200, monitor.snapshot()
        assert monitor.percentile(0.95) >= 100
        assert "ms" in monitor.sentence("en")
        assert monitor.snapshot()["p95_ms"] >= 100
    finally:
        await monitor.stop()


async def test_lag_sentence_localized() -> None:
    from havoc.services.lag import LoopLagMonitor

    monitor = LoopLagMonitor()
    english = monitor.sentence("en")
    persian = monitor.sentence("fa")
    assert english and persian and english != persian


# ---------------------------------------------------------------------------
# Flag parsing
# ---------------------------------------------------------------------------


def test_seed_is_a_value_flag() -> None:
    """``--seed 1234`` must keep the number, not turn it into a boolean.

    As a boolean flag the number fell back into the prompt and the seed became
    the string ``"true"``, so every "same picture again" drew a different one.
    """
    from havoc.utils.parsing import extract_flags, tokenize

    tokens = tokenize("a cat --seed 1234")
    positional, flags, consumed = extract_flags(tokens)
    assert flags.get("seed") == "1234"
    assert "seed" in consumed
    assert positional == ["a", "cat"]


# ---------------------------------------------------------------------------
# Local model + router
# ---------------------------------------------------------------------------


async def test_local_model_is_a_spare_then_the_engine() -> None:
    from havoc.services.local_model import LocalModel

    model = LocalModel(base_url="http://127.0.0.1:8080/v1", model="tiny")
    assert model.configured is True
    # Configured, not ready: nothing has answered yet, and nothing claims it has.
    assert model.available is False
    assert model.active_label == "local"

    from havoc.services.ai_router import AIRouter

    router = AIRouter([])
    assert router.available is False
    router.local = model
    assert router.available is True, "a configured local model is an available provider"
    assert router.describe()["local"]["provider"] == "local"


def test_no_local_model_means_no_change() -> None:
    from havoc.services.local_model import LocalModel

    model = LocalModel(base_url="")
    assert model.configured is False
    assert model.available is False
