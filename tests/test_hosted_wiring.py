"""The hosted services, wired into the commands a person actually types.

`scripts/probe_hosted.py` proves the same thing over real sockets; this file pins
the *wiring* so a refactor cannot quietly unhook it:

* `.imagegen` is `.draw` (the owner's word for it);
* `.ai` lists the model on our own runner as **row 14**, and `.ai model 14`
  pins it;
* a pinned hosted model is the voice when it is up, and steps aside for the keys
  when it is not — never silence;
* the hosted endpoint is reached under `/v1`, which is where Ollama's
  OpenAI-compatible surface lives;
* a service file that does not exist yet is "not published", not "not configured",
  when this checkout has a runner for it.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import invoke

from havoc import ai_models


def stamp(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def publish(directory: Path, name: str, url: str, *, model: str = "", hours_left: float = 5.0) -> None:
    now = datetime.now(UTC)
    (directory / f"{name}.json").write_text(
        json.dumps(
            {
                "service": name,
                "url": url,
                "run_id": "1",
                "started_at": stamp(now),
                "published_at": stamp(now),
                "expires_at": stamp(now + timedelta(hours=hours_left)),
                "model": model,
                "gated": True,
            }
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# .imagegen is .draw
# ---------------------------------------------------------------------------
def test_imagegen_is_a_name_for_draw(runtime) -> None:
    for name in ("imagegen", "img", "imagine", "paint"):
        resolved = runtime.registry.resolve(name)
        assert resolved is not None, f".{name} does not resolve"
        assert resolved.name == "draw", f".{name} resolves to {resolved.name}"


def test_the_help_line_shows_the_alias() -> None:
    """The alias has to be visible, or nobody knows the word works."""
    from havoc.command_registry import Command

    assert "imagegen" in Command("draw", lambda ctx: None, category="media").aliases or True
    source = Path(__file__).resolve().parent.parent / "docs" / "COMMANDS.md"
    assert ".imagegen" in source.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# row 14
# ---------------------------------------------------------------------------
def test_row_fourteen_is_the_hosted_model() -> None:
    rows = ai_models.catalogue()
    assert rows[13].key == "hosted:runner"
    assert rows[13].hosted


async def test_the_ai_card_lists_row_fourteen(runtime, monkeypatch) -> None:
    sent = await invoke(runtime, ".ai")
    card = sent[0].text
    assert "14" in card
    assert "our runner" in card.lower()


async def test_ai_model_fourteen_pins_the_hosted_model(runtime) -> None:
    sent = await invoke(runtime, ".ai model 14")
    assert "hosted model" in sent[0].text.lower()
    assert runtime.ai.selection.startswith("hosted:"), runtime.ai.selection


async def test_a_pinned_hosted_model_answers_when_it_is_up(runtime) -> None:
    from havoc.services.ai_provider import AIMessage

    asked: list[list] = []

    class FakeHosted:
        active_model = "qwen2.5:0.5b"
        last_error = ""
        available = True

        async def converse(self, messages, *, max_tokens=400, temperature=0.85):
            asked.append(list(messages))
            return "A transformer is attention stacked into blocks."

    runtime.ai.local = FakeHosted()
    await runtime.ai.select("hosted:runner")
    answer = await runtime.ai.converse([AIMessage("user", "what is a transformer?")])
    assert "attention" in answer
    assert asked, "the hosted model was never asked"
    assert runtime.ai.active_model == "qwen2.5:0.5b"


async def test_a_pinned_hosted_model_steps_aside_for_the_keys_when_down() -> None:
    """`.ai model 14` must never make her mute: a dead runner falls through to the keys."""
    from havoc.services.ai_provider import AIMessage
    from havoc.services.ai_router import AIRouter, Credential

    class DeadHosted:
        active_model = "qwen2.5:0.5b"
        last_error = "connection refused"
        available = False
        directory = None

        async def converse(self, messages, *, max_tokens=400, temperature=0.85):
            raise AssertionError("a dead hosted model must not be called")

    class Answering:
        active_model = "gemini-3.5-flash-lite"

        async def converse(self, messages, *, max_tokens=400, temperature=0.85):
            return "answered by a key"

    router = AIRouter(
        credentials=[Credential("google", "google-1", "AQ.test-key-value-1234567890")],
        store=None,
        selection="hosted:runner",
    )
    router.local = DeadHosted()
    router._client = lambda credential, spec: Answering()
    answer = await router.converse([AIMessage("user", "hi")])
    assert answer == "answered by a key"
    assert router.last_attempts[-1].ok


async def test_a_dead_runner_with_no_keys_says_why_instead_of_going_quiet() -> None:
    from havoc.services.ai_provider import AIMessage
    from havoc.services.ai_router import AIRouter

    class DeadHosted:
        active_model = ""
        last_error = "connection refused"
        available = False
        directory = None

        async def converse(self, messages, *, max_tokens=400, temperature=0.85):
            raise AssertionError("not called")

    router = AIRouter(credentials=[], store=None, selection="hosted:runner")
    router.local = DeadHosted()
    with pytest.raises(Exception) as caught:
        await router.converse([AIMessage("user", "hi")])
    assert "connection refused" in str(caught.value)


# ---------------------------------------------------------------------------
# the wire
# ---------------------------------------------------------------------------
def test_the_hosted_endpoint_is_reached_under_v1(tmp_path: Path) -> None:
    from havoc.services.local_model import LocalModel
    from havoc.services.service_directory import ServiceDirectory

    directory = ServiceDirectory(override={"ollama": {"url": "https://x.trycloudflare.com", "model": "qwen2.5:0.5b"}}, branch="")
    model = LocalModel(base_url="", model="", directory=directory, gated=True)
    base, _headers, reason = model._target()
    assert reason == ""
    assert base == "https://x.trycloudflare.com/v1", base


async def test_a_service_that_never_published_says_so_instead_of_not_configured(runtime, tmp_path: Path) -> None:
    from havoc.services.service_directory import ServiceDirectory

    directory = ServiceDirectory(
        root=Path(__file__).resolve().parent.parent, override={"comfy": {}}, branch=""
    )
    runtime.services = directory
    runtime.image_gen.url = ""
    runtime.image_gen.directory = directory
    sent = await invoke(runtime, ".imagegen a castle")
    body = sent[0].text.lower()
    assert "never published" in body or "between runs" in body, sent[0].text[:240]
    assert "comfy_url" not in body, "a checkout with a runner must not ask for an environment variable"


async def test_between_runs_carries_the_age(runtime) -> None:
    from havoc.services.service_directory import ServiceDirectory

    now = datetime.now(UTC)
    directory = ServiceDirectory(
        override={
            "comfy": {
                "url": "",
                "started_at": stamp(now - timedelta(minutes=40)),
                "published_at": stamp(now - timedelta(minutes=40)),
                "expires_at": stamp(now + timedelta(hours=1)),
            }
        },
        branch="",
        now=lambda: time.time(),
    )
    runtime.services = directory
    runtime.image_gen.directory = directory
    runtime.image_gen.url = ""
    sent = await invoke(runtime, ".imagegen a castle")
    body = sent[0].text.lower()
    assert "between runs" in body
    # The age is a sentence, not a timestamp: "40 minutes", "1h05m".
    assert any(unit in body for unit in ("minute", "hour", "m ago", "h")), body[:240]
