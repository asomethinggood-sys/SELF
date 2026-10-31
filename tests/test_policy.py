"""One voice, and the reins.

Two promises, both of them his words:

* **One voice.** One message out of one message in. A deed that draws a picture
  and a sentence about it are *one* bubble, not two — "I get two answers" was the
  complaint. A deed that already spoke in the room adds nothing on top of itself.
* **The reins.** Per person, per room: answer, ignore, or react instead. Written
  with a name, a handle or an id; listed, and reversible.
"""

from __future__ import annotations

import pytest

from conftest import FakeMessage, incoming, invoke
from havoc.services.intent import detect

GROUP = -1001234567890
OWNER = 42
SARA = 777


class _Talker:
    """A model that only talks. Every deed below must still happen."""

    model = "talker"
    available = True

    def __init__(self, reply: str = "باشه.") -> None:
        self.reply = reply

    async def converse(self, messages, *, max_tokens: int = 400, temperature: float = 0.85) -> str:
        return self.reply


async def _persona_on(runtime) -> None:
    runtime.ai = _Talker()
    await runtime.store.set_feature("persona", True)
    await runtime.feature_manager.refresh()


def _said(runtime, since: int) -> list[str]:
    return [message.text for message in runtime.client.sent[since:]]


# ---------------------------------------------------------------------------
# One voice
# ---------------------------------------------------------------------------
async def test_a_picture_and_its_sentence_are_one_message(runtime) -> None:
    """The fortune travels in the answer, so delivery is a single send."""
    await _persona_on(runtime)
    answer = await runtime.yura_chat.answer("فال بگیر", chat_id=OWNER, language="fa")
    assert answer.text or answer.media
    if answer.media:
        # Held as bytes with a name, because the command's temp file is deleted
        # the moment it finishes.
        blob, name, *_ = answer.media[0]
        assert isinstance(blob, bytes) and blob
        assert name.endswith(".png")
        assert runtime.client.sent == [], "the picture must not also be sent on its own"


async def test_a_deed_that_spoke_adds_nothing(runtime) -> None:
    """`send --here` is the message; a receipt under it is a second answer."""
    runtime.ai = _Talker()
    answer = await runtime.yura_chat.answer(
        "say I am a good girl and repeat 2 times", chat_id=GROUP, language="en"
    )
    assert answer.text == "" or answer.media == []
    said = [message.text for message in runtime.client.sent if message.chat_id == GROUP]
    assert "I am a good girl" in said
    assert not any("⟡ sent" in text for text in said), said


# ---------------------------------------------------------------------------
# The reins: the store
# ---------------------------------------------------------------------------
async def test_a_rule_is_per_person_and_per_room(runtime) -> None:
    await runtime.store.set_person_rule(SARA, "ignore", name="Sara")
    assert await runtime.store.is_ignored(SARA)
    # A rule written for one room does not silence them everywhere.
    await runtime.store.set_person_rule(SARA, "answer", chat_id=GROUP)
    rule = await runtime.store.person_rule(SARA, GROUP)
    assert rule["mode"] == "answer"
    rule = await runtime.store.person_rule(SARA, 999)
    assert rule["mode"] == "ignore"
    # And removing it puts them back to normal.
    await runtime.store.clear_person_rule(SARA)
    assert await runtime.store.person_rule(SARA, GROUP) is None


async def test_the_owner_cannot_be_silenced(runtime) -> None:
    said = "\n".join(message.text for message in await invoke(runtime, f".policy ignore {OWNER}", chat_id=OWNER))
    assert "not going to silence you" in said
    assert await runtime.store.person_rule(OWNER, None) is None


async def test_rules_are_listed_and_cleared(runtime) -> None:
    await invoke(runtime, f".policy ignore {SARA}", chat_id=OWNER)
    listed = "\n".join(message.text for message in await invoke(runtime, ".policy list", chat_id=OWNER))
    assert str(SARA) in listed
    await invoke(runtime, f".policy clear {SARA}", chat_id=OWNER)
    assert await runtime.store.person_rule(SARA, None) is None


async def test_the_rule_command_reads_an_emoji_word(runtime) -> None:
    await invoke(runtime, f".policy react {SARA} heart", chat_id=OWNER)
    rule = await runtime.store.person_rule(SARA, OWNER)
    assert rule["mode"] == "react" and rule["emoji"] == "❤️"


# ---------------------------------------------------------------------------
# The reins: what she actually does
# ---------------------------------------------------------------------------
async def test_ignore_means_no_answer_at_all(runtime) -> None:
    await _persona_on(runtime)
    await runtime.store.set_person_rule(SARA, "ignore", name="Sara")
    before = len(runtime.client.sent)
    await incoming(runtime, FakeMessage(id=1, chat_id=GROUP, sender_id=SARA, text="yura hi", is_group=True))
    assert _said(runtime, before) == []


async def test_react_means_a_heart_instead_of_words(runtime) -> None:
    await _persona_on(runtime)
    await runtime.store.set_person_rule(SARA, "react", emoji="❤️", name="Sara")
    before = len(runtime.client.sent)
    await incoming(
        runtime, FakeMessage(id=2, chat_id=GROUP, sender_id=SARA, text="yura hi", is_group=True)
    )
    assert _said(runtime, before) == []
    assert runtime.client.reactions[-1] == (GROUP, 2, "❤️")


async def test_answering_again_is_one_command_away(runtime) -> None:
    await _persona_on(runtime)
    await runtime.store.set_person_rule(SARA, "ignore", name="Sara")
    await runtime.store.set_person_rule(SARA, "answer", name="Sara")
    before = len(runtime.client.sent)
    await incoming(
        runtime, FakeMessage(id=3, chat_id=GROUP, sender_id=SARA, text="yura hi", is_group=True)
    )
    assert _said(runtime, before) == ["باشه."]


async def test_a_rule_for_this_chat_only(runtime) -> None:
    await _persona_on(runtime)
    await runtime.store.set_person_rule(SARA, "ignore", chat_id=GROUP, name="Sara")
    before = len(runtime.client.sent)
    await incoming(
        runtime, FakeMessage(id=4, chat_id=GROUP, sender_id=SARA, text="yura hi", is_group=True)
    )
    assert _said(runtime, before) == []
    other = -100999999999
    before = len(runtime.client.sent)
    await incoming(
        runtime, FakeMessage(id=5, chat_id=other, sender_id=SARA, text="yura hi", is_group=True)
    )
    assert _said(runtime, before) == ["باشه."], "the rule was tied to one room"


# ---------------------------------------------------------------------------
# Understanding: the words that write a rule
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "line"),
    (
        ("don't answer sara", "ignore on sara"),
        ("don't answer sara in this group", "policy ignore sara --here"),
        ("don't answer him here", "policy ignore --reply --here"),
        ("answer sara again", "ignore off sara"),
        ("react to sara with a heart", "policy react sara ❤️"),
        ("just heart sara's messages", "policy react sara ❤️"),
        ("react to sara's messages with fire", "policy react sara 🔥"),
        ("به سارا جواب نده", "policy ignore سارا"),
        ("جواب سارا نده", "policy ignore سارا"),
        ("به سارا جواب بده", "policy answer سارا"),
        ("به سارا قلب بده", "policy react سارا ❤️"),
        ("به پیامهای سارا قلب بده", "policy react سارا ❤️"),
    ),
)
def test_the_sentence_writes_the_rule(text: str, line: str) -> None:
    found = detect(text)
    assert found is not None and found.lines == (line,)


@pytest.mark.parametrize(
    "text",
    (
        "به پیامش قلب واکنش بده",  # a reaction to one message, not a rule
        "این پیام رو پاک کن",  # a delete, not a rule
        "یورا استیکر قلب بفرست",  # a sticker, not a rule about "استیکر"
        "say hi",
    ),
)
def test_a_near_miss_is_not_a_rule(text: str) -> None:
    found = detect(text)
    assert found is None or not found.lines[0].startswith("policy")
