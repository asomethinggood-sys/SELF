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


def merge_into(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Keep a short history, so one good run does not erase the last bad one."""
    document: dict[str, Any] = {"app": "havoc", "kind": "bot-status", "history": []}
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(existing, dict):
            document.update({k: v for k, v in existing.items() if k not in {"current", "history"}})
            history = existing.get("history")
            if isinstance(history, list):
                document["history"] = history[-9:]
    except (OSError, ValueError):
        pass
    document["current"] = payload
    document["history"] = [*document["history"], payload][-10:]
    document["runs_seen"] = len(document["history"])
    document["last_healthy_at"] = next(
        (entry.get("written_at") for entry in reversed(document["history"]) if entry.get("healthy")),
        "",
    )
    return document


def publish(path: Path, document: dict[str, Any], *, commit: bool = True) -> str:
    """Write the document, then commit and push it. Returns what happened."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not commit:
        return "written"
    if not (ROOT / ".git").exists():
        return "written (no git)"
    try:
        subprocess.run(["git", "config", "user.name", "HAVOC status"], cwd=ROOT, check=False)
        subprocess.run(
            ["git", "config", "user.email", "status@users.noreply.github.com"], cwd=ROOT, check=False
        )
        subprocess.run(["git", "add", str(path)], cwd=ROOT, check=True)
        staged = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=ROOT, check=False
        ).returncode
        if staged == 0:
            return "unchanged"
        subprocess.run(
            ["git", "commit", "-qm", f"bot status {datetime.now(UTC):%Y-%m-%dT%H:%MZ} [skip ci]"],
            cwd=ROOT,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        return f"write failed: {exc}"
    branch = os.environ.get("GITHUB_REF_NAME", "")
    for _ in range(3):
        subprocess.run(["git", "fetch", "-q", "origin", branch or "HEAD"], cwd=ROOT, check=False)
        subprocess.run(["git", "rebase", "-q", "--autostash", "FETCH_HEAD"], cwd=ROOT, check=False)
        push = subprocess.run(
            ["git", "push", "-q", "origin", f"HEAD:refs/heads/{branch}"], cwd=ROOT, check=False
        )
        if push.returncode == 0:
            return "committed"
        subprocess.run(["git", "rebase", "--abort"], cwd=ROOT, check=False)
    return "committed locally (push failed)"


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
    document = merge_into(path, payload)
    outcome = publish(path, document, commit=not args.no_commit)
    print(json.dumps({"healthy": payload["healthy"], "outcome": outcome}, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
