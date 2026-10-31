"""The toolbox: send, repeat, hide, self-destruct, react, ignore, plan.

These tests are written the way he talks, not the way the code is shaped. If a
sentence in here stops working, his phone stopped working.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from conftest import FakeMessage, FakeUser, invoke

from havoc.event_engine import EventType, normalize_message
from havoc.services import intent as intent_module
from havoc.services.plans import build_plan
from havoc.utils.time import utc_now

OWNER = 42
FRIEND = 999
GROUP = -1001234567890


async def _ready(runtime) -> None:
    await runtime.store.set_feature("persona", True)
    await runtime.feature_manager.refresh()
    runtime.permissions.set_self_id(OWNER)


class _StubProvider:
    """One scripted answer; keeps what she was asked."""

    model = "stub-model"
    available = True

    def __init__(self, reply: str = "mrrp") -> None:
        self.reply = reply
        self.calls: list[list[object]] = []

    async def converse(self, messages, *, max_tokens: int = 400, temperature: float = 0.85) -> str:
        self.calls.append(list(messages))
        return self.reply


def _said(sent) -> str:
    return "\n".join(message.text for message in sent)


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
async def test_a_message_to_saved_messages_is_sent_and_recorded(runtime) -> None:
    await invoke(runtime, ".send saved hello there", chat_id=OWNER)
    sent = [row for row in runtime.client.sent if "hello there" in row.text]
    assert sent, "nothing was sent"
    rows = await runtime.store.outbox(5)
    assert rows and rows[0]["text"] == "hello there"
    assert rows[0]["status"] == "sent"


async def test_the_spoiler_really_hides_the_text(runtime) -> None:
    await invoke(runtime, ".send saved boo --spoiler", chat_id=OWNER)
    sent = [row.text for row in runtime.client.sent if "boo" in row.text]
    assert sent and '<span class="tg-spoiler">boo</span>' in sent[0]


async def test_self_destruct_queues_a_delete(runtime) -> None:
    await invoke(runtime, ".send saved bye --delete-after 10s", chat_id=OWNER)
    jobs = await runtime.db.fetch_all("SELECT kind, payload FROM jobs")
    unsend = [job for job in jobs if job["kind"] == "reach_unsend"]
    assert len(unsend) == 1
    assert '"message_id"' in unsend[0]["payload"]


async def test_a_repeat_is_a_send_now_and_the_rest_on_the_clock(runtime) -> None:
    """Ten of them must not be a loop that holds the reply for ten messages."""
    await invoke(runtime, ".send saved ping --repeat 3 --every 30s", chat_id=OWNER)
    jobs = await runtime.db.fetch_all("SELECT kind FROM jobs")
    assert len([job for job in jobs if job["kind"] == "reach_send"]) == 2
    assert [row for row in runtime.client.sent if "ping" in row.text], "the first one did not go"


async def test_sending_later_is_scheduled_not_sent(runtime) -> None:
    await invoke(runtime, ".send saved later --at 30m", chat_id=OWNER)
    assert not [row for row in runtime.client.sent if "later" in row.text]
    jobs = await runtime.db.fetch_all("SELECT kind, run_at FROM jobs")
    assert any(job["kind"] == "reach_send" for job in jobs)


async def test_several_targets_at_once(runtime) -> None:
    await invoke(runtime, ".send saved,@999 hello both", chat_id=OWNER)
    assert len([row for row in runtime.client.sent if "hello both" in row.text]) >= 1


async def test_an_unknown_name_is_refused_by_name(runtime) -> None:
    sent = await invoke(runtime, ".send nobody-at-all hi", chat_id=OWNER)
    assert "nobody-at-all" in _said(sent)


# ---------------------------------------------------------------------------
# Reacting
# ---------------------------------------------------------------------------
class _RecordingClient:
    """A client that records raw requests, the way Telethon's is called."""

    def __init__(self) -> None:
        self.calls: list[object] = []

    def __call__(self, request):
        self.calls.append(request)

        async def _done():
            return True

        return _done()


async def test_a_reaction_reaches_the_client(runtime, monkeypatch) -> None:
    """1.7 tried `client.send_reaction`, which does not exist, so every reaction
    was a silent False. The raw request is the only way it ever worked."""
    client = _RecordingClient()
    monkeypatch.setattr(runtime, "client", client)
    runtime.sender.client = client
    ok = await runtime.sender.react(GROUP, 500, "❤")
    assert ok is True
    assert client.calls, "no SendReactionRequest was made"
    assert "SendReactionRequest" in type(client.calls[0]).__name__


# ---------------------------------------------------------------------------
# Ignoring somebody
# ---------------------------------------------------------------------------
async def test_ignoring_a_person_silences_her_towards_them(runtime) -> None:
    await _ready(runtime)
    runtime.ai = _StubProvider()
    await runtime.people.touch(FRIEND, "Ali")
    await runtime.people.set_dm(FRIEND, True)
    await invoke(runtime, f".ignore on {FRIEND}", chat_id=OWNER)
    message = FakeMessage(
        id=980,
        chat_id=FRIEND,
        sender_id=FRIEND,
        text="hey",
        out=False,
        is_private=True,
        is_group=False,
    )
    message.sender = FakeUser(user_id=FRIEND, first_name="Ali")
    normalized = normalize_message(
        message, self_id=OWNER, prefix=".", event_type=EventType.MESSAGE
    )
    await runtime.persona_replies.on_dm_chat(normalized)
    assert runtime.persona_replies.last_skip == "ignored"

    await invoke(runtime, f".ignore off {FRIEND}", chat_id=OWNER)
    assert not await runtime.store.is_ignored(FRIEND)


# ---------------------------------------------------------------------------
# Timelines
# ---------------------------------------------------------------------------
HIS_SENTENCE = (
    "clock off for 10 minutes, then clock on and off again after 3 minutes, "
    "then set an auto reply for private chats saying he's busy, then stop it, "
    "and in our saved messages say I'm your good girl"
)


def test_his_six_step_sentence_becomes_seven_steps() -> None:
    plan = build_plan(HIS_SENTENCE, intent=intent_module)
    actions = [step.action for step in plan.steps]
    assert actions == [
        "clock off",
        "clock on",
        "clock off",
        "autoreply add --type any --private => he's busy",
        "autoreply on",
        "autoreply off",
        "send saved I'm your good girl",
    ]


def test_the_moments_are_what_he_said() -> None:
    start = utc_now()
    plan = build_plan(HIS_SENTENCE, intent=intent_module, now=start)
    offsets = [round((step.at - start).total_seconds()) for step in plan.steps]
    assert offsets[0] == 0          # now
    assert offsets[1] == 600        # ten minutes later
    assert offsets[2] == 780        # three more minutes
    assert offsets[3:] == [780, 780, 780, 780]


def test_the_sentence_is_read_as_one_instruction() -> None:
    found = intent_module.detect(HIS_SENTENCE, features={"clock"}, commands={"clock", "autoreply"})
    assert found is not None
    assert found.lines and found.lines[0].startswith("plan do ")


async def test_a_plan_runs_its_steps_once(runtime) -> None:
    due = utc_now() - timedelta(seconds=1)
    plan = build_plan("clock off for 10 minutes, then clock on", intent=intent_module, now=due)
    created = await runtime.plans.create(plan, said="test", owner_id=OWNER)
    assert created["ok"]
    fired = await runtime.scheduler.tick()
    assert fired["jobs"] >= 1
    assert await runtime.store.get_setting("clock.enabled", None) is False
    # A second tick must not run the same step again.
    await runtime.scheduler.tick()
    steps = await runtime.store.plan_steps(created["id"])
    assert sum(1 for step in steps if step["status"] == "done") == 1


async def test_cancelling_stops_the_rest(runtime) -> None:
    future = utc_now() + timedelta(hours=1)
    plan = build_plan("clock off for 10 minutes, then clock on", intent=intent_module, now=future)
    created = await runtime.plans.create(plan, said="test", owner_id=OWNER)
    stopped = await runtime.plans.cancel(str(created["id"]))
    assert stopped["ok"]
    steps = await runtime.store.plan_steps(created["id"])
    assert all(step["status"] == "cancelled" for step in steps)
    plan_row = await runtime.store.plan(created["id"])
    assert plan_row["status"] == "cancelled"


async def test_a_cancelled_plan_does_not_fire(runtime) -> None:
    due = utc_now() - timedelta(seconds=1)
    plan = build_plan("clock off for 10 minutes, then clock on", intent=intent_module, now=due)
    created = await runtime.plans.create(plan, said="test", owner_id=OWNER)
    await runtime.plans.cancel(str(created["id"]))
    await runtime.scheduler.tick()
    assert await runtime.store.get_setting("clock.enabled", "unset") != False  # noqa: E712


# ---------------------------------------------------------------------------
# The sentences he types
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("sentence", "expected"),
    (
        ("message @hmmm_6 and say I'm yura", "send @hmmm_6 I'm yura"),
        ("say I'm your good girl in our saved messages", "send saved I'm your good girl"),
        ("message @sara boo 10 times", "send @sara boo --repeat 10"),
        ("send @sara surprise with a spoiler", "send @sara surprise --spoiler"),
        ("react with ❤", "emoji ❤"),
        ("write to @sara and wish her a happy birthday", "send @sara wish her a happy birthday --compose"),
        # Every rule is written through `.policy` now: it is the same instruction,
        # and it can carry a lifespan or a room that the old binary could not.
        ("don't talk to @ali", "policy ignore @ali"),
        ("talk to ali again", "policy answer ali"),
    ),
)
def test_the_toolbox_sentences(sentence: str, expected: str) -> None:
    found = intent_module.detect(sentence)
    assert found is not None, sentence
    assert found.lines[0] == expected


def test_his_ordinary_chatter_is_untouched() -> None:
    for sentence in ("how are you today?", "i was thinking about you", "the clock on my profile is pretty"):
        assert intent_module.detect(sentence, features={"clock"}, commands={"clock"}) is None or (
            intent_module.detect(sentence, features={"clock"}, commands={"clock"}).label == "clock"
            and "question" in sentence
        )


# ---------------------------------------------------------------------------
# The clock: jobs must actually fire
# ---------------------------------------------------------------------------
async def test_a_queued_send_fires_when_its_moment_comes(runtime) -> None:
    await runtime.scheduler.enqueue(
        "reach_send", utc_now() - timedelta(seconds=1), {"chat_id": 999, "text": "late hello"}
    )
    fired = await runtime.scheduler.tick()
    assert fired["jobs"] >= 1
    assert [row for row in runtime.client.sent if "late hello" in row.text], "the job never sent"


async def test_a_self_destruct_really_deletes(runtime) -> None:
    await runtime.scheduler.enqueue(
        "reach_unsend", utc_now() - timedelta(seconds=1), {"chat_id": 999, "message_id": 1001}
    )
    await runtime.scheduler.tick()
    assert runtime.client.deleted, "nothing was deleted"


async def test_a_plan_step_runs_a_real_console_line(runtime) -> None:
    await runtime.store.set_setting("clock.enabled", True)
    plan = build_plan("clock off for 10 minutes", intent=intent_module)
    created = await runtime.plans.create(plan, said="clock off for 10 minutes", owner_id=OWNER)
    step = (await runtime.store.plan_steps(created["id"]))[0]
    # The scheduler hands the handler its own row; the payload is inside it.
    await runtime.db.execute("DELETE FROM jobs")
    await runtime.scheduler.enqueue(
        "plan_step",
        utc_now() - timedelta(seconds=1),
        {"plan_id": created["id"], "step_id": step["id"], "index": 0},
    )
    fired = await runtime.scheduler.tick()
    assert fired["jobs"] == 1, fired
    assert await runtime.store.get_setting("clock.enabled", "unset") is False
    steps = await runtime.store.plan_steps(created["id"])
    assert steps[0]["status"] == "done"


# ---------------------------------------------------------------------------
# Sentences she writes herself
# ---------------------------------------------------------------------------
async def test_compose_writes_the_message_instead_of_copying_it(runtime) -> None:
    runtime.ai = _StubProvider(reply="happy birthday!\nmay the year be soft on you.\nall the best, truly.")
    sent = await invoke(
        runtime, ".send saved --compose wish him a happy birthday", chat_id=OWNER
    )
    assert [row for row in runtime.client.sent if "happy birthday" in row.text], sent
    assert not [row for row in runtime.client.sent if "wish him" in row.text]


async def test_a_composed_repeat_is_a_different_sentence_each_time(runtime) -> None:
    """Ten times must not be the same wish ten times — she writes each one."""
    runtime.ai = _StubProvider(reply="one.\ntwo.\nthree.")
    await invoke(
        runtime, ".send saved --compose say goodnight --repeat 3", chat_id=OWNER
    )
    jobs = await runtime.db.fetch_all("SELECT payload FROM jobs WHERE kind = 'reach_send'")
    bodies = [json.loads(row["payload"])["text"] for row in jobs]
    assert len(bodies) == 2, bodies
    assert bodies == ["two.", "three."]


def test_a_model_answer_is_cleaned_before_it_is_sent() -> None:
    from havoc.features.reach import _clean_written

    reply = '1. "hey, are you free tonight?"\n2. hi! how did it go?\nSure! Here they are:'
    assert _clean_written(reply, 5) == ["hey, are you free tonight?", "hi! how did it go?"]


async def test_a_broadcast_to_many_is_queued_not_looped(runtime) -> None:
    """"send to 10 different people": ten jobs, not a ten-second silence."""
    await invoke(runtime, ".broadcast saved,999,777,555,333 hello everyone", chat_id=OWNER)
    jobs = await runtime.db.fetch_all("SELECT payload FROM jobs WHERE kind = 'reach_send'")
    assert len(jobs) == 5, len(jobs)
    assert not [row for row in runtime.client.sent if "hello everyone" in row.text]


async def test_multitasking_runs_everything_queued_at_once(runtime) -> None:
    """Many actions at one moment must all happen on the next tick."""
    for index in range(4):
        await runtime.scheduler.enqueue(
            "reach_send", utc_now() - timedelta(seconds=1), {"chat_id": 999, "text": f"burst {index}"}
        )
    fired = await runtime.scheduler.tick()
    assert fired["jobs"] == 4, fired
    assert len([row for row in runtime.client.sent if "burst" in row.text]) == 4


async def test_reacting_to_a_reply_goes_through_the_console(runtime, monkeypatch) -> None:
    calls: list[tuple[int, int, str]] = []

    async def fake_react(chat_id, message_id, emoji):
        calls.append((chat_id, message_id, emoji))
        return True

    monkeypatch.setattr(runtime.reach, "react", fake_react)
    reply = FakeMessage(id=321, chat_id=GROUP, sender_id=FRIEND, text="look at this")
    await invoke(runtime, ".emoji ❤", chat_id=GROUP, reply=reply)
    assert calls == [(GROUP, 321, "❤")]
