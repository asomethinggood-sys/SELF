"""The AI provider: model fallback, discovery, and failure kinds.

The network is stubbed: this is about the logic that decides *which* model to
call and what to do when one is gone, not about OpenRouter's uptime.
"""

from __future__ import annotations

import pytest

from havoc.services import ai_provider as ai_module
from havoc.services.ai_provider import AIError, AIMessage, AIProvider
from havoc.services.http import HttpError


def _provider(**kwargs) -> AIProvider:
    defaults = dict(
        base_url="https://example.test/v1",
        api_key="sk-test",
        model="one:free, two:free",
        enabled=True,
    )
    defaults.update(kwargs)
    return AIProvider(**defaults)


@pytest.fixture
def calls(monkeypatch):
    """Records every chat-completions call and answers with a script."""
    made: list[dict] = []

    async def fake_post(url, body, *, timeout=30.0, headers=None):
        made.append(body)
        model = body["model"]
        behaviour = fake_post.script.get(model, "ok")
        if behaviour == "missing":
            raise HttpError("HTTP 404: no such model", kind="http_error", status=404)
        return {"choices": [{"message": {"content": f"reply from {model}"}}]}

    fake_post.script = {}
    monkeypatch.setattr(ai_module, "post_json", fake_post)
    return made, fake_post


async def test_it_falls_back_when_the_first_model_is_gone(calls) -> None:
    made, script = calls
    script.script = {"one:free": "missing"}
    provider = _provider()
    reply = await provider.converse([AIMessage("user", "hi")])
    assert reply == "reply from two:free"
    assert [body["model"] for body in made] == ["one:free", "two:free"]
    assert provider.active_model == "two:free"


async def test_a_bad_key_stops_the_chain(calls, monkeypatch) -> None:
    made, _script = calls

    async def fake_post(url, body, *, timeout=30.0, headers=None):
        made.append(body)
        raise HttpError("HTTP 401", kind="unauthorized", status=401)

    monkeypatch.setattr(ai_module, "post_json", fake_post)
    provider = _provider()
    with pytest.raises(AIError) as caught:
        await provider.converse([AIMessage("user", "hi")])
    assert caught.value.kind == "unauthorized"
    assert len(made) == 1  # no point trying the next model with the same key


async def test_auto_discovers_free_models_and_prefers_the_good_ones(monkeypatch) -> None:
    async def fake_get(url, *, params=None, timeout=15.0, headers=None):
        assert url.endswith("/models")
        return {
            "data": [
                {"id": "paid/expensive"},
                {"id": "some/obscure:free"},
                {"id": "meta-llama/llama-3.3-70b-instruct:free"},
                {"id": "google/gemma-4-31b-it:free"},
            ]
        }

    monkeypatch.setattr(ai_module, "get_json", fake_get)
    provider = _provider(model="auto")
    discovered = await provider.discover_models(limit=2)
    assert discovered[0] == "meta-llama/llama-3.3-70b-instruct:free"
    assert discovered[1] == "google/gemma-4-31b-it:free"
    assert all(model.endswith(":free") for model in discovered)


async def test_auto_replies_even_when_discovery_orders_nothing(monkeypatch, calls) -> None:
    made, _script = calls

    async def fake_get(url, *, params=None, timeout=15.0, headers=None):
        return {"data": [{"id": "only/one:free"}]}

    monkeypatch.setattr(ai_module, "get_json", fake_get)
    provider = _provider(model="auto")
    reply = await provider.converse([AIMessage("user", "hi")])
    assert reply == "reply from only/one:free"
    assert made[0]["model"] == "only/one:free"


async def test_retired_slugs_trigger_one_discovery_retry(monkeypatch, calls) -> None:
    made, script = calls
    script.script = {"one:free": "missing", "two:free": "missing"}

    async def fake_get(url, *, params=None, timeout=15.0, headers=None):
        return {"data": [{"id": "fresh/model:free"}]}

    monkeypatch.setattr(ai_module, "get_json", fake_get)
    provider = _provider()
    reply = await provider.converse([AIMessage("user", "hi")])
    assert reply == "reply from fresh/model:free"
    # The two dead slugs, then exactly one retry with the discovered model.
    assert [body["model"] for body in made] == ["one:free", "two:free", "fresh/model:free"]


async def test_a_discovery_failure_keeps_the_original_error(calls, monkeypatch) -> None:
    _made, script = calls
    script.script = {"one:free": "missing", "two:free": "missing"}

    async def fake_get(url, *, params=None, timeout=15.0, headers=None):
        raise HttpError("network error", kind="network")

    monkeypatch.setattr(ai_module, "get_json", fake_get)
    provider = _provider()
    with pytest.raises(AIError) as caught:
        await provider.converse([AIMessage("user", "hi")])
    assert caught.value.kind == "http_error"


async def test_disabled_provider_refuses_cleanly() -> None:
    provider = AIProvider(enabled=False)
    assert provider.available is False
    with pytest.raises(AIError) as caught:
        await provider.converse([AIMessage("user", "hi")])
    assert caught.value.kind == "disabled"


async def test_an_empty_conversation_is_rejected() -> None:
    provider = _provider()
    with pytest.raises(AIError) as caught:
        await provider.converse([AIMessage("system", "only rules")])
    assert caught.value.kind == "bad_response"


async def test_an_empty_reply_is_a_bad_response(calls, monkeypatch) -> None:
    made, _script = calls

    async def fake_post(url, body, *, timeout=30.0, headers=None):
        made.append(body)
        return {"choices": [{"message": {"content": "   "}}]}

    monkeypatch.setattr(ai_module, "post_json", fake_post)
    provider = _provider()
    with pytest.raises(AIError) as caught:
        await provider.converse([AIMessage("user", "hi")])
    assert caught.value.kind == "bad_response"


def test_the_client_does_not_call_itself_havoc() -> None:
    """Measured, not stylistic.

    The CDN in front of openrouter.ai answers 403 with an HTML error page for
    every User-Agent containing the word ``havoc`` — "HAVOC/2.0" and "havoc/2.0"
    alike — while "Yura/2.0", curl and python-urllib are served normally. A
    blocked request is indistinguishable from a bad key unless this is known.
    """
    from havoc.services.http import USER_AGENT

    assert "havoc" not in USER_AGENT.lower()
    assert "Yura" in USER_AGENT
    assert USER_AGENT.strip() == USER_AGENT
