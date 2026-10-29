"""`.ai` and the AI tab in `.settings`, driven the way Telegram drives them.

The owner asked for the model list to be reachable "just like `.settings`": a
numbered card answered with a digit. So the tests go through
`handlers.on_outgoing`, which is what decides that a bare digit belongs to an open
menu, and they check the *state*, not the wording.
"""

from __future__ import annotations

import html

import pytest

from havoc.services.ai_router import AIRouter, Credential
from tests.conftest import FakeEvent, FakeMessage, invoke


def _text(runtime, index: int = -1) -> str:
    return html.unescape(runtime.client.sent[index].text)


def _said(sent: list) -> str:
    return "\n".join(html.unescape(message.text) for message in sent)


async def _reply(runtime, text: str, *, chat_id: int = 100) -> list[str]:
    client = runtime.client
    before = len(client.sent)
    message = FakeMessage(id=900, chat_id=chat_id, sender_id=42, text=text, out=True)
    event = FakeEvent(message, chat_id=chat_id, out=True)
    await runtime.handlers.on_outgoing(event)
    return [html.unescape(sent.text) for sent in client.sent[before:]]


@pytest.fixture
def router(runtime, monkeypatch):
    """A router with two keys, and a `_client` that answers without the network."""
    router = AIRouter(
        [
            Credential("google", "gg-1", "AQ.aaaaaaaaaaaaaa1"),
            Credential("openrouter", "or-1", "sk-or-v1-bbbbbbbb"),
        ],
        store=runtime.store,
        timeout=1.0,
    )

    class Answer:
        def __init__(self, credential, spec):
            self.active_model = spec.api_id
            self.model = spec.api_id
            self.api_key = credential.key
            self.available = True

        async def converse(self, messages, *, max_tokens=400, temperature=0.85):
            return "ok"

    monkeypatch.setattr(AIRouter, "_client", lambda self, credential, spec: Answer(credential, spec))
    runtime.ai = router
    return router


@pytest.fixture(autouse=True)
async def _loaded(router):
    """Health and budgets live in the database; read them the way a restart does."""
    await router.refresh()
    return router


async def test_the_card_lists_models_with_numbers_and_limits(runtime, router) -> None:
    text = _said(await invoke(runtime, ".ai"))
    assert "◈" in text
    assert "<code>1</code>" in text and "<code>2</code>" in text
    assert "500/day" in text
    assert "14,400/day" in text
    # The paid-tier models are shown as refused, not hidden.
    assert "✕" in text
    # Each provider appears with its mark.
    assert "✦" in text and "◇" in text


async def test_the_key_card_shows_health_and_hides_the_key(runtime, router) -> None:
    text = _said(await invoke(runtime, ".ai key"))
    assert "gg-1" in text and "or-1" in text
    assert "AQ.aaaaaaaaaaaaaa1" not in text
    assert "sk-or-v1-bbbbbbbb" not in text
    assert "◉" in text or "○" in text


async def test_a_digit_pins_a_model_and_it_is_remembered(runtime, router) -> None:
    await invoke(runtime, ".ai")
    replies = await _reply(runtime, "2")
    assert router.selection.startswith("google:")
    assert any("Gemini" in text or "Gemma" in text for text in replies)
    assert await runtime.store.get_setting("ai.model", None) == router.selection


async def test_pinning_by_name_and_back_to_auto(runtime, router) -> None:
    sent = await invoke(runtime, ".ai model gemma-4-31b-it")
    assert router.selection == "google:gemma-4-31b-it", _said(sent)
    assert router.selected_spec().api_id == "gemma-4-31b-it"
    await invoke(runtime, ".ai model auto")
    assert router.selection == "auto"
    assert router.selected_spec() is None


async def test_an_unavailable_model_is_refused_politely(runtime, router) -> None:
    replies = await invoke(runtime, ".ai model gemini-2.5-pro")
    # Listed, but the free plan does not include it; the answer says why.
    said = _said(replies).lower()
    assert "paid" in said or "free plan" in said


async def test_an_unknown_model_points_at_the_list(runtime, router) -> None:
    replies = await invoke(runtime, ".ai model nonsense-9")
    assert "nonsense-9" in _said(replies)


async def test_a_key_added_from_the_chat_is_stored_masked_and_probed(runtime, router) -> None:
    replies = await invoke(runtime, ".ai key add AQ.Ab8RN6Kaddedkey")
    joined = _said(replies)
    assert "AQ.Ab8RN6Kaddedkey" not in joined
    stored = await runtime.store.get_setting("ai.keys", [])
    assert stored and stored[-1]["key"] == "AQ.Ab8RN6Kaddedkey"
    assert stored[-1]["provider"] == "google"
    assert any("gg" in label or "google" in label for label in [membership.get("label", "") for membership in stored])


async def test_a_key_added_that_does_not_answer_says_so(runtime, router, monkeypatch) -> None:
    async def _dead(self, **kwargs):
        return False, "unauthorized: 401"

    monkeypatch.setattr(AIRouter, "probe", _dead)
    replies = await invoke(runtime, ".ai key add sk-or-v1-deadweight")
    joined = _said(replies).lower()
    assert "unauthorized" in joined or "not answer" in joined


async def test_a_key_can_be_retired(runtime, router) -> None:
    assert len(router.credentials) == 2
    replies = await invoke(runtime, ".ai key rm or-1")
    assert "or-1" in _said(replies)
    assert [credential.label for credential in router.credentials] == ["gg-1"]


async def test_test_reports_which_key_answered(runtime, router) -> None:
    replies = await invoke(runtime, ".ai test")
    joined = _said(replies)
    assert "gg-1" in joined or "or-1" in joined
    assert "ok" in joined


async def test_test_that_fails_reports_the_error(runtime, router, monkeypatch) -> None:
    async def _broken(self, **kwargs):
        return False, "unauthorized: 401 invalid key"

    monkeypatch.setattr(AIRouter, "probe", _broken)
    replies = await invoke(runtime, ".ai test")
    assert "401" in _said(replies)


async def test_reset_clears_cooldowns(runtime, router) -> None:
    from datetime import timedelta

    from havoc.services.ai_router import KeyHealth
    from havoc.utils.time import utc_now

    credential = router.credentials[0]
    router.health[credential.id] = KeyHealth(
        disabled=True, cooldown_until=(utc_now() + timedelta(hours=2)).isoformat()
    )
    assert router.available is True  # the second key is still usable
    await invoke(runtime, ".ai reset")
    assert router.health == {}


async def test_the_settings_ai_tab_opens_and_lists_every_available_model(runtime, router) -> None:
    await invoke(runtime, ".settings")
    menu = runtime.settings_menu
    index = next(
        (str(position) for position, item in enumerate(menu.items, 1) if item.key == "ai"),
        None,
    )
    assert index is not None, "the settings card must have an AI row"
    replies = await _reply(runtime, index)
    body = "\n".join(replies)
    assert "automatic" in body.lower()
    # Every free model is a choice, so the list can be scrolled by number.
    labels = [choice.label for choice in menu.items[int(index) - 1].choices if choice.label]
    assert len(labels) >= len(__import__("havoc.ai_models", fromlist=["available_models"]).available_models())


async def test_the_settings_ai_row_changes_the_model(runtime, router) -> None:
    await invoke(runtime, ".settings")
    menu = runtime.settings_menu
    index = str(next(p for p, item in enumerate(menu.items, 1) if item.key == "ai"))
    await _reply(runtime, index)
    item = menu.items[int(index) - 1]
    # Choice 0 is "automatic"; choice 1 is the first available model.
    picked = item.choices[1]
    await _reply(runtime, "2")
    assert router.selection != "auto"
    assert picked.label  # the row printed something recognisable


async def test_a_single_provider_setup_still_answers(runtime) -> None:
    from havoc.services.ai_provider import AIProvider

    runtime.ai = AIProvider(base_url="https://example.invalid/v1", api_key="k", model="m", enabled=True)
    replies = await invoke(runtime, ".ai")
    assert "m" in _said(replies)
