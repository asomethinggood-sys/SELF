"""Permission decisions: ownership, allow/deny lists, quiet hours, dry-run."""

from __future__ import annotations

from datetime import datetime

from conftest import make_config

from havoc.permissions import Permissions


async def _permissions(runtime, **overrides) -> Permissions:
    config = make_config(runtime.config.data_dir.parent, **overrides)
    return Permissions(config, runtime.store)


async def test_owner_detection(runtime) -> None:
    permissions = await _permissions(runtime)
    permissions.set_self_id(42)
    assert permissions.is_owner(42) is True
    assert permissions.is_owner(43) is False
    assert permissions.is_self(42) is True
    assert permissions.self_id == 42


def test_owner_ids_from_config(runtime) -> None:
    config = make_config(runtime.config.data_dir.parent, owner_ids=(7, 8))
    permissions = Permissions(config, runtime.store)
    assert permissions.is_owner(7) is True
    assert permissions.is_owner(9) is False


async def test_feature_gate_requires_enabled_feature(runtime) -> None:
    permissions = await _permissions(runtime)
    decision = await permissions.can_act(100, "autoreply")
    assert decision.allowed is False
    await runtime.features.set_enabled("autoreply", True)
    decision = await permissions.can_act(100, "autoreply")
    assert decision.allowed is True


async def test_emergency_stop_blocks_everything(runtime) -> None:
    permissions = await _permissions(runtime)
    await runtime.features.set_enabled("autoreply", True)
    await runtime.store.set_emergency_stop(True)
    decision = await permissions.can_act(100, "autoreply")
    assert decision.allowed is False
    assert "stop" in decision.code or "emergency" in decision.code


async def test_chat_denylist_blocks(runtime) -> None:
    permissions = await _permissions(runtime)
    await runtime.features.set_enabled("autoreply", True)
    await runtime.store.set_chat_setting(100, "automation.denied", True)
    decision = await permissions.can_act(100, "autoreply")
    assert decision.allowed is False


async def test_allowlist_limits_scope(runtime) -> None:
    config = make_config(runtime.config.data_dir.parent, automation_allowlist=(500,))
    permissions = Permissions(config, runtime.store)
    await runtime.features.set_enabled("autoreply", True)
    allowed = await permissions.can_act(500, "autoreply")
    denied = await permissions.can_act(501, "autoreply")
    assert allowed.allowed is True
    assert denied.allowed is False


async def test_denylist_wins_over_allowlist(runtime) -> None:
    config = make_config(
        runtime.config.data_dir.parent, automation_allowlist=(500,), automation_denylist=(500,)
    )
    permissions = Permissions(config, runtime.store)
    await runtime.features.set_enabled("autoreply", True)
    assert (await permissions.can_act(500, "autoreply")).allowed is False


async def test_quiet_hours_window(runtime) -> None:
    permissions = await _permissions(runtime)
    await runtime.store.set_setting("quiet_hours", {"start": "22:00", "end": "06:00", "timezone": "UTC"})
    assert await permissions.in_quiet_hours(moment=datetime(2026, 1, 1, 23, 30)) is True
    assert await permissions.in_quiet_hours(moment=datetime(2026, 1, 1, 12, 0)) is False
    assert await permissions.in_quiet_hours(moment=datetime(2026, 1, 1, 5, 59)) is True
    assert await permissions.in_quiet_hours(moment=datetime(2026, 1, 1, 6, 0)) is False


async def test_quiet_hours_blocks_automation(runtime) -> None:
    permissions = await _permissions(runtime)
    await runtime.features.set_enabled("autoreply", True)
    await runtime.store.set_setting("quiet_hours", {"start": "00:00", "end": "23:59", "timezone": "UTC"})
    decision = await permissions.can_act(100, "autoreply")
    assert decision.allowed is False
    assert "quiet" in decision.code


async def test_dry_run_state_is_persistent(runtime) -> None:
    permissions = await _permissions(runtime)
    assert await permissions.dry_run_active() is False
    await permissions.set_dry_run(True)
    assert await permissions.dry_run_active() is True
    assert await runtime.store.get_bool("runtime.dry_run") is True
    await permissions.set_dry_run(False)
    assert await permissions.dry_run_active() is False
