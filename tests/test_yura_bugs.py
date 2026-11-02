"""The four things he reported, each pinned to the line that was wrong.

1. **She does not answer in DMs.** A one-to-one message to the bot account
   carries the account's own id as its chat id, which is also Saved Messages' id.
   The DM handler decided "Saved Messages, not mine", the Saved Messages handler
   demanded an *outgoing* message, and nothing answered. Every DM fell between
   the two.
2. **"block sara" did nothing.** The sentence reader had no rule for the
   Telegram verbs about a person, so the sentence reached the model, the model
   wrote a paragraph, and neither the hands nor the console ever ran.
3. **"unblock sara" meant the opposite.** It matched the *social* un-ignore rule
   and quietly turned a mute into "answer her again".
4. **A name in the room could not be found.** A group member has no dialog of
   their own, so ``.block sara`` answered "there is no chat called 'sara'".
5. **The picture server refused the job.** The checkpoint was hardcoded to
   ``v1-5-pruned-emaonly`` while the runner has ``sd-turbo``: ComfyUI's
   validator answered 400 with a node id in it.

Plus the tool-call resilience: a provider that rejects a payload carrying
``tools`` must still answer.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from conftest import FakeEvent, FakeMessage, FakeUser

from havoc.event_engine import EventType, normalize_message, now_event
from havoc.features import persona as persona_module
from havoc.services import intent as intent_module
from havoc.services.actions import Tool
from havoc.services.ai_provider import AIMessage, AIProvider
from havoc.services.imagegen import ImageGenerator

OWNER = 42
BOT = 999
GROUP = -1001234567890


async def _persona_on(runtime) -> None:
    # The bot account is its own id, exactly as the probe that found the bug:
    # a DM to it arrives with chat_id == self_id, which is also Saved Messages'
    # id. That collision is the bug; a test that avoids it tests nothing.
    runtime.permissions.set_self_id(BOT)
    await runtime.feature_manager.set_enabled("persona", True)
    await runtime.feature_manager.refresh()


# ---------------------------------------------------------------------------
# 1. A DM to the bot account is answered
# ---------------------------------------------------------------------------
def _dm_to_the_bot(text: str = "hey, are you there", *, sender: int = OWNER, mid: int = 700):
    message = FakeMessage(
        id=mid, chat_id=BOT, sender_id=sender, text=text, out=False,
        is_private=True, is_group=False,
    )
    message.sender = FakeUser(user_id=sender, first_name="Owner")
    return normalize_message(message, self_id=BOT, prefix=".", event_type=EventType.MESSAGE)


async def test_a_dm_to_the_bot_account_is_answered(runtime) -> None:
    await _persona_on(runtime)
    event = _dm_to_the_bot()
    assert event.is_private and not event.is_group and not event.is_outgoing
    await runtime.persona_replies.on_dm_chat(event)
    assert runtime.client.sent, f"she said nothing: last skip was {runtime.persona_replies.last_skip!r}"


async def test_saved_messages_is_still_not_a_dm(runtime) -> None:
    """The owner's own outgoing line stays with `on_self_message`."""
    await _persona_on(runtime)
    message = FakeMessage(
        id=701, chat_id=BOT, sender_id=BOT, text="note to self", out=True,
        is_private=True, is_group=False,
    )
    event = normalize_message(message, self_id=BOT, prefix=".", event_type=EventType.OUTGOING)
    await runtime.persona_replies.on_dm_chat(event)
    assert runtime.client.sent == []
    # ...and it is not counted as a refusal either: this handler is simply not
    # the one for that chat.
    assert runtime.persona_replies.last_skip != "not_dm"


async def test_one_dm_is_one_answer(runtime) -> None:
    await _persona_on(runtime)
    event = _dm_to_the_bot("hello there")
    await runtime.persona_replies.on_dm_chat(event)
    await runtime.persona_replies.on_self_message(event)  # also subscribed, must stay quiet
    assert len(runtime.client.sent) == 1


# ---------------------------------------------------------------------------
# 2 and 3. The Telegram verbs about a person
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("said", "line"),
    [
        ("block sara", "block sara --yes"),
        ("block @sara", "block @sara --yes"),
        ("block sara from messaging me", "block sara --yes"),
        ("don't let sara message me", "block sara --yes"),
        ("unblock sara", "unblock sara --yes"),
        ("ban sara", "ban sara --yes"),
        ("kick sara", "kick sara --yes"),
        ("remove sara from the group", "kick sara --yes"),
        ("سارا رو بلاک کن", "block سارا --yes"),
        ("یورا سارا رو بن کن", "ban سارا --yes"),
    ],
)
def test_the_block_and_ban_orders_are_read(said: str, line: str) -> None:
    found = intent_module.detect(said)
    assert found is not None, f"{said!r} was read as nothing at all"
    assert found.lines == (line,), (said, found.lines)


def test_unblock_is_not_the_social_rule() -> None:
    """"unblock sara" once became "answer sara again" — the opposite sentence."""
    found = intent_module.detect("unblock sara")
    assert found is not None
    assert found.lines == ("unblock sara --yes",)
    assert not any(line.startswith("policy answer") for line in found.lines)


def test_a_sentence_about_blocks_is_still_a_sentence() -> None:
    for said in ("the block was fun", "we were blocked by traffic", "she blocked the door"):
        assert intent_module.detect(said) is None, said


# ---------------------------------------------------------------------------
# 4. A name in the room resolves — with no dialog of their own
# ---------------------------------------------------------------------------
class _Member(FakeUser):
    def __init__(self, user_id: int, first: str, last: str = "", username: str | None = None):
        super().__init__(user_id=user_id, first_name=first, last_name=last, username=username)


async def test_a_group_member_is_found_by_name(runtime) -> None:
    runtime.client.members = [_Member(880, "Sara", "Ahmadi", "sara_a"), _Member(881, "Ali")]
    target = await runtime.reach.resolve("sara", in_chat=GROUP)
    assert target is not None, "a member of the room could not be found by name"
    assert target.id == 880


async def test_a_shared_first_name_is_not_a_guess(runtime) -> None:
    runtime.client.members = [_Member(880, "Sara"), _Member(881, "Sara")]
    assert await runtime.reach.resolve("sara", in_chat=GROUP) is None


async def test_block_by_name_reaches_telegram(runtime) -> None:
    """`.block sara` builds a real BlockRequest for the member's id."""
    runtime.client.members = [_Member(880, "Sara", "Ahmadi")]
    from conftest import invoke

    await invoke(runtime, ".block sara --yes", chat_id=GROUP)
    requests = [type(request).__name__ for request in runtime.client.raw_requests]
    assert "BlockRequest" in requests, requests
    request = runtime.client.raw_requests[-1]
    peer = getattr(request, "id", None)
    assert getattr(peer, "user_id", getattr(peer, "value", None)) == 880


async def test_block_with_no_name_uses_the_message_he_replied_to(runtime) -> None:
    from conftest import invoke

    quoted = FakeMessage(id=690, chat_id=GROUP, sender_id=880, text="spam spam", out=False)
    await invoke(runtime, ".block --yes", reply=quoted, chat_id=GROUP)
    requests = [type(request).__name__ for request in runtime.client.raw_requests]
    assert "BlockRequest" in requests, runtime.client.sent[-1].text if runtime.client.sent else requests


# ---------------------------------------------------------------------------
# 5. The picture server is asked what it actually has
# ---------------------------------------------------------------------------
async def test_the_generator_uses_the_checkpoint_the_server_has(monkeypatch) -> None:
    monkeypatch.setenv("COMFY_CKPT", "v1-5-pruned-emaonly.safetensors")
    generator = ImageGenerator(url="http://127.0.0.1:9", checkpoint="v1-5-pruned-emaonly.safetensors")

    async def only_sd_turbo(base: str, headers: dict) -> list[str]:
        return ["sd-turbo.safetensors"]

    monkeypatch.setattr(generator, "checkpoints", only_sd_turbo)
    chosen = await generator.pick_checkpoint("http://127.0.0.1:9", {})
    assert chosen == "sd-turbo.safetensors"
    assert generator.last_checkpoint == "sd-turbo.safetensors"


async def test_a_server_that_has_the_configured_checkpoint_keeps_it(monkeypatch) -> None:
    generator = ImageGenerator(checkpoint="mine.safetensors")

    async def has_it(base: str, headers: dict) -> list[str]:
        return ["mine.safetensors", "other.safetensors"]

    monkeypatch.setattr(generator, "checkpoints", has_it)
    assert await generator.pick_checkpoint("http://x", {}) == "mine.safetensors"


async def test_a_server_that_cannot_say_is_not_a_failure(monkeypatch) -> None:
    """An older ComfyUI without `/object_info` still gets the configured name."""
    generator = ImageGenerator(checkpoint="v1-5-pruned-emaonly.safetensors")

    async def nothing(base: str, headers: dict) -> list[str]:
        return []

    monkeypatch.setattr(generator, "checkpoints", nothing)
    assert await generator.pick_checkpoint("http://x", {}) == "v1-5-pruned-emaonly.safetensors"


# ---------------------------------------------------------------------------
# 6. A provider without function calling still answers
# ---------------------------------------------------------------------------
class _NoToolsProvider(AIProvider):
    """A model that 400s on a payload carrying `tools`, like several free ones."""

    def __init__(self) -> None:
        super().__init__(api_key="x", base_url="http://127.0.0.1:9")
        self.models = ["fake"]
        self.payloads: list[dict] = []

    async def _completion(self, messages, *, max_tokens=400, temperature=0.85, extra=None):
        self.payloads.append({"extra": extra})
        if extra and "tools" in extra:
            from havoc.services.ai_provider import AIError

            raise AIError("HTTP 400: tools are not supported", kind="http_error")
        return {"role": "assistant", "content": "fine, done."}


async def test_a_model_without_function_calling_answers_anyway() -> None:
    provider = _NoToolsProvider()
    tool = Tool(name="recall", summary="look", properties={}, run=None)
    text, calls = await provider.converse_tools(
        [AIMessage("user", "hi")], tools=[tool.as_function()], max_tokens=50
    )
    assert text == "fine, done."
    assert calls == []
    assert provider.tools_unsupported is True
    assert provider.payloads[0]["extra"] is not None  # it did try with tools first


# ---------------------------------------------------------------------------
# 7. Her words and the card agree: the result goes back into the prompt
# ---------------------------------------------------------------------------
async def test_the_prompt_is_told_what_already_happened(runtime) -> None:
    note = runtime.yura_chat._did_note(["block · sara: blocked Sara"], "en")
    assert "already done" in note
    assert "block · sara" in note
    assert "do not " in note and "that is done" in note


def test_the_quick_hand_table_names_the_reported_verbs() -> None:
    from havoc.runtime import Runtime  # noqa: F401  (import cost is the point)

    text = Path("havoc/services/actions.py").read_text()
    for verb in ("block", "ban", "promote", "whois", "unsend"):
        assert verb in text, verb
    assert "QUICK_HANDS" in text


def test_the_tool_catalogue_is_small_enough_to_be_read() -> None:
    """A prompt that is a wall of text is a prompt a free model ignores."""
    source = Path("havoc/services/self_console.py").read_text()
    assert "MAX_CATALOGUE_CHARS = 6500" in source
    assert json.dumps({"ok": True})  # keep the import honest


# ---------------------------------------------------------------------------
# 8. A name he shares a group with, with no dialog and no participants list
# ---------------------------------------------------------------------------
async def test_a_name_is_found_in_the_rooms_he_talks_in(runtime) -> None:
    """`.whois sara` from another room: one sara, one answer — and a block."""
    from conftest import FakeChat, invoke

    runtime.client.dialogs = [FakeChat(chat_id=GROUP, title="the group")]
    runtime.client.members = [_Member(880, "Sara", "Ahmadi", "sara_a")]
    sent = await invoke(runtime, ".whois sara", chat_id=GROUP + 7)
    assert "Sara Ahmadi" in " ".join(m.text for m in sent), [m.text[:80] for m in sent]
    # And it sticks: the row is written for a person *he* named, so the next
    # sentence about her does not pay for another scan.
    assert await runtime.people.all(limit=5), "nothing was written down"


async def test_two_saras_in_his_rooms_are_not_a_guess(runtime) -> None:
    from conftest import FakeChat, invoke

    runtime.client.dialogs = [
        FakeChat(chat_id=GROUP, title="one"),
        FakeChat(chat_id=GROUP - 1, title="two"),
    ]
    runtime.client.members = [_Member(880, "Sara"), _Member(881, "Sara")]
    sent = await invoke(runtime, ".whois sara", chat_id=GROUP + 7)
    text = " ".join(m.text for m in sent)
    assert "can't find" in text.lower() or "پیدا نکردم" in text, text[:120]
    assert runtime.client.raw_requests == []


async def test_a_dialog_member_still_beats_a_scan(runtime) -> None:
    """If he already has a chat with a sara, that one is the sara."""
    from conftest import FakeChat, invoke

    runtime.client.dialogs = [FakeChat(chat_id=550, title="Sara")]
    runtime.client.members = [_Member(880, "Sara")]
    sent = await invoke(runtime, ".block sara --yes", chat_id=GROUP)
    peer = getattr(runtime.client.raw_requests[-1], "id", None)
    assert getattr(peer, "value", None) == 550, "the scan overrode a chat he has"
    assert sent


# ---------------------------------------------------------------------------
# 9. Native function calling, and the prompt that makes it possible
# ---------------------------------------------------------------------------
class _NativeCaller:
    """A model that answers with an OpenAI-shaped tool call and then with words."""

    model = "native"
    available = True

    def __init__(self) -> None:
        self.calls: list[list[object]] = []

    async def converse_tools(self, messages, *, tools=None, max_tokens: int = 400, temperature=0.85):
        self.calls.append(list(messages))
        if len(self.calls) == 1:
            return "", [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "command",
                        "arguments": '{"command": "block", "args": "sara --yes"}',
                    },
                }
            ]
        return "she's gone.", []


async def test_a_native_tool_call_reaches_telegram(runtime) -> None:
    from conftest import incoming

    await runtime.feature_manager.set_enabled("persona", True)
    await runtime.feature_manager.refresh()
    runtime.client.members = [_Member(880, "Sara")]
    runtime.ai = _NativeCaller()
    message = FakeMessage(
        id=910, chat_id=GROUP, sender_id=OWNER, text="yura, get rid of sara for me",
        is_group=True, is_private=False,
    )
    message.sender = FakeUser(user_id=OWNER, first_name="Owner")
    await incoming(runtime, message)
    wire = [type(request).__name__ for request in runtime.client.raw_requests]
    assert "BlockRequest" in wire, f"a native tool call went nowhere: {wire}"


async def test_the_prompt_carries_the_verb_to_hand_table(runtime) -> None:
    class Recorder:
        model = "recorder"
        available = True

        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def converse(self, messages, *, max_tokens: int = 400, temperature: float = 0.85):
            self.prompts.append(str(messages[0].content))
            return "mm."

        async def converse_tools(self, messages, *, tools=None, max_tokens: int = 400, temperature=0.85):
            self.prompts.append(str(messages[0].content))
            return "mm.", []

    from conftest import incoming

    await runtime.feature_manager.set_enabled("persona", True)
    await runtime.feature_manager.refresh()
    recorder = Recorder()
    runtime.ai = recorder
    message = FakeMessage(
        id=911, chat_id=GROUP, sender_id=OWNER, text="yura, are you awake",
        is_group=True, is_private=False,
    )
    message.sender = FakeUser(user_id=OWNER, first_name="Owner")
    await incoming(runtime, message)
    prompt = recorder.prompts[-1]
    assert "when he says this, use that" in prompt
    assert '"command": "block"' in prompt
    assert "## when he says this, use that" in prompt


# ---------------------------------------------------------------------------
# 10. The filler words people put around a name
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("said", "line"),
    [
        ("block sara now", "block sara --yes"),
        ("i want you to block sara", "block sara --yes"),
        ("i need you to ban sara please", "ban sara --yes"),
        ("go ahead and block sara", "block sara --yes"),
        ("don't reply to sara anymore", "policy ignore sara"),
        ("don't answer sara anymore", "policy ignore sara"),
        ("stop talking to sara", "policy ignore sara"),
        ("دیگه به سارا جواب نده", "policy ignore سارا"),
        ("از این به بعد به سارا جواب بده", "policy answer سارا"),
    ],
)
def test_the_words_around_the_name_are_not_the_name(said: str, line: str) -> None:
    """A rule about "sara anymore" is a refusal about a name he never said."""
    found = intent_module.detect(said)
    assert found is not None, f"{said!r} was read as nothing at all"
    assert found.lines == (line,), (said, found.lines)


def test_no_person_is_named_after_a_filler() -> None:
    for said in ("block sara now", "ban sara please", "don't reply to sara anymore"):
        found = intent_module.detect(said)
        assert found is not None
        text = found.lines[0]
        for filler in ("now", "please", "anymore"):
            assert not text.endswith(f" {filler}"), (said, text)


# ---------------------------------------------------------------------------
# 11. A letter that arrived late still gets an answer
# ---------------------------------------------------------------------------
def _hers(text: str, *, minutes_old: float, mid: int = 1, group: bool = False, sender: int = 880):
    from datetime import UTC, datetime, timedelta

    when = datetime.now(UTC) - timedelta(minutes=minutes_old)
    message = FakeMessage(
        id=mid, chat_id=GROUP if group else sender, sender_id=sender, text=text,
        out=False, is_private=not group, is_group=group, date=when,
    )
    message.sender = FakeUser(user_id=sender, first_name="Her")
    return message


class _Quiet:
    """A provider with both methods, answering one friendly line."""

    model = "quiet"
    available = True

    def __init__(self, reply: str = "hey! sorry, i was away.") -> None:
        self.reply = reply
        self.prompts: list[str] = []

    async def converse(self, messages, *, max_tokens: int = 400, temperature: float = 0.85):
        self.prompts.append(str(messages[-1].content))
        return self.reply

    async def converse_tools(self, messages, *, tools=None, max_tokens: int = 400, temperature=0.85):
        self.prompts.append(str(messages[-1].content))
        return self.reply, []


async def _typed_dm(runtime, *, dm: bool = True) -> None:
    await runtime.feature_manager.set_enabled("persona", True)
    await runtime.feature_manager.refresh()
    await runtime.people.touch(880, "Her")
    await runtime.people.set_dm(880, dm)


async def test_a_dm_that_waited_while_she_was_between_runs_is_answered(runtime) -> None:
    """The 10-minute rule held a room and a letter to the same clock.

    The account runs in short jobs; a DM that arrives between two of them comes
    back on catch-up already older than the gate and was skipped as "stale" — the
    person heard nothing while the owner, typing while she was up, was answered.
    """
    from conftest import FakeEvent

    await _typed_dm(runtime)
    runtime.ai = _Quiet()
    await runtime.handlers._handle_incoming(FakeEvent(_hers("hru?", minutes_old=40)))
    assert runtime.persona_replies.last_skip != "stale"
    # It is queued for one answer, and the answer carries the age.
    assert 880 in runtime.persona_replies._dm_late
    await asyncio.sleep(persona_module.CATCHUP_GRACE_SECONDS + 0.4)
    assert runtime.client.sent, "a DM that waited forty minutes got nothing"


async def test_a_burst_of_late_dms_is_answered_once(runtime) -> None:
    from conftest import FakeEvent

    await _typed_dm(runtime)
    runtime.ai = _Quiet()
    for index, text in enumerate(["hru?", "yura how u doing??", "yura talk to me"], start=1):
        await runtime.handlers._handle_incoming(FakeEvent(_hers(text, minutes_old=45 - index, mid=10 + index)))
    await asyncio.sleep(persona_module.CATCHUP_GRACE_SECONDS + 0.4)
    assert len(runtime.client.sent) == 1, [m.text[:40] for m in runtime.client.sent]
    assert "talk to me" in runtime.ai.prompts[-1], "the newest line was not the one answered"


async def test_an_old_group_message_is_still_silence(runtime) -> None:
    from conftest import FakeEvent

    await _typed_dm(runtime)
    runtime.ai = _Quiet()
    runtime.client.members = [_Member(880, "Her")]
    await runtime.handlers._handle_incoming(FakeEvent(_hers("hello all", minutes_old=40, group=True)))
    assert runtime.persona_replies.last_skip == "stale"


async def test_the_prompt_says_how_long_it_waited(runtime) -> None:
    from conftest import FakeEvent

    await _typed_dm(runtime)
    recorder = _Quiet()
    runtime.ai = recorder
    await runtime.handlers._handle_incoming(FakeEvent(_hers("hru?", minutes_old=40)))
    await asyncio.sleep(persona_module.CATCHUP_GRACE_SECONDS + 0.4)
    assert recorder.prompts, "she never thought about it"
    assert "40 minutes ago" in recorder.prompts[-1]
    assert "picked up late" in recorder.prompts[-1]


# ---------------------------------------------------------------------------
# 12. Why she is quiet is never a mystery
# ---------------------------------------------------------------------------
async def test_a_known_person_with_dms_off_gets_a_notice_not_silence(runtime) -> None:
    from conftest import FakeEvent

    await _typed_dm(runtime, dm=False)
    await runtime.handlers._handle_incoming(FakeEvent(_hers("hi, are you there?", minutes_old=1)))
    notices = [m for m in runtime.client.sent if "DMs are switched off" in m.text]
    assert len(notices) == 1, [m.text[:60] for m in runtime.client.sent]
    assert notices[0].chat_id == OWNER, "the notice must reach him, not her"
    # One knock is one notice: a second message does not repeat it.
    await runtime.handlers._handle_incoming(FakeEvent(_hers("hello?", minutes_old=1, mid=2)))
    assert len([m for m in runtime.client.sent if "DMs are switched off" in m.text]) == 1


async def test_a_stranger_is_never_mentioned_to_him(runtime) -> None:
    """Silence for a stranger is the design; it is not a setting to report."""
    from conftest import FakeEvent

    await _typed_dm(runtime)
    stranger = _hers("hey, nice account", minutes_old=1, mid=3, sender=1234567)
    await runtime.handlers._handle_incoming(FakeEvent(stranger))
    assert runtime.client.sent == []


async def test_yura_dm_lists_who_she_answers(runtime) -> None:
    from conftest import invoke

    sent = await invoke(runtime, ".yura dm")
    assert "nobody in private" in " ".join(m.text for m in sent)

    await runtime.people.touch(880, "Her")
    await runtime.people.set_dm(880, True)
    sent = await invoke(runtime, ".yura dm")
    text = " ".join(m.text for m in sent)
    assert "Her" in text and "880" in text


def test_the_late_note_reads_like_a_person_in_both_languages() -> None:
    from havoc.services.yura_chat import _late_note

    english = _late_note(300, "en")
    assert "5 minutes ago" in english
    assert "picked up late" in english
    persian = _late_note(300, "fa")
    assert "دقیقه" in persian


async def test_a_waiting_dm_is_answered_before_the_job_stops(runtime) -> None:
    """The account lives in short jobs; a queued letter must not be dropped by one.

    Telegram will not hand the message over again — catch-up has already advanced
    past it — so the queue is emptied on the way out.
    """

    await _typed_dm(runtime)
    runtime.ai = _Quiet()
    await runtime.handlers._handle_incoming(FakeEvent(_hers("yura talk to me", minutes_old=40)))
    assert runtime.persona_replies._dm_late, "nothing was queued"
    await runtime.event_engine.emit(now_event(EventType.SHUTDOWN))
    assert runtime.client.sent, "the job stopped and took her message with it"
    assert not runtime.persona_replies._dm_late
