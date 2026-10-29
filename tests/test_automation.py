"""Automation safety: gating, loop protection and every subscriber."""

from __future__ import annotations

from conftest import FakeMessage, incoming

from havoc.event_engine import EventType


async def _enable(runtime, *names: str) -> None:
    for name in names:
        await runtime.features.set_enabled(name, True)


def _chats(runtime, chat_id: int) -> list[str]:
    return [message.text for message in runtime.client.sent if message.chat_id == chat_id]


# ---------------------------------------------------------------------------
# Auto-replies
# ---------------------------------------------------------------------------
async def test_autoreply_requires_feature_enabled(runtime) -> None:
    await runtime.store.add_autoreply("price", "See the pinned message")
    await incoming(runtime, FakeMessage(id=10, chat_id=55, sender_id=77, text="what is the price?"))
    assert _chats(runtime, 55) == []


async def test_autoreply_replies_once_per_cooldown(runtime) -> None:
    await runtime.store.add_autoreply("price", "See the pinned message", cooldown=300)
    await _enable(runtime, "autoreply")
    message = FakeMessage(id=11, chat_id=55, sender_id=77, text="price?")
    await incoming(runtime, message)
    await incoming(runtime, FakeMessage(id=12, chat_id=55, sender_id=77, text="price again?"))
    assert _chats(runtime, 55) == ["See the pinned message"]


async def test_autoreply_matches_exact_and_regex(runtime) -> None:
    await runtime.store.add_autoreply("hello", "hi there", match_type="exact")
    await runtime.store.add_autoreply(r"^ping\s*\d+$", "pong", match_type="regex")
    await _enable(runtime, "autoreply")
    await incoming(runtime, FakeMessage(id=13, chat_id=56, sender_id=77, text="hello"))
    await incoming(runtime, FakeMessage(id=14, chat_id=56, sender_id=77, text="ping 42"))
    await incoming(runtime, FakeMessage(id=15, chat_id=56, sender_id=77, text="hello there"))
    assert _chats(runtime, 56) == ["hi there", "pong"]


async def test_autoreply_skips_outgoing_and_bots(runtime) -> None:
    await runtime.store.add_autoreply("hello", "hi there")
    await _enable(runtime, "autoreply")
    outgoing = FakeMessage(id=16, chat_id=57, sender_id=42, text="hello", out=True)
    await incoming(runtime, outgoing, event_type=EventType.OUTGOING)
    class Bot:
        bot = True
        id = 900
    await incoming(runtime, FakeMessage(id=17, chat_id=57, sender_id=900, text="hello", sender=Bot()))
    assert _chats(runtime, 57) == []


async def test_autoreply_does_not_answer_its_own_message(runtime) -> None:
    """Loop protection: our own reply must not trigger another reply."""
    await runtime.store.add_autoreply("hi", "hi there", cooldown=0)
    await _enable(runtime, "autoreply")
    await incoming(runtime, FakeMessage(id=18, chat_id=58, sender_id=77, text="hi"))
    # The bot's answer arrives again as an incoming message (as it would in a
    # group where the userbot sees its own outgoing message).
    await incoming(runtime, FakeMessage(id=19, chat_id=58, sender_id=77, text="hi there"))
    assert _chats(runtime, 58) == ["hi there"]


# ---------------------------------------------------------------------------
# Away mode
# ---------------------------------------------------------------------------
async def test_away_only_answers_private_chats(runtime) -> None:
    await runtime.store.set_setting("away.message", "I am away")
    await _enable(runtime, "away")
    await incoming(runtime, FakeMessage(id=20, chat_id=70, sender_id=77, text="hello", is_private=True, is_group=False))
    await incoming(runtime, FakeMessage(id=21, chat_id=71, sender_id=78, text="hello", is_private=False))
    assert _chats(runtime, 70) == ["I am away"]
    assert _chats(runtime, 71) == []


async def test_away_respects_chat_denylist(runtime) -> None:
    await runtime.store.set_setting("away.message", "I am away")
    await runtime.store.set_chat_setting(72, "automation.denied", True)
    await _enable(runtime, "away")
    await incoming(runtime, FakeMessage(id=22, chat_id=72, sender_id=77, text="hi", is_private=True, is_group=False))
    assert _chats(runtime, 72) == []


# ---------------------------------------------------------------------------
# Watches / triggers / reactions
# ---------------------------------------------------------------------------
async def test_watch_notifies_owner_once(runtime) -> None:
    await runtime.store.add_watch("urgent")
    await _enable(runtime, "watches", "notifications")
    await incoming(runtime, FakeMessage(id=23, chat_id=73, sender_id=77, text="this is URGENT please"))
    await incoming(runtime, FakeMessage(id=24, chat_id=73, sender_id=77, text="urgent again"))
    owner_chats = [message for message in runtime.client.sent if message.chat_id == 42]
    assert len(owner_chats) == 1
    assert "urgent" in owner_chats[0].text.lower()


async def test_watch_honours_chat_scope(runtime) -> None:
    await runtime.store.add_watch("invoice", scope_chat_id=74)
    await _enable(runtime, "watches", "notifications")
    await incoming(runtime, FakeMessage(id=25, chat_id=75, sender_id=77, text="invoice attached"))
    assert [m for m in runtime.client.sent if m.chat_id == 42] == []
    await incoming(runtime, FakeMessage(id=26, chat_id=74, sender_id=77, text="invoice attached"))
    assert len([m for m in runtime.client.sent if m.chat_id == 42]) == 1


async def test_trigger_template_action(runtime) -> None:
    await runtime.store.add_template("welcome", "Welcome {name}!")
    await runtime.store.add_trigger("welcome-rule", "joined the group", action_type="template", payload="welcome")
    await _enable(runtime, "triggers")
    await incoming(runtime, FakeMessage(id=27, chat_id=76, sender_id=77, text="Sara joined the group today"))
    assert any("Welcome" in text for text in _chats(runtime, 76))


async def test_trigger_notify_action_tells_owner(runtime) -> None:
    await runtime.store.add_trigger("ping-rule", "ping", action_type="notify")
    await _enable(runtime, "triggers")
    await incoming(runtime, FakeMessage(id=28, chat_id=77, sender_id=77, text="ping"))
    assert any(m.chat_id == 42 for m in runtime.client.sent)


async def test_trigger_missing_template_is_not_fatal(runtime) -> None:
    await runtime.store.add_trigger("broken", "boom", action_type="template", payload="nope")
    await _enable(runtime, "triggers")
    await incoming(runtime, FakeMessage(id=29, chat_id=78, sender_id=77, text="boom"))
    assert runtime.event_engine.subscriptions  # engine still healthy
    assert runtime.event_engine.metrics.subscriber_errors == 0


# ---------------------------------------------------------------------------
# Gating: emergency stop, quiet hours, dry-run
# ---------------------------------------------------------------------------
async def test_emergency_stop_halts_automation(runtime) -> None:
    await runtime.store.add_autoreply("status", "all good")
    await _enable(runtime, "autoreply")
    await runtime.store.set_emergency_stop(True)
    await incoming(runtime, FakeMessage(id=30, chat_id=79, sender_id=77, text="status?"))
    assert _chats(runtime, 79) == []


async def test_quiet_hours_block_automation(runtime) -> None:
    await runtime.store.add_autoreply("hello", "hi")
    await _enable(runtime, "autoreply")
    await runtime.store.set_setting("quiet_hours", {"start": "00:00", "end": "23:59", "timezone": "UTC"})
    await incoming(runtime, FakeMessage(id=31, chat_id=80, sender_id=77, text="hello"))
    assert _chats(runtime, 80) == []


async def test_dry_run_previews_instead_of_sending(runtime) -> None:
    await runtime.store.add_autoreply("hello", "hi there")
    await _enable(runtime, "autoreply")
    await runtime.permissions.set_dry_run(True)
    runtime.sender.dry_run = True
    await incoming(runtime, FakeMessage(id=32, chat_id=81, sender_id=77, text="hello"))
    # Nothing enters the chat; the owner gets a preview instead.
    assert _chats(runtime, 81) == []
    previews = _chats(runtime, 42)
    assert len(previews) == 1 and "dry" in previews[0].lower()


# ---------------------------------------------------------------------------
# Notifications and system events
# ---------------------------------------------------------------------------
async def test_deleted_message_notification(runtime) -> None:
    await _enable(runtime, "notifications")
    from havoc.event_engine import NormalizedEvent

    event = NormalizedEvent(
        type=EventType.DELETED,
        chat_id=82,
        correlation_id="del:82:5",
        extra={"deleted_ids": [5]},
    )
    await runtime.event_engine.emit(event)
    assert any(m.chat_id == 42 for m in runtime.client.sent)


async def test_event_engine_dedupes(runtime) -> None:
    await _enable(runtime, "notifications")
    from havoc.event_engine import NormalizedEvent

    event = NormalizedEvent(type=EventType.DELETED, chat_id=83, correlation_id="same", extra={"deleted_ids": [1]})
    await runtime.event_engine.emit(event)
    await runtime.event_engine.emit(event)
    assert len([m for m in runtime.client.sent if m.chat_id == 42]) == 1
    assert runtime.event_engine.metrics.dropped_duplicates == 1


async def test_subscriber_failure_is_isolated(runtime) -> None:
    async def boom(event) -> None:
        raise RuntimeError("subscriber exploded")

    runtime.event_engine.subscribe("boom", boom, {EventType.MESSAGE}, priority=1)
    await incoming(runtime, FakeMessage(id=33, chat_id=84, sender_id=77, text="hi"))
    assert runtime.event_engine.metrics.subscriber_errors == 1
    assert runtime.event_engine.subscriptions  # still registered
