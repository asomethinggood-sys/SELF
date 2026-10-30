"""The four things he photographed, pinned as tests.

They came in as screenshots of a real chat, so each test is written from the
screenshot rather than from the code:

1. she sent him the *tool call* as a code block with a copy button instead of
   turning the clock off — the block had two JSON objects in it, one per line,
   which the parser could not read as a whole;
2. every small request stopped at a permission question;
3. the question itself arrived as ``&lt;b&gt;Waiting for you&lt;/b&gt;`` — escaped HTML;
4. ``.yura yes`` answered "this command is restricted to the account owner".

A fix that only works in a comment is not a fix, so each one is checked by
driving the same code path the live account drives.
"""

from __future__ import annotations

import json

import pytest

from havoc.services.actions import (
    Tool,
    ToolCall,
    Turn,
    parse_calls,
    scrub_tool_text,
)
from havoc.services.agent import Agent
from havoc.services.ai_provider import AIMessage

#: The exact fence from the 20:00 screenshot: two objects, newline separated.
TWO_IN_ONE = (
    "دستور خوندم، دَدّا.\n\n"
    "```tool\n"
    '{"tool": "command", "args": {"command": "clock", "args": "off"}}\n'
    '{"tool": "command", "args": {"command": "remind", "args": "1h drink water"}}\n'
    "```"
)


# ---------------------------------------------------------------------------
# 1. Tool code is executed, never displayed
# ---------------------------------------------------------------------------
def test_two_objects_in_one_fence_are_two_calls() -> None:
    calls, clean = parse_calls(TWO_IN_ONE)
    assert [call.name for call in calls] == ["command", "command"]
    assert calls[0].args == {"command": "clock", "args": "off"}
    assert calls[1].args == {"command": "remind", "args": "1h drink water"}
    assert "```" not in clean and '"tool"' not in clean


def test_an_array_of_calls_is_also_read() -> None:
    body = json.dumps(
        [
            {"tool": "command", "args": {"command": "clock", "args": "on"}},
            {"tool": "command", "args": {"command": "about"}},
        ]
    )
    calls, _ = parse_calls(f"ok\n```tool\n{body}\n```")
    assert [call.name for call in calls] == ["command", "command"]


def test_a_broken_tool_block_never_reaches_the_reader() -> None:
    """Half-written JSON is not an answer she is allowed to send."""
    leftover = 'fine\n\n```tool\n{"tool": "command", "args": {oops}\n```'
    assert "```" not in scrub_tool_text(leftover)
    assert "oops" not in scrub_tool_text(leftover)
    bare = 'then he said {"tool": "command", "args": {}} to me'
    assert "{\"tool\"" not in scrub_tool_text(bare)


def test_a_real_code_block_survives_the_scrubber() -> None:
    """She talks; she is not a leaky parser. Ordinary code stays ordinary code."""
    text = "here:\n```python\nprint(1)\n```"
    assert scrub_tool_text(text) == text


def _tool(name: str, scope: str, log: list, card: str = "") -> Tool:
    async def run(turn: Turn, args: dict[str, str]):
        from havoc.services.actions import ActionResult

        log.append((name, dict(args)))
        return ActionResult(True, note=f"{name} done", title=card, lines=("line",))

    return Tool(
        name=name,
        summary=f"the {name} tool",
        properties={"what": "what"},
        required=("what",),
        run=run,
        scope=scope,
    )


class Scripted:
    model = "stub"
    available = True

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[list[object]] = []

    async def converse(self, messages, *, max_tokens: int = 400, temperature: float = 0.8):
        self.calls.append(list(messages))
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]

    async def converse_tools(self, messages, *, tools, max_tokens: int = 400):
        return await self.converse(messages, max_tokens=max_tokens), []


async def test_a_screenshot_fence_runs_every_call_and_shows_none_of_it(runtime) -> None:
    """The turn he photographed, end to end: two commands, no code block."""
    log: list = []
    layer = runtime.actions
    for name in ("clock18", "remind18"):
        layer.register(_tool(name, "write", log))
    provider = Scripted(
        '```tool\n{"tool": "clock18", "args": {"what": "profile"}}\n'
        '{"tool": "remind18", "args": {"what": "1h drink water"}}\n```',
        "باشه دَدّا، ساعت خاموشه و یادآور گذاشتم.",
    )
    step = await Agent(layer).run(
        provider,
        [AIMessage("user", "turn off the clock and set a timer")],
        turn=Turn(chat_id=100, actor_id=42, is_owner=True),
        max_tokens=200,
    )
    assert log == [
        ("clock18", {"what": "profile"}),
        ("remind18", {"what": "1h drink water"}),
    ]
    assert step.waiting is None
    assert "```" not in (step.text or ""), "she must never send him her machinery"
    assert '"tool"' not in (step.text or "")


# ---------------------------------------------------------------------------
# 2. Small things are not questions
# ---------------------------------------------------------------------------
async def test_a_reminder_is_not_a_question(runtime) -> None:
    log: list = []
    layer = runtime.actions
    layer.register(_tool("remind18b", "write", log))
    outcome = await layer.execute(
        ToolCall("remind18b", {"what": "1h drink water"}), Turn(chat_id=100, is_owner=True)
    )
    assert outcome.status == "done"
    assert log == [("remind18b", {"what": "1h drink water"})]
    assert await layer.pending(100) is None, "nothing was parked for a yes"


# ---------------------------------------------------------------------------
# 3. A card is HTML, not a lesson in HTML entities
# ---------------------------------------------------------------------------
def test_a_card_is_not_escaped(runtime) -> None:
    from havoc.features.persona import _visible
    from havoc.services.yura_chat import Answer

    card = Answer(text="◆ <b>Waiting for you</b>\n<i>May I?</i>", rendered=True)
    html = _visible(card)
    assert "&lt;b&gt;" not in html
    assert "<b>Waiting for you</b>" in html


def test_offline_text_is_still_escaped(runtime) -> None:
    from havoc.features.persona import _visible
    from havoc.services.yura_chat import Answer

    answer = Answer(text="a < b & c ╰(*´︶`*)╯")  # source defaults to offline
    html = _visible(answer)
    assert "&lt;" in html and "&amp;" in html


async def test_an_action_turn_arrives_as_html(runtime) -> None:
    """The whole path: tool card + her prose, rendered once, in that order."""
    from havoc.services.yura_chat import YuraChat

    assert isinstance(runtime.yura_chat, YuraChat)
    log: list = []
    runtime.actions.register(_tool("note18", "write", log, card="Saved"))
    runtime.ai = Scripted(
        '```tool\n{"tool": "note18", "args": {"what": "the bridge"}}\n```',
        "*kept it.* daddaaa ✦ it is in the book now",
    )
    answer = await runtime.yura_chat.answer(
        "remember the bridge", chat_id=100, is_owner=True
    )
    assert log == [("note18", {"what": "the bridge"})]
    assert answer.rendered is True
    assert "&lt;" not in answer.text
    assert "<b>" in answer.text and "<i>" in answer.text
    assert "```" not in answer.text


# ---------------------------------------------------------------------------
# 4. The owner is a number, and the number is set
# ---------------------------------------------------------------------------
def test_the_owner_is_known_by_id_without_a_lookup() -> None:
    from havoc.config import load_config
    from havoc.permissions import Permissions

    config, _ = load_config(env={})
    assert 7542695649 in config.owner_ids

    class Store:
        async def get_setting(self, *_a, **_k):
            return None

        async def set_setting(self, *_a, **_k):
            return None

    permissions = Permissions(config, Store())
    assert permissions.is_owner(7542695649) is True
    assert permissions.is_owner(1) is False


async def test_her_own_console_act_counts_as_the_account(runtime) -> None:
    """`.yura yes` is the account acting, so the owner check must see that.

    The bug was here: she ran the command with sender id 0 and not-outgoing, so
    the dispatcher asked "is 0 the owner?" and the answer was no.
    """
    from havoc.services.self_console import run

    runtime.permissions.set_self_id(42)
    reply = await run(runtime, "about", chat_id=100, sender_id=42)
    assert reply.ok, reply.note
    assert "restricted" not in (reply.note or "").lower()


@pytest.mark.parametrize("reply", ["plain answer ✦", "another ✦ answer"])
async def test_an_ordinary_turn_is_untouched(runtime, reply) -> None:
    """No tool, no card, no receipt — exactly the answer she gave before hands."""
    class Plain:
        model = "stub"
        available = True

        async def converse(self, messages, *, max_tokens: int = 400, temperature: float = 0.8):
            return reply

        async def converse_tools(self, messages, *, tools, max_tokens: int = 400):
            return reply, []

    runtime.ai = Plain()
    answer = await runtime.yura_chat.answer("hey", chat_id=100, is_owner=True)
    assert answer.rendered is False
    assert answer.source == "model"


async def test_the_owner_id_from_the_report_passes_the_gate(tmp_path) -> None:
    """The account he gave: 7542695649. A stranger still gets nothing."""
    from conftest import FakeTelegram, invoke, make_config

    from havoc.runtime import Runtime

    config = make_config(tmp_path, owner_ids=(7542695649,))
    runtime = await Runtime.create(config, __import__("logging").getLogger("havoc.test18"))
    runtime.telegram = FakeTelegram()
    runtime.client = runtime.telegram.client
    runtime.sender.client = runtime.client
    runtime.permissions.set_self_id(7542695649)
    runtime.health.mark_connected()
    runtime.load_features()
    try:
        assert runtime.permissions.is_owner(7542695649) is True
        sent = await invoke(runtime, ".config", sender_id=7542695649, out=False)
        assert not any("restricted" in message.text.lower() for message in sent), [
            message.text for message in sent
        ]
        assert any("command_prefix" in message.text for message in sent)

        refused = await invoke(runtime, ".config", sender_id=999, out=False)
        assert any("restricted" in message.text.lower() for message in refused)
    finally:
        await runtime.db.close()


async def test_turning_the_clock_off_needs_no_yes(runtime) -> None:
    """His first sentence, end to end through the real command tool."""
    runtime.ai = Scripted(
        '```tool\n{"tool": "command", "args": {"command": "clock", "args": "off"}}\n```',
        "خاموشش کردم دَدّا",
    )
    answer = await runtime.yura_chat.answer(
        "turn off live clock on my profile", chat_id=100, is_owner=True
    )
    assert "may i" not in answer.text.lower(), answer.text
    assert await runtime.actions.pending(100) is None
    body = answer.text.lower()
    assert "clock" in body or "ساعت" in body, answer.text


async def test_he_can_see_and_change_the_asking(runtime) -> None:
    """`.yura auto` — the switch behind "turn the permission asking off"."""
    from conftest import invoke

    seen = await invoke(runtime, ".yura auto", sender_id=42)
    assert any("on" in message.text.lower() for message in seen)

    await invoke(runtime, ".yura auto off", sender_id=42)
    assert await runtime.actions.is_auto() is False

    await invoke(runtime, ".yura auto on", sender_id=42)
    assert await runtime.actions.is_auto() is True
