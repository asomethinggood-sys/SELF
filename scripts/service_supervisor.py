#!/usr/bin/env python3
"""Start any hosted service whose address file is missing or out of date.

Four things have to be running at once for the bot to have search, pictures and a
model: itself, and one runner each for `search`, `comfy` and `ollama`. The bot
queues its own successor in the first step of every run; the services do the same
for themselves. What neither can see is *the others*: if the search runner dies
at minute 90 and its own last-gasp dispatch fails (a rate limit, an API blip, a
cancelled run), it is simply gone until somebody notices.

This is the thing that notices. It reads `.services/<name>.json`, which each
service writes when it comes up (`scripts/service_host.py`), and dispatches the
workflow of any service whose file is missing, expired, or was never published.
It is cheap: no address file means one `gh`-style API call, and nothing at all
when everything is healthy.

Run it from the bot's workflow at start-up, and from the supervisor every five
minutes. `--json` prints the decision so a log can say what happened.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from havoc.services.service_directory import ServiceDirectory  # noqa: E402
from havoc.services.service_token import SERVICES  # noqa: E402

# GitHub Enterprise (and this repo's own probes) point elsewhere; the default is
# the public API. Overridable so the supervisor can be run against a stub.
API = "https://api.github.com"


def api_root() -> str:
    return (os.environ.get("GITHUB_API_URL") or API).rstrip("/")
# A service publishes the moment it is up, and every run lasts ~5h45m against a
# 6h ceiling. Anything under an hour left is close enough to the end that a fresh
# run should be queued now rather than after the tunnel drops.
RESTART_WITHIN = timedelta(hours=1)
#: Do not start another run of a workflow that started one recently. A service
#: that is failing (a missing secret, a bad checkpoint) must not turn into a
#: dispatch every five minutes: that burns the queue and hides the real error.
RETRY_AFTER = timedelta(minutes=45)
# The ceiling every service workflow runs with, minus the publish overhead.
MAX_RUN = timedelta(hours=6)

WORKFLOW = "service-{name}.yml"


def api(method: str, path: str, token: str, payload: dict[str, Any] | None = None) -> tuple[int, Any]:
    url = f"{api_root()}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read()
            return response.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as error:
        body = error.read()
        try:
            parsed = json.loads(body) if body.strip() else {}
        except json.JSONDecodeError:
            parsed = {"raw": body.decode("utf-8", "replace")[:400]}
        return error.code, parsed
    except (urllib.error.URLError, TimeoutError) as error:
        return 0, {"error": str(error)}


def workflow_runs(repo: str, name: str, token: str) -> tuple[int, float | None, str]:
    """``(live, newest_created_at, error)`` for one workflow.

    Scoped by workflow file on purpose: `/actions/runs` is the 50 most recent runs
    of the *whole repo*, and with four long-lived workflows on a branch a service
    drifts out of that window within hours. Reading it unscoped would say "nothing
    alive" about something that is very much alive, and queue a second copy — the
    exact bug `scripts/handover.py` had.
    """
    path = f"/repos/{repo}/actions/workflows/{WORKFLOW.format(name=name)}/runs?per_page=50"
    status, payload = api("GET", path, token)
    if status == 404:
        # The file has never run on this branch: there is no live run, by
        # definition, and dispatch will start the first one.
        return 0, None, ""
    if status != 200 or not isinstance(payload, dict):
        return -1, None, f"HTTP {status}"
    runs = payload.get("workflow_runs") or []
    live = sum(
        1
        for run in runs
        if str(run.get("status")) in {"queued", "in_progress", "waiting", "requested", "pending"}
    )
    newest = None
    for run in runs:
        created = stamp(str(run.get("created_at") or ""))
        if created is not None and (newest is None or created.timestamp() > newest):
            newest = created.timestamp()
    return live, newest, ""


def dispatch(repo: str, name: str, ref: str, token: str) -> tuple[bool, str]:
    path = f"/repos/{repo}/actions/workflows/{WORKFLOW.format(name=name)}/dispatches"
    status, payload = api("POST", path, token, {"ref": ref})
    if status in {201, 204}:
        return True, "dispatched"
    reason = payload.get("message") if isinstance(payload, dict) else None
    return False, f"HTTP {status}{': ' + reason if reason else ''}"


def stamp(value: str) -> datetime | None:
    # ISO-8601, and never naive: comparing a naive stamp to an aware ``now``
    # raises TypeError, which would turn "the file is old" into a crash.
    """`.services/<name>.json` carries ISO-8601 strings; ``None`` when unreadable."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def decide(directory: ServiceDirectory, name: str, now: datetime) -> tuple[bool, str]:
    """Do we need to start this service, and why (in one clause).

    This reads the *published facts*, not the bot's usability state. The bot's
    state also covers "I cannot use it because my own secret is missing", which is
    a configuration problem here — starting a fresh runner would not fix it and
    would cost a slot for six hours.
    """
    body = directory.raw(name)
    if not body:
        return True, "it has never published an address"
    if not str(body.get("url") or "").strip():
        return True, f"its last run published no address ({body.get('note') or 'the tunnel did not open'})"
    expires = stamp(str(body.get("expires_at") or ""))
    started = stamp(str(body.get("started_at") or ""))
    if expires is not None:
        left = expires - now
        if left <= RESTART_WITHIN:
            return True, f"its window ended {describe(left)}" if left.total_seconds() < 0 else f"its window ends in {describe(left)}"
        return False, f"up, {describe(left)} of window left"
    if started is not None and now - started > MAX_RUN:
        return True, f"it started {describe(now - started)} ago and never said when it ends"
    return False, "up"


def describe(delta: timedelta) -> str:
    signed = int(delta.total_seconds())
    sign = "ago" if signed < 0 else ""
    seconds = abs(signed)
    if seconds < 90:
        text = f"{seconds}s"
    elif seconds < 5400:
        text = f"{seconds // 60}m"
    else:
        text = f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{text} {sign}".strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--service", action="append", choices=list(SERVICES), help="limit to one service (repeatable)")
    parser.add_argument("--ref", default="", help="branch to run on (default: this checkout's branch)")
    parser.add_argument("--dry-run", action="store_true", help="say what would happen, dispatch nothing")
    parser.add_argument("--json", action="store_true", help="print the decision as JSON")
    parser.add_argument("--wait", action="store_true", help="wait for a dispatched run to appear")
    parser.add_argument("--root", default="", help="where .services/ lives (default: this checkout)")
    parser.add_argument("--no-fetch", action="store_true", help="do not refresh .services from the branch first")
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    ref = args.ref or os.environ.get("GITHUB_REF_NAME", "") or "main"
    now = datetime.now(UTC)
    # The directory counts in epoch seconds; the report wants a readable stamp.
    root = Path(args.root).resolve() if args.root else ROOT
    directory = ServiceDirectory(root=root, branch=ref, now=lambda: now.timestamp())
    if not args.no_fetch:
        # The checkout this runs in may be minutes old (the supervisor fires every
        # five minutes, the checkout is from whenever the job started). Reading a
        # stale file would say "expired" about a service that has just published.
        directory.pull_now(force=True)

    wanted = args.service or list(SERVICES)
    report: dict[str, Any] = {"checked_at": now.isoformat(timespec="seconds"), "ref": ref, "services": {}}
    started = 0

    for name in wanted:
        need, why = decide(directory, name, now)
        entry: dict[str, Any] = {"needed": need, "reason": why}
        if need and not args.dry_run:
            if not token or not repo:
                entry.update(started=False, error="GITHUB_TOKEN / GITHUB_REPOSITORY are not set")
                print(f"{name}: needs a run ({why}) but no token/repo — cannot dispatch", file=sys.stderr)
                report["services"][name] = entry
                continue
            alive, newest, listing_error = workflow_runs(repo, name, token)
            recent = newest is not None and (now - datetime.fromtimestamp(newest, UTC)) < RETRY_AFTER
            if alive > 0:
                entry.update(started=False, alive=alive, note="a run is already queued or in progress")
            elif recent:
                # Something tried recently and did not publish. Dispatching again
                # now would be a loop, and the loop would hide the reason.
                entry.update(
                    started=False,
                    note=(
                        "a run started "
                        f"{describe(now - datetime.fromtimestamp(newest, UTC))} ago and has not published; "
                        "not starting another for a while"
                    ),
                    retry_after="a run started recently",
                )
            elif alive < 0:
                # Listing failed (rate limit, permissions). Dispatch anyway: a
                # queued duplicate is cheap and queued behind the concurrency
                # group, which is this workflow's whole design.
                ok, detail = dispatch(repo, name, ref, token)
                entry.update(started=ok, dispatched=detail, listing=listing_error)
                if not ok:
                    entry["error"] = detail
            else:
                ok, detail = dispatch(repo, name, ref, token)
                entry.update(started=ok, dispatched=detail)
                if not ok:
                    entry["error"] = detail
            if entry.get("started"):
                started += 1
        report["services"][name] = entry

    report["started"] = started
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for name, entry in report["services"].items():
            mark = "→" if entry.get("started") else ("…" if entry["needed"] else "✓")
            detail = entry.get("error") or entry.get("note") or entry.get("dispatched") or entry["reason"]
            print(f" {mark} {name}: {detail}")
        print(f"started {started} service(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
