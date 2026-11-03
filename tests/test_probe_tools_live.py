"""The live tool probe must be proven here before it is pointed at a real model.

``scripts/probe_tools_live.py`` is the only thing in this repository that follows
a non-plain sentence all the way through: the model chooses a tool, the action
layer authorizes and runs it, and the answer the owner reads is checked for
scaffolding. It runs on a hosted runner against the real gateway, where a broken
probe costs a dispatch and part of a 50-request daily allowance to discover.

These tests drive the same ``run_turn`` with a stub model, so the record's shape
and both of its verdicts are proven for free. The stub answers with a fenced
``tool`` block rather than native ``tool_calls``: that is the route a free model
actually takes, and it exercises ``parse_calls`` on the way.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from conftest import make_config

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import probe_tools_live  # noqa: E402

ASK = probe_tools_live.ASKS["en"]

#: The shape the catalogue asks for: ``remind`` requires ``when`` and ``what``.
TOOL_CALL = '```tool\n{"tool": "remind", "args": {"when": "30m", "what": "call mom"}}\n```'

ANSWER = "Done — I will tell you in half an hour."


class StubModel:
    """A model that asks for a tool, then reports it in plain words."""

    available = True
    active_model = "stub-with-hands"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.requests = 0

    async def converse(self, messages, *, max_tokens: int = 400, **kwargs) -> str:
        self.requests += 1
        return self.replies.pop(0) if self.replies else ANSWER


@pytest.fixture
async def probe_runtime(tmp_path, quiet_logger):
    """The same runtime the probe builds on the runner: real, and unable to send."""
    runtime = await probe_tools_live.build_runtime(make_config(tmp_path), quiet_logger)
    try:
        yield runtime
    finally:
        await runtime.db.close()


async def test_a_tool_the_model_chooses_actually_runs(probe_runtime) -> None:
    """Selection is not execution: the reminder has to exist afterwards."""
    model = StubModel(TOOL_CALL)
    record = await probe_tools_live.run_turn(probe_runtime, model, ASK, "en")

    assert record["ok"] is True, record["failure"]
    assert record["did"] == ["remind"], "the tool was chosen but did not run"
    assert record["outcomes"][0]["tool"] == "remind"
    assert record["outcomes"][0]["status"] == "done"
    assert record["asked_first"] is False, "a reminder must not come back as a question"
    assert record["leaked_tool_code"] is False
    assert record["tools_offered"] > 0, "the catalogue was empty, so nothing could be chosen"

    # The strongest evidence available without Telegram: a row that was not there
    # before the model answered.
    rows = await probe_runtime.db.fetch_all("SELECT text FROM reminders")
    assert rows, "the tool reported done but wrote nothing"
    assert "call mom" in rows[0]["text"]

    # A tool call is two requests: the ask, and the ask again with the result.
    assert model.requests == 2


async def test_a_model_that_only_talks_about_the_tool_is_not_ok(probe_runtime) -> None:
    """The failure this probe exists to catch: she describes the hand she never used."""
    record = await probe_tools_live.run_turn(
        probe_runtime, StubModel("I will remind you in 30 minutes!"), ASK, "en"
    )

    assert record["ok"] is False
    assert record["did"] == []
    assert record["failure"], "a failure without a reason is not a report"
    assert not await probe_runtime.db.fetch_all("SELECT text FROM reminders")


async def test_scaffolding_in_the_answer_fails_even_when_the_tool_ran(probe_runtime) -> None:
    """The owner reads an answer, not a transcript of the call."""
    record = await probe_tools_live.run_turn(
        probe_runtime, StubModel(TOOL_CALL, 'I used ```tool``` to remind you.'), ASK, "en"
    )

    assert record["did"] == ["remind"], "the tool still ran"
    assert record["leaked_tool_code"] is True
    assert record["ok"] is False, "a reply that leaks the call is not a green probe"


async def test_the_probe_says_so_when_no_provider_is_configured(tmp_path, quiet_logger) -> None:
    """No key is a fact to report, not an exception to swallow — and no request is spent."""
    record = await probe_tools_live.probe(
        ASK, "en", config=make_config(tmp_path), logger=quiet_logger
    )

    assert record["ok"] is False
    assert record["failure"] == "AI is not configured"
    assert record["did"] == []
    assert record["kind"] == "tool-probe"


def test_the_record_is_readable_and_json_safe() -> None:
    """It is committed to the branch, so it has to survive both readers."""
    record = {
        "provider": "router",
        "keys": 5,
        "model": "gemini-3.5-flash-lite",
        "ask": ASK,
        "language": "en",
        "tools_offered": 17,
        "outcomes": [{"tool": "remind", "status": "done", "note": "#1 at 10:30"}],
        "did": ["remind"],
        "asked_first": False,
        "said": ANSWER,
        "leaked_tool_code": False,
        "ok": True,
        "failure": "",
    }
    text = probe_tools_live.readable(record)
    # The line a human reads first when wondering whether her hands moved.
    ran_line = next(line for line in text.splitlines() if line.startswith("ran"))
    assert ran_line.strip().endswith("remind"), ran_line
    assert "gemini-3.5-flash-lite" in text
    assert ANSWER in text
    assert json.loads(json.dumps(record, ensure_ascii=False))["did"] == ["remind"]
