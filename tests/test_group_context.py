"""Who is talking, and what about.

The owner reported three things, all in the same breath:

* replying to her in a group still did nothing (other people's replies worked);
* she answered a friend as if the friend were the owner;
* "yura what do you think" about a photo someone had just posted reached her as
  a question about nothing.

The first is a restart gap — her sent message ids are now persisted. The other
two are the room: every group turn now carries the speaker and the last lines of
the chat.
"""

from __future__ import annotations

from conftest import FakeMessage, FakeUser
from test_picture_reply import StubProvider, _all_text

from havoc.event_engine import EventType, normalize_message
from havoc.services.context import MAX_LINE_CHARS, ChatContext, build

GROUP = -1001234567890
OWNER = 42
FRIEND = 999

FRIEND_USER = FakeUser(user_id=FRIEND, first_name="Ali")
OWNER_USER = FakeUser(user_id=OWNER, first_name="Him")


async def _enable(runtime) -> None:
    await runtime.store.set_feature("persona", True)
    await runtime.feature_manager.refresh()


def _event(text: str, *, sender_id: int = FRIEND, out: bool = False, reply=None, message_id: int = 700):
    message = FakeMessage(
        id=message_id,
        chat_id=GROUP,
        sender_id=sender_id,
        text=text,
        out=out,
        is_group=True,
        reply=reply,
    )
    if sender_id == OWNER:
        message.sender = OWNER_USER
    else:
        message.sender = FRIEND_USER
    return normalize_message(
        message,
        self_id=OWNER,
        prefix=".",
        event_type=EventType.OUTGOING if out else EventType.MESSAGE,
    )


class RecordingClient:
    """Records sends and serves a scripted chat history."""

    def __init__(self, history=()) -> None:
        self.sent: list[tuple[object, str, dict]] = []
        self.history = list(history)
        self.reactions: list[tuple[object, int, str]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return FakeMessage(id=5000 + len(self.sent), chat_id=int(chat_id), text=text, out=True)

    async def send_file(self, chat_id, file, **kwargs):
        self.sent.append((chat_id, kwargs.get("caption", ""), {**kwargs, "file": file}))
        return FakeMessage(id=5000 + len(self.sent), text="", out=True)

    async def get_messages(self, entity=None, **kwargs):
        return list(self.history)

    async def send_reaction(self, chat_id, message_id, emoji):
        self.reactions.append((chat_id, message_id, emoji))
        return True


async def _runtime_with(runtime, history=()):
    await _enable(runtime)
    runtime.ai = StubProvider("noted, kitten")
    client = RecordingClient(history)
    runtime.client = client
    runtime.sender.client = client
    replies = runtime.persona_replies
    replies._group_last.clear()
    replies._group_window.clear()
    return replies, client


# ---------------------------------------------------------------------------
# The restart gap: her ids survive, so a later reply still reaches her
# ---------------------------------------------------------------------------
async def test_a_reply_to_her_works_after_a_restart(runtime) -> None:
    replies, client = await _runtime_with(runtime)
    await runtime.store.set_setting("yura.own_ids." + str(GROUP), [600])
    replies._own_ids.clear()  # a fresh process, nothing in memory

    hers = FakeMessage(id=600, chat_id=GROUP, sender_id=OWNER, text="i lift, obviously", out=True)
    await replies.on_group_message(_event("what do you think", out=True, sender_id=OWNER, reply=hers))

    assert client.sent, "after a restart her own message was forgotten"


async def test_her_ids_are_persisted_when_she_sends(runtime) -> None:
    replies, client = await _runtime_with(runtime)
    await replies._note_own(GROUP, 4242)
    stored = await runtime.store.get_setting("yura.own_ids." + str(GROUP), None)
    assert stored == [4242]


# ---------------------------------------------------------------------------
# Addressing: a reply to the owner's own message counts
# ---------------------------------------------------------------------------
async def test_a_reply_to_his_own_message_is_not_answered(runtime) -> None:
    """Replying to *him* is his conversation, not something aimed at her.

    This gate used to accept it, and she answered every thread he was in — a lot
    of model spend for a message nobody sent her.
    """
    replies, client = await _runtime_with(runtime)
    his = FakeMessage(id=650, chat_id=GROUP, sender_id=OWNER, text="look at this", out=True)
    await replies.on_group_message(_event("hey", out=True, sender_id=OWNER, reply=his))
    assert client.sent == []
    assert replies.last_skip == "not_addressed"


async def test_a_reply_to_her_is_answered(runtime) -> None:
    replies, client = await _runtime_with(runtime)
    await replies._note_own(GROUP, 600)
    hers = FakeMessage(id=600, chat_id=GROUP, sender_id=OWNER, text="mrrp", out=True)
    await replies.on_group_message(_event("what do you think", out=True, sender_id=OWNER, reply=hers))
    assert client.sent


async def test_a_reply_to_a_stranger_is_still_ignored(runtime) -> None:
    replies, client = await _runtime_with(runtime)
    stranger = FakeMessage(id=651, chat_id=GROUP, sender_id=FRIEND, text="my private secret", out=False)
    await replies.on_group_message(_event("hm", reply=stranger))
    assert client.sent == []
    assert replies.last_skip == "not_addressed"


# ---------------------------------------------------------------------------
# Who is talking
# ---------------------------------------------------------------------------
async def test_a_friend_is_named_and_not_confused_with_the_owner(runtime) -> None:
    replies, client = await _runtime_with(runtime)
    await replies.on_group_message(_event("yura what do you think"))
    prompt = _all_text(runtime.ai.calls[-1])
    assert "Ali" in prompt
    assert "not him" in prompt


async def test_the_owner_is_named_as_the_owner(runtime) -> None:
    replies, client = await _runtime_with(runtime)
    await replies._note_own(GROUP, 600)
    hers = FakeMessage(id=600, chat_id=GROUP, sender_id=OWNER, text="mrrp", out=True)
    await replies.on_group_message(_event("what was that", out=True, sender_id=OWNER, reply=hers))
    prompt = _all_text(runtime.ai.calls[-1])
    assert "be soft with him" in prompt
    assert "not him" not in prompt


# ---------------------------------------------------------------------------
# What was being talked about
# ---------------------------------------------------------------------------
def _history():
    news = FakeMessage(id=680, chat_id=GROUP, sender_id=FRIEND, text="did you see the news about the bridge", out=False)
    news.sender = FRIEND_USER
    photo = FakeMessage(id=681, chat_id=GROUP, sender_id=FRIEND, text="", out=False)
    photo.sender = FRIEND_USER
    from conftest import FakeMedia

    photo.media = FakeMedia(photo=object())
    hers = FakeMessage(id=682, chat_id=GROUP, sender_id=OWNER, text="oh that is bad", out=True)
    hers.sender = OWNER_USER
    return [news, photo, hers]


async def test_the_last_lines_of_the_chat_reach_the_prompt(runtime) -> None:
    replies, client = await _runtime_with(runtime, _history())
    await replies._note_own(GROUP, 600)
    hers = FakeMessage(id=600, chat_id=GROUP, sender_id=OWNER, text="mrrp", out=True)
    await replies.on_group_message(
        _event("what do you think about it", out=True, sender_id=OWNER, reply=hers)
    )

    prompt = _all_text(runtime.ai.calls[-1])
    assert "the bridge" in prompt
    assert "[photo]" in prompt
    assert "the owner: oh that is bad" in prompt


async def test_her_own_lines_are_marked_as_hers(runtime) -> None:
    from conftest import FakeMedia  # noqa: F401 - parity with the builder above

    history = _history()
    her_line = FakeMessage(id=683, chat_id=GROUP, sender_id=OWNER, text="mrrp", out=True)
    history.append(her_line)
    replies, client = await _runtime_with(runtime, history)
    await replies._note_own(GROUP, 683)

    hers = FakeMessage(id=683, chat_id=GROUP, sender_id=OWNER, text="mrrp", out=True)
    hers.sender = OWNER_USER
    await replies.on_group_message(
        _event("go on then", out=True, sender_id=OWNER, message_id=684, reply=hers)
    )

    prompt = _all_text(runtime.ai.calls[-1])
    assert "yura (you): mrrp" in prompt


async def test_a_stranger_does_not_hear_the_owners_private_names(runtime) -> None:
    replies, client = await _runtime_with(runtime)
    await replies.on_group_message(_event("yura are you around"))
    prompt = _all_text(runtime.ai.calls[-1])
    assert "never his pet names" in prompt
    assert "never what he told you in private" in prompt


async def test_the_privacy_rule_is_not_applied_to_the_owner(runtime) -> None:
    replies, client = await _runtime_with(runtime)
    await replies._note_own(GROUP, 600)
    hers = FakeMessage(id=600, chat_id=GROUP, sender_id=OWNER, text="mrrp", out=True)
    await replies.on_group_message(_event("you there?", out=True, sender_id=OWNER, reply=hers))
    prompt = _all_text(runtime.ai.calls[-1])
    assert "private names" not in prompt


# ---------------------------------------------------------------------------
# The builder itself
# ---------------------------------------------------------------------------
async def test_the_builder_never_raises_without_a_client(runtime) -> None:
    runtime.client = None
    context = await build(runtime, _event("yura hi"))
    assert context.is_group is True
    assert context.speaker == "Ali"


async def test_the_builder_survives_a_client_that_fails(runtime) -> None:
    class Broken:
        async def get_messages(self, *args, **kwargs):
            raise RuntimeError("FLOOD_WAIT")

    runtime.client = Broken()
    context = await build(runtime, _event("yura hi"))
    assert isinstance(context, ChatContext)
    assert context.transcript == []


async def test_long_lines_are_cut_not_dropped(runtime) -> None:
    long = FakeMessage(id=690, chat_id=GROUP, sender_id=FRIEND, text="x" * 900, out=False)
    long.sender = FRIEND_USER
    runtime.client = RecordingClient([long])
    context = await build(runtime, _event("yura hi"))
    assert context.transcript
    assert len(context.transcript[0]) <= MAX_LINE_CHARS + 20


async def test_a_private_turn_gets_no_transcript(runtime) -> None:
    runtime.client = RecordingClient(_history())
    message = FakeMessage(id=691, chat_id=OWNER, sender_id=OWNER, text="hi", out=True, is_group=False)
    message.sender = OWNER_USER
    event = normalize_message(message, self_id=OWNER, prefix=".", event_type=EventType.OUTGOING)
    context = await build(runtime, event)
    assert context.transcript == []
    assert context.is_owner is True


# ---------------------------------------------------------------------------
# Why she stayed quiet
# ---------------------------------------------------------------------------
async def test_the_reason_she_stayed_quiet_is_recorded(runtime) -> None:
    replies, client = await _runtime_with(runtime)
    await replies.on_group_message(_event("does anyone have the notes"))
    assert replies.last_skip == "not_addressed"
    assert replies.skips["not_addressed"] >= 1


# ---------------------------------------------------------------------------
# What goes into her memory is what they said — not the prompt around it
# ---------------------------------------------------------------------------
async def test_the_wrapper_is_not_stored_in_the_chat_log(runtime) -> None:
    """She used to be trained by her own history to quote the prompt scaffolding."""
    replies, client = await _runtime_with(runtime)
    await replies._note_own(GROUP, 600)
    hers = FakeMessage(id=600, chat_id=GROUP, sender_id=OWNER, text="do you like the tea", out=True)
    await replies.on_group_message(_event("what about it", out=True, sender_id=OWNER, reply=hers))

    history = await runtime.memory.history(GROUP)
    spoken = [content for role, content in history if role == "user"]
    assert spoken, "nothing was written to the log"
    assert spoken[-1] == "what about it"
    for content in spoken:
        assert "you replied to" not in content.lower()
        assert "«" not in content


async def test_being_named_is_enough(runtime) -> None:
    replies, client = await _runtime_with(runtime)
    await replies.on_group_message(_event("yura, you there?"))
    assert client.sent


# ---------------------------------------------------------------------------
# Not a broken record, and not the notes read back
# ---------------------------------------------------------------------------
async def test_she_is_told_what_she_already_said_here(runtime) -> None:
    """The complaint was repetition: the same "get lost" three times in a row."""
    replies, client = await _runtime_with(runtime)
    await runtime.memory.append(GROUP, "assistant", "پاشو برو گمشو پی کارت")
    await runtime.memory.append(GROUP, "assistant", "حوصلت رو ندارم")

    await replies.on_group_message(_event("yura, say something"))

    prompt = _all_text(runtime.ai.calls[-1])
    assert "already said these" in prompt
    assert "پاشو برو گمشو پی کارت" in prompt


async def test_a_reply_that_is_the_notes_is_not_sent(runtime) -> None:
    """A model that answers with the room block must not be believed."""
    replies, client = await _runtime_with(runtime)
    runtime.ai = StubProvider(
        "## the room\n"
        "- you are in the group \"General\", not a private chat.\n"
        "- it is Ali talking, not him. be rude with them: short, dry.\n"
        "- answer in your own words."
    )

    await replies.on_group_message(_event("yura, you there?"))

    assert client.sent, "nothing was sent at all"
    sent = client.sent[-1][1]
    assert "the room" not in sent.lower()
    assert "not him" not in sent
    assert "own words" not in sent.lower()


async def test_the_notes_are_never_sent_but_the_real_line_survives(runtime) -> None:
    replies, client = await _runtime_with(runtime)
    runtime.ai = StubProvider(
        "## the room\n- it is Ali talking, not him.\n\nبزن به چاک، حوصله ندارم."
    )

    await replies.on_group_message(_event("yura, you there?"))

    sent = client.sent[-1][1]
    assert "بزن به چاک" in sent
    assert "Ali" not in sent


async def test_the_bar_is_a_rare_flourish_in_a_group(runtime) -> None:
    """It is hers to send — every fifth answer, not under every third."""
    replies, client = await _runtime_with(runtime)
    for index in range(10):
        replies._group_last.clear()
        replies._group_window.clear()
        await replies.on_group_message(_event(f"yura, you there? {index}"))

    assert client.sent, "nothing was sent, so the bar was never tested"
    with_bar = [text for _, text, _ in client.sent if "attention" in text.lower()]
    assert len(with_bar) == 2, "the bar is not a footer under every answer"
    for text in with_bar:
        assert "▰" in text and "%" in text


async def test_a_cut_reply_never_leaves_half_a_sentence(runtime) -> None:
    """A model that hits the ceiling mid-thought used to send the fragment."""
    replies, client = await _runtime_with(runtime)
    runtime.ai = StubProvider(
        "باشه بابا، حرف تو درسته و قبولش دارم. " * 60
        + "ولی این یکی رو دیگه نمی‌تونم قبول کنم چون"
    )

    await replies.on_group_message(_event("yura, you there?"))

    sent = client.sent[-1][1]
    assert "قبول کنم چون" not in sent
    assert "حرف تو درسته" in sent


# ---------------------------------------------------------------------------
# "It replies to everyone that replies to me, and whenever someone tags me"
# ---------------------------------------------------------------------------
async def test_a_reply_to_him_is_not_addressed_even_when_telegram_flags_it(runtime) -> None:
    """Telegram sets ``mentioned`` on every reply to the account, not just on
    mentions. Trusting that flag made her answer each person who replied to him.
    """
    replies, client = await _runtime_with(runtime)
    his = FakeMessage(id=660, chat_id=GROUP, sender_id=OWNER, text="look at this", out=True)

    event = _event("hm, what is that", reply=his)
    event.mentions_me = True  # what the server really sends

    await replies.on_group_message(event)
    assert client.sent == []
    assert replies.last_skip == "not_addressed"


async def test_tagging_him_does_not_summon_her(runtime) -> None:
    replies, client = await _runtime_with(runtime)
    event = _event("hey @him look")
    event.mentions_me = True

    await replies.on_group_message(event)
    assert client.sent == []
    assert replies.last_skip == "not_addressed"


async def test_her_answer_is_not_sent_as_a_reply_bubble(runtime) -> None:
    """A reply bubble puts their message on top of her answer — "I get my prompt back"."""
    replies, client = await _runtime_with(runtime)
    theirs = FakeMessage(id=661, chat_id=GROUP, sender_id=FRIEND, text="yura, hi", out=False)

    await replies.on_group_message(_event("yura, hi", reply=theirs, message_id=661))

    assert client.sent, "she did not answer at all"
    for _, _, kwargs in client.sent:
        assert not kwargs.get("reply_to"), "her answer was threaded as a reply"


async def test_the_echo_at_the_top_of_her_answer_is_cut(runtime) -> None:
    replies, client = await _runtime_with(runtime)
    runtime.ai = StubProvider("> یورا این کیه\n\nگمشو، حوصله ندارم")

    await replies.on_group_message(_event("یورا این کیه"))

    sent = client.sent[-1][1]
    assert "یورا این کیه" not in sent
    assert "گمشو" in sent


async def test_her_own_verse_quote_survives(runtime) -> None:
    replies, client = await _runtime_with(runtime)
    runtime.ai = StubProvider("> بیتی از حافظ\n\nخوش است")

    await replies.on_group_message(_event("یورا یه بیت بگو"))

    sent = client.sent[-1][1]
    assert "بیتی از حافظ" in sent


# ---------------------------------------------------------------------------
# A reply to her is still a reply to her after a restart
# ---------------------------------------------------------------------------
async def test_a_reply_to_her_words_survives_a_lost_id(runtime) -> None:
    """Ids live in settings and can go missing; her own words do not."""
    replies, client = await _runtime_with(runtime)
    await replies._note_said(GROUP, "i lift, obviously")
    await runtime.store.set_setting("yura.own_ids." + str(GROUP), [])
    replies._own_ids.clear()  # a fresh process, and the id list is gone

    hers = FakeMessage(id=602, chat_id=GROUP, sender_id=OWNER, text="i lift, obviously", out=True)
    await replies.on_group_message(_event("what do you think", out=True, sender_id=OWNER, reply=hers))

    assert client.sent, "a reply to her own words was ignored"


async def test_his_own_message_is_still_not_hers(runtime) -> None:
    replies, client = await _runtime_with(runtime)
    his = FakeMessage(id=603, chat_id=GROUP, sender_id=OWNER, text="look at this bridge", out=True)

    await replies.on_group_message(_event("hm", out=True, sender_id=OWNER, reply=his))

    assert client.sent == []
    assert replies.last_skip == "not_addressed"


async def test_a_member_quoting_her_line_is_not_her(runtime) -> None:
    """Only a message this account sent can count as hers, however it reads."""
    replies, client = await _runtime_with(runtime)
    await replies._note_said(GROUP, "i lift, obviously")
    theirs = FakeMessage(id=604, chat_id=GROUP, sender_id=FRIEND, text="i lift, obviously", out=False)

    await replies.on_group_message(_event("hm", reply=theirs))

    assert client.sent == []
