"""Corrections: no filter, a memory instead.

The owner's instruction: she may swear and she may be nasty, and when she gets it
wrong he tells her — and that is the mechanism. A blocklist would have made her a
token eater that never changes. So: the correction is stored, every later prompt
carries it, and an answer that walks over it is counted so she can own it next
time. Nothing is silently rewritten.
"""

from __future__ import annotations

from conftest import FakeMessage, FakeUser, invoke, last_text

from havoc.event_engine import EventType, normalize_message
from havoc.services.lessons import LessonBook, extract_correction, forbidden_tokens

GROUP = -1001234567890
OWNER = 42
FRIEND = 999

FRIEND_USER = FakeUser(user_id=FRIEND, first_name="Ali")
OWNER_USER = FakeUser(user_id=OWNER, first_name="Him")


class StubProvider:
    model = "stub-model"
    available = True

    def __init__(self, reply: str = "mrrp") -> None:
        self.reply = reply
        self.calls: list[list[object]] = []

    async def converse(self, messages, *, max_tokens: int = 400, temperature: float = 0.85) -> str:
        self.calls.append(list(messages))
        return self.reply


def _prompt(runtime) -> str:
    parts = []
    for call in runtime.ai.calls:
        for message in call:
            if getattr(message, "role", "") == "system":
                parts.append(str(message.content))
    return "\n".join(parts)


async def _enable(runtime, reply: str = "mrrp") -> None:
    await runtime.store.set_feature("persona", True)
    await runtime.feature_manager.refresh()
    runtime.ai = StubProvider(reply)
    runtime.permissions.set_self_id(OWNER)
    runtime.persona_replies._group_last.clear()
    runtime.persona_replies._group_window.clear()


def _event(text: str, *, sender_id: int = OWNER, out: bool = True, reply=None, mid: int = 700):
    message = FakeMessage(
        id=mid, chat_id=GROUP, sender_id=sender_id, text=text, out=out,
        is_group=True, is_private=False, reply=reply,
    )
    message.sender = OWNER_USER if sender_id == OWNER else FRIEND_USER
    return normalize_message(
        message, self_id=OWNER, prefix=".",
        event_type=EventType.OUTGOING if out else EventType.MESSAGE,
    )


# ---------------------------------------------------------------------------
# Reading a correction
# ---------------------------------------------------------------------------
def test_a_correction_is_recognised_in_both_languages() -> None:
    assert extract_correction("don't say 'bro'") == "don't say 'bro'"
    assert extract_correction("stop swearing at me") == "stop swearing at me"
    assert extract_correction("be nicer to her") == "be nicer to her"
    assert extract_correction("diğer") == ""  # not a correction at all
    assert extract_correction("دیگه فحش نده") == "دیگه فحش نده"
    assert extract_correction("این کارو نکن") == "این کارو نکن"


def test_a_compliment_is_not_a_correction() -> None:
    assert extract_correction("don't say that, you're sweet") == ""
    assert extract_correction("don't be silly?") == ""


def test_the_banned_word_is_pulled_out_of_the_sentence() -> None:
    assert forbidden_tokens("don't say 'bro'") == ("bro",)
    assert forbidden_tokens("never use literally again") == ("literally",)
    assert forbidden_tokens("be nicer to her") == ()


# ---------------------------------------------------------------------------
# Keeping it
# ---------------------------------------------------------------------------
async def test_a_lesson_is_kept_once(runtime) -> None:
    first = await runtime.lessons.add("don't say 'bro'")
    assert first is not None
    assert await runtime.lessons.add("don't say 'bro' ") is None
    assert await runtime.lessons.count() == 1


async def test_a_lesson_can_be_about_one_person(runtime) -> None:
    await runtime.lessons.add("don't call her that", scope="person", subject_id=FRIEND)
    await runtime.lessons.add("stop swearing at me")
    mine = await runtime.lessons.for_person(FRIEND)
    someone_else = await runtime.lessons.for_person(12345)
    assert {lesson.rule for lesson in mine} == {"don't call her that", "stop swearing at me"}
    assert {lesson.rule for lesson in someone_else} == {"stop swearing at me"}


async def test_a_lesson_can_be_forgotten_by_id_or_phrase(runtime) -> None:
    lesson = await runtime.lessons.add("don't say 'bro'")
    assert lesson is not None
    assert await runtime.lessons.remove(f"#{lesson.id}") == 1
    await runtime.lessons.add("don't say 'bro'")
    assert await runtime.lessons.remove("bro") == 1


# ---------------------------------------------------------------------------
# Learning from what he says
# ---------------------------------------------------------------------------
async def test_saying_stop_swearing_teaches_her(runtime) -> None:
    await _enable(runtime)
    her_message = FakeMessage(id=600, chat_id=GROUP, sender_id=OWNER, text="mrrp", out=True)
    await runtime.persona_replies._note_own(GROUP, 600)
    await runtime.persona_replies.on_group_message(
        _event("yura stop swearing at me", reply=her_message, mid=701)
    )
    rules = [lesson.rule for lesson in await runtime.lessons.all()]
    assert "stop swearing at me" in rules


async def test_a_lesson_attaches_to_the_person_when_replied_to(runtime) -> None:
    await _enable(runtime)
    theirs = FakeMessage(id=650, chat_id=GROUP, sender_id=FRIEND, text="hm", out=False)
    theirs.sender = FRIEND_USER
    await runtime.persona_replies.on_group_message(
        _event("don't call her that", reply=theirs, mid=702)
    )
    rows = await runtime.lessons.for_person(FRIEND)
    assert [lesson.rule for lesson in rows] == ["don't call her that"]


async def test_a_stranger_cannot_teach_her(runtime) -> None:
    await _enable(runtime)
    await runtime.persona_replies.on_group_message(
        _event("stop swearing at me", sender_id=FRIEND, out=False, mid=703)
    )
    assert await runtime.lessons.count() == 0


# ---------------------------------------------------------------------------
# Carrying it
# ---------------------------------------------------------------------------
async def test_the_lesson_reaches_the_next_prompt(runtime) -> None:
    await _enable(runtime)
    await runtime.lessons.add("don't say 'bro'")
    her = FakeMessage(id=601, chat_id=GROUP, sender_id=OWNER, text="mrrp", out=True)
    await runtime.persona_replies._note_own(GROUP, 601)
    await runtime.persona_replies.on_group_message(_event("you there?", reply=her, mid=704))
    prompt = _prompt(runtime)
    assert "what he has taught you" in prompt
    assert "don't say 'bro'" in prompt


async def _hers(runtime, message_id: int = 600) -> FakeMessage:
    """A message from her in the group, so his reply to it is answered."""
    message = FakeMessage(id=message_id, chat_id=GROUP, sender_id=OWNER, text="mrrp", out=True)
    await runtime.persona_replies._note_own(GROUP, message_id)
    return message


async def test_walking_over_a_rule_is_counted_not_hidden(runtime) -> None:
    await _enable(runtime, reply="i said what i said, bro")
    lesson = await runtime.lessons.add("don't say 'bro'")
    assert lesson is not None
    await runtime.persona_replies.on_group_message(_event("what was that", reply=await _hers(runtime)))
    # Delivered, not rewritten.
    assert "bro" in runtime.client.sent[-1].text
    rows = await runtime.lessons.all()
    assert rows[0].times_broken == 1


async def test_the_next_turn_tells_her_she_forgot(runtime) -> None:
    await _enable(runtime, reply="bro. what.")
    await runtime.lessons.add("don't say 'bro'")
    await runtime.persona_replies.on_group_message(
        _event("hey", out=False, sender_id=FRIEND, text_reply=None, mid=705)
        if False
        else _event("yura hey", out=False, sender_id=FRIEND, mid=705)
    )
    runtime.persona_replies._group_last.clear()
    runtime.ai.reply = "sorry. hi."
    await runtime.persona_replies.on_group_message(
        _event("yura again", out=False, sender_id=FRIEND, mid=706)
    )
    prompt = _prompt(runtime)
    assert "last time you walked over this one" in prompt
    assert "forgot this 1 time" in prompt


# ---------------------------------------------------------------------------
# The commands
# ---------------------------------------------------------------------------
async def test_teach_and_rules_round_trip(runtime) -> None:
    await _enable(runtime)
    await invoke(runtime, ".yura teach don't say 'bro'", chat_id=GROUP, is_private=False)
    assert "Learned" in last_text(runtime)
    await invoke(runtime, ".yura rules", chat_id=GROUP, is_private=False)
    assert "bro" in last_text(runtime)


async def test_unteach_drops_one(runtime) -> None:
    await _enable(runtime)
    lesson = await runtime.lessons.add("don't say 'bro'")
    assert lesson is not None
    await invoke(runtime, ".yura unteach bro", chat_id=GROUP, is_private=False)
    assert await runtime.lessons.count() == 0


async def test_rules_says_so_when_there_are_none(runtime) -> None:
    await invoke(runtime, ".yura rules", chat_id=GROUP, is_private=False)
    assert "Nothing taught yet" in last_text(runtime)


def test_the_book_type_is_importable() -> None:
    assert LessonBook is not None
