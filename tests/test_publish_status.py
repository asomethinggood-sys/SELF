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


def test_the_status_file_is_not_ignored() -> None:
    """`git add` refuses an ignored path, and the status would vanish silently."""
    ignore = (Path(__file__).resolve().parent.parent / ".gitignore").read_text()
    assert "bot-status.json" not in ignore.splitlines()


def test_publish_does_not_depend_on_a_clean_worktree() -> None:
    """A run's checkout can be dirty or mid-rebase; publishing builds its own commit.

    The old path was `add -f` + `commit` + `rebase` + `push`, which fails silently
    on a worktree another process touched — and a stale status file then looks
    exactly like the truth.
    """
    source = (Path(__file__).resolve().parent.parent / "scripts" / "publish_status.py").read_text()
    assert '"commit-tree"' in source
    assert '"read-tree"' in source
    assert '"update-index"' in source
    assert '"rebase"' not in source


# ---------------------------------------------------------------------------
# Publishing for real: into a git repo, against a branch that moves
# ---------------------------------------------------------------------------
def _git(cwd: Path, *args: str) -> str:
    import subprocess

    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    """A bare origin and a checkout of it, with one status entry already there."""
    import subprocess

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    _git(work, "config", "user.email", "run@example.com")
    _git(work, "config", "user.name", "run")
    (work / "bot-status.json").write_text(json.dumps({"app": "havoc", "kind": "bot-status", "history": []}))
    _git(work, "add", "bot-status.json")
    _git(work, "commit", "-qm", "init")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "-u", "origin", "main")
    return origin, work


def test_publishing_lands_on_a_branch_that_moved_while_the_bot_ran(tmp_path: Path, monkeypatch) -> None:
    """Another run published first, the worktree is dirty, and it still lands."""
    import subprocess

    origin, work = _repo(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    subprocess.run(["git", "clone", "-q", "-b", "main", str(origin), str(elsewhere)], check=True)
    _git(elsewhere, "config", "user.email", "other@example.com")
    _git(elsewhere, "config", "user.name", "other")
    theirs = {
        "app": "havoc",
        "kind": "bot-status",
        "history": [{"run_id": "other-run", "written_at": "then", "healthy": True}],
        "current": {"run_id": "other-run", "written_at": "then", "healthy": True},
    }
    (elsewhere / "bot-status.json").write_text(json.dumps(theirs))
    _git(elsewhere, "commit", "-qam", "bot status from another run [skip ci]")
    _git(elsewhere, "push", "-q", "origin", "main")

    (work / "stray.txt").write_text("the bot left this behind")  # dirty worktree
    monkeypatch.setattr(publish_status, "ROOT", work)
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    payload = publish_status.build(exit_code=0, uptime_seconds=3000, reason="stepped aside")
    outcome = publish_status.publish(work / "bot-status.json", publish_status.merge_documents({}, payload))
    assert outcome == "committed"

    check = tmp_path / "check"
    subprocess.run(["git", "clone", "-q", "-b", "main", str(origin), str(check)], check=True)
    on_branch = json.loads((check / "bot-status.json").read_text())
    assert on_branch["current"]["reason"] == "stepped aside"
    assert any(entry.get("run_id") == "other-run" for entry in on_branch["history"]), "the other run was erased"


def test_a_publish_that_cannot_push_says_so(tmp_path: Path, monkeypatch, capsys) -> None:
    """The old code printed `committed locally (push failed)` and exited 0."""
    origin, work = _repo(tmp_path)
    (origin / "config").write_text("[core]\n\tbare = true\n")  # still a real repo…
    _git(work, "remote", "set-url", "origin", str(tmp_path / "nowhere.git"))  # …that is gone
    monkeypatch.setattr(publish_status, "ROOT", work)
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    code = publish_status.main(["--exit-code", "0", "--uptime", "3000", "--reason", "stepped aside"])
    printed = capsys.readouterr().out
    assert code == 1, printed
    assert "push failed" in printed


def test_a_bare_path_is_the_repositorys_file_not_the_callers_directory(
    tmp_path: Path, monkeypatch
) -> None:
    """A relative ``--path`` belongs to the repository, not to the current directory.

    ``main()`` used to hand the bare default straight to ``Path``, so running the
    suite from a checkout rewrote the committed ``bot-status.json`` — ten real run
    records replaced by one ``run_id: local`` entry. The file is the only witness
    of whether the bots lived, so no test may be able to reach it.
    """
    checkout = Path(__file__).resolve().parent.parent
    real = checkout / "bot-status.json"
    before = real.read_bytes()

    _origin, work = _repo(tmp_path)
    monkeypatch.setattr(publish_status, "ROOT", work)
    monkeypatch.setenv("GITHUB_REF_NAME", "main")

    code = publish_status.main(
        ["--path", "bot-status.json", "--exit-code", "0", "--uptime", "3000",
         "--reason", "stepped aside", "--no-commit"]
    )
    assert code == 0
    written = json.loads((work / "bot-status.json").read_text())
    assert written["current"]["reason"] == "stepped aside"
    assert real.read_bytes() == before, "the repository's own status file was rewritten"
