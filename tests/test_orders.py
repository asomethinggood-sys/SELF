"""The orders he actually types must *do* the thing, once.

Every case here is a sentence he sent and watched fail: "یورا فال بگیر",
"say I am a good girl and repeat 100 times", "به پیامش قلب واکنش بده". The rule
they share is the one he kept repeating: **the ability has to execute** — talking
about it is the bug.
"""

from __future__ import annotations

import pytest

from conftest import FakeMessage, incoming, invoke
from havoc.services.intent import detect


def _lines(text: str) -> tuple[str, ...]:
    found = detect(text)
    return found.lines if found else ()


# ---------------------------------------------------------------------------
# A sentence that carries a count
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    (
        ("say I am a good girl and repeat 100 times", "send --here I am a good girl --repeat 100"),
        ("Say i'm a good girl and repeat it 100 times", "send --here i'm a good girl --repeat 100"),
        ("repeat I am a good girl 100 times", "send --here I am a good girl --repeat 100"),
        ("send hi in 10 different messages in this group", "send --here hi --repeat 10"),
        ("send @sara boo 3 times", "send @sara boo --repeat 3"),
        ("من دختر خوبم رو ۱۰۰ بار بگو", "send --here من دختر خوبم --repeat 100"),
    ),
)
def test_a_count_in_the_sentence_is_a_repeat(text: str, expected: str) -> None:
    assert _lines(text) == (expected,)


def test_a_repeat_never_sends_nothing() -> None:
    # "3 times" with no words is not a message, and a message with no count is
    # not a repeat: both are rejected rather than guessed at.
    assert _lines("repeat 3 times") == ()
    assert _lines("say hi") == ()


# ---------------------------------------------------------------------------
# Reactions point at a message
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    (
        "به پیامش قلب واکنش بده",
        "به این پیام ❤️ بده",
        "بهش ری‌اکشن 🔥 بده",
        "ایموجی 🔥 بگیر",
    ),
)
def test_a_persian_reaction_is_read_as_one(text: str) -> None:
    lines = _lines(text)
    assert len(lines) == 1 and lines[0].startswith("emoji ")


def test_a_deleted_message_is_not_a_reaction() -> None:
    # Both mention a message and a verb; only one of them is a reaction.
    assert _lines("این پیام رو پاک کن") == ("delete",)


# ---------------------------------------------------------------------------
# Persian reminders and summaries
# ---------------------------------------------------------------------------
def test_a_persian_reminder_keeps_its_subject() -> None:
    assert _lines("یادم بنداز ۳۰ دقیقه دیگه شیر بخرم") == ("remind 30m شیر بخرم",)
    assert _lines("یادم بنداز ساعت ۵ شیر بخرم") == ("remind 05:00 شیر بخرم",)


def test_a_persian_alarm_is_not_about_the_words_reminding() -> None:
    # "فردا ساعت ۹ بهم یادآوری کن" names a time and nothing else: the reminder
    # is about a reminder, not about "بهم یادآوری کن".
    assert _lines("فردا ساعت ۹ بهم یادآوری کن") == ("remind tomorrow 09:00 یادآوری",)


def test_persian_summary_shapes() -> None:
    assert _lines("از این چت خلاصه بگیر") == ("summarize",)
    assert _lines("خلاصه این گروه") == ("summarize",)


# ---------------------------------------------------------------------------
# An order that she runs herself carries no duplicate and no scolding
# ---------------------------------------------------------------------------
async def test_the_command_answers_once_and_is_not_scolded(runtime) -> None:
    """The captured reply is the answer, not a second message.

    The dispatcher adds "here is the usage" whenever ``sent_count`` did not move.
    Capturing the text without counting it made every successful command print
    its own usage underneath — the "⌁ remind چیزی برای کار کردن می‌خواهد" line.
    """
    await runtime.yura_chat._run_intent("یادم بنداز ۳۰ دقیقه دیگه شیر بخرم", chat_id=42, language="fa")
    card = runtime.yura_chat._cards[-1]
    joined = "\n".join(card[1])
    assert "شیر بخرم" in joined
    assert ".remind" not in joined, joined
    # Nothing was sent beside her answer: one deed, one message.
    assert runtime.client.sent == []


async def test_a_repeat_is_one_now_and_the_rest_on_the_clock(runtime) -> None:
    sent = await invoke(runtime, ".send this hi --repeat 3", chat_id=-100)
    texts = [message.text for message in sent]
    assert "hi" in texts
    assert any("queued" in text for text in texts), texts
    jobs = await runtime.db.fetch_all("SELECT kind FROM jobs WHERE kind = 'reach_send'")
    assert len(jobs) == 2


async def test_this_chat_is_a_real_address(runtime) -> None:
    sent = await invoke(runtime, ".send this hello there", chat_id=-100)
    body = "\n".join(message.text for message in sent)
    assert "hello there" in body
    assert "this chat" in body


async def test_the_floor_runs_what_the_model_only_talked_about(runtime) -> None:
    """A model that answers in words still gets the deed done."""

    class WordsOnly:
        model = "words"
        available = True

        async def converse(self, messages, *, max_tokens: int = 400, temperature: float = 0.85) -> str:
            return "باشه دَدّا ✦"

    runtime.ai = WordsOnly()
    runtime.client.dialogs = []
    answer = await runtime.yura_chat.answer("یورا فال بگیر", chat_id=42, is_owner=True)
    assert answer.text
    # The fortune is drawn as a file, which is the deed; her sentence is the frame.
    assert any(getattr(message, "kwargs", {}).get("file") for message in runtime.client.sent)
