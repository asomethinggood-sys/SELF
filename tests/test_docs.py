"""The committed documentation and locale files stay in sync with the code."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_locale_files_have_parity() -> None:
    en = json.loads((ROOT / "havoc" / "locales" / "en.json").read_text(encoding="utf-8"))
    fa = json.loads((ROOT / "havoc" / "locales" / "fa.json").read_text(encoding="utf-8"))
    assert set(en) == set(fa)
    assert all(isinstance(value, str) for value in en.values())
    assert all(isinstance(value, str) for value in fa.values())
    # Commands without arguments deliberately have an empty usage string, so
    # only descriptions must be non-empty.
    for key, value in fa.items():
        if key.endswith(".description"):
            assert value.strip(), key


def test_locale_checker_passes() -> None:
    result = _run("scripts/check_locales.py")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK:" in result.stdout


def test_command_docs_are_up_to_date() -> None:
    result = _run("scripts/generate_docs.py", "--check")
    assert result.returncode == 0, (
        "docs/COMMANDS.md is stale — run 'python scripts/generate_docs.py'\n" + result.stdout + result.stderr
    )


@pytest.mark.parametrize(
    "path",
    ["README.md", "docs/INSTALL.md", "docs/CONFIGURATION.md", "docs/SECURITY.md", "docs/TROUBLESHOOTING.md"],
)
def test_documents_exist_and_are_substantial(path: str) -> None:
    text = (ROOT / path).read_text(encoding="utf-8")
    assert len(text.splitlines()) >= 20
    assert "#" in text


def test_env_example_documents_required_keys() -> None:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for key in ("API_ID", "API_HASH", "PHONE", "TIMEZONE", "DEFAULT_LANGUAGE"):
        assert key in text


def test_no_secrets_or_databases_are_tracked() -> None:
    result = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:  # pragma: no cover - git unavailable
        pytest.skip("git not available")
    tracked = result.stdout.split()
    forbidden = [path for path in tracked if path.endswith((".session", ".db", ".env")) or path.startswith(("data/", "backups/"))]
    assert forbidden == []
