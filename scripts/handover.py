"""Keep the bot alive: make sure a successor run is queued before this one ends.

Why this exists
---------------
GitHub gives a job six hours at most, and it does not always give that: a run can
be cancelled, a runner can vanish, a login can fail. Every one of those ends the
process, and nothing on the runner can start it again. The only thing that *can*
is another workflow run — and GitHub's own token is allowed to dispatch one
(`workflow_dispatch`, the documented exception to "token events do not trigger
workflows"; verified from a run of this repository, not assumed).

So each run queues its own successor **at the start**. When this job ends — an
orderly finish, a timeout, a crash, a cancellation by the owner — the queued run
starts within seconds. The bot's downtime becomes the runner's boot time instead
of "until the next schedule", which on a branch that has no schedule is forever.

Why it is careful
-----------------
* It only ever queues *one* successor. A chain that queues two per run doubles
  itself every six hours and burns the account's Actions minutes.
* It never queues while another run is already queued or in progress, so a push
  that starts a deploy is enough; no second run piles up behind it.
* It can be asked to explain itself (`--dry-run`, `--json`) instead of acting,
  which is what the tests use.

Usage
-----
``python scripts/handover.py`` reads ``GITHUB_TOKEN``, ``GITHUB_REPOSITORY``,
``GITHUB_REF_NAME`` and ``GITHUB_RUN_ID`` from the environment, and
``HANDOVER_WORKFLOW`` (default ``auth-and-run.yml``) for the file to dispatch.

Exit codes: 0 when a successor is queued or already present, 1 when the API said
no (so the workflow log says so instead of pretending).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime

#: GitHub statuses that mean "a run exists and is not finished".
LIVE_STATUSES = frozenset({"queued", "in_progress", "requested", "waiting", "pending", "accepted"})

API = "https://api.github.com"


def _request(
    method: str,
    url: str,
    token: str,
    payload: dict | None = None,
    *,
    timeout: float = 20.0,
) -> tuple[int, str]:
    """One API call. Returns ``(status, body)``; never raises for an HTTP error."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - a body is a courtesy, not a requirement
            body = ""
        return exc.code, body
    except (urllib.error.URLError, OSError) as exc:
        return 0, f"network: {exc}"


def live_runs(
    token: str,
    repo: str,
    *,
    branch: str | None = None,
    workflow: str | None = None,
    api: str = API,
    timeout: float = 20.0,
) -> list[dict]:
    """Runs of ``workflow`` on ``branch`` that are queued or in progress."""
    url = f"{api}/repos/{repo}/actions/runs?per_page=50"
    if branch:
        url += f"&branch={branch}"
    status, body = _request("GET", url, token, timeout=timeout)
    if status != 200:
        raise RuntimeError(f"list runs failed: HTTP {status} {body[:200]}")
    try:
        runs = json.loads(body).get("workflow_runs") or []
    except ValueError as exc:  # pragma: no cover - GitHub always answers JSON
        raise RuntimeError(f"list runs returned no JSON: {exc}") from exc
    wanted = [run for run in runs if str(run.get("status")) in LIVE_STATUSES]
    if workflow:
        wanted = [run for run in wanted if str(run.get("path", "")).endswith(workflow)]
    return wanted


#: A run older than this is a corpse wearing a "in progress" badge: GitHub kills
#: a job at six hours, and anything near that ceiling is holding the concurrency
#: group for no benefit. 320 minutes leaves room to checkpoint and save.
STALE_MINUTES = 320


def stale_runs(
    token: str,
    repo: str,
    *,
    workflow: str = "auth-and-run.yml",
    branch: str | None = None,
    now: float | None = None,
    api: str = API,
    timeout: float = 20.0,
) -> list[dict]:
    """Runs that are still "in progress" long past the point of being useful."""
    import time as _time

    stamp = _time.time() if now is None else now
    url = f"{api}/repos/{repo}/actions/runs?per_page=50"
    if branch:
        url += f"&branch={branch}"
    status, body = _request("GET", url, token, timeout=timeout)
    if status != 200:
        raise RuntimeError(f"list runs failed: HTTP {status} {body[:200]}")
    runs = json.loads(body).get("workflow_runs") or []
    stale: list[dict] = []
    for run in runs:
        if str(run.get("status")) not in {"in_progress"}:
            continue
        if workflow and not str(run.get("path", "")).endswith(workflow):
            continue
        started = str(run.get("run_started_at") or run.get("created_at") or "")
        try:
            began = datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if stamp - began > STALE_MINUTES * 60:
            stale.append(run)
    return stale


def reap(
    token: str,
    repo: str,
    *,
    workflow: str = "auth-and-run.yml",
    branch: str | None = None,
    api: str = API,
    dry_run: bool = False,
) -> list[str]:
    """Cancel the stale runs. Returns the ids it stopped (or would stop)."""
    ids: list[str] = []
    for run in stale_runs(token, repo, workflow=workflow, branch=branch, api=api):
        run_id = str(run.get("id"))
        ids.append(run_id)
        if dry_run:
            continue
        _request("POST", f"{api}/repos/{repo}/actions/runs/{run_id}/force-cancel", token)
    return ids


def ensure_successor(
    token: str,
    repo: str,
    ref: str,
    workflow: str = "auth-and-run.yml",
    *,
    self_run_id: str = "",
    api: str = API,
    dry_run: bool = False,
) -> dict:
    """Queue a successor unless one is already waiting. Returns a report dict."""
    report: dict = {"workflow": workflow, "ref": ref, "action": "", "live": [], "status": None}

    try:
        runs = live_runs(token, repo, branch=ref, workflow=workflow, api=api)
    except RuntimeError as exc:
        report["action"] = "error"
        report["detail"] = str(exc)
        return report

    others = [run for run in runs if str(run.get("id")) != str(self_run_id)]
    report["live"] = [
        {"id": run.get("id"), "status": run.get("status"), "event": run.get("event")} for run in others
    ]
    if others:
        report["action"] = "already-covered"
        return report

    if dry_run:
        report["action"] = "would-dispatch"
        return report

    status, body = _request(
        "POST",
        f"{api}/repos/{repo}/actions/workflows/{workflow}/dispatches",
        token,
        {"ref": ref},
    )
    report["status"] = status
    if status in (204, 201, 200):
        report["action"] = "dispatched"
    else:
        report["action"] = "error"
        report["detail"] = f"HTTP {status} {body[:200]}"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="queue the next bot run")
    parser.add_argument("--workflow", default=os.environ.get("HANDOVER_WORKFLOW", "auth-and-run.yml"))
    parser.add_argument("--dry-run", action="store_true", help="say what would happen")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument(
        "--reap",
        action="store_true",
        help=f"cancel runs stuck in progress for more than {STALE_MINUTES} minutes first",
    )
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    ref = os.environ.get("GITHUB_REF_NAME", "").strip()
    self_run = os.environ.get("GITHUB_RUN_ID", "").strip()
    missing = [
        name
        for name, value in (("GITHUB_TOKEN", token), ("GITHUB_REPOSITORY", repo), ("GITHUB_REF_NAME", ref))
        if not value
    ]
    if missing:
        print(f"handover: not in a GitHub Actions context ({', '.join(missing)} unset)", file=sys.stderr)
        return 1

    if args.reap:
        # A wedged run holds the concurrency group: no successor can start while
        # it sits there, so it is stopped before the queue is considered.
        try:
            stopped = reap(
                token, repo, workflow=args.workflow, branch=ref, dry_run=args.dry_run
            )
        except RuntimeError as exc:
            print(f"handover: reap failed — {exc}", file=sys.stderr)
            stopped = []
        if stopped:
            print(f"handover: stopped {len(stopped)} stale run(s): {', '.join(stopped)}")
            if not args.dry_run:
                import time as _time

                _time.sleep(30)  # let GitHub clear the group before refilling it

    report = ensure_successor(
        token, repo, ref, args.workflow, self_run_id=self_run, dry_run=args.dry_run
    )
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        action = report["action"]
        if action == "already-covered":
            print(f"handover: {len(report['live'])} run(s) already queued or running; nothing to do")
        elif action == "dispatched":
            print(f"handover: queued {args.workflow} on {ref}")
        elif action == "would-dispatch":
            print(f"handover: would queue {args.workflow} on {ref}")
        else:
            print(f"handover: FAILED — {report.get('detail', action)}", file=sys.stderr)
    return 0 if report["action"] in {"already-covered", "dispatched", "would-dispatch"} else 1


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
