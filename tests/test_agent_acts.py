"""His 1.7 messages, replayed: every one of them has to *do* the thing.

The screenshots had three commands in them and three paragraphs of prose — the
model wrote its call as bare JSON, the fence-only parser ignored it, and the JSON
was sent to him with a copy button. These tests replay that exactly, with a model
that either writes bare JSON, wrong JSON, or no call at all.
"""

from __future__ import annotations

from havoc.services.tools import build_tools


class ScreenshotModel:
    """Writes what the live model wrote, then repairs once it is told how."""

    model = "stub"
    available = True

    def __init__(self, first: str, second: str = "", third: str = "باشه دددااا ✦") -> None:
        self.script = [first, second, third]
        self.round = 0
        self.calls: list = []

    async def converse(self, messages, *, max_tokens: int = 400, temperature: float = 0.8):
        self.calls.append(messages)
        return self.script[-1]

    async def converse_tools(self, messages, *, tools, max_tokens: int = 400):
        self.calls.append(messages)
        self.round += 1
        index = min(self.round - 1, len(self.script) - 1)
        return self.script[index], []


class WordsOnly:
    model = "stub"
    available = True

    async def converse(self, messages, **kwargs):
        return "باشه دددااا ✦"

    async def converse_tools(self, messages, *, tools, max_tokens: int = 400):
        return "باشه دددااا ✦", []


def _owner(runtime) -> None:
    runtime.permissions.set_self_id(7542695649)


async def test_his_exact_autoreply_line_runs_even_written_as_bare_json(runtime) -> None:
    """Round 1 is his screenshot; round 2 is the repair after the console said how."""
    _owner(runtime)
    runtime.ai = ScreenshotModel(
        first='{"command": "autoreply", "args": "add private * he\'s not here lil bro"}',
        second='{"command": "autoreply", "args": "add --type any --private => he\'s not here lil bro"}',
    )
    answer = await runtime.yura_chat.answer(
        "Set an auto reply to every private chat. If anyone messages me say he's not here lil bro",
        chat_id=100,
        is_owner=True,
    )
    rules = await runtime.store.list_autoreplies()
    assert rules, "no auto-reply was saved"
    assert rules[0]["scope"] == "private"
    assert rules[0]["response"] == "he's not here lil bro"
    assert '"command"' not in answer.text and "```" not in answer.text
    assert answer.rendered is True


async def test_a_model_that_only_talks_still_sets_the_rule(runtime) -> None:
    """"If it answers but sends no block, nothing happens" was the whole complaint."""
    _owner(runtime)
    runtime.ai = WordsOnly()
    await runtime.yura_chat.answer(
        "if anyone messages me say he's not here lil bro", chat_id=100, is_owner=True
    )
    rules = await runtime.store.list_autoreplies()
    assert rules and rules[0]["scope"] == "private"
    assert await runtime.store.feature_enabled("autoreply") is True


async def test_his_exact_name_line_changes_the_name(runtime) -> None:
    _owner(runtime)
    runtime.ai = ScreenshotModel(first='{"command": "profile", "args": "name him"}')
    answer = await runtime.yura_chat.answer("Change my profile name to him", chat_id=100, is_owner=True)
    rows = await runtime.store.profile_history(limit=1)
    assert rows and rows[0]["field"] == "first_name" and rows[0]["new_value"] == "him"
    assert '"command"' not in answer.text


async def test_one_call_can_carry_several_lines(runtime) -> None:
    """A compound request written as a script runs every line that works."""
    _owner(runtime)
    tools = {tool.name: tool for tool in build_tools(runtime)}
    from havoc.services.actions import Turn

    turn = Turn(chat_id=100, actor_id=7542695649, language="en", is_owner=True)
    result = await tools["command"].run(
        turn, {"command": "clock", "args": "on\nremind 1h drink water"}
    )
    assert result.ok, result.note
    assert await runtime.store.get_setting("clock.enabled", "unset") is True
    assert len(await runtime.store.list_reminders(status="pending")) == 1
