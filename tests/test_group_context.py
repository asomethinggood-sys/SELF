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
async def test_a_reply_to_the_owners_message_in_a_group_is_answered(runtime) -> None:
    replies, client = await _runtime_with(runtime)
    his = FakeMessage(id=650, chat_id=GROUP, sender_id=OWNER, text="look at this", out=True)
    await replies.on_group_message(_event("hey", out=True, sender_id=OWNER, reply=his))
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
    assert "NOT the owner" in prompt


async def test_the_owner_is_named_as_the_owner(runtime) -> None:
    replies, client = await _runtime_with(runtime)
    await replies._note_own(GROUP, 600)
    hers = FakeMessage(id=600, chat_id=GROUP, sender_id=OWNER, text="mrrp", out=True)
    await replies.on_group_message(_event("what was that", out=True, sender_id=OWNER, reply=hers))
    prompt = _all_text(runtime.ai.calls[-1])
    assert "the owner of this account" in prompt
    assert "NOT the owner" not in prompt


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
