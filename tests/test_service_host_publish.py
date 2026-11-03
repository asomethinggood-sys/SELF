"""The service publisher must update only its report without stalling the host."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import service_host  # noqa: E402


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def test_service_publish_uses_latest_origin_without_touching_worktree(tmp_path, monkeypatch) -> None:
    origin = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(origin))
    checkout = tmp_path / "checkout"
    git(tmp_path, "clone", str(origin), str(checkout))
    branch = "arena/b9a136e7-self"
    git(checkout, "checkout", "-b", branch)
    (checkout / "base.txt").write_text("base")
    git(checkout, "add", "base.txt")
    git(checkout, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "initial")
    git(checkout, "push", "origin", branch)
    (checkout / ".services").mkdir()
    report = checkout / ".services" / "comfy.json"
    report.write_text('{"ok": true}')
    (checkout / "base.txt").write_text("local dirty file")
    monkeypatch.setattr(service_host, "ROOT", checkout)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_TOKEN", "dummy")
    monkeypatch.setenv("GITHUB_REF_NAME", branch)
    assert service_host.commit("service up [skip ci]", str(report))
    assert git(checkout, "show", f"origin/{branch}:.services/comfy.json") == '{"ok": true}'
    assert (checkout / "base.txt").read_text() == "local dirty file"
    assert git(checkout, "status", "--porcelain").startswith("M base.txt")
