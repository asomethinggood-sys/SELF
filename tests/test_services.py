"""Supporting services: cache, rate limiter, sender, health, scheduler display."""

from __future__ import annotations

import asyncio

import pytest
from conftest import FakeChat, FakeMessage

from havoc.health import Health
from havoc.services.cache import TTLCache
from havoc.services.rate_limiter import RateLimiter
from havoc.services.sender import Sender, SendError


# ---------------------------------------------------------------------------
# TTLCache
# ---------------------------------------------------------------------------
async def test_ttl_cache_expires() -> None:
    cache: TTLCache[str] = TTLCache(maxsize=2, ttl=0.05)
    cache.set("a", "1")
    assert cache.get("a") == "1"
    await asyncio.sleep(0.08)
    assert cache.get("a") is None


async def test_ttl_cache_evicts_oldest() -> None:
    cache: TTLCache[int] = TTLCache(maxsize=2, ttl=60)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)
    assert cache.get("a") is None
    assert cache.get("c") == 3


async def test_ttl_cache_pop_and_clear() -> None:
    cache: TTLCache[int] = TTLCache(maxsize=4, ttl=60)
    cache.set("a", 1)
    assert cache.pop("a") == 1
    assert cache.pop("a") is None
    cache.set("b", 2)
    cache.clear()
    assert len(cache) == 0


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------
async def test_cooldown_blocks_repeat() -> None:
    limiter = RateLimiter(outbound_limit=100)
    assert limiter.cooldown_remaining("k", 60) == 0
    limiter.run("k", 60)
    assert limiter.cooldown_remaining("k", 60) > 0
    limiter.touch("other")
    assert limiter.cooldown_remaining("k", 0) == 0


async def test_outbound_budget_is_enforced() -> None:
    from havoc.services.rate_limiter import RateLimited

    # A frozen clock keeps the budget full, so the wait times out immediately.
    limiter = RateLimiter(outbound_limit=2, outbound_window=60, clock=lambda: 0.0)
    await limiter.acquire_outbound()
    await limiter.acquire_outbound()
    with pytest.raises(RateLimited) as info:
        await limiter.acquire_outbound(timeout=0.01)
    assert info.value.scope == "outbound"
    assert limiter.blocked_reason if hasattr(limiter, "blocked_reason") else True


async def test_flood_wait_blocks_sends() -> None:
    from havoc.services.rate_limiter import RateLimited

    limiter = RateLimiter(outbound_limit=10, clock=lambda: 0.0)
    limiter.note_flood_wait(30)
    assert limiter.flood_remaining() == 30
    with pytest.raises(RateLimited):
        limiter.check_flood()


# ---------------------------------------------------------------------------
# Sender
# ---------------------------------------------------------------------------
async def test_sender_splits_long_messages(runtime) -> None:
    sender: Sender = runtime.sender
    long_text = "word " * 2000
    result = await sender.send(100, long_text)
    assert result.ok
    assert len(runtime.client.sent) >= 2
    assert all(len(message.text) <= 4096 for message in runtime.client.sent)


async def test_sender_dry_run_does_not_touch_telegram(runtime) -> None:
    runtime.sender.dry_run = True
    result = await runtime.sender.send(100, "hello")
    assert result.dry_run is True
    assert runtime.client.sent == []


async def test_sender_wraps_errors(runtime) -> None:
    runtime.client.fail_with = RuntimeError("network")
    with pytest.raises(SendError) as info:
        await runtime.sender.send(100, "hello")
    assert info.value.kind
    runtime.client.fail_with = None


async def test_sender_rejects_empty_text(runtime) -> None:
    with pytest.raises(SendError):
        await runtime.sender.send(100, "   ")


async def test_respond_targets_event_chat(runtime) -> None:
    from conftest import FakeEvent

    message = FakeMessage(id=1, chat_id=321, text="hi")
    event = FakeEvent(message, chat_id=321)
    await runtime.sender.respond(event, "answer")
    assert runtime.client.sent[-1].chat_id == 321


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
async def test_health_tracks_state(runtime) -> None:
    health: Health = runtime.health
    health.mark_disconnected()
    assert health.connected is False
    health.mark_reconnect()
    assert health.connected is True and health.reconnects == 1
    health.mark_flood_wait()
    assert health.flood_waits == 1


async def test_health_snapshot_and_checks(runtime) -> None:
    snapshot = await runtime.health.snapshot(runtime)
    assert snapshot["connected"] is True
    assert "database" in snapshot and "schema_version" in snapshot
    checks = {check.name: check for check in await runtime.health.self_check(runtime)}
    assert checks
    # Hard failures are errors; "session file" and "scheduler ticks" are
    # expected warnings on a fresh, not-yet-logged-in runtime.
    assert all(check.ok for check in checks.values() if check.severity == "error")
    assert checks["database"].ok is True
    assert checks["localization"].ok is True
    assert checks["session file"].severity == "warning"


async def test_health_records_errors_with_redaction(runtime) -> None:
    runtime.health.record_error("unit", RuntimeError("token 123456:abcdefghijklmnop leaked"))
    entry = runtime.health.errors[-1]
    assert "abcdefghijklmnop" not in entry["message"]


async def test_metrics_counters(runtime) -> None:
    runtime.metrics.count("commands")
    runtime.metrics.count("commands", 2)
    assert runtime.metrics.snapshot()["counters"]["commands"] == 3


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------
async def test_chat_link_and_display_name() -> None:
    from havoc.features.helpers import chat_link, display_name

    chat = FakeChat(chat_id=100, title="Test Chat", username="testchat")
    assert display_name(chat, "fallback") == "Test Chat"
    assert "testchat" in chat_link(chat) or "100" in chat_link(chat)


async def test_human_size_and_duration() -> None:
    from havoc.features.helpers import human_duration, human_size

    assert human_size(2048) == "2.0 KB"
    # Media durations use the familiar mm:ss form.
    assert human_duration(90) == "1:30"
    assert human_duration(3661) == "1:01:01"
    assert human_duration(None) == "—"


async def test_a_file_is_sent_once_with_its_caption(runtime) -> None:
    """Media must not be re-sent once per caption chunk."""
    long_caption = "word " * 1500  # far past one message
    with pytest.raises(SendError) as info:
        await runtime.sender.send(100, long_caption, file="photo.jpg", caption=long_caption)
    assert info.value.kind == "caption_too_long"
    assert [m for m in runtime.client.sent if "file" in m.kwargs] == []


async def test_a_file_with_a_short_caption_is_sent_once(runtime) -> None:
    result = await runtime.sender.send(100, "look", file="photo.jpg", caption="look")
    assert result.ok
    files = [message for message in runtime.client.sent if "file" in message.kwargs]
    assert len(files) == 1
    assert files[0].text == "look"


async def test_sending_a_file_without_a_caption_works(runtime) -> None:
    result = await runtime.sender.send_media(100, "photo.jpg")
    assert result.ok
    files = [message for message in runtime.client.sent if "file" in message.kwargs]
    assert len(files) == 1
    assert files[0].text == ""


async def test_an_album_is_passed_through(runtime) -> None:
    await runtime.sender.send_media(100, ["a.png", "b.png"], caption="two")
    files = [message for message in runtime.client.sent if "file" in message.kwargs]
    assert files[0].kwargs["file"] == ["a.png", "b.png"]


async def test_a_media_send_is_dry_run_aware(runtime) -> None:
    runtime.sender.dry_run = True
    result = await runtime.sender.send_media(100, "photo.jpg")
    assert result.dry_run is True
    assert runtime.client.sent == []
