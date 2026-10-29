"""Profile features: presets, scheduling, history and safeguards."""

from __future__ import annotations

import json
from datetime import timedelta

from conftest import invoke

from havoc.utils.time import utc_now


async def test_preset_create_and_apply(runtime) -> None:
    await invoke(runtime, ".preset create work Ali Online")
    await invoke(runtime, ".preset apply work --yes")
    history = await runtime.store.profile_history()
    assert any(row["reason"] == "preset:work" for row in history)


async def test_preset_apply_needs_confirmation(runtime) -> None:
    await invoke(runtime, ".preset create work Ali")
    before = len(await runtime.store.profile_history())
    sent = await invoke(runtime, ".preset apply work")
    assert any("--yes" in message.text or "confirm" in message.text.lower() for message in sent)
    assert len(await runtime.store.profile_history()) == before


async def test_preset_preview(runtime) -> None:
    await invoke(runtime, ".preset create night --name Ali --bio Sleeping")
    await invoke(runtime, ".preset preview night")
    text = "\n".join(m.text for m in runtime.client.sent)
    assert "night" in text and "Sleeping" in text


async def test_preset_delete(runtime) -> None:
    await invoke(runtime, ".preset create temp Ali")
    await invoke(runtime, ".preset delete temp")
    assert await runtime.store.get_profile_preset("temp") is None


async def test_schedule_preset_creates_job(runtime) -> None:
    await invoke(runtime, ".preset create work Ali")
    await invoke(runtime, ".preset schedule work 2h")
    rows = await runtime.store.db.fetch_all("SELECT * FROM jobs WHERE kind = 'profile.preset'")
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload["preset"] == "work"
    assert await runtime.store.feature_enabled("profile_scheduler") is True


async def test_schedule_preset_lists_and_cancels(runtime) -> None:
    await invoke(runtime, ".preset create work Ali")
    await invoke(runtime, ".preset schedule work 3h")
    await invoke(runtime, ".preset scheduled")
    assert "work" in "\n".join(m.text for m in runtime.client.sent)
    job = await runtime.store.db.fetch_one("SELECT id FROM jobs WHERE kind = 'profile.preset'")
    assert job is not None
    await invoke(runtime, f".preset unschedule {job['id']}")
    assert await runtime.store.db.fetch_all("SELECT * FROM jobs WHERE kind = 'profile.preset'") == []


async def test_scheduled_preset_runs(runtime) -> None:
    await invoke(runtime, ".preset create work Ali Building")
    await runtime.features.set_enabled("profile_scheduler", True)
    job_id = await runtime.scheduler.enqueue(
        "profile.preset",
        utc_now() - timedelta(seconds=1),
        {"preset": "work", "language": "en", "timezone": "UTC"},
    )
    await runtime.scheduler.tick()
    history = await runtime.store.profile_history()
    assert any(row["reason"] == "preset-schedule:work" for row in history)
    row = await runtime.store.db.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    assert row["status"] == "done"


async def test_scheduled_preset_is_dropped_when_feature_disabled(runtime) -> None:
    await invoke(runtime, ".preset create work Ali")
    await runtime.features.set_enabled("profile_scheduler", False)
    await runtime.scheduler.enqueue("profile.preset", utc_now() - timedelta(seconds=1), {"preset": "work"})
    await runtime.scheduler.tick()
    assert await runtime.store.profile_history() == []


async def test_scheduled_preset_with_missing_preset_notifies(runtime) -> None:
    await runtime.features.set_enabled("profile_scheduler", True)
    await runtime.scheduler.enqueue("profile.preset", utc_now() - timedelta(seconds=1), {"preset": "ghost"})
    await runtime.scheduler.tick()
    assert any(message.chat_id == 42 for message in runtime.client.sent)


async def test_bio_snapshot_and_restore(runtime) -> None:
    await invoke(runtime, ".bio set working late")
    await invoke(runtime, ".profile restore --yes")
    latest = (await runtime.store.profile_history(limit=5))[0]
    assert latest is not None


async def test_profile_preview_does_not_change_anything(runtime) -> None:
    before = len(await runtime.store.profile_history())
    await invoke(runtime, ".profile preview New Name")
    assert len(await runtime.store.profile_history()) == before


async def test_bio_limit_is_enforced(runtime) -> None:
    await invoke(runtime, f".bio set {'x' * 200}")
    text = "\n".join(m.text for m in runtime.client.sent).lower()
    assert "long" in text or "limit" in text or "invalid" in text
    assert await runtime.store.get_setting("profile.bio_last") is None
