"""Every shape a model writes a call in — and every one of them does the thing.

The owner's screenshots from 1.7: three copies of
``{"command": "autoreply", "args": "add private * he's not here lil bro"}`` arrived
as *her message*, with a copy button in Telegram, and nothing ran. The fence was
expected and the model did not use one. These tests exist so no shape it writes
can be a message instead of an action.
"""

from __future__ import annotations

import json

import pytest

from havoc.services.actions import calls_from_native, parse_calls, scrub_tool_text

SCHEMA = {
    "command": ("command", "args"),
    "remind": ("when", "what"),
    "recall": ("about",),
    "search": ("about",),
    "translate": ("text", "to"),
    "remember": ("what",),
    "forget": ("what",),
}


# ---------------------------------------------------------------------------
# The shapes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected_name", "expected_args"),
    [
        # his screenshots, verbatim
        (
            '{"command": "autoreply", "args": "add private * he\'s not here lil bro"}',
            "command",
            {"command": "autoreply", "args": "add private * he's not here lil bro"},
        ),
        (
            '{"command": "profile", "args": "name him"}',
            "command",
            {"command": "profile", "args": "name him"},
        ),
        # the documented shape
        ('{"tool": "remind", "args": {"when": "30m", "what": "call mom"}}', "remind", {"when": "30m", "what": "call mom"}),
        # OpenAI's spelling
        ('{"name": "recall", "arguments": "{\\"about\\": \\"sara\\"}"}', "recall", {"about": "sara"}),
        # a command named where a tool belongs
        ('{"tool": "fal", "args": "page 166"}', "command", {"command": "fal", "args": "page 166"}),
        # the whole line in the command slot
        ('{"command": "clock off"}', "command", {"command": "clock", "args": "off"}),
        # one property, written bare
        ('{"tool": "search", "args": "sara"}', "search", {"about": "sara"}),
    ],
)
def test_a_call_is_read_whatever_shape_it_is_written_in(text, expected_name, expected_args) -> None:
    calls, clean = parse_calls(text, schema=SCHEMA)
    assert [(call.name, dict(call.args)) for call in calls] == [(expected_name, expected_args)]
    assert clean == ""


def test_a_bare_call_inside_prose_leaves_only_the_prose() -> None:
    text = 'hi dadda\n\n{"command": "profile", "args": "name him"}\n\nsee you'
    calls, clean = parse_calls(text, schema=SCHEMA)
    assert [call.name for call in calls] == ["command"]
    assert clean == "hi dadda\n\nsee you"


def test_the_same_call_three_times_is_one_call() -> None:
    """He received it three times because the model was unsure. Running it three
    times is worse than running it once."""
    body = '{"command": "autoreply", "args": "add hi => there"}'
    calls, _ = parse_calls("\n\n".join([body, body, body]), schema=SCHEMA)
    assert len(calls) == 1


def test_a_fence_still_works() -> None:
    calls, clean = parse_calls(
        'ok\n```tool\n{"command": "fal", "args": "page 166"}\n```', schema=SCHEMA
    )
    assert calls and calls[0].args["command"] == "fal"
    assert clean == "ok"


def test_two_objects_without_a_fence_are_two_calls() -> None:
    text = '{"command": "clock", "args": "off"}\n{"tool": "recall", "args": {"about": "sara"}}'
    calls, _ = parse_calls(text, schema=SCHEMA)
    assert [call.name for call in calls] == ["command", "recall"]


def test_native_calls_are_unaffected_by_the_schema() -> None:
    payload = [{"function": {"name": "remind", "arguments": json.dumps({"when": "1h", "what": "x"})}}]
    calls = calls_from_native(payload, SCHEMA)
    assert calls[0].name == "remind" and calls[0].args["what"] == "x"


# ---------------------------------------------------------------------------
# Nothing machine-shaped reaches a person
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        '{"command": "autoreply", "args": "add private * he\'s not here lil bro"}',
        'then {"tool": "command", "args": {"command": "fal"}} and that',
        '{"name": "recall", "arguments": "{\\"about\\": \\"sara\\"}"}',
        'ok [[do: {"tool": "remind", "args": {"when": "1h", "what": "x"}}]]',
        '```tool\n{"command": "clock", "args": "off"\n```',
    ],
)
def test_no_machinery_survives_the_scrubber(text: str) -> None:
    cleaned = scrub_tool_text(text)
    assert '"tool"' not in cleaned
    assert '"command"' not in cleaned
    assert "```" not in cleaned
    assert "[[do" not in cleaned


def test_a_real_code_block_and_real_prose_survive() -> None:
    text = "here:\n```python\nprint(1)\n```"
    assert scrub_tool_text(text) == text
    prose = 'he said {"hello": "world"} and left'
    assert scrub_tool_text(prose) == prose
