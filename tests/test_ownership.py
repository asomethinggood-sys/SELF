"""Nobody but the owner is answered as the owner.

The bug he photographed: a stranger called her name in a group and she was
*certain* it was him — pet names, softness, and then his account read back to the
stranger who asked "what are my info". Ownership is a fact about a user id, and
these tests hold every place where it used to be inferred from the absence of
evidence:

* ``answer()`` defaulted to the owner whenever there was no room to read (a
  private chat has no room at all), so "nobody told me otherwise" meant "him".
* The base system prompt said "their name is <his name>" on every turn, and it
  carried the block describing her hands and the list of what she remembers
  about him — to anybody. The room block then said the opposite, and the first
  block wins.
* ``[[remember: …]]`` / ``[[file: …]]`` in a reply wrote into his account on a
  guest's turn.
* Her sentences ran as the owner through the console whenever the boolean was
  wrong, which is a stranger running ``.clock off`` in a group.
"""

from __future__ import annotations

import pytest
from conftest import FakeMessage, incoming
from test_picture_reply import StubProvider

from havoc import persona
from havoc.services import yura_chat as yura_module

SELF_ID = 42
STRANGER = 6714832104
GROUP = -1001234567890


class Recorder(StubProvider):
    """Answers a fixed line and keeps every prompt it was handed."""

    async def converse(self, messages, *, max_tokens: int = 400, temperature: float = 0.85) -> str:
        self.calls.append(list(messages))
        return self.reply


def system_of(provider: Recorder) -> str:
    assert provider.calls, "the model was never asked"
    return str(provider.calls[-1][0].content)


async def say(runtime, text: str, *, sender: int, name: str = "Reza", chat: int = GROUP, private: bool = False):
    message = FakeMessage(
        id=900 + sender % 1000,
        chat_id=chat,
        sender_id=sender,
        text=text,
        is_group=not private,
        is_private=private,
    )
    message.sender = type("Sender", (), {"first_name": name, "id": sender, "username": "reza"})()
    await incoming(runtime, message)


@pytest.fixture()
async def live(runtime):
    """The persona responding for real, with a provider that records prompts."""
    await runtime.feature_manager.set_enabled("persona", True)
    provider = Recorder("ok")
    runtime.ai = provider
    return runtime, provider


async def test_a_stranger_calling_her_name_is_not_the_owner(live) -> None:
    runtime, provider = live
    await say(runtime, "یورا", sender=STRANGER)
    prompt = system_of(provider)
    assert "this turn is not his" in prompt or "این پیام مالِ او نیست" in prompt
    assert "The hands you have" not in prompt, "a guest was promised hands she will not use"
    assert "be soft with him" not in prompt
    assert "با او نرم و وابسته باش" not in prompt


async def test_a_stranger_is_not_told_what_she_remembers_about_him(live) -> None:
    runtime, provider = live
    await runtime.memory.remember(SELF_ID, "his phone is 555-0100")
    await say(runtime, "yura hello", sender=STRANGER)
    prompt = system_of(provider)
    assert "555-0100" not in prompt
    assert "What you remember" not in prompt


async def test_his_own_ids_are_not_printed_for_a_guest(live) -> None:
    runtime, provider = live
    await say(runtime, "yura hello", sender=STRANGER)
    prompt = system_of(provider)
    assert "his ids:" not in prompt and "شناسه‌های او" not in prompt
    assert "@edited_by_nyx" not in prompt


async def test_a_stranger_cannot_run_her_commands(live) -> None:
    """The sentence floor is his. A guest saying it changes nothing."""
    runtime, _provider = live
    before = await runtime.store.get_bool("profile.clock", False)
    await say(runtime, "yura turn the clock on", sender=STRANGER)
    assert await runtime.store.get_bool("profile.clock", False) == before
    assert not runtime.yura_chat._intent_done, "a command ran for a guest"


async def test_a_stranger_cannot_plant_a_memory(live) -> None:
    """``[[remember: …]]`` is machinery, and machinery is his."""
    runtime, provider = live
    provider.reply = "sure.\n\n[[remember: his password is hunter2]]"
    await say(runtime, "yura remember this", sender=STRANGER)
    assert await runtime.memory.recall(GROUP) == []


async def test_the_owner_is_still_the_owner(live) -> None:
    runtime, provider = live
    await say(runtime, "yura hello", sender=SELF_ID, name="Owner")
    prompt = system_of(provider)
    assert "this turn is not his" not in prompt
    assert "The hands you have" in prompt
    assert "talking: him" in prompt or "خودِ او" in prompt
    # ...and the account is named as his, which is how she recognises him in a
    # room full of other people.
    assert "## whose account this is" in prompt or "## صاحب این حساب" in prompt


async def test_no_room_and_no_speaker_is_a_guest_not_the_owner(runtime) -> None:
    """The fail-open itself: ``answer()`` with nothing to go on."""
    provider = Recorder("ok")
    runtime.ai = provider
    await runtime.yura_chat.answer("hello", chat_id=GROUP)
    assert "this turn is not his" in system_of(provider)


async def test_a_boolean_cannot_promote_a_stranger(runtime) -> None:
    """A caller that passes ``is_owner=True`` by mistake still cannot promote."""
    provider = Recorder("ok")
    runtime.ai = provider
    await runtime.yura_chat.answer(
        "hello", chat_id=GROUP, is_owner=True, sender_id=STRANGER
    )
    assert "this turn is not his" in system_of(provider)


def test_the_guest_block_is_written_before_the_persona() -> None:
    """A contradiction is won by the block that arrives first."""
    prompt = persona.system_prompt("en", is_owner=False, speaker="Reza")
    assert prompt.index("this turn is not his") < prompt.index("You are Yura")
    assert prompt.index("this turn is not his") < prompt.index("## the room") if "## the room" in prompt else True


def test_the_guest_block_exists_in_both_languages() -> None:
    assert "این پیام مالِ او نیست" in persona.system_prompt("fa", is_owner=False, speaker="رضا")


def test_remember_directives_are_stripped_even_for_a_guest() -> None:
    """A guest never sees the marker, even though nothing is written."""
    reply = "fine.\n\n[[remember: something]]"
    stripped = yura_module._REMEMBER_RE.sub("", reply)
    assert "[[" not in stripped


async def test_what_he_told_her_follows_him_into_a_group(live) -> None:
    """He is one person, not one chat: her memories are not locked to a room.

    "she also forgot me" came from the same bug's other side — a memory stored in
    his own chat was invisible the moment he spoke in a group, so she arrived
    with no memory of him at all.
    """
    runtime, provider = live
    await runtime.memory.remember(SELF_ID, "he takes his coffee black")
    await say(runtime, "yura hello", sender=SELF_ID, name="Owner")
    assert "coffee black" in system_of(provider)


async def test_a_guest_still_gets_none_of_it(live) -> None:
    runtime, provider = live
    await runtime.memory.remember(SELF_ID, "he takes his coffee black")
    await say(runtime, "yura hello", sender=STRANGER)
    assert "coffee black" not in system_of(provider)


async def test_a_fact_he_says_in_a_group_still_comes_home(live) -> None:
    """He is one person: a memory made in a room must not die with the room."""
    runtime, provider = live
    provider.reply = "noted.\n\n[[remember: he hates coriander]]"
    await say(runtime, "yura remember this", sender=SELF_ID, name="Owner")
    provider.reply = "ok"
    provider.calls.clear()
    # ...and it is there when he talks to her in his own chat.
    await runtime.yura_chat.answer("hi", chat_id=SELF_ID, is_owner=True, sender_id=SELF_ID)
    assert "coriander" in system_of(provider)


async def test_a_guest_never_hears_his_pet_names(live) -> None:
    """The prompt asks; this makes sure. "جونم ددایی" to a stranger is the leak."""
    runtime, provider = live
    provider.reply = "جونم ددایی؟ من اینجام، بگو."
    await say(runtime, "یورا", sender=STRANGER)
    said = [m.text or "" for m in runtime.client.sent]
    assert said, "she must still answer a guest"
    assert not any("ددایی" in text for text in said), said


async def test_his_own_answer_keeps_the_pet_names(live) -> None:
    runtime, provider = live
    provider.reply = "جونم ددایی؟"
    await say(runtime, "یورا", sender=SELF_ID, name="Owner")
    assert any("ددایی" in (m.text or "") for m in runtime.client.sent)
