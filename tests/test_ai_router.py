"""The rotating router: what happens when a key is spent, wrong, or unreachable.

Every test here stubs the network at the `AIProvider` boundary — the level the
router itself calls — so the assertions are about the rotation logic and not
about HTTP.
"""

from __future__ import annotations

import base64
import json
from datetime import timedelta
from pathlib import Path

import pytest

from havoc import ai_models
from havoc.services import ai_provider, ai_router
from havoc.services.ai_provider import AIError, AIMessage, AIProvider
from havoc.services.ai_router import AIRouter, Credential, KeyHealth, read_key_file
from havoc.utils.time import utc_now


class FakeClient:
    """Stands in for `AIProvider`: replies, fails, or fails a specific way."""

    def __init__(self, credential, spec, behaviour):
        self.api_key = credential.key
        self.active_model = spec.api_id
        self.model = spec.api_id
        self.base_url = credential.base_url
        self.spec = spec
        self.behaviour = behaviour
        self.calls: list[tuple[str, str]] = []
        # The real class is callable this way too: `probe` in the router's
        # diagnostics uses the same boundary.
        self.available = True

    async def converse(self, messages, *, max_tokens: int = 400, temperature: float = 0.85) -> str:
        self.calls.append((self.api_key, self.spec.api_id))
        outcome = self.behaviour(self.api_key, self.spec.api_id)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def patched_client(monkeypatch):
    """Patch `_client` so each key/model pair answers according to `behaviour`."""

    def install(behaviour):
        def _client(self, credential: Credential, spec: ai_models.ModelSpec):
            return FakeClient(credential, spec, behaviour)

        monkeypatch.setattr(AIRouter, "_client", _client)

    return install


def _creds(*pairs: tuple[str, str]) -> list[Credential]:
    return [Credential(provider, f"{provider}-{index}", key) for index, (provider, key) in enumerate(pairs, 1)]


async def _router(tmp_path: Path, credentials: list[Credential], **kwargs) -> AIRouter:
    router = AIRouter(credentials, timeout=1.0, **kwargs)
    await router.refresh()
    return router


def test_read_key_file_accepts_base64_and_refuses_garbage(tmp_path: Path) -> None:
    path = tmp_path / "keys.json"
    path.write_text(
        json.dumps(
            {
                "keys": [
                    {"provider": "google", "label": "gg-1", "key_b64": base64.b64encode(b"AQ.abc").decode()},
                    {"provider": "openrouter", "label": "or-1", "key": "sk-or-v1-x"},
                    {"provider": "google", "label": "empty"},
                    "not-a-dict",
                ]
            }
        ),
        encoding="utf-8",
    )
    found = read_key_file(path)
    assert [(entry.label, entry.key) for entry in found] == [("gg-1", "AQ.abc"), ("or-1", "sk-or-v1-x")]
    bad = tmp_path / "broken.json"
    bad.write_text("{nope", encoding="utf-8")
    assert read_key_file(bad) == []
    assert read_key_file(tmp_path / "missing.json") == []


def test_a_key_hint_never_shows_the_whole_key() -> None:
    credential = Credential("google", "gg-1", "AQ.Ab8RN6KsomethinglongmzNQ")
    assert credential.hint.startswith("AQ.Ab8RN")
    assert credential.hint.endswith("mzNQ")
    assert "somethinglong" not in credential.hint
    assert len(credential.id) == 12 and credential.key not in credential.id


async def test_the_first_working_key_answers_and_is_counted(patched_client) -> None:
    patched_client(lambda key, model: f"hello from {key}")
    router = await _router(Path("."), _creds(("google", "AQ.one")))
    assert await router.converse([AIMessage("user", "hi")]) == "hello from AQ.one"
    assert router.active_label == "google-1"
    spec = ai_models.find("gemini-3.5-flash-lite")
    assert router.used(router.credentials[0], spec) == 1
    health = router.health_of(router.credentials[0])
    assert health.successes == 1 and not health.disabled


async def test_a_spent_key_steps_aside_and_the_next_one_answers(patched_client) -> None:
    """429 with a daily limit: that key/model pair is done for today, not retried."""
    calls: list[str] = []

    def behaviour(key: str, model: str) -> str:
        calls.append(key)
        if key == "AQ.spent":
            raise AIError("429 free-models-per-day", kind="rate_limited")
        return "second key answered"

    patched_client(behaviour)
    router = await _router(Path("."), _creds(("google", "AQ.spent"), ("google", "AQ.fresh")))
    assert await router.converse([AIMessage("user", "hi")]) == "second key answered"
    assert calls == ["AQ.spent", "AQ.fresh"]
    spent = router.credentials[0]
    burned = ai_models.find("gemini-3.5-flash-lite")
    assert router.used(spent, burned) == 500
    # Google counts per model, so that pair is gone while the key's other models
    # stay on the table.
    assert (spent, burned) not in {(credential, spec) for credential, spec in router.plan()}
    # The second call does not even try it.
    calls.clear()
    assert await router.converse([AIMessage("user", "again")]) == "second key answered"
    assert calls == ["AQ.fresh"]


async def test_a_wrong_key_is_disabled_rather_than_retried(patched_client) -> None:
    def behaviour(key: str, model: str) -> str:
        if key == "AQ.wrong":
            raise AIError("401 unauthorized", kind="unauthorized")
        return "ok"

    patched_client(behaviour)
    router = await _router(Path("."), _creds(("google", "AQ.wrong"), ("google", "AQ.right")))
    assert await router.converse([AIMessage("user", "hi")]) == "ok"
    assert router.health_of(router.credentials[0]).disabled is True
    assert router.credentials[0] not in {credential for credential, _ in router.plan()}


async def test_a_rate_limit_without_a_day_word_cools_down_for_ninety_seconds(patched_client) -> None:
    def behaviour(key: str, model: str) -> str:
        if key == "AQ.busy":
            raise AIError("429 slow down", kind="rate_limited")
        return "ok"

    patched_client(behaviour)
    router = await _router(Path("."), _creds(("google", "AQ.busy"), ("google", "AQ.calm")))
    await router.converse([AIMessage("user", "hi")])
    health = router.health_of(router.credentials[0])
    assert health.cooling(utc_now()) is True
    assert not health.disabled
    # Not used up: the budget is untouched, only time is being waited out.
    assert router.used(router.credentials[0], ai_models.find("gemini-3.5-flash-lite")) == 0


async def test_a_network_error_cools_briefly_and_does_not_disable(patched_client) -> None:
    def behaviour(key: str, model: str) -> str:
        if key == "AQ.down":
            raise AIError("timeout", kind="timeout")
        return "ok"

    patched_client(behaviour)
    router = await _router(Path("."), _creds(("google", "AQ.down"), ("google", "AQ.up")))
    assert await router.converse([AIMessage("user", "hi")]) == "ok"
    down = router.health_of(router.credentials[0])
    assert down.disabled is False and down.cooling(utc_now()) is True


async def test_when_every_key_fails_the_last_error_is_raised(patched_client) -> None:
    patched_client(lambda key, model: AIError("401 nope", kind="unauthorized"))
    router = await _router(Path("."), _creds(("google", "AQ.a"), ("google", "AQ.b")))
    with pytest.raises(AIError) as caught:
        await router.converse([AIMessage("user", "hi")])
    assert caught.value.kind == "unauthorized"
    assert router.failures == 1
    assert router.available is False
    assert router.last_attempts, "the failures must be reported"
    assert not any(attempt.ok for attempt in router.last_attempts)
    assert len(router.last_attempts) <= ai_router.MAX_ATTEMPTS
    assert {attempt.label for attempt in router.last_attempts} == {"google-1", "google-2"}


async def test_no_keys_at_all_raises_disabled() -> None:
    router = await _router(Path("."), [])
    assert router.available is False
    with pytest.raises(AIError) as caught:
        await router.converse([AIMessage("user", "hi")])
    assert caught.value.kind == "disabled"


async def test_a_pinned_model_is_the_only_one_tried(patched_client) -> None:
    patched_client(lambda key, model: f"answered by {model}")
    router = await _router(Path("."), _creds(("google", "AQ.one")))
    chosen = await router.select("gemma-4-31b-it")
    assert chosen is not None and chosen.api_id == "gemma-4-31b-it"
    assert await router.converse([AIMessage("user", "hi")]) == "answered by gemma-4-31b-it"
    assert {spec.key for _, spec in router.plan()} == {"google:gemma-4-31b-it"}
    # "auto" hands the choice back.
    assert await router.select("auto") is None
    assert len(router.plan()) > 1
    assert await router.select("gemini-2.5-pro") is None  # listed, not free
    assert router.selection == "auto"


async def test_an_unknown_model_is_refused_without_changing_the_selection() -> None:
    router = await _router(Path("."), _creds(("google", "AQ.one")))
    assert await router.select("gpt-9-imaginary") is None
    assert router.selection == "auto"


async def test_budgets_are_persisted_and_survive_a_restart(patched_client, tmp_path: Path) -> None:
    from havoc.database import Database
    from havoc.migrations import apply_migrations
    from havoc.services.storage import Store

    db = Database(tmp_path / "budget.sqlite3")
    await db.connect()
    await apply_migrations(db)
    store = Store(db)
    try:
        patched_client(lambda key, model: "ok")
        spec = ai_models.find("gemini-3.8-flash")
        assert spec.rpd == 20
        router = AIRouter(_creds(("google", "AQ.daily")), store=store, timeout=1.0)
        await router.refresh()
        # Burn the day's allowance on this model.
        router.usage[f"{router.credentials[0].id}|{spec.key}"] = {"day": AIRouter._today(), "count": 20}
        await router._save_usage()

        again = AIRouter(_creds(("google", "AQ.daily")), store=store, timeout=1.0)
        await again.refresh()
        assert again.used(again.credentials[0], spec) == 20
        assert again.remaining(again.credentials[0], spec) == 0
        assert spec not in {model for _, model in again.plan()}
        # A different model on the same key is still available: budgets are per model.
        assert ai_models.find("gemini-3.5-flash-lite") in {model for _, model in again.plan()}
    finally:
        await db.close()


async def test_yesterdays_counters_are_forgotten(tmp_path: Path) -> None:
    from havoc.database import Database
    from havoc.migrations import apply_migrations
    from havoc.services.storage import Store

    db = Database(tmp_path / "prune.sqlite3")
    await db.connect()
    await apply_migrations(db)
    store = Store(db)
    try:
        credential = _creds(("google", "AQ.old"))[0]
        spec = ai_models.find("gemini-3.8-flash")
        yesterday = (utc_now() - timedelta(days=1)).strftime("%Y-%m-%d")
        await store.set_setting(
            ai_router.USAGE_SETTING, {f"{credential.id}|{spec.key}": {"day": yesterday, "count": 20}}
        )
        router = AIRouter([credential], store=store, timeout=1.0)
        await router.refresh()
        assert router.used(credential, spec) == 0
    finally:
        await db.close()


async def test_add_and_retire_a_key(tmp_path: Path) -> None:
    from havoc.database import Database
    from havoc.migrations import apply_migrations
    from havoc.services.storage import Store

    db = Database(tmp_path / "keys.sqlite3")
    await db.connect()
    await apply_migrations(db)
    store = Store(db)
    try:
        router = AIRouter([], store=store, timeout=1.0)
        await router.refresh()
        added = await router.add_key("auto", "AQ.Ab8RN6Knewkey")
        assert added.provider == "google" and added.label == "google-1"
        assert router.available is True
        # Stored in the database, never in the repository.
        stored = await store.get_setting(ai_router.KEYS_SETTING, [])
        assert stored == [{"provider": "google", "label": "google-1", "key": "AQ.Ab8RN6Knewkey"}]

        # An sk-or- key is recognised as OpenRouter without being told.
        other = await router.add_key("auto", "sk-or-v1-second")
        assert other.provider == "openrouter" and other.label == "openrouter-1"

        assert await router.retire("google-1") is True
        assert await router.retire("google-1") is False
        assert [credential.label for credential in router.credentials] == ["openrouter-1"]
        assert await store.get_setting(ai_router.KEYS_SETTING, []) == [
            {"provider": "openrouter", "label": "openrouter-1", "key": "sk-or-v1-second"}
        ]
    finally:
        await db.close()


async def test_reset_health_clears_cooldowns_and_budgets() -> None:
    router = await _router(Path("."), _creds(("google", "AQ.one")))
    credential = router.credentials[0]
    router.health[credential.id] = KeyHealth(disabled=True, cooldown_until=(utc_now() + timedelta(hours=1)).isoformat())
    spec = ai_models.find("gemini-3.8-flash")
    router.usage[f"{credential.id}|{spec.key}"] = {"day": AIRouter._today(), "count": 20}
    assert router.available is False
    await router.reset_health()
    assert router.available is True
    assert router.used(credential, spec) == 0
    assert router.plan()


async def test_status_reports_one_row_per_key_with_budgets_and_marks() -> None:
    router = await _router(Path("."), _creds(("google", "AQ.one"), ("openrouter", "sk-or-v1-two")))
    router.health[router.credentials[0].id] = KeyHealth(disabled=True, error="unauthorized: 401")
    rows = router.status()
    assert [row["provider"] for row in rows] == ["google", "openrouter"]
    assert rows[0]["disabled"] is True and rows[0]["error"].startswith("unauthorized")
    assert rows[1]["disabled"] is False
    assert rows[0]["budget"] > 15_000  # every free google model added up
    assert rows[1]["budget"] == 50
    assert all("AQ.one" != row["hint"] for row in rows)


async def test_probe_goes_through_the_rotation(patched_client) -> None:
    patched_client(lambda key, model: "ok")
    router = await _router(Path("."), _creds(("google", "AQ.one")))
    worked, detail = await router.probe()
    assert worked is True and detail == "ok"

    patched_client(lambda key, model: AIError("401 bad", kind="unauthorized"))
    failed_router = await _router(Path("."), _creds(("google", "AQ.two")))
    worked, detail = await failed_router.probe()
    assert worked is False and "unauthorized" in detail


async def test_load_reads_the_file_the_environment_and_the_database(tmp_path: Path) -> None:
    from havoc.config import Config
    from havoc.database import Database
    from havoc.migrations import apply_migrations
    from havoc.services.storage import Store

    key_file = tmp_path / "ai-keys.json"
    key_file.write_text(
        json.dumps(
            {
                "keys": [
                    {"provider": "google", "label": "gg-1", "key_b64": base64.b64encode(b"AQ.fromfile").decode()},
                    {"provider": "openrouter", "label": "or-1", "key_b64": base64.b64encode(b"sk-or-v1-file").decode()},
                ]
            }
        ),
        encoding="utf-8",
    )
    db = Database(tmp_path / "load.sqlite3")
    await db.connect()
    await apply_migrations(db)
    store = Store(db)
    try:
        await store.set_setting("ai.api_key", "sk-or-v1-chat")
        await store.set_setting(ai_router.MODEL_SETTING, "google:gemma-4-31b-it")
        config = Config(ai_provider="openai_compatible", ai_api_key="AQ.fromenv,sk-or-v1-env", ai_keys_path=str(key_file))
        router = await AIRouter.load(config, store)
        labels = [credential.label for credential in router.credentials]
        assert labels == ["gg-1", "or-1", "env-1", "env-2", "chat-key"]
        assert router.selection == "google:gemma-4-31b-it"
        assert router.available is True
        # The environment parse got the provider right from the key's shape.
        assert [c.provider for c in router.credentials[2:4]] == ["google", "openrouter"]
    finally:
        await db.close()


async def test_load_with_a_missing_file_is_survivable(tmp_path: Path) -> None:
    from havoc.config import Config

    config = Config(ai_provider="openai_compatible", ai_keys_path=str(tmp_path / "nope.json"))
    router = await AIRouter.load(config, None)
    assert router.credentials == []
    assert router.available is False
    assert router.describe()["provider"] == "none"


async def test_duck_typing_matches_the_single_provider_interface() -> None:
    """Anything that used `AIProvider` must keep working with the router."""
    router = await _router(Path("."), [])
    for name in ("available", "complete", "converse", "probe", "describe", "model"):
        assert hasattr(router, name), name
    assert hasattr(AIProvider, "converse") and hasattr(ai_provider, "AIError")


async def test_complete_builds_a_system_message_when_asked(monkeypatch) -> None:
    seen: list[list[AIMessage]] = []

    class Recorder(FakeClient):
        async def converse(self, messages, *, max_tokens: int = 400, temperature: float = 0.85) -> str:
            seen.append(list(messages))
            return "done"

    monkeypatch.setattr(
        AIRouter,
        "_client",
        lambda self, credential, spec: Recorder(credential, spec, lambda key, model: "done"),
    )
    router = await _router(Path("."), _creds(("google", "AQ.one")))
    assert await router.complete("hello", system="be brief", max_tokens=10) == "done"
    assert [message.role for message in seen[0]] == ["system", "user"]
    assert seen[0][0].content == "be brief"


async def test_an_openrouter_daily_limit_spends_the_whole_account(patched_client) -> None:
    """The gateway's allowance is per account, so one refusal takes the key out."""

    def behaviour(key: str, model: str) -> str:
        if key == "sk-or-v1-gateway":
            raise AIError("429 free-models-per-day", kind="rate_limited")
        return "the spare key answered"

    patched_client(behaviour)
    router = await _router(
        Path("."), _creds(("openrouter", "sk-or-v1-gateway"), ("openrouter", "sk-or-v1-spare"))
    )
    assert await router.converse([AIMessage("user", "hi")]) == "the spare key answered"
    gateway = router.credentials[0]
    gateway_specs = [spec for spec in ai_models.catalogue() if spec.provider == "openrouter"]
    assert all(router.used(gateway, spec) == spec.rpd for spec in gateway_specs)
    assert gateway not in {credential for credential, _ in router.plan()}


async def test_the_token_budget_reaches_the_provider_unchanged(monkeypatch) -> None:
    """A program needs a program's worth of tokens; the router must not shrink it."""
    seen: list[int] = []

    class Recorder(FakeClient):
        async def converse(self, messages, *, max_tokens: int = 400, temperature: float = 0.85) -> str:
            seen.append(max_tokens)
            return "ok"

    monkeypatch.setattr(
        AIRouter, "_client", lambda self, credential, spec: Recorder(credential, spec, lambda k, m: "ok")
    )
    router = await _router(Path("."), _creds(("google", "AQ.one")))
    await router.converse([AIMessage("user", "write a pygame snake")], max_tokens=4000)
    assert seen == [4000]
