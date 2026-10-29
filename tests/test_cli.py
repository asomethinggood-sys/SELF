"""The command-line entry points: version, check, migrate."""

from __future__ import annotations

from pathlib import Path

import pytest

from havoc import main as cli_main


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    values = {
        "API_ID": "12345",
        "API_HASH": "0123456789abcdef0123456789abcdef",
        "PHONE": "+491234567890",
        "DATA_DIR": str(tmp_path / "data"),
        "DATABASE_PATH": str(tmp_path / "data" / "cli.db"),
        "LOG_DIR": str(tmp_path / "logs"),
        "BACKUP_DIR": str(tmp_path / "backups"),
        "PLUGIN_DIR": str(tmp_path / "plugins"),
        "SESSION_PATH": str(tmp_path / "data" / "session"),
        "HAVOC_ENV_FILE": str(tmp_path / "missing.env"),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


def test_version_prints_versions(env, capsys) -> None:
    assert cli_main.cli(["version"]) == cli_main.EXIT_OK
    out = capsys.readouterr().out
    assert "Yura 2.0.0" in out
    assert "Telethon installed" in out


def test_check_passes_with_valid_config(env, capsys) -> None:
    code = cli_main.cli(["check"])
    out = capsys.readouterr().out
    assert "configuration check" in out
    assert "Result:" in out
    assert code == cli_main.EXIT_OK


def test_check_fails_without_credentials(monkeypatch, tmp_path, capsys) -> None:
    for key in ("API_ID", "API_HASH", "SESSION_STRING"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HAVOC_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    code = cli_main.cli(["check"])
    captured = capsys.readouterr()
    assert code == cli_main.EXIT_CONFIG
    assert "API_ID" in captured.out + captured.err


def test_migrate_creates_schema(env, capsys) -> None:
    assert cli_main.cli(["migrate"]) == cli_main.EXIT_OK
    out = capsys.readouterr().out
    assert "schema" in out.lower()
    assert Path(env["DATABASE_PATH"]).exists()


def test_migrate_is_idempotent(env, capsys) -> None:
    cli_main.cli(["migrate"])
    capsys.readouterr()
    assert cli_main.cli(["migrate"]) == cli_main.EXIT_OK
    out = capsys.readouterr().out
    assert "nothing to do" in out.lower()
    assert "→ 2" in out


def test_unknown_command_exits_with_error(env) -> None:
    with pytest.raises(SystemExit):
        cli_main.cli(["nonsense"])
