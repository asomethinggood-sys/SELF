"""The bot's own status file: honest, bounded, redacted."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import publish_status  # noqa: E402


def test_a_crash_is_not_reported_as_healthy(tmp_path: Path) -> None:
    log = tmp_path / "bot.log"
    log.write_text("INFO starting\nTraceback (most recent call last):\nAttributeError: no start\n")
    payload = publish_status.build(
        exit_code=3, uptime_seconds=9.0, reason="died", log_path=log
    )
    assert payload["healthy"] is False
    assert payload["exit_code"] == 3
    assert payload["log_tail"][-1].endswith("no start")


def test_the_ceiling_is_not_a_failure() -> None:
    """`timeout` ends the run at six hours; that is the plan, not a crash."""
    payload = publish_status.build(exit_code=124, uptime_seconds=20_000, reason="ceiling")
    assert payload["healthy"] is True


def test_a_short_but_clean_exit_is_still_early_death() -> None:
    payload = publish_status.build(exit_code=0, uptime_seconds=12, reason="returned")
    assert payload["healthy"] is False


def test_secrets_never_reach_the_file(tmp_path: Path) -> None:
    log = tmp_path / "bot.log"
    log.write_text("connecting with hash abcdef0123456789abcdef0123456789abcd\n")
    payload = publish_status.build(
        exit_code=1,
        uptime_seconds=3,
        reason="failed to connect with abcdef0123456789abcdef0123456789abcd",
        log_path=log,
        secrets=["abcdef0123456789abcdef0123456789abcd"],
    )
    blob = json.dumps(payload)
    assert "abcdef0123456789abcdef0123456789abcd" not in blob


def test_history_keeps_the_bad_run_when_a_good_one_follows(tmp_path: Path) -> None:
    path = tmp_path / "bot-status.json"
    bad = publish_status.build(exit_code=3, uptime_seconds=9, reason="died")
    good = publish_status.build(exit_code=124, uptime_seconds=20_000, reason="ceiling")
    document: dict[str, Any] = publish_status.merge_into(path, bad)
    path.write_text(json.dumps(document))
    document = publish_status.merge_into(path, good)
    path.write_text(json.dumps(document))
    document = publish_status.merge_into(path, good)
    assert document["current"]["healthy"] is True
    assert any(entry["healthy"] is False for entry in document["history"])
    assert len(document["history"]) <= 10
    assert document["last_healthy_at"]
