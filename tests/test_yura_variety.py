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
        answer = await chat.answer("hey", chat_id=7, language="en", is_owner=True)
        said.append(" ".join((answer.text or "").split()))
    assert len(set(said)) == len(said), f"{len(said) - len(set(said))} repeats in 60 turns"
    assert chat.fallbacks >= 60


async def test_memory_does_not_stop_the_bank_working(runtime) -> None:
    """A chat with a long history still gets a fresh line, not a repeat of it."""
    chat = runtime.yura_chat
    runtime.ai = _Offline()
    for index in range(30):
        await chat.memory.append(5, "assistant", f"old line {index}")
    texts = [(await chat.answer("hi", chat_id=5, language="en", is_owner=True)).text for _ in range(20)]
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
    answer = await chat.answer("hey", chat_id=9, language="en", is_owner=True)
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
    """``avoid`` is honoured even when the bank is asked for many lines.

    200 turns is twice the ring buffer: a picker that only remembered the last
    sixty lines would start repeating here, which is exactly how the owner
    described the bug.
    """
    chat = runtime.yura_chat
    runtime.ai = _Offline()
    seen: set[str] = set()
    for _ in range(200):
        answer = await chat.answer("hello", chat_id=13, language="en", is_owner=True)
        text = " ".join((answer.text or "").split())
        assert text not in seen, text
        seen.add(text)


async def test_a_spent_daily_allowance_keeps_her_offline_instead_of_retrying(runtime) -> None:
    """50 free requests a day: once the gateway says so, stop asking.

    Otherwise every message in the rest of the day pays a request and a timeout
    to learn the same answer, and the owner waits for it.
    """
    from havoc.services.ai_provider import AIError

    calls: list[str] = []

    class RateLimited:
        model = "limited"
        available = True
        active_model = "limited"

        async def converse(self, messages, **kwargs):
            calls.append("request")
            raise AIError("Rate limit exceeded: free-models-per-day", kind="rate_limited")

    chat = runtime.yura_chat
    runtime.ai = RateLimited()
    first = await chat.answer("hi", chat_id=3, language="en", is_owner=True)
    assert chat.fallbacks == 1
    assert chat.quota_until is not None
    assert "rate_limited" in chat.last_error
    # She says it out loud once, with the time it ends — an offline line that
    # pretends nothing happened is what made a spent allowance look like a
    # broken bot — and then goes back to being herself.
    notice, _, line = (first.text or "").partition("\n\n")
    assert "allowance" in notice.lower()
    assert persona.is_own_line(line)

    for _ in range(5):
        answer = await chat.answer("again", chat_id=3, language="en", is_owner=True)
        assert persona.is_own_line(answer.text or ""), answer.text
    assert calls == ["request"], "kept calling a spent allowance"
    assert chat.fallbacks == 6  # every one of those turns was served offline


async def test_the_allowance_comes_back_after_the_reset(runtime, monkeypatch) -> None:
    from datetime import timedelta

    from havoc.services import yura_chat as module

    chat = runtime.yura_chat
    later = module.utc_now() + timedelta(days=1)
    monkeypatch.setattr(module, "utc_now", lambda: later)
    chat.quota_until = later - timedelta(minutes=1)
    assert chat._quota_spent() is False
    assert chat.quota_until is None


async def test_the_bank_never_repeats_even_after_every_sentence_is_used() -> None:
    """With 140 sentences, turn 321 is where a naive picker repeats."""
    used: set[str] = set()
    for turn in range(700):
        line = yura_lines.pick("en", avoid=used)
        assert line not in used, f"repeat at turn {turn}: {line}"
        used.add(line)
    assert len(used) == 700


async def test_a_long_conversation_never_repeats_a_line(runtime) -> None:
    """1,200 turns — twice the window a naive memory keeps — and not one repeat."""
    chat = runtime.yura_chat
    runtime.ai = _Offline()
    seen: set[str] = set()
    for turn in range(1200):
        answer = await chat.answer("hello", chat_id=31, language="en", is_owner=True)
        text = " ".join((answer.text or "").split())
        assert text not in seen, f"repeat at turn {turn}: {text}"
        seen.add(text)


# ---------------------------------------------------------------------------
# A rate limit is a fact to report, not a silence to hide
# ---------------------------------------------------------------------------
async def test_a_minute_rate_limit_is_announced_once_and_then_held(runtime) -> None:
    """``429`` from every key is not a broken bot — it is a short wait.

    The rotation cools each key for 90 seconds. Asking again on the next message
    spends the wait and learns nothing, so the answer is offline *and* says so,
    once per cooldown instead of once per message.
    """
    from havoc.services.ai_provider import AIError

    calls: list[str] = []

    class Throttled:
        model = "throttled"
        available = True
        active_model = "throttled"

        async def converse(self, messages, **kwargs):
            calls.append("request")
            raise AIError("429: too many requests", kind="rate_limited")

    chat = runtime.yura_chat
    runtime.ai = Throttled()
    first = await chat.answer("hi", chat_id=4, language="en", is_owner=True)
    assert chat.hold_until is not None
    assert chat.quota_until is None, "a minute is not a day"
    assert "throttled" in (first.text or "").lower()
    assert len(calls) == 1

    for _ in range(3):
        answer = await chat.answer("again", chat_id=4, language="en", is_owner=True)
        assert answer.text
    assert len(calls) == 1, "a held model must not be asked again"


async def test_the_hold_clears_when_it_expires(runtime) -> None:
    from datetime import timedelta

    from havoc.utils.time import utc_now

    chat = runtime.yura_chat
    chat.hold_until = utc_now() + timedelta(seconds=30)
    assert chat.model_until() is not None
    chat.hold_until = utc_now() - timedelta(seconds=1)
    assert chat.model_until() is None
    assert chat.hold_until is None


async def test_the_card_says_when_the_model_comes_back(runtime) -> None:
    from conftest import invoke

    from havoc.utils.time import utc_now

    runtime.yura_chat.hold_until = utc_now().replace(hour=23, minute=59, second=0, microsecond=0)
    sent = await invoke(runtime, ".ai")
    text = "\n".join(message.text for message in sent)
    assert "23:59" in text or "offline" in text.lower()
