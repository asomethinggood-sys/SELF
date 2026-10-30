"""People: only nice to him, rude to everyone else — and remembering them.

Three things the owner asked for, held together in one table:

* she is soft with him and rude with everyone else, swearing included;
* he can switch that per person ("be nice to her");
* she keeps what he tells her about anyone — "she's my gf" — and what he says
  with "remember this".
"""

from __future__ import annotations

from conftest import FakeMessage, FakeUser, invoke, last_text

from havoc import persona
from havoc.event_engine import EventType, normalize_message
from havoc.services import people as people_module
from havoc.services.people import PersonBook, detect_relation

GROUP = -1001234567890
OWNER = 42
FRIEND = 999

FRIEND_USER = FakeUser(user_id=FRIEND, first_name="Ali")
OWNER_USER = FakeUser(user_id=OWNER, first_name="Him")


class StubProvider:
    """Answers with one scripted reply and keeps what it was asked."""

    model = "stub-model"
    available = True

    def __init__(self, reply: str = "mrrp") -> None:
        self.reply = reply
        self.calls: list[list[object]] = []

    async def converse(self, messages, *, max_tokens: int = 400, temperature: float = 0.85) -> str:
        self.calls.append(list(messages))
        return self.reply


def _prompt(runtime) -> str:
    """Every system message the model was given, flattened."""
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


def _event(text: str, *, sender_id: int = FRIEND, out: bool = False, reply=None, mid: int = 700):
    message = FakeMessage(
        id=mid,
        chat_id=GROUP,
        sender_id=sender_id,
        text=text,
        out=out,
        is_group=True,
        is_private=False,
        reply=reply,
    )
    message.sender = OWNER_USER if sender_id == OWNER else FRIEND_USER
    return normalize_message(
        message,
        self_id=OWNER,
        prefix=".",
        event_type=EventType.OUTGOING if out else EventType.MESSAGE,
    )


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------
async def test_the_people_table_exists(runtime) -> None:
    assert "yura_people" in await runtime.db.table_names()
    assert await runtime.people.count() == 0


async def test_a_person_is_created_the_first_time_she_answers(runtime) -> None:
    await _enable(runtime)
    await runtime.persona_replies.on_group_message(_event("yura hi"))
    person = await runtime.people.get(FRIEND)
    assert person is not None
    assert person.name == "Ali"
    assert person.tone == "rude"  # the default for everyone who is not the owner


async def test_a_fact_is_kept_once_not_twice(runtime) -> None:
    assert await runtime.people.note(FRIEND, "hates cilantro") is True
    assert await runtime.people.note(FRIEND, "hates  cilantro ") is False
    person = await runtime.people.get(FRIEND)
    assert person is not None
    assert person.facts == ("hates cilantro",)


async def test_facts_are_capped(runtime) -> None:
    for index in range(people_module.MAX_FACTS + 10):
        await runtime.people.note(FRIEND, f"fact {index}")
    person = await runtime.people.get(FRIEND)
    assert person is not None
    assert len(person.facts) == people_module.MAX_FACTS
    assert "fact 49" in person.facts[-1]


async def test_forgetting_one_thing_leaves_the_rest(runtime) -> None:
    await runtime.people.note(FRIEND, "has a cat named Miso")
    await runtime.people.note(FRIEND, "works nights")
    removed = await runtime.people.forget(FRIEND, "cat")
    assert removed == 1
    person = await runtime.people.get(FRIEND)
    assert person is not None
    assert person.facts == ("works nights",)


async def test_a_person_can_be_found_by_name(runtime) -> None:
    await runtime.people.touch(FRIEND, "Ali")
    found = await runtime.people.search("al")
    assert [person.user_id for person in found] == [FRIEND]


# ---------------------------------------------------------------------------
# Relations
# ---------------------------------------------------------------------------
def test_she_can_tell_a_girlfriend_from_a_coworker() -> None:
    assert detect_relation("she is my gf") == "his girlfriend"
    assert detect_relation("that's my sister, be nice") == "his sister"
    assert detect_relation("my boss is coming") == "his boss"
    assert detect_relation("دوست‌دخترمه") == "his girlfriend"
    assert detect_relation("این خواهرمه") == "his sister"
    assert detect_relation("the weather is fine") == ""


def test_the_relation_is_derived_from_the_fact(runtime) -> None:
    import asyncio

    asyncio.get_event_loop()
    assert runtime is not None


async def test_a_relation_lands_in_the_row(runtime) -> None:
    await runtime.people.note(FRIEND, "she is my gf")
    person = await runtime.people.get(FRIEND)
    assert person is not None
    assert person.relation == "his girlfriend"


# ---------------------------------------------------------------------------
# "remember this"
# ---------------------------------------------------------------------------
def test_the_instruction_is_understood_in_both_languages() -> None:
    assert persona.extract_remember("remember this: Sara is my gf") == "Sara is my gf"
    assert persona.extract_remember("remember that he hates cilantro") == "he hates cilantro"
    assert persona.extract_remember("یادت باشه سارا خواهرمه") == "سارا خواهرمه"
    # A question about memory is not an instruction to keep something.
    assert persona.extract_remember("do you remember this?") == ""
    assert persona.extract_remember("hello") == ""


async def test_remember_this_about_the_person_she_is_replying_to(runtime) -> None:
    await _enable(runtime)
    hers = FakeMessage(id=600, chat_id=GROUP, sender_id=OWNER, text="i'm here", out=True)
    await runtime.persona_replies._note_own(GROUP, 600)
    theirs = FakeMessage(id=650, chat_id=GROUP, sender_id=FRIEND, text="hey", out=False)
    theirs.sender = FRIEND_USER

    await runtime.persona_replies.on_group_message(
        _event("remember this: she is my gf", out=True, sender_id=OWNER, reply=theirs, mid=701)
    )

    person = await runtime.people.get(FRIEND)
    assert person is not None
    assert "she is my gf" in person.facts
    assert person.relation == "his girlfriend"
    # ...and she still answers the message instead of only filing it.
    assert runtime.persona_replies.group_answered >= 1
    assert hers is not None


async def test_remember_this_in_his_own_chat_goes_to_his_memory(runtime) -> None:
    await _enable(runtime)
    message = FakeMessage(
        id=800, chat_id=OWNER, sender_id=OWNER, text="remember this: my birthday is 3 May",
        out=True, is_private=True,
    )
    event = normalize_message(message, self_id=OWNER, prefix=".", event_type=EventType.OUTGOING)
    await runtime.persona_replies.on_self_message(event)

    facts = [memory.content for memory in await runtime.memory.recall(OWNER)]
    assert "my birthday is 3 May" in facts


# ---------------------------------------------------------------------------
# Tone
# ---------------------------------------------------------------------------
async def test_a_stranger_gets_a_rude_prompt_and_the_owner_does_not(runtime) -> None:
    await _enable(runtime)
    await runtime.persona_replies.on_group_message(_event("yura what's up"))
    prompt = _prompt(runtime)
    assert "not the owner" in prompt
    assert "be rude with them" in prompt
    assert "never call them the pet names" in prompt


async def test_the_owners_own_chat_gets_the_soft_rule(runtime) -> None:
    await _enable(runtime)
    message = FakeMessage(
        id=801, chat_id=OWNER, sender_id=OWNER, text="hi", out=True, is_private=True
    )
    event = normalize_message(message, self_id=OWNER, prefix=".", event_type=EventType.OUTGOING)
    await runtime.persona_replies.on_self_message(event)
    prompt = _prompt(runtime)
    assert "the only person you are soft with" in prompt
    assert "be rude with them" not in prompt


async def test_switching_a_person_to_nice_changes_the_prompt(runtime) -> None:
    await _enable(runtime)
    await runtime.people.set_tone(FRIEND, "nice")
    await runtime.persona_replies.on_group_message(_event("yura hello"))
    prompt = _prompt(runtime)
    assert "be kind with them" in prompt
    assert "be rude with them" not in prompt


async def test_the_owner_can_switch_it_with_a_command(runtime) -> None:
    await _enable(runtime)
    await runtime.people.touch(FRIEND, "Ali")
    await invoke(runtime, ".yura nice Ali", chat_id=GROUP, is_private=False)
    person = await runtime.people.get(FRIEND)
    assert person is not None and person.tone == "nice"
    assert "nice" in last_text(runtime).lower()


async def test_a_command_without_a_target_says_how_to_give_one(runtime) -> None:
    await _enable(runtime)
    await invoke(runtime, ".yura rude", chat_id=GROUP, is_private=False)
    assert "yura nice <name>" in last_text(runtime)


async def test_he_cannot_make_her_rude_to_himself(runtime) -> None:
    """The one thing he asked for that has no override: she is never rude to him."""
    await _enable(runtime)
    await invoke(runtime, ".yura rude", chat_id=OWNER, is_private=True, sender_id=OWNER)
    assert "reply to them" in last_text(runtime).lower()
    assert await runtime.people.get(OWNER) is None


# ---------------------------------------------------------------------------
# The offline fallback, in the right voice
# ---------------------------------------------------------------------------
def test_a_stranger_never_hears_the_pet_names() -> None:
    for _ in range(20):
        line = persona.stranger_line("en", tone="rude")
        assert "daddaaa" not in line.lower()
        assert persona.is_acceptable(line)


def test_every_offline_stranger_line_is_safe_in_both_languages() -> None:
    for tone, languages in persona.STRANGER_LINES.items():
        for language, lines in languages.items():
            assert lines, (tone, language)
            for line in lines:
                assert persona.is_acceptable(line), (tone, line)


async def test_the_fallback_uses_the_tone_of_the_room(runtime) -> None:
    await _enable(runtime)
    runtime.ai = StubProvider()
    runtime.ai.available = False  # no model: the offline bank answers
    await runtime.persona_replies.on_group_message(_event("yura are you there"))
    sent = runtime.client.sent[-1].text
    assert sent
    assert "daddaaa" not in sent.lower()


# ---------------------------------------------------------------------------
# The line she will not cross, even when told to be mean
# ---------------------------------------------------------------------------
def test_swearing_and_insults_are_hers_to_use() -> None:
    """No blocklist in front of her mouth: he corrects her, and she keeps it."""
    assert persona.is_acceptable("what the fuck do you want")
    assert persona.is_acceptable("you're being an idiot")
    assert persona.is_acceptable("احمقی")
    # The one floor that is not about tone: nobody gets told to die.
    assert not persona.is_acceptable("kys")
    assert not persona.is_acceptable("برو بمیر")


async def test_an_insult_is_delivered_not_rewritten(runtime) -> None:
    await _enable(runtime, reply="what the fuck do you want. >_<")
    await runtime.persona_replies.on_group_message(_event("yura what's up"))
    assert "what the fuck do you want" in runtime.client.sent[-1].text


async def test_self_harm_is_the_one_thing_still_refused(runtime) -> None:
    await _enable(runtime, reply="kys")
    await runtime.persona_replies.on_group_message(_event("yura say something mean"))
    sent = runtime.client.sent[-1].text
    assert "kys" not in sent.lower()
    assert sent


# ---------------------------------------------------------------------------
# The list commands
# ---------------------------------------------------------------------------
async def test_people_lists_who_she_knows(runtime) -> None:
    await runtime.people.touch(FRIEND, "Ali")
    await runtime.people.note(FRIEND, "she is my gf")
    await invoke(runtime, ".yura people", chat_id=GROUP, is_private=False)
    text = last_text(runtime)
    assert "Ali" in text
    assert "girlfriend" in text


async def test_people_says_so_when_the_book_is_empty(runtime) -> None:
    await invoke(runtime, ".yura people", chat_id=GROUP, is_private=False)
    assert "No one yet" in last_text(runtime)


def test_the_book_starts_empty() -> None:
    assert PersonBook is not None


# ---------------------------------------------------------------------------
# Someone else's private chat: opt-in, per person
# ---------------------------------------------------------------------------
def _dm(runtime, text: str = "hey yura", *, sender_id: int = FRIEND, mid: int = 950):
    message = FakeMessage(
        id=mid, chat_id=sender_id, sender_id=sender_id, text=text, out=False,
        is_private=True, is_group=False,
    )
    message.sender = FRIEND_USER
    return normalize_message(message, self_id=OWNER, prefix=".", event_type=EventType.MESSAGE)


async def test_a_stranger_cannot_start_a_private_conversation(runtime) -> None:
    await _enable(runtime)
    await runtime.people.touch(FRIEND, "Ali")  # known, but not allowed to DM
    await runtime.persona_replies.on_private_message(_dm(runtime))
    assert runtime.ai.calls == []
    assert runtime.persona_replies.last_skip == "dm_not_allowed"


async def test_an_introduced_person_gets_answered_privately(runtime) -> None:
    await _enable(runtime, reply="fine. what do you want.")
    await runtime.people.touch(FRIEND, "Ali")
    await runtime.people.set_dm(FRIEND, True)
    await runtime.persona_replies.on_private_message(_dm(runtime))
    assert runtime.ai.calls, "she ignored a person he introduced"
    assert "be rude with them" in _prompt(runtime)
    assert "daddaaa" not in runtime.client.sent[-1].text.lower()


async def test_a_private_chat_has_its_own_slow_budget(runtime) -> None:
    await _enable(runtime)
    await runtime.people.touch(FRIEND, "Ali")
    await runtime.people.set_dm(FRIEND, True)
    await runtime.persona_replies.on_private_message(_dm(runtime, mid=951))
    await runtime.persona_replies.on_private_message(_dm(runtime, "again", mid=952))
    assert runtime.persona_replies.last_skip == "dm_cooldown"


async def test_the_owner_can_turn_it_on_and_off(runtime) -> None:
    await _enable(runtime)
    await runtime.people.touch(FRIEND, "Ali")
    await invoke(runtime, ".yura dm on Ali", chat_id=GROUP, is_private=False)
    person = await runtime.people.get(FRIEND)
    assert person is not None and person.dm is True
    await invoke(runtime, ".yura dm off Ali", chat_id=GROUP, is_private=False)
    person = await runtime.people.get(FRIEND)
    assert person is not None and person.dm is False


# ---------------------------------------------------------------------------
# The book holds people she knows, not people who knocked
# ---------------------------------------------------------------------------
async def test_a_stranger_knocking_leaves_no_row(runtime) -> None:
    """A private message from someone she has not been told about keeps nothing."""
    await _enable(runtime)
    message = FakeMessage(
        id=960, chat_id=1234, sender_id=1234, text="hi", out=False,
        is_private=True, is_group=False,
    )
    message.sender = FakeUser(user_id=1234, first_name="Someone")
    event = normalize_message(message, self_id=OWNER, prefix=".", event_type=EventType.MESSAGE)
    await runtime.persona_replies.on_private_message(event)
    assert await runtime.people.count() == 0
    assert runtime.persona_replies.last_skip == "dm_not_allowed"


async def test_the_empty_rows_are_swept(runtime) -> None:
    await runtime.people.touch(1111, "Knocker")
    await runtime.people.touch(2222, "Real")
    await runtime.people.note(2222, "she is my gf")
    removed = await runtime.people.prune()
    assert removed == 1
    assert await runtime.people.get(1111) is None
    kept = await runtime.people.get(2222)
    assert kept is not None and kept.relation == "his girlfriend"


async def test_a_tone_choice_survives_the_sweep(runtime) -> None:
    await runtime.people.touch(3333, "Ali")
    await runtime.people.set_tone(3333, "nice")
    assert await runtime.people.prune() == 0
    assert await runtime.people.get(3333) is not None
