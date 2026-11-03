"""Direct replies are not unsolicited automation, but retain safety gates."""
from __future__ import annotations

from conftest import FakeMessage

from havoc.event_engine import normalize_message


async def test_owner_reply_works_with_persona_automation_off(runtime):
    runtime.permissions.set_self_id(42)
    await runtime.store.set_feature("persona", False)
    await runtime.persona_replies._note_own(42, 80)
    original = FakeMessage(id=80, chat_id=42, sender_id=42, text="hello", out=True, is_private=True, is_group=False)
    runtime.yura_chat._sent[42] = {"hello": None}
    reply = FakeMessage(id=81, chat_id=42, sender_id=42, text="hello", reply=original, out=True, is_private=True, is_group=False)
    await runtime.event_engine.emit(normalize_message(reply, self_id=42, prefix="."))
    assert runtime.client.sent
    assert not await runtime.store.feature_enabled("persona")


async def test_reply_remains_recognized_when_telegram_cannot_fetch_target(runtime):
    runtime.permissions.set_self_id(42)
    await runtime.store.set_feature("persona", False)
    await runtime.persona_replies._note_own(42, 80)
    reply = FakeMessage(id=81, chat_id=42, sender_id=42, text="why?", out=True, is_private=True, is_group=False)
    reply.reply_to_msg_id = 80
    await runtime.event_engine.emit(normalize_message(reply, self_id=42, prefix="."))
    assert runtime.client.sent


async def test_verified_incoming_owner_can_repeat_her_words(runtime):
    runtime.permissions.set_self_id(42)
    runtime.permissions.add_id(77)
    await runtime.store.set_feature("persona", False)
    await runtime.persona_replies._note_own(77, 80)
    runtime.yura_chat._sent[77] = {"hello": None}
    original = FakeMessage(id=80, chat_id=77, sender_id=42, text="hello", out=True, is_private=True, is_group=False)
    reply = FakeMessage(id=81, chat_id=77, sender_id=77, text="hello", reply=original, out=False, is_private=True, is_group=False)
    await runtime.event_engine.emit(normalize_message(reply, self_id=42, prefix="."))
    assert runtime.client.sent, [(s.name, s.last_error) for s in runtime.event_engine.subscriptions if s.failures]


async def test_stranger_reply_does_not_bypass_disabled_automation(runtime):
    runtime.permissions.set_self_id(42)
    await runtime.store.set_feature("persona", False)
    await runtime.persona_replies._note_own(-1001, 80)
    original = FakeMessage(id=80, chat_id=-1001, sender_id=42, text="hello", out=True)
    reply = FakeMessage(id=81, chat_id=-1001, sender_id=88, text="what?", reply=original)
    await runtime.event_engine.emit(normalize_message(reply, self_id=42, prefix="."))
    assert not runtime.client.sent


async def test_direct_reply_still_respects_emergency_stop(runtime):
    runtime.permissions.set_self_id(42)
    await runtime.store.set_feature("persona", False)
    await runtime.store.set_emergency_stop(True)
    await runtime.persona_replies._note_own(42, 80)
    original = FakeMessage(id=80, chat_id=42, sender_id=42, text="hello", out=True, is_private=True, is_group=False)
    reply = FakeMessage(id=81, chat_id=42, sender_id=42, text="what?", reply=original, out=True, is_private=True, is_group=False)
    await runtime.event_engine.emit(normalize_message(reply, self_id=42, prefix="."))
    assert not runtime.client.sent
    assert runtime.persona_replies.last_skip == "permission:emergency_stop"
