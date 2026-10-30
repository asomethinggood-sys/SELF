"""Her in a group: only what is addressed to her, and never more than asked.

This is the bug the owner reported — replying to her worked in Saved Messages and
did nothing in a group — so the tests are written around the three ways a message
can be addressed to her, and around everything that must stay silent.
"""

from __future__ import annotations

import time

from conftest import FakeDocument, FakeMedia, FakeMessage
from test_picture_reply import PNG, StubProvider, _all_text, _picture_part  # noqa: F401

from havoc.event_engine import EventType, normalize_message

GROUP = -1001234567890


class GroupClient:
    """Downloads photos, records sends, and offers reactions."""

    def __init__(self) -> None:
        self.sent: list[tuple[object, str, dict]] = []
        self.downloads = 0
        self.reactions: list[tuple[object, int, str]] = []

    async def download_media(self, message, file=None, **kwargs):
        self.downloads += 1
        file.write(PNG)
        return file

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return FakeMessage(id=7000 + len(self.sent), chat_id=int(chat_id), text=text, out=True)

    async def send_file(self, chat_id, file, **kwargs):
        self.sent.append((chat_id, kwargs.get("caption", ""), {**kwargs, "file": file}))
        return FakeMessage(id=7000 + len(self.sent), text="", out=True)

    async def send_reaction(self, chat_id, message_id, emoji):
        self.reactions.append((chat_id, message_id, emoji))
        return True


async def _enable(runtime) -> None:
    await runtime.store.set_feature("persona", True)
    await runtime.feature_manager.refresh()


def _hers(text: str = "i lift, obviously") -> FakeMessage:
    return FakeMessage(id=600, chat_id=GROUP, sender_id=42, text=text, out=True, is_group=True)


def _incoming(text: str, *, reply: FakeMessage | None = None, media=None, sender_id: int = 999):
    message = FakeMessage(
        id=601,
        chat_id=GROUP,
        sender_id=sender_id,
        text=text,
        out=False,
        is_group=True,
        reply=reply,
        media=media,
    )
    return normalize_message(message, self_id=42, prefix=".", event_type=EventType.MESSAGE)


async def _replies(runtime):
    await _enable(runtime)
    runtime.ai = StubProvider()
    client = GroupClient()
    runtime.client = client
    runtime.sender.client = client
    replies = runtime.persona_replies
    replies._group_last.clear()
    replies._group_window.clear()
    # 600 is the message she sent: reality is that her own process knows the id.
    replies._note_own(GROUP, 600)
    return replies, client


# ---------------------------------------------------------------------------
# The three ways to reach her
# ---------------------------------------------------------------------------
async def test_a_reply_to_her_message_is_answered(runtime) -> None:
    replies, client = await _replies(runtime)
    await replies.on_group_message(_incoming("why?", reply=_hers()))

    assert client.sent, "she stayed silent on a reply"
    chat_id, text, kwargs = client.sent[-1]
    assert chat_id == GROUP
    assert kwargs.get("reply_to") == 601
    assert "lift" in _all_text(runtime.ai.calls[-1])


async def test_her_name_in_the_text_is_enough(runtime) -> None:
    replies, client = await _replies(runtime)
    await replies.on_group_message(_incoming("yura are you awake"))
    assert client.sent


async def test_the_persian_name_works_too(runtime) -> None:
    replies, client = await _replies(runtime)
    await replies.on_group_message(_incoming("یورا بیداری؟"))
    assert client.sent


async def test_a_mention_is_enough(runtime) -> None:
    replies, client = await _replies(runtime)
    await replies.on_group_message(_incoming("tg://user?id=42 come here"))
    assert client.sent


# ---------------------------------------------------------------------------
# Everything that must stay silent
# ---------------------------------------------------------------------------
async def test_a_message_that_is_not_for_her_is_ignored(runtime) -> None:
    replies, client = await _replies(runtime)
    await replies.on_group_message(_incoming("does anyone have the notes"))
    assert client.sent == []
    assert runtime.ai.calls == []


async def test_her_own_group_message_is_never_answered(runtime) -> None:
    replies, client = await _replies(runtime)
    message = FakeMessage(id=602, chat_id=GROUP, sender_id=42, text="i said what i said", out=True, is_group=True)
    await replies.on_group_message(
        normalize_message(message, self_id=42, prefix=".", event_type=EventType.MESSAGE)
    )
    assert client.sent == []


async def test_a_private_message_is_not_her_group_path(runtime) -> None:
    replies, client = await _replies(runtime)
    message = FakeMessage(id=603, chat_id=999, sender_id=999, text="yura", out=False, is_group=False)
    await replies.on_group_message(
        normalize_message(message, self_id=42, prefix=".", event_type=EventType.MESSAGE)
    )
    assert client.sent == []


async def test_the_emergency_stop_silences_a_group(runtime) -> None:
    replies, client = await _replies(runtime)
    await runtime.store.set_emergency_stop(True)
    await replies.on_group_message(_incoming("yura hi"))
    assert client.sent == []


async def test_the_group_valve_holds(runtime) -> None:
    replies, client = await _replies(runtime)
    await replies.on_group_message(_incoming("yura one"))
    assert client.sent
    before = len(client.sent)
    await replies.on_group_message(_incoming("yura two"))
    assert len(client.sent) == before, "the cooldown did not hold"


async def test_the_group_hourly_cap_holds(runtime) -> None:
    replies, client = await _replies(runtime)
    replies._group_last[GROUP] = None  # type: ignore[assignment]
    replies._group_window[GROUP] = [time.monotonic()] * replies.MAX_GROUP_PER_HOUR
    await replies.on_group_message(_incoming("yura hi"))
    assert client.sent == []


async def test_an_outgoing_reply_to_her_is_answered(runtime) -> None:
    """The reported bug: in a group the owner's own message is outgoing.

    Listening only for incoming messages meant replying to her there did
    nothing at all, while the same gesture worked in Saved Messages.
    """
    replies, client = await _replies(runtime)
    message = FakeMessage(
        id=610, chat_id=GROUP, sender_id=42, text="why?", out=True, is_group=True, reply=_hers()
    )
    await replies.on_group_message(
        normalize_message(message, self_id=42, prefix=".", event_type=EventType.OUTGOING)
    )
    assert client.sent, "her own account's group reply was ignored"


async def test_her_own_group_message_is_recognised_by_id(runtime) -> None:
    replies, client = await _replies(runtime)
    message = FakeMessage(
        id=600, chat_id=GROUP, sender_id=42, text="i said what i said", out=True, is_group=True
    )
    await replies.on_group_message(
        normalize_message(message, self_id=42, prefix=".", event_type=EventType.OUTGOING)
    )
    assert client.sent == [], "she answered herself"


async def test_an_outgoing_command_is_left_to_the_dispatcher(runtime) -> None:
    replies, client = await _replies(runtime)
    message = FakeMessage(
        id=611, chat_id=GROUP, sender_id=42, text=".yura poem", out=True, is_group=True
    )
    await replies.on_group_message(
        normalize_message(message, self_id=42, prefix=".", event_type=EventType.OUTGOING)
    )
    assert client.sent == []


# ---------------------------------------------------------------------------
# Pictures in a group
# ---------------------------------------------------------------------------
def _photo(text: str = "", *, reply: FakeMessage | None = None):
    media = FakeMedia(document=FakeDocument(file_name="cat.jpg"))
    media.photo = object()
    return _incoming(text, reply=reply, media=media)


async def test_a_group_photo_nobody_addressed_to_her_is_not_even_downloaded(runtime) -> None:
    replies, client = await _replies(runtime)
    await replies.on_group_message(_photo("look at this"))
    assert client.downloads == 0
    assert client.sent == []


async def test_a_group_photo_replied_to_her_is_looked_at(runtime) -> None:
    replies, client = await _replies(runtime)
    await replies.on_group_message(_photo("and this", reply=_hers("my aesthetic")))
    assert client.downloads == 1
    assert _picture_part(runtime.ai.calls[-1]) is not None


# ---------------------------------------------------------------------------
# The flourish: sometimes a reaction instead of a line
# ---------------------------------------------------------------------------
async def test_every_ninth_reply_is_a_reaction_not_a_message(runtime) -> None:
    replies, client = await _replies(runtime)
    replies._since_reaction = 8
    await replies.on_group_message(_incoming("nice", reply=_hers()))
    assert client.reactions, "no reaction was sent"
    assert client.sent == [], "she should have stayed quiet"
    assert replies.reacted == 1


async def test_the_reaction_goes_to_the_message_it_answers(runtime) -> None:
    replies, client = await _replies(runtime)
    replies._since_reaction = 8
    await replies.on_group_message(_incoming("yes", reply=_hers()))
    chat_id, message_id, emoji = client.reactions[0]
    assert chat_id == GROUP and message_id == 600
    assert emoji


# ---------------------------------------------------------------------------
# The thread behind a reply
# ---------------------------------------------------------------------------
async def test_a_three_deep_thread_reaches_the_prompt(runtime) -> None:
    replies, client = await _replies(runtime)
    first = FakeMessage(id=590, chat_id=GROUP, sender_id=42, text="we were talking about tea", out=True)
    second = FakeMessage(id=595, chat_id=GROUP, sender_id=42, text="and about the cup", out=True, reply=first)
    third = _hers("i prefer the small one")
    third.reply_to_msg_id = 595
    third._reply = second
    await replies.on_group_message(_incoming("why?", reply=third))

    prompt = _all_text(runtime.ai.calls[-1])
    assert "why?" in prompt
    assert "small one" in prompt


async def test_a_third_party_quote_never_enters_the_prompt(runtime) -> None:
    replies, client = await _replies(runtime)
    stranger = FakeMessage(id=580, chat_id=GROUP, sender_id=999, text="my private secret", out=False)
    await replies.on_group_message(_incoming("yura what do you think", reply=stranger))
    prompt = _all_text(runtime.ai.calls[-1])
    assert "my private secret" not in prompt
