"""The credential probe: verdict mapping is what matters.

The probes themselves need Telegram, so they are exercised by hand; everything
that decides *what the answer means* is tested against the errors Telegram is
known to return for public client ids.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "probe_credentials.py"
spec = importlib.util.spec_from_file_location("probe_credentials", SCRIPT)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class Published(Exception):
    pass


class ApiIdInvalid(Exception):
    pass


class PhoneInvalid(Exception):
    pass


class Flood(Exception):
    pass


class Connect(Exception):
    pass


def test_published_flood_is_recognised() -> None:
    verdict, detail = module.classify(Published("API_ID_PUBLISHED_FLOOD"), stage="auth.sendCode")
    assert verdict == "published_flood"
    assert "PUBLISHED" in detail


def test_invalid_pair_is_recognised() -> None:
    assert module.classify(ApiIdInvalid("API_ID_INVALID"), stage="x")[0] == "invalid"


def test_phone_number_invalid_means_the_pair_was_accepted() -> None:
    # The api_id passed and only the throwaway number was rejected. That is not
    # proof of a working login, so it must not be reported as one.
    assert module.classify(PhoneInvalid("PHONE_NUMBER_INVALID"), stage="x")[0] == "accepted"


def test_camel_case_exception_names_are_understood() -> None:
    """Telethon says PhoneNumberInvalidError; the wire says PHONE_NUMBER_INVALID."""

    class PhoneNumberInvalidError(Exception):
        pass

    verdict, _ = module.classify(PhoneNumberInvalidError("The phone number is invalid"), stage="x")
    assert verdict == "accepted"


def test_camel_case_published_flood_is_understood() -> None:
    class ApiIdPublishedFloodError(Exception):
        pass

    verdict, _ = module.classify(ApiIdPublishedFloodError("published"), stage="x")
    assert verdict == "published_flood"


def test_flood_is_reported_separately_from_published_flood() -> None:
    assert module.classify(Flood("FLOOD_WAIT_3600"), stage="x")[0] == "flood"


def test_network_errors_are_distinguished() -> None:
    assert module.classify(Connect("Connection refused"), stage="connect")[0] == "network"
    assert module.classify(TimeoutError("timed out"), stage="connect")[0] == "network"


def test_unexpected_errors_keep_the_stage_for_debugging() -> None:
    verdict, detail = module.classify(ValueError("nonsense"), stage="help.getConfig")
    assert verdict == "unknown"
    assert detail.startswith("help.getConfig")


def test_no_error_is_ok() -> None:
    assert module.classify(None, stage="x")[0] == "ok"


def test_probe_number_cannot_be_someone_else() -> None:
    """A real number here would cost its owner a login code."""
    assert module.PROBE_PHONE.startswith("+999")
    assert len(module.PROBE_PHONE) > 8


def test_published_pairs_are_the_documented_ones() -> None:
    pairs = {label: (api_id, api_hash) for label, api_id, api_hash in module.PUBLISHED_PAIRS}
    assert pairs["telegram-desktop"] == ("2040", "b18441a1ff607e10a989891a5462e627")
    for api_id, api_hash in pairs.values():
        assert api_id.isdigit()
        assert len(api_hash) == 32


def test_summary_says_none_are_usable_when_all_fail() -> None:
    verdicts = [
        module.Verdict("telegram-desktop", "2040", "published_flood"),
        module.Verdict("telegram-android", "6", "invalid"),
    ]
    report = module.summarise(verdicts)
    assert "None of the pairs is usable" in report
    assert "**published_flood**" in report


def test_summary_is_explicit_about_inconclusive_results() -> None:
    verdicts = [module.Verdict("telegram-desktop", "2040", "accepted")]
    report = module.summarise(verdicts)
    assert "not proven" in report
    assert "decisive test" in report
    assert "Proven to work" not in report


def test_summary_warns_that_usable_is_a_stopgap() -> None:
    verdicts = [module.Verdict("telegram-desktop", "2040", "works")]
    report = module.summarise(verdicts)
    assert "stopgap" in report
    assert "telegram-desktop" in report


def test_markdown_is_pipe_safe() -> None:
    verdicts = [module.Verdict("x", "1", "unknown", "a | b")]
    assert "a \\| b" in module.summarise(verdicts)


def test_cli_rejects_half_a_pair(capsys) -> None:
    assert module.main(["--api-id", "123"]) == 2
    assert "together" in capsys.readouterr().err


def test_cli_writes_both_reports(monkeypatch, tmp_path, capsys) -> None:
    async def fake_probe_all(pairs, *, timeout=20.0):
        return [module.Verdict("custom", "123", "works", "")]

    monkeypatch.setattr(module, "probe_all", fake_probe_all)
    json_path = tmp_path / "results.json"
    md_path = tmp_path / "report.md"
    code = module.main(
        [
            "--api-id",
            "123",
            "--api-hash",
            "a" * 32,
            "--json-out",
            str(json_path),
            "--markdown-out",
            str(md_path),
        ]
    )
    assert code == 0
    data = json.loads(json_path.read_text())
    assert data[0]["verdict"] == "works"
    assert "stopgap" in md_path.read_text()


def test_cli_exits_nonzero_when_nothing_works(monkeypatch, capsys) -> None:
    async def fake_probe_all(pairs, *, timeout=20.0):
        return [module.Verdict("custom", "123", "published_flood")]

    monkeypatch.setattr(module, "probe_all", fake_probe_all)
    assert module.main(["--api-id", "123", "--api-hash", "a" * 32]) == 1
