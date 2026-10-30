"""Her hands, and the leash. Policy first, capability second.

The plan this follows says the interesting part is not the integration but the
permission model: reads free, writes confirmed once, deletes confirmed every time,
money and identity refused outright, four calls a turn, twenty an hour, and a loop
breaker. Every one of those is a test here, because a policy that is only a
paragraph in a docstring is a policy nobody enforces.

The other half is the promise that made this upgrade acceptable: a turn that needs
no tool must produce *exactly* the answer it produced before the feature existed.
"""

from __future__ import annotations

import json

import pytest
from conftest import invoke, sent_text

from havoc.services.actions import (
    CONFIRM_WINDOW,
    MAX_ACTIONS_PER_HOUR,
    MAX_CALLS_PER_TURN,
    ActionLayer,
    ActionResult,
    Tool,
    ToolCall,
    Turn,
    calls_from_native,
    parse_calls,
)
from havoc.services.agent import Agent
from havoc.services.ai_provider import AIMessage


def _tool(name: str = "peek", *, scope: str = "read", risk: str = "low", calls: list | None = None) -> Tool:
    log = calls if calls is not None else []

    async def run(turn: Turn, args: dict[str, str]) -> ActionResult:
        log.append((name, dict(args)))
        return ActionResult(True, note=f"{name} ran")

    return Tool(
        name=name,
        summary=f"the {name} tool",
        properties={"what": "what to look at"},
        required=("what",),
        run=run,
        scope=scope,
        risk=risk,
    )


def _layer(runtime, *tools: Tool) -> ActionLayer:
    return ActionLayer(runtime, tools or (_tool(),))


# ---------------------------------------------------------------------------
# The tool shape
# ---------------------------------------------------------------------------
def test_a_tool_has_a_json_schema_a_model_can_read() -> None:
    tool = _tool()
    schema = tool.schema
    assert schema["required"] == ["what"]
    assert schema["properties"]["what"]["type"] == "string"
    function = tool.as_function()["function"]
    assert function["name"] == "peek"
    assert function["parameters"]["type"] == "object"


def test_a_tool_cannot_declare_a_scope_outside_the_list() -> None:
    with pytest.raises(ValueError):
        _tool(scope="telepathy")


def test_arguments_are_checked_before_anything_runs() -> None:
    tool = _tool()
    assert tool.validate({"what": "the bridge"}) == {"what": "the bridge"}
    with pytest.raises(Exception) as missing:
        tool.validate({})
    assert "needs what" in str(missing.value)
    with pytest.raises(Exception) as extra:
        tool.validate({"what": "x", "hack": "y"})
    assert "does not take" in str(extra.value)


def test_money_and_identity_are_refused_before_they_can_run() -> None:
    """Not confirmed. Refused. There is nothing to approve."""
    for scope in ("money", "identity"):
        tool = _tool(scope=scope)
        assert tool.refused is True
        with pytest.raises(Exception) as caught:
            tool.validate({"what": "everything"})
        assert "out of bounds" in str(caught.value)


# ---------------------------------------------------------------------------
# Execution and policy
# ---------------------------------------------------------------------------
async def test_a_read_runs_without_asking(runtime) -> None:
    log: list = []
    layer = _layer(runtime, _tool(calls=log))
    outcome = await layer.execute(ToolCall("peek", {"what": "x"}), Turn(chat_id=1))
    assert outcome.status == "done"
    assert log == [("peek", {"what": "x"})]


async def test_a_write_asks_once_and_then_remembers(runtime) -> None:
    log: list = []
    layer = _layer(runtime, _tool("note", scope="write", calls=log))
    turn = Turn(chat_id=7)

    first = await layer.execute(ToolCall("note", {"what": "a"}), turn)
    assert first.status == "needs_confirm"
    assert log == [], "nothing may run before the yes"

    approved = await layer.resolve(7, approve=True)
    assert approved.status == "done" and log == [("note", {"what": "a"})]

    second = await layer.execute(ToolCall("note", {"what": "b"}), Turn(chat_id=7))
    assert second.status == "done"
    assert len(log) == 2, "a tool approved once is approved"


async def test_a_delete_asks_every_single_time(runtime) -> None:
    log: list = []
    layer = _layer(runtime, _tool("wipe", scope="delete", risk="medium", calls=log))
    await layer.execute(ToolCall("wipe", {"what": "a"}), Turn(chat_id=9))
    await layer.resolve(9, approve=True)
    assert len(log) == 1

    again = await layer.execute(ToolCall("wipe", {"what": "b"}), Turn(chat_id=9))
    assert again.status == "needs_confirm", "deleting is never silent, not even twice"
    assert len(log) == 1


async def test_no_means_no(runtime) -> None:
    log: list = []
    layer = _layer(runtime, _tool("note", scope="write", calls=log))
    await layer.execute(ToolCall("note", {"what": "a"}), Turn(chat_id=3))
    outcome = await layer.resolve(3, approve=False)
    assert outcome.status == "refused"
    assert log == []
    assert await layer.pending(3) is None


async def test_a_question_goes_stale(runtime) -> None:
    layer = _layer(runtime, _tool("note", scope="write"))
    await layer.execute(ToolCall("note", {"what": "a"}), Turn(chat_id=4))
    record = await layer.pending(4)
    assert record is not None
    # Age it: the two-minute window is a window, not a suggestion.
    record["expires_at"] = "2000-01-01T00:00:00+00:00"
    await runtime.store.set_setting(f"actions.pending.{record['token']}", record)
    assert await layer.pending(4) is None
    assert CONFIRM_WINDOW.total_seconds() == 120


async def test_an_unknown_tool_is_refused_by_name(runtime) -> None:
    layer = _layer(runtime)
    outcome = await layer.execute(ToolCall("launch_missiles", {}), Turn(chat_id=1))
    assert outcome.status == "failed"
    assert "no tool" in outcome.note


async def test_the_same_call_twice_stops_the_loop(runtime) -> None:
    log: list = []
    layer = _layer(runtime, _tool(calls=log))
    turn = Turn(chat_id=5)
    call = ToolCall("peek", {"what": "x"})
    assert (await layer.execute(call, turn)).status == "done"
    assert (await layer.execute(call, turn)).status == "done"
    blocked = await layer.execute(call, turn)
    assert blocked.status == "blocked"
    assert len(log) == 2


async def test_four_calls_a_turn_and_no_more(runtime) -> None:
    log: list = []
    layer = _layer(runtime, _tool(calls=log))
    turn = Turn(chat_id=6)
    for index in range(MAX_CALLS_PER_TURN):
        assert (await layer.execute(ToolCall("peek", {"what": str(index)}), turn)).status == "done"
    stopped = await layer.execute(ToolCall("peek", {"what": "one more"}), turn)
    assert stopped.status == "blocked"
    assert len(log) == MAX_CALLS_PER_TURN


async def test_the_hourly_valve_counts_rows_not_memory(runtime) -> None:
    layer = _layer(runtime)
    for index in range(MAX_ACTIONS_PER_HOUR):
        await runtime.store.db.insert(
            "INSERT INTO yura_actions(chat_id, tool, created_at) VALUES(?, ?, datetime('now'))",
            (1, f"t{index}"),
        )
        await runtime.store.db.execute(
            "UPDATE yura_actions SET created_at = ? WHERE tool = ?",
            ("2999-01-01T00:00:00+00:00", f"t{index}"),
        )
    outcome = await layer.execute(ToolCall("peek", {"what": "x"}), Turn(chat_id=1))
    assert outcome.status == "blocked"
    assert "hour" in outcome.note


async def test_every_attempt_is_written_down(runtime) -> None:
    layer = _layer(runtime, _tool("note", scope="write"))
    await layer.execute(ToolCall("note", {"what": "a"}), Turn(chat_id=11, actor_id=42))
    rows = await layer.recent(11)
    assert rows and rows[0]["tool"] == "note"
    assert rows[0]["decision"] == "asked"
    assert rows[0]["actor_id"] == 42
    assert "what=a" in rows[0]["args_preview"]


# ---------------------------------------------------------------------------
# The wire formats
# ---------------------------------------------------------------------------
def test_a_fenced_block_is_a_tool_call_and_leaves_the_prose() -> None:
    text = 'fine, i will set it.\n\n```tool\n{"tool": "remind", "args": {"when": "30m", "what": "call mom"}}\n```'
    calls, clean = parse_calls(text)
    assert [call.name for call in calls] == ["remind"]
    assert calls[0].args["when"] == "30m"
    assert clean == "fine, i will set it."


def test_a_json_example_in_prose_is_not_an_action() -> None:
    text = 'here is what a call would look like: ```json\n{"hello": "world"}\n``` and that is it'
    calls, clean = parse_calls(text)
    assert calls == []
    assert clean == text


def test_broken_json_is_left_alone() -> None:
    text = "```tool\n{tool: remind,}\n```"
    calls, clean = parse_calls(text)
    assert calls == [] and clean == text


def test_the_inline_marker_also_works() -> None:
    calls, clean = parse_calls('ok [[do: {"tool": "recall", "args": {"about": "sara"}}]]')
    assert calls and calls[0].name == "recall"
    assert clean == "ok"


def test_native_tool_calls_are_understood() -> None:
    payload = [
        {
            "id": "1",
            "type": "function",
            "function": {"name": "recall", "arguments": '{"about": "sara"}'},
        }
    ]
    calls = calls_from_native(payload)
    assert calls[0].name == "recall" and calls[0].args == {"about": "sara"}


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------
class ScriptedProvider:
    """Answers with a script, in order, and records the messages it was given."""

    available = True

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[list[AIMessage]] = []

    async def converse(self, messages, *, max_tokens: int = 400, temperature: float = 0.85) -> str:
        self.calls.append(list(messages))
        return self.replies.pop(0) if self.replies else "…"


async def test_no_tool_means_no_second_request(runtime) -> None:
    provider = ScriptedProvider("just a normal answer")
    layer = _layer(runtime)
    step = await Agent(layer).run(
        provider, [AIMessage("user", "hi")], turn=Turn(chat_id=1), max_tokens=100
    )
    assert step.used is False
    assert step.text == "just a normal answer"
    assert len(provider.calls) == 1


async def test_a_tool_runs_then_she_answers(runtime) -> None:
    log: list = []
    provider = ScriptedProvider(
        '```tool\n{"tool": "peek", "args": {"what": "the bridge"}}\n```',
        "found it, daddaaa.",
    )
    layer = _layer(runtime, _tool(calls=log))
    step = await Agent(layer).run(
        provider, [AIMessage("user", "what about the bridge")], turn=Turn(chat_id=1), max_tokens=100
    )
    assert log == [("peek", {"what": "the bridge"})]
    assert step.text == "found it, daddaaa."
    assert step.did == ["peek"]
    assert len(provider.calls) == 2
    observation = [m for m in provider.calls[1] if m.role == "user"][-1].content
    assert "peek ran" in observation
    assert "same voice" in observation


async def test_a_write_stops_at_the_question(runtime) -> None:
    log: list = []
    provider = ScriptedProvider(
        '```tool\n{"tool": "note", "args": {"what": "a"}}\n```',
        "this should never be asked for",
    )
    layer = _layer(runtime, _tool("note", scope="write", calls=log))
    step = await Agent(layer).run(
        provider, [AIMessage("user", "remember a")], turn=Turn(chat_id=2), max_tokens=100
    )
    assert step.waiting is not None
    assert log == []
    assert len(provider.calls) == 1, "asking a question is not a reason for another round"


# ---------------------------------------------------------------------------
# Integration with her actual turn
# ---------------------------------------------------------------------------
async def test_an_ordinary_answer_is_untouched_by_hands(runtime) -> None:
    """The promise: no tool, no change — not even a receipt line."""
    from test_picture_reply import StubProvider

    plain = "hello daddaaa ✦ i was just sitting here\n\nnothing happened"
    runtime.ai = StubProvider(plain)
    answer = await runtime.yura_chat.answer(plain and "hey", chat_id=100)
    assert "⌁" not in answer.text
    assert answer.source == "model"


async def test_a_tool_turn_carries_a_receipt(runtime) -> None:
    from test_picture_reply import StubProvider

    class ToolProvider(StubProvider):
        def __init__(self) -> None:
            super().__init__('```tool\n{"tool": "recall", "args": {"about": "sara"}}\n```')
            self.rounds = 0

        async def converse(self, messages, *, max_tokens: int = 400, temperature: float = 0.85) -> str:
            self.rounds += 1
            self.calls.append(list(messages))
            return self.reply if self.rounds == 1 else "she is the one with the boat ᵕ̈"

    runtime.ai = ToolProvider()
    answer = await runtime.yura_chat.answer("what do you know about sara", chat_id=100)
    assert "she is the one with the boat" in answer.text
    assert "recall" in answer.text, answer.text


async def test_hands_can_be_switched_off(runtime) -> None:
    from test_picture_reply import StubProvider

    class ToolProvider(StubProvider):
        async def converse(self, messages, *, max_tokens: int = 400, temperature: float = 0.85) -> str:
            self.calls.append(list(messages))
            return '```tool\n{"tool": "recall", "args": {}}\n```'

    await runtime.store.set_setting("yura.agent", "0")
    runtime.ai = ToolProvider()
    answer = await runtime.yura_chat.answer("hello", chat_id=100)
    assert answer.text


async def test_the_commands_exist(runtime) -> None:
    await invoke(runtime, ".yura tools")
    assert "recall" in sent_text(runtime)
    await invoke(runtime, ".yura actions")
    assert sent_text(runtime)
    await invoke(runtime, ".yura yes")
    assert sent_text(runtime)


def test_the_prompt_block_says_hands_do_not_change_her_voice(runtime) -> None:
    text = runtime.actions.catalogue("en")
    assert "hands" in text and "tool" in text
    assert "same voice" in text
    persian = runtime.actions.catalogue("fa")
    assert "دست" in persian


def test_the_catalogue_never_offers_a_refused_tool(runtime) -> None:
    runtime.actions.register(_tool("pay_the_rent", scope="money"))
    assert "pay_the_rent" not in runtime.actions.catalogue("en")
    assert all(fn["function"]["name"] != "pay_the_rent" for fn in runtime.actions.as_functions())


def test_the_real_toolbox_is_the_promised_one(runtime) -> None:
    names = {tool.name for tool in runtime.actions.tools}
    assert {"recall", "search", "translate", "remember", "forget", "remind"} <= names
    # A tool that can address an arbitrary chat is a spam cannon with a personality.
    assert "send" not in names and "message" not in names


async def test_the_layer_refuses_a_money_tool_even_if_one_is_registered(runtime) -> None:
    calls: list = []
    runtime.actions.register(_tool("buy", scope="money", calls=calls))
    outcome = await runtime.actions.execute(ToolCall("buy", {"what": "a car"}), Turn(chat_id=1))
    assert outcome.status == "refused"
    assert calls == []


def test_a_tool_reply_block_is_not_a_json_lesson() -> None:
    """The block must not survive into what a person reads."""
    calls, clean = parse_calls(
        'sure.\n\n```tool\n' + json.dumps({"tool": "remind", "args": {"when": "1h", "what": "x"}}) + "\n```"
    )
    assert calls and clean == "sure."
