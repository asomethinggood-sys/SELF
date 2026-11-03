#!/usr/bin/env python3
"""Publish the bot's own status into the repository.

Why this exists
---------------
A GitHub Actions log is a dead end: it is not readable from everywhere, it
expires, and when a run *succeeds* while the bot inside it died after nine
seconds, the log is the only witness — and nobody looks. That is exactly how 1.7
shipped a build that logged in, crashed on a removed method, and was reported as
a green run.

So the bot now leaves a written trace of every run in the branch it runs from:
`.runtime/bot-status.json` is written on the way out, committed, and pushed. It
holds the exit code, how long the process lasted, why it stopped, and the last
lines of its own log. Anyone — the owner, the next agent, a reader of the
repository — can open one file and see whether she was alive.

The file never contains secrets: every string is passed through the same
redaction the log handlers use, and the log tail is capped.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # allow `python scripts/publish_status.py`
    sys.path.insert(0, str(ROOT))

from havoc.utils.security import redact_text  # noqa: E402

DEFAULT_PATH = Path("bot-status.json")
TAIL_LINES = 60
TAIL_CHARS = 4000


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def readable_tail(path: Path | str | None, *, lines: int = TAIL_LINES) -> list[str]:
    """The last few lines of a log file, redacted and bounded."""
    if not path:
        return []
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    tail = [line.rstrip() for line in text.splitlines()[-lines:]]
    out: list[str] = []
    budget = TAIL_CHARS
    for line in tail:
        line = redact_text(line)[:600]
        if budget <= 0:
            break
        out.append(line)
        budget -= len(line)
    return out


def build(
    *,
    exit_code: int | None,
    uptime_seconds: float | None,
    reason: str,
    log_path: str | Path | None = None,
    extra: dict[str, Any] | None = None,
    secrets: Iterable[str] = (),
) -> dict[str, Any]:
    """The document that describes one run of the bot."""
    tail = readable_tail(log_path)
    if secrets:
        tail = [redact_text(line, list(secrets)) for line in tail]
        redacted_reason = redact_text(reason, list(secrets))
    else:
        redacted_reason = redact_text(reason)
    healthy = bool(exit_code in (0, None)) and (uptime_seconds is None or uptime_seconds >= 120)
    if exit_code == 124:  # `timeout` ended it at the run's ceiling: that is normal
        healthy = True
    payload: dict[str, Any] = {
        "app": "havoc",
        "kind": "bot-run",
        "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        "repository": os.environ.get("GITHUB_REPOSITORY", ""),
        "branch": os.environ.get("GITHUB_REF_NAME", ""),
        "commit": os.environ.get("GITHUB_SHA", ""),
        "written_at": now(),
        "started_at": os.environ.get("BOT_STARTED_AT", ""),
        "exit_code": exit_code,
        "uptime_seconds": round(uptime_seconds, 1) if uptime_seconds is not None else None,
        "healthy": healthy,
        "reason": redacted_reason,
        "log_tail": tail,
    }
    if extra:
        payload["extra"] = {key: redact_text(value) for key, value in extra.items()}
    return payload


def _loads(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def merge_documents(existing: dict[str, Any] | None, payload: dict[str, Any]) -> dict[str, Any]:
    """Keep a short history, so one good run does not erase the last bad one.

    ``existing`` is whatever the branch already holds, not just what this
    checkout holds: a run that started from older code must not drop the entry a
    newer run published in the meantime. The same run is never counted twice.
    """
    document: dict[str, Any] = {"app": "havoc", "kind": "bot-status", "history": []}
    history: list[dict[str, Any]] = []
    if isinstance(existing, dict):
        document.update({k: v for k, v in existing.items() if k not in {"current", "history"}})
        if isinstance(existing.get("history"), list):
            history = [entry for entry in existing["history"] if isinstance(entry, dict)][-9:]
    # The same entry, word for word, is a re-publish; anything else is a run of
    # its own, even if it shares a run id and a timestamp with the one before.
    fingerprint = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    if not any(json.dumps(entry, sort_keys=True, ensure_ascii=False) == fingerprint for entry in history):
        history = [*history, payload][-10:]
    document["history"] = history
    document["current"] = payload
    document["runs_seen"] = len(history)
    document["last_healthy_at"] = next(
        (entry.get("written_at") for entry in reversed(history) if entry.get("healthy")),
        "",
    )
    return document


def merge_into(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Merge a payload into the document in ``path`` (what a local run can see)."""
    try:
        existing = _loads(path.read_text(encoding="utf-8"))
    except OSError:
        existing = {}
    return merge_documents(existing, payload)


def write_document(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _git(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        check=False,
    )


def _repo_path(path: Path) -> str:
    """The path as git knows it: relative to the repository root."""
    try:
        return Path(path).resolve().relative_to(Path(ROOT).resolve()).as_posix()
    except ValueError:
        return Path(path).as_posix()


def _commit_on_top(base: str, path: Path) -> tuple[str, str]:
    """A commit that changes only ``path``, built on top of ``base``.

    Plumbing, not ``add``/``commit``/``rebase``: it needs no clean worktree, it
    cannot be stopped by a rebase somebody else left in progress, and it cannot
    conflict with a branch that moved while the bot was running — the branch is
    read first, merged into the document, and then written over ``base``.
    """
    blob = _git("hash-object", "-w", "--", str(path))
    if blob.returncode != 0:
        return "", f"hash-object failed: {blob.stderr.strip()[:200]}"
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        for step, args in (
            ("read-tree", ("read-tree", base)),
            ("update-index", ("update-index", "--add", "--cacheinfo", f"100644,{blob.stdout.strip()},{_repo_path(path)}")),
            ("write-tree", ("write-tree",)),
        ):
            done = _git(*args, env=env)
            if done.returncode != 0:
                return "", f"{step} failed: {done.stderr.strip()[:200]}"
            if step == "write-tree":
                tree = done.stdout.strip()
    message = f"bot status {datetime.now(UTC):%Y-%m-%dT%H:%MZ} [skip ci]"
    # Runners do not necessarily have a global git identity. In particular,
    # her workflow can publish without ever configuring user.name/user.email.
    # Give this machine-generated commit its own explicit identity rather than
    # depending on whichever earlier workflow step happened to set one.
    identity = {
        "GIT_AUTHOR_NAME": "Yura status",
        "GIT_AUTHOR_EMAIL": "status@users.noreply.github.com",
        "GIT_COMMITTER_NAME": "Yura status",
        "GIT_COMMITTER_EMAIL": "status@users.noreply.github.com",
    }
    commit = _git("commit-tree", tree, "-p", base, "-m", message, env=identity)
    if commit.returncode != 0:
        return "", f"commit-tree failed: {commit.stderr.strip()[:200]}"
    return commit.stdout.strip(), ""


def publish(path: Path, document: dict[str, Any], *, commit: bool = True) -> str:
    """Write the document, then publish it. Returns what happened.

    Returns ``"committed"`` only when the branch really moved, and a string
    starting with ``"push failed"`` (or ``"write failed"``) otherwise, so the
    caller can fail loudly instead of leaving a stale file that looks like the
    truth.
    """
    write_document(path, document)
    if not commit:
        return "written"
    if not (ROOT / ".git").exists():
        return "written (no git)"
    branch = os.environ.get("GITHUB_REF_NAME", "") or "HEAD"
    last = "no attempt"
    for attempt in range(3):
        if attempt:
            time.sleep(2)
        fetched = _git("fetch", "-q", "origin", branch)
        if fetched.returncode != 0:
            last = f"push failed (fetch): {fetched.stderr.strip()[:200]}"
            continue
        rev = _git("rev-parse", "FETCH_HEAD")
        base = rev.stdout.strip()
        if rev.returncode != 0 or not base:
            last = f"push failed (rev-parse): {rev.stderr.strip()[:200]}"
            continue
        # What the branch says right now, not what this checkout said at boot.
        on_branch = _loads(_git("show", f"{base}:{_repo_path(path)}").stdout)
        merged = merge_documents(on_branch, document["current"]) if document.get("current") else document
        write_document(path, merged)
        commit_sha, error = _commit_on_top(base, path)
        if not commit_sha:
            last = f"write failed: {error}"
            continue
        pushed = _git("push", "-q", "origin", f"{commit_sha}:refs/heads/{branch}")
        if pushed.returncode == 0:
            return "committed"
        last = f"push failed: {pushed.stderr.strip()[:200]}"
    return last


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="publish the bot's status into the repository")
    parser.add_argument("--path", default=str(DEFAULT_PATH), help="where the document lives")
    parser.add_argument("--exit-code", type=int, default=None, help="the bot process's exit code")
    parser.add_argument("--uptime", type=float, default=None, help="how long the bot process lived")
    parser.add_argument("--reason", default="", help="one line: why the process stopped")
    parser.add_argument("--log", default="", help="log file whose tail is published")
    parser.add_argument("--extra", action="append", default=[], help="key=value pairs to include")
    parser.add_argument("--no-commit", action="store_true", help="write the file only")
    args = parser.parse_args(argv)

    extra: dict[str, Any] = {}
    for pair in args.extra:
        key, _, value = pair.partition("=")
        if key:
            extra[key] = value
    payload = build(
        exit_code=args.exit_code,
        uptime_seconds=args.uptime,
        reason=args.reason,
        log_path=args.log or None,
        extra=extra,
        secrets=[
            os.environ.get("API_HASH", ""),
            os.environ.get("SESSION_STRING", ""),
            os.environ.get("AI_API_KEY", ""),
        ],
    )
    path = Path(args.path)
    if not path.is_absolute():
        # Resolve status paths against the repository, not the caller's cwd.
        path = Path(ROOT) / path
    document = merge_into(path, payload)
    outcome = publish(path, document, commit=not args.no_commit)
    print(json.dumps({"healthy": payload["healthy"], "outcome": outcome}, ensure_ascii=False))
    # Silence is how a stale status file starts to look like the truth: a
    # publish that never reached the branch has to fail the command.
    return 1 if outcome.startswith(("push failed", "write failed")) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
