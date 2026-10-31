"""One sentence, four moments: the chain he asked for, run to the wire.

"سارا رو واسه دو ساعت ایگنور کن، دو ساعت که تموم شد برو پیوی محمد و بگو سلام و
بعد ده دقیقه پیامتو پاک کن بعدش فال بگیر" is not four instructions typed one after
another — it is a *timeline*: a two-hour rule, a direct message when the rule
expires, a deletion ten minutes after that message, and a fal after it.

The tests here start at that sentence and end at the Telegram client and the
database: the rule row with its own expiry, the send that really happened, the
outbox row for **that** send marked deleted, and the fal. Reading the sentence
correctly is not the claim; doing it is.
"""

from __future__ import annotations

from conftest import FakeMessage, incoming, invoke
from test_abilities import OWNER

from havoc.services import intent as intent_module
from havoc.services.plans import build_plan
from havoc.services.self_console import run as run_console

CHAIN = (
    "سارا رو واسه دو ساعت ایگنور کن، دو ساعت که تموم شد برو پیوی محمد و بگو سلام "
    "و بعد ده دقیقه پیامتو پاک کن بعدش فال بگیر"
)
SARA = 888
MOHAMMAD = 777


def _offsets(plan) -> list[int]:
    start = plan.steps[0].at
    return [round((step.at - start).total_seconds()) for step in plan.steps]


def _actions(plan) -> list[str]:
    return [step.action for step in plan.steps]


# ---------------------------------------------------------------------------
# Reading it
# ---------------------------------------------------------------------------
def test_the_persian_chain_is_four_steps_in_order() -> None:
    plan = build_plan(CHAIN, intent=intent_module)
    assert _actions(plan) == [
        "policy ignore سارا --for 2h",
        "send محمد سلام",
        "unsend last",
        "fal",
    ]


def test_the_moments_are_relative_to_the_step_before() -> None:
    plan = build_plan(CHAIN, intent=intent_module)
    # now · two hours later · ten minutes after that · immediately after that
    assert _offsets(plan) == [0, 7200, 7800, 7800]


def test_the_window_belongs_to_the_rule_and_stripping_it_would_make_it_permanent() -> None:
    plan = build_plan("سارا رو واسه دو ساعت ایگنور کن", intent=intent_module)
    assert _actions(plan) == ["policy ignore سارا --for 2h"]


def test_a_persian_order_in_the_middle_of_a_plan_is_still_one_step() -> None:
    plan = build_plan("پیوی محمد بگو سلام بعدش فال بگیر", intent=intent_module)
    assert _actions(plan) == ["send محمد سلام", "fal"]


def test_a_bare_wait_is_a_delay_only_when_an_order_follows() -> None:
    """“دو ساعت خیلی زیاده” is a sentence about two hours, not a two-hour wait."""
    plan = build_plan("دو ساعت خیلی زیاده بعدش فال بگیر", intent=intent_module)
    assert _actions(plan) == ["fal"]
    assert _offsets(plan)[0] == 0


def test_the_whole_sentence_is_read_as_a_timeline_not_one_order() -> None:
    found = intent_module.detect(CHAIN)
    assert found is not None
    assert found.lines == (f"plan do {CHAIN}",)


def test_english_multiple_orders_still_land_on_their_minutes() -> None:
    plan = build_plan(
        "turn the clock off for ten minutes, then turn it on, then stop it after two hours",
        intent=intent_module,
    )
    assert _actions(plan) == ["clock off", "clock on", "clock off"]
    assert _offsets(plan) == [0, 600, 7800]


def test_a_long_chain_keeps_every_distinct_moment() -> None:
    plan = build_plan(
        "clock off for one minute, then clock on, then clock off, then clock on, then fal",
        intent=intent_module,
    )
    # The window's own undo and the explicit "clock on" land on the same second,
    # so they are one step: a plan must never say the same thing twice.
    keys = [(step.action, step.at) for step in plan.steps]
    assert len(keys) == len(set(keys))
    assert _actions(plan) == ["clock off", "clock on", "clock off", "fal"]


# ---------------------------------------------------------------------------
# Doing it
# ---------------------------------------------------------------------------
async def _wire(runtime) -> None:
    await runtime.store.add_alias("محمد", str(MOHAMMAD))
    await runtime.store.add_alias("سارا", str(SARA))
    await runtime.store.set_feature("automation", True)
    await runtime.feature_manager.refresh()


async def _run_all(runtime, plan_id: int) -> None:
    """Run every step the way the scheduler would, oldest first."""
    for row in await runtime.store.plan_steps(plan_id):
        await runtime.plans.run_step({"plan_id": plan_id, "step_id": int(row["id"])})


async def test_the_plan_runs_end_to_end(runtime) -> None:
    await _wire(runtime)
    before = len(runtime.client.sent)
    reply = await run_console(
        runtime, f"plan do {CHAIN}", chat_id=OWNER, sender_id=OWNER, language="fa"
    )
    assert reply.ok, reply.note

    plan = (await runtime.store.plans(1))[0]
    steps = await runtime.store.plan_steps(int(plan["id"]))
    assert [row["action"] for row in steps] == [
        "policy ignore سارا --for 2h",
        "send محمد سلام",
        "unsend last",
        "fal",
    ]

    await _run_all(runtime, int(plan["id"]))

    # 1. the rule exists, and it carries its own expiry
    rules = await runtime.db.fetch_all("SELECT * FROM reply_rules")
    assert any(
        int(row["user_id"]) == SARA and row["mode"] == "ignore" and row["expires_at"]
        for row in rules
    )

    # 2. the direct message went out, and it said what he told her to say
    sent = runtime.client.sent[before:]
    assert [row.text for row in sent] == ["سلام"]

    # 3. that exact message — not Saved Messages, not somebody else — is deleted
    outbox = await runtime.store.outbox(10)
    dm_row = next(row for row in outbox if int(row["chat_id"]) == MOHAMMAD)
    assert dm_row["status"] == "deleted", dict(dm_row)

    # 4. the fal happened
    assert any("fal" in str(row["action"]) for row in steps)
    assert (await runtime.store.plan(int(plan["id"])))["status"] == "done"


async def test_the_deletion_step_only_touches_the_plans_own_message(runtime) -> None:
    """A delete step in a plan deletes the plan's bubble, not the newest anywhere."""
    await _wire(runtime)
    await runtime.store.add_outbox(555, message_id=1234, text="something older", status="sent")
    reply = await run_console(
        runtime, f"plan do {CHAIN}", chat_id=OWNER, sender_id=OWNER, language="fa"
    )
    assert reply.ok
    plan_id = int((await runtime.store.plans(1))[0]["id"])
    await _run_all(runtime, plan_id)

    rows = {int(row["id"]): row for row in await runtime.store.outbox(20)}
    stray = next(row for row in rows.values() if int(row["chat_id"]) == 555)
    assert stray["status"] == "sent", "the delete step reached outside the plan"


async def test_the_rule_silences_her_for_the_two_hours(runtime) -> None:
    await _wire(runtime)
    await runtime.store.set_person_rule(SARA, "ignore", name="سارا", seconds=7200)
    before = len(runtime.client.sent)
    await incoming(
        runtime,
        FakeMessage(
            id=910,
            chat_id=-1001234567890,
            sender_id=SARA,
            text="hey yura, you there?",
            is_group=True,
        ),
    )
    assert runtime.client.sent[before:] == [], "she answered somebody she was told to ignore"


async def test_the_dm_step_names_a_person_the_way_he_says_it(runtime) -> None:
    await _wire(runtime)
    reply = await run_console(runtime, "send محمد سلام", chat_id=OWNER, sender_id=OWNER)
    assert reply.ok, reply.note
    assert [row.text for row in runtime.client.sent] == ["سلام"]
    assert int(runtime.client.sent[-1].chat_id) == MOHAMMAD


# ---------------------------------------------------------------------------
# The same sentence typed to her, not to the console
# ---------------------------------------------------------------------------
async def test_typing_the_chain_to_her_makes_a_plan(runtime) -> None:
    """The same sentence, but spoken to her — not to the console."""
    await _wire(runtime)
    before = len(runtime.client.sent)
    await invoke(runtime, f".yura {CHAIN}", chat_id=OWNER, sender_id=OWNER)
    answers = [row.text for row in runtime.client.sent[before:]]
    assert answers, "she said nothing about the chain"
    assert any("4" in text or "۴" in text for text in answers), answers
    assert await runtime.store.plans(5), "no plan was created"


async def test_her_answer_names_the_four_moments_in_order(runtime) -> None:
    await _wire(runtime)
    await invoke(runtime, f".yura {CHAIN}", chat_id=OWNER, sender_id=OWNER)
    steps = await runtime.store.plan_steps(int((await runtime.store.plans(1))[0]["id"]))
    assert len(steps) == 4
    # Said back to him as a timeline: each step carries its own moment.
    assert all(str(row["at"]) for row in steps)


# ---------------------------------------------------------------------------
# The same orders, said the other ways
# ---------------------------------------------------------------------------
def test_persian_ways_of_saying_ignore_a_person() -> None:
    for said in ("سارا رو بیخیال شو", "از سارا بیخیال شو", "بیخیال سارا شو", "سارا رو ول کن"):
        found = intent_module.detect(said)
        assert found is not None, said
        assert found.lines == ("policy ignore سارا",), (said, found.lines)


def test_english_ways_of_saying_ignore_a_person() -> None:
    for said in ("ignore sara", "stay away from sara", "leave sara alone", "be cold to sara"):
        found = intent_module.detect(said)
        assert found is not None, said
        assert found.lines == ("policy ignore sara",), (said, found.lines)


def test_a_hedge_after_the_name_is_not_part_of_the_name() -> None:
    found = intent_module.detect("be distant with zero from here on")
    assert found is not None and found.lines == ("policy ignore zero",)


def test_leave_this_group_is_still_leaving_not_a_rule() -> None:
    found = intent_module.detect("leave this group")
    assert found is not None and found.lines == ("leave",)


def test_persian_ways_of_naming_a_private_chat() -> None:
    for said in ("به محمد بگو سلام", "برو به محمد پیام بده سلام", "پیوی محمد بگو سلام"):
        found = intent_module.detect(said)
        assert found is not None, said
        assert found.lines == ("send محمد سلام",), (said, found.lines)


def test_a_send_two_hours_out_is_a_plan_not_a_send_now() -> None:
    said = "دو ساعت دیگه به محمد پیام بده سلام"
    found = intent_module.detect(said)
    assert found is not None and found.lines == (f"plan do {said}",)
    plan = build_plan(said, intent=intent_module)
    assert [step.action for step in plan.steps] == ["send محمد سلام"]
    assert _offsets(plan) == [0]
    # The step is *later*: the planner's own clock, not this second.
    from havoc.utils.time import utc_now

    assert (plan.steps[0].at - utc_now()).total_seconds() > 7000


def test_deleting_her_previous_line_in_persian() -> None:
    for said in ("پیام قبلیت رو پاک کن", "پیامت رو پاک کن", "پیامتو پاک کن"):
        found = intent_module.detect(said)
        assert found is not None, said
        assert found.lines == ("unsend last",), (said, found.lines)


def test_an_english_chain_written_the_way_people_write_it() -> None:
    plan = build_plan(
        "ignore sara for 2 hours then dm mohammad saying hello and delete it in 10 minutes",
        intent=intent_module,
    )
    assert _actions(plan) == ["policy ignore sara --for 2h", "send mohammad hello", "unsend last"]
    assert _offsets(plan) == [0, 7200, 7800]


# ---------------------------------------------------------------------------
# The model's own hand: a timeline it wrote, not one the floor read
# ---------------------------------------------------------------------------
def _plan_tool(runtime):
    from havoc.services.tools import build_tools

    return {tool.name: tool for tool in build_tools(runtime)}["plan"]


async def test_the_model_can_write_the_steps_itself(runtime) -> None:
    from havoc.services.actions import Turn

    tool = _plan_tool(runtime)
    steps = (
        '[{"action": "send محمد سلام", "after": "2h"},'
        ' {"action": "unsend last", "after": "10m"},'
        ' {"action": "fal"}]'
    )
    result = await tool.run(Turn(chat_id=OWNER, actor_id=OWNER, language="en"), tool.validate({"steps": steps}))
    assert result.ok, result.detail
    plan = (await runtime.store.plans(1))[0]
    rows = await runtime.store.plan_steps(int(plan["id"]))
    assert [row["action"] for row in rows] == ["send محمد سلام", "unsend last", "fal"]
    assert str(plan["label"]).startswith("send محمد")  # not the JSON


async def test_a_model_step_that_is_not_a_command_is_refused(runtime) -> None:
    from havoc.services.actions import Turn

    tool = _plan_tool(runtime)
    result = await tool.run(
        Turn(chat_id=OWNER, actor_id=OWNER, language="en"),
        tool.validate({"steps": '[{"action": "launch rocket", "after": "1m"}]'}),
    )
    assert not result.ok and "launch" in result.detail
    assert await runtime.store.plans(5) == []


async def test_a_flattened_step_list_still_reads(runtime) -> None:
    """A model that sends an array of objects gets it flattened to words."""
    from havoc.services.actions import Turn

    tool = _plan_tool(runtime)
    args = tool.validate(
        {"steps": [{"action": "fal", "after": "1m"}, {"action": "unsend last", "after": "5m"}]}
    )
    result = await tool.run(Turn(chat_id=OWNER, actor_id=OWNER, language="en"), args)
    assert result.ok, result.detail
    assert result.note.startswith("plan #") and "2 steps" in result.note


async def test_a_broken_step_is_recorded_and_the_rest_still_runs(runtime) -> None:
    """A deletion with nothing to delete marks itself, and the fal still happens."""
    await _wire(runtime)
    reply = await run_console(
        runtime, "plan do پیامتو پاک کن بعدش فال بگیر", chat_id=OWNER, sender_id=OWNER
    )
    assert reply.ok
    plan_id = int((await runtime.store.plans(1))[0]["id"])
    await _run_all(runtime, plan_id)

    rows = await runtime.store.plan_steps(plan_id)
    assert [row["status"] for row in rows] == ["failed", "done"]
    assert rows[0]["note"]
    plan = await runtime.store.plan(plan_id)
    assert plan["status"] == "failed" and "delete" in str(plan["note"])


# ---------------------------------------------------------------------------
# Her own hand, on a sentence no reader knows
# ---------------------------------------------------------------------------
async def test_the_model_builds_a_chain_end_to_end(runtime) -> None:
    """A paraphrase the floor cannot read becomes a real plan, from a tool call."""
    import json as _json

    class ChainCaller:
        model = "chaincaller"
        available = True

        def __init__(self) -> None:
            self.rounds = 0

        async def converse_tools(self, messages, *, tools=None, max_tokens=400, temperature=0.85):
            self.rounds += 1
            if self.rounds == 1:
                return "right, three things, on a clock.", [
                    {
                        "function": {
                            "name": "plan",
                            "arguments": {
                                "steps": _json.dumps(
                                    [
                                        {"action": "send محمد سلام", "after": "2h"},
                                        {"action": "unsend last", "after": "10m"},
                                        {"action": "fal"},
                                    ],
                                    ensure_ascii=False,
                                )
                            },
                        }
                    }
                ]
            return "planned it — two hours to محمد, then the fal.", []

    await _wire(runtime)
    runtime.ai = ChainCaller()
    answer = await runtime.yura_chat.answer(
        "یه برنامه بچین: دو ساعت بعد به محمد سلام بده، ده دقیقه بعدش پیامتو پاک کن، بعدشم فال بگیر یورا",
        chat_id=OWNER,
        is_owner=True,
    )
    assert answer.text
    plans = await runtime.store.plans(5)
    assert len(plans) == 1, "a plan was made more than once"
    steps = await runtime.store.plan_steps(int(plans[0]["id"]))
    assert [row["action"] for row in steps] == ["send محمد سلام", "unsend last", "fal"]


async def test_a_plan_remembers_the_room_it_was_asked_in(runtime) -> None:
    """'say hi here' in a group must arrive in the group, not in Saved Messages."""
    await _wire(runtime)
    room = -1001234567890
    reply = await run_console(
        runtime, "plan do این گروه بگو سلام بعدش فال بگیر", chat_id=room, sender_id=OWNER
    )
    assert reply.ok, reply.note
    plan = (await runtime.store.plans(1))[0]
    assert int(plan["chat_id"]) == room


async def test_here_means_the_room_and_the_step_arrives_there(runtime) -> None:
    """A plan made in a group speaks in that group, hours later."""
    await _wire(runtime)
    room = -1001234567890
    found = intent_module.detect("این گروه بگو سلام")
    assert found is not None and found.lines == ("send --here سلام",)

    reply = await run_console(
        runtime, "plan do این گروه بگو سلام بعدش فال بگیر", chat_id=room, sender_id=OWNER
    )
    assert reply.ok, reply.note
    plan_id = int((await runtime.store.plans(1))[0]["id"])
    before = len(runtime.client.sent)
    await _run_all(runtime, plan_id)
    assert [(row.chat_id, row.text) for row in runtime.client.sent[before:]] == [(room, "سلام")]


def test_say_hi_here_is_the_room_not_a_name() -> None:
    assert intent_module.detect("say hi here").lines == ("send --here hi",)
    assert intent_module.detect("say hi in this group").lines == ("send --here hi",)


def test_a_bare_persian_say_is_conversation_not_an_order() -> None:
    """"بگو یه چیزی" is "say something", a sentence — not a broadcast."""
    assert intent_module.detect("بگو یه چیزی") is None
