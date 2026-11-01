#!/usr/bin/env python3
"""Start the local-model workflow when its six hours are up.

The bot's own workflow already runs continuously on a branch GitHub will not
schedule: it queues its successor itself, and each run lives about six hours.
This script gives the *local model* the same six-hour rhythm without giving it a
chain of its own — a two-minute job that dispatches itself the moment it ends is
a job that runs three hundred times a day.

So the clock is borrowed from a process that is already awake:

    auth-and-run.yml  ──(once per run, ≈ every 6 h)──▶  local_model_schedule.py
                                                              │
                                                   release(.local-model.json) old?
                                                              ▼
                                                    dispatch local-model.yml

``.local-model.json`` is written by ``local-model.yml`` itself (the run's report:
model, timings, checks), so the decision is based on when the model last actually
answered, not on a bookmark that could drift.

    python scripts/local_model_schedule.py --dry-run
    python scripts/local_model_schedule.py --every-hours 5 --workflow local-model.yml

Exit codes: 0 whether or not a run was needed (this must never fail a deploy),
2 only when asked to act and the API refused.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.github.com"
DEFAULT_WORKFLOW = "local-model.yml"
#: Two minutes of slack: a run that started at 09:00 and one that starts at
#: 14:58 are six hours apart as far as a human is concerned.
SLACK_MINUTES = 10


def last_run(report: Path) -> datetime | None:
    """When the local model last answered, from the report the workflow writes."""
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    stamp = str(data.get("at") or data.get("finished_at") or "")
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def due(report: Path, every_hours: float, *, now: datetime | None = None) -> tuple[bool, str]:
    """``(should_dispatch, why)`` — the whole decision, in one place."""
    now = now or datetime.now(UTC)
    stamp = last_run(report)
    if stamp is None:
        return True, "no report yet — the model has never been started on a runner"
    elapsed = (now - stamp).total_seconds() / 3600.0
    if elapsed >= every_hours - SLACK_MINUTES / 60.0:
        return True, f"the last check was {elapsed:.1f}h ago (every {every_hours:g}h)"
    return False, f"the last check was {elapsed:.1f}h ago; next one at {every_hours:g}h"


def dispatch(workflow: str, *, ref: str, token: str, timeout: float = 20.0) -> tuple[int, str]:
    """Ask GitHub to run the workflow on this branch."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        return 0, "GITHUB_REPOSITORY is not set"
    url = f"{API}/repos/{repo}/actions/workflows/{workflow}/dispatches"
    body = json.dumps({"ref": ref}).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, "dispatched"
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001 - a body is a courtesy
            detail = ""
        return exc.code, detail or str(exc)
    except (urllib.error.URLError, OSError) as exc:
        return 0, f"network: {exc}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default=str(ROOT / ".local-model.json"))
    parser.add_argument("--every-hours", type=float, default=5.5)
    parser.add_argument("--workflow", default=DEFAULT_WORKFLOW)
    parser.add_argument("--ref", default=os.environ.get("GITHUB_REF_NAME", ""))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    report = Path(args.report)
    should, why = due(report, args.every_hours)
    print(f"local model: {why}")
    if not should:
        return 0
    if args.dry_run:
        print(f"::notice::would dispatch {args.workflow} on {args.ref or '<current branch>'}")
        return 0
    token = os.environ.get("GITHUB_TOKEN", "")
    ref = args.ref or os.environ.get("GITHUB_REF_NAME", "")
    if not token or not ref:
        print("::warning::no token or branch to dispatch with; skipping")
        return 0
    status, detail = dispatch(args.workflow, ref=ref, token=token)
    if status in {200, 201, 204}:
        print(f"::notice::dispatched {args.workflow} on {ref}")
        return 0
    print(f"::warning::could not dispatch {args.workflow}: {status} {detail}")
    return 0  # never fail a deploy over the spare tyre


if __name__ == "__main__":
    raise SystemExit(main())
