"""Variety and the loop guard.

Two complaints, one module: she used to say the same sentence until the owner
noticed, and an ambient reply that echoed back could make her answer herself.
Both are behaviour that only shows up over many turns, so they are exercised
over many turns here instead of with a single call.
"""

from __future__ import annotations

import html

from conftest import FakeMessage, invoke, last_text

from havoc import persona, yura_lines
from havoc.event_engine import EventType, normalize_message
from havoc.services.yura_chat import YuraChat


def _event(runtime, text: str, *, chat_id: int = 42, sender_id: int = 42):
    # Saved Messages: the only chat the ambient path may ever answer.
    runtime.permissions.set_self_id(42)
    message = FakeMessage(id=901, chat_id=chat_id, sender_id=sender_id, text=text, out=True)
    return normalize_message(message, self_id=42, prefix=".", event_type=EventType.OUTGOING)


class _Scripted:
    """A working provider with one reply, for the path ``is_own_line`` cannot see."""

    model = "scripted"
    available = True

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)

    async def converse(self, messages, **kwargs):
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


class _Offline:
    """A provider that always fails, forcing the offline bank."""

    model = "dead"
    available = False

    async def converse(self, messages, **kwargs):  # pragma: no cover - never called
        raise AssertionError("offline provider must not be called")


async def test_the_bank_is_large_enough_to_be_variety() -> None:
    assert yura_lines.capacity("en") >= 3000
    assert yura_lines.capacity("fa") >= 3000
    assert persona.capacity() >= 6000


async def test_sixty_fallback_replies_never_repeat(runtime) -> None:
    """The owner's complaint, measured: 60 turns, 60 different sentences."""
    chat = runtime.yura_chat
    runtime.ai = _Offline()
    said = []
    for _ in range(60):
        answer = await chat.answer("hey", chat_id=7, language="en")
        said.append(" ".join((answer.text or "").split()))
    assert len(set(said)) == len(said), f"{len(said) - len(set(said))} repeats in 60 turns"
    assert chat.fallbacks >= 60


async def test_memory_does_not_stop_the_bank_working(runtime) -> None:
    """A chat with a long history still gets a fresh line, not a repeat of it."""
    chat = runtime.yura_chat
    runtime.ai = _Offline()
    for index in range(30):
        await chat.memory.append(5, "assistant", f"old line {index}")
    texts = [(await chat.answer("hi", chat_id=5, language="en")).text for _ in range(20)]
    assert len(set(texts)) == len(texts)


async def test_a_user_question_is_never_mistaken_for_her_own_line() -> None:
    questions = (
        "what is 2+2",
        "hey yura how are you",
        "i had a rough day",
        "سلام خوبی",
        "نیاز به کمک دارم",
        "daddaaa are you there",
    )
    for question in questions:
        assert persona.is_own_line(question) is False, question


async def test_every_composed_line_is_recognised_as_hers() -> None:
    for language in ("en", "fa"):
        for choice in range(500):
            line = persona.build_line(language, choice=choice)
            assert persona.is_own_line(line) is True, line


async def test_the_loop_guard_remembers_what_this_process_sent(runtime) -> None:
    chat: YuraChat = runtime.yura_chat
    runtime.ai = _Offline()
    answer = await chat.answer("hey", chat_id=9, language="en")
    assert chat.was_sent(9, answer.text or "") is True
    assert chat.was_sent(9, "   " + (answer.text or "") + "  ") is True  # spacing insensitive
    assert chat.was_sent(11, answer.text or "") is False  # other chat, other memory
    assert chat.was_sent(9, "what is 2+2") is False


async def test_an_echo_of_her_own_reply_is_skipped(runtime) -> None:
    """The ambient path must not answer a message this process just sent."""
    await runtime.store.set_feature("persona", True)
    await runtime.feature_manager.refresh()
    runtime.ai = _Offline()

    await invoke(runtime, ".yura hey", chat_id=42)
    first = html.unescape(last_text(runtime))
    assert first, "nothing was sent, so the echo test proves nothing"
    assert runtime.yura_chat.was_sent(42, first) is True

    runtime.client.sent.clear()
    before = runtime.persona_replies.skipped
    await runtime.persona_replies.on_self_message(_event(runtime, first))
    assert runtime.client.sent == [], "she answered her own message"
    assert runtime.persona_replies.skipped == before + 1


async def test_a_model_sentence_echoed_back_is_also_skipped(runtime) -> None:
    """``is_own_line`` cannot know model output, so memory has to cover it."""
    await runtime.store.set_feature("persona", True)
    await runtime.feature_manager.refresh()
    sentence = "mrrp. i have decided you are my favourite person in this server >_<"
    assert persona.is_own_line(sentence) is False  # nothing about it is in the bank
    runtime.ai = _Scripted([sentence])

    runtime.client.sent.clear()
    await runtime.persona_replies.on_self_message(_event(runtime, "i had a long day"))
    assert html.unescape(runtime.client.sent[-1].text) == sentence

    runtime.client.sent.clear()
    before = runtime.persona_replies.skipped
    await runtime.persona_replies.on_self_message(_event(runtime, sentence))
    assert runtime.client.sent == [], "the model's own sentence came back and was answered"
    assert runtime.persona_replies.skipped == before + 1


async def test_offline_lines_avoid_lethal_and_isolating_content() -> None:
    """``BANNED_FRAGMENTS`` over the sample *and* over live draws."""
    for line in persona.all_lines():
        assert persona.is_safe(line), line
    for language in ("en", "fa"):
        for _ in range(300):
            assert persona.is_safe(persona.build_line(language))


async def test_the_same_sentence_does_not_come_back_within_the_recent_window(runtime) -> None:
    """``avoid`` is honoured even when the bank is asked for many lines."""
    chat = runtime.yura_chat
    runtime.ai = _Offline()
    seen: set[str] = set()
    for _ in range(100):
        answer = await chat.answer("hello", chat_id=13, language="en")
        text = " ".join((answer.text or "").split())
        assert text not in seen, text
        seen.add(text)
