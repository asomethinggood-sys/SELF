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
        ("don't answer sara", "policy ignore sara"),
        ("don't answer sara in this group", "policy ignore sara --here"),
        ("don't answer him here", "policy ignore --reply --here"),
        ("answer sara again", "policy answer sara"),
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


# ---------------------------------------------------------------------------
# Two words on the end used to break the whole sentence
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "line"),
    (
        # His exact complaint: without the tail it worked, with it she "just talked".
        ("یورا جواب زیرو رو بده از این به بعد", "policy answer زیرو"),
        ("یورا جواب زیرو رو بده", "policy answer زیرو"),
        ("از این به بعد جواب زیرو رو نده", "policy ignore زیرو"),
        ("don't answer sara from now on", "policy ignore sara"),
        ("answer sara again, from now on", "policy answer sara"),
        ("heart sara from now on", "policy react sara ❤️"),
        # A leading "for good" is the same sentence too.
        ("for good don't answer sara", "policy ignore sara"),
    ),
)
def test_a_permanent_tail_is_not_part_of_the_rule(text: str, line: str) -> None:
    found = detect(text)
    assert found is not None and found.lines[0] == line


def test_a_leading_forever_still_means_the_plain_sentence() -> None:
    assert detect("برای همیشه ساکت شو").lines == ("yura off",)


# ---------------------------------------------------------------------------
# A rule with a lifespan ends by itself
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "line"),
    (
        ("don't answer sara for two hours", "policy ignore sara --for 2h"),
        ("جواب سارا رو نده برای دو ساعت", "policy ignore سارا --for 2h"),
        ("به سارا قلب بده برای یک ساعت", "policy react سارا ❤️ --for 1h"),
    ),
)
def test_a_window_is_read_as_a_lifespan(text: str, line: str) -> None:
    assert detect(text).lines[0] == line


async def test_a_temporary_rule_stops_being_a_rule(runtime) -> None:
    await runtime.store.set_person_rule(SARA, "ignore", name="Sara", seconds=-60)
    # Already past its window: the row is gone on the way past.
    assert await runtime.store.person_rule(SARA, None) is None
    assert await runtime.store.person_rules() == []


async def test_the_window_reaches_the_store(runtime) -> None:
    await invoke(runtime, f".policy ignore {SARA} --for 2h", chat_id=OWNER)
    rule = await runtime.store.person_rule(SARA, None)
    assert rule["mode"] == "ignore" and rule["expires_at"]


# ---------------------------------------------------------------------------
# Per person: a sticker instead of words
# ---------------------------------------------------------------------------
def test_the_sentence_asks_for_a_sticker_rule() -> None:
    assert detect("به سارا استیکر بده").lines == ("policy sticker سارا ❤️",)
    assert detect("answer sara with a sticker").lines == ("policy sticker sara ❤️",)


async def test_sticker_mode_never_answers_in_words(runtime) -> None:
    await _persona_on(runtime)
    await runtime.store.set_person_rule(SARA, "sticker", emoji="❤️", name="Sara")
    before = len(runtime.client.sent)
    await incoming(
        runtime, FakeMessage(id=9, chat_id=GROUP, sender_id=SARA, text="yura hi", is_group=True)
    )
    assert _said(runtime, before) == []


# ---------------------------------------------------------------------------
# The model has the same reins in its hands
# ---------------------------------------------------------------------------
async def test_the_model_can_write_a_rule(runtime) -> None:
    from havoc.services.actions import Turn
    from havoc.services.tools import build_tools

    tools = {tool.name: tool for tool in build_tools(runtime)}
    assert "reply_rule" in tools
    turn = Turn(chat_id=GROUP, actor_id=OWNER, language="en", is_owner=True)
    result = await tools["reply_rule"].run(turn, {"mode": "ignore", "who": str(SARA), "for": "2h"})
    assert result.ok, result.detail
    rule = await runtime.store.person_rule(SARA, GROUP)
    assert rule["mode"] == "ignore" and rule["expires_at"]


async def test_the_model_cannot_silence_him(runtime) -> None:
    """The tool reaches the same command, which refuses this already."""
    from havoc.services.tools import build_tools

    tools = {tool.name: tool for tool in build_tools(runtime)}
    from havoc.services.actions import Turn

    turn = Turn(chat_id=GROUP, actor_id=OWNER, language="en", is_owner=True)
    result = await tools["reply_rule"].run(turn, {"mode": "ignore", "who": str(OWNER)})
    assert "silence you" in (result.note or "") or not result.ok


# ---------------------------------------------------------------------------
# The reins cover every automatic answerer, not only her voice
# ---------------------------------------------------------------------------
async def test_an_auto_reply_obeys_the_rule(runtime) -> None:
    await runtime.store.add_autoreply("*", "he is busy", match_type="any", scope="any")
    await runtime.store.set_feature("autoreply", True)
    await runtime.feature_manager.refresh()
    friend = 555
    before = len(runtime.client.sent)
    await incoming(runtime, FakeMessage(id=20, chat_id=friend, sender_id=friend, text="hi", is_private=True))
    assert _said(runtime, before) == ["he is busy"]

    await runtime.store.set_person_rule(friend, "ignore", name="Bob")
    before = len(runtime.client.sent)
    await incoming(
        runtime, FakeMessage(id=21, chat_id=friend, sender_id=friend, text="hi again", is_private=True)
    )
    assert _said(runtime, before) == [], "an ignored person got an auto-reply"

    await runtime.store.set_person_rule(friend, "react", emoji="🔥", name="Bob")
    before = len(runtime.client.sent)
    await incoming(
        runtime, FakeMessage(id=22, chat_id=friend, sender_id=friend, text="hi thrice", is_private=True)
    )
    assert _said(runtime, before) == []
    assert runtime.client.reactions[-1] == (friend, 22, "🔥")


async def test_a_model_tool_call_writes_the_rule_end_to_end(runtime) -> None:
    """The other half of "she should have access to everything".

    The sentence is deliberately one no rule recognises ("be distant with
    zero from here on"): the model understood it, called `reply_rule`, and the
    rule exists — no regex had to know the words.
    """
    class ToolCaller:
        model = "toolcaller"
        available = True

        def __init__(self) -> None:
            self.rounds = 0

        async def converse_tools(self, messages, *, tools=None, max_tokens: int = 400, temperature: float = 0.85):
            self.rounds += 1
            if self.rounds == 1:
                return "okay, i'll leave zero alone", [
                    {"function": {"name": "reply_rule", "arguments": {"mode": "ignore", "who": str(SARA)}}}
                ]
            return "done — zero gets nothing from me now.", []

    runtime.ai = ToolCaller()
    answer = await runtime.yura_chat.answer("be distant with zero from here on", chat_id=GROUP, is_owner=True)
    rule = await runtime.store.person_rule(SARA, GROUP)
    assert rule is not None and rule["mode"] == "ignore"
    assert "done" in answer.text or "okay" in answer.text
