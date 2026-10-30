"""Burst notices: information, not a flood of their own.

The owner's Saved Messages filled up with "Event burst" lines minutes apart —
the bot warning about noise by making more of it. One notice per half hour, with
the suppressed ones counted into the next, is the shape it should have had.
"""

from __future__ import annotations

import html

from havoc.event_engine import EventType, NormalizedEvent
from havoc.features import automation


def _burst(chat: int = -100123) -> NormalizedEvent:
    return NormalizedEvent(
        type=EventType.BURST,
        chat_id=chat,
        text="",
        correlation_id="burst:test",
        extra={"count": 40, "window": 10, "sample_chat": chat},
    )


class _Clock:
    """A monotonic clock the test can move."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def _enable(runtime) -> None:
    await runtime.store.set_feature("notifications", True)
    await runtime.feature_manager.refresh()


async def test_a_burst_is_reported_once(runtime, monkeypatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(automation.time, "monotonic", clock)
    await _enable(runtime)
    sent = runtime.client.sent
    before = len(sent)
    await runtime.automation.on_burst(_burst())
    assert len(sent) == before + 1
    assert "burst" in html.unescape(sent[-1].text).lower()


async def test_the_same_burst_ten_minutes_later_is_folded_in(runtime, monkeypatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(automation.time, "monotonic", clock)
    await _enable(runtime)
    before = len(runtime.client.sent)
    await runtime.automation.on_burst(_burst())
    clock.now += 600
    await runtime.automation.on_burst(_burst())
    await runtime.automation.on_burst(_burst())
    assert len(runtime.client.sent) == before + 1, "bursts inside the window must not each send"

    clock.now += 1800
    await runtime.automation.on_burst(_burst())
    latest = html.unescape(runtime.client.sent[-1].text)
    assert "burst" in latest.lower()
    # The two that were held back are counted, not forgotten.
    assert "2" in latest


async def test_no_notice_when_notifications_are_off(runtime, monkeypatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(automation.time, "monotonic", clock)
    await runtime.store.set_feature("notifications", False)
    await runtime.feature_manager.refresh()
    before = len(runtime.client.sent)
    await runtime.automation.on_burst(_burst())
    assert len(runtime.client.sent) == before
