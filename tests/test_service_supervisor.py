"""The service supervisor, against a stub GitHub API over a real socket.

`scripts/service_supervisor.py` decides whether to start a service runner. Every
way it can be wrong is expensive: a wrong "yes" starts a six-hour job that
duplicates a live one, a wrong "no" leaves the bot without search or pictures for
hours. So it is tested the way it runs — as a **subprocess**, talking HTTP to a
server, with real `.services/*.json` files on disk — rather than by calling its
functions.

The stub records every request, which is how the scoping is checked: the listing
must be `/repos/<r>/actions/workflows/service-<name>.yml/runs`, never the
repository-wide `/actions/runs` (that listing holds the 50 newest runs of the
whole repo, and four long-lived workflows drop out of that window within hours).
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "service_supervisor.py"


class StubAPI(BaseHTTPRequestHandler):
    """A GitHub Actions API with no opinions of its own."""

    runs: dict[str, list[dict[str, str]]] = {}
    missing: set[str] = set()
    seen: list[tuple[str, str]] = []
    fail_listing: bool = False

    def log_message(self, *args: object) -> None:  # noqa: D102 - silence
        pass

    def _send(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 - http.server's naming
        StubAPI.seen.append(("GET", self.path))
        if StubAPI.fail_listing:
            self._send(500, {"message": "boom"})
            return
        for name in ("search", "comfy", "ollama"):
            if f"service-{name}.yml/runs" in self.path:
                if name in StubAPI.missing:
                    self._send(404, {"message": "Not Found"})
                else:
                    self._send(200, {"workflow_runs": StubAPI.runs.get(name, [])})
                return
        self._send(404, {"message": "no such workflow"})

    def do_POST(self) -> None:  # noqa: N802 - http.server's naming
        StubAPI.seen.append(("POST", self.path))
        self._send(204, {})


@pytest.fixture()
def api():
    StubAPI.runs = {}
    StubAPI.missing = set()
    StubAPI.seen = []
    StubAPI.fail_listing = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), StubAPI)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def publish(root: Path, name: str, *, url: str = "https://x.trycloudflare.com", left: timedelta | None = None) -> None:
    """Write a `.services/<name>.json` as a service would, ``left`` hours before it ends."""
    now = datetime.now(UTC)
    started = now - timedelta(minutes=5)
    expires = now + (left if left is not None else timedelta(hours=5))
    directory = root / ".services"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(
        json.dumps(
            {
                "service": name,
                "url": url,
                "run_id": "1",
                "started_at": iso(started),
                "published_at": iso(started),
                "expires_at": iso(expires),
                "gated": True,
            }
        ),
        encoding="utf-8",
    )


def run(api_url: str, root: Path, *extra: str) -> tuple[dict, subprocess.CompletedProcess]:
    environment = {
        "GITHUB_TOKEN": "stub",
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_API_URL": api_url,
        "GITHUB_REF_NAME": "main",
        "PATH": "/usr/bin:/bin",
    }
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), "--json", *extra],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(ROOT),
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout), done


def posts() -> list[str]:
    return [path for method, path in StubAPI.seen if method == "POST"]


def listings() -> list[str]:
    return [path for method, path in StubAPI.seen if method == "GET"]


def test_a_service_that_never_ran_is_started(api: str, tmp_path: Path) -> None:
    report, _ = run(api, tmp_path)
    assert report["services"]["search"]["started"] is True
    assert sorted(posts()) == [
        "/repos/owner/repo/actions/workflows/service-comfy.yml/dispatches",
        "/repos/owner/repo/actions/workflows/service-ollama.yml/dispatches",
        "/repos/owner/repo/actions/workflows/service-search.yml/dispatches",
    ]


def test_every_listing_is_scoped_to_its_own_workflow(api: str, tmp_path: Path) -> None:
    run(api, tmp_path)
    assert listings(), "nothing was asked"
    for path in listings():
        assert "/actions/workflows/service-" in path, path
        assert path.endswith("/runs?per_page=50"), path


def test_a_healthy_service_is_left_alone(api: str, tmp_path: Path) -> None:
    publish(tmp_path, "search", left=timedelta(hours=5))
    publish(tmp_path, "comfy", left=timedelta(hours=5))
    publish(tmp_path, "ollama", left=timedelta(hours=5))
    report, _ = run(api, tmp_path)
    assert all(not entry["needed"] for entry in report["services"].values()), report
    assert posts() == []
    # And it does not even ask the API: a healthy service costs nothing.
    assert listings() == []


def test_a_service_near_its_ceiling_is_replaced(api: str, tmp_path: Path) -> None:
    publish(tmp_path, "search", left=timedelta(minutes=20))
    publish(tmp_path, "comfy", left=timedelta(hours=5))
    publish(tmp_path, "ollama", left=timedelta(hours=5))
    report, _ = run(api, tmp_path)
    assert report["services"]["search"]["needed"] is True
    assert report["services"]["search"]["started"] is True
    assert posts() == ["/repos/owner/repo/actions/workflows/service-search.yml/dispatches"]


def test_a_live_run_is_not_duplicated(api: str, tmp_path: Path) -> None:
    StubAPI.runs["search"] = [{"status": "in_progress", "created_at": iso(datetime.now(UTC))}]
    report, _ = run(api, tmp_path, "--service", "search")
    assert report["services"]["search"]["started"] is False
    assert report["services"]["search"]["alive"] == 1
    assert posts() == []


def test_a_recent_failed_attempt_is_not_retried_every_five_minutes(api: str, tmp_path: Path) -> None:
    # A run that started ten minutes ago and published nothing: a missing secret,
    # a bad checkpoint. Dispatching again now would be a loop that hides the error.
    StubAPI.runs["search"] = [
        {"status": "completed", "conclusion": "success", "created_at": iso(datetime.now(UTC) - timedelta(minutes=10))}
    ]
    report, _ = run(api, tmp_path, "--service", "search")
    assert report["services"]["search"]["needed"] is True
    assert report["services"]["search"]["started"] is False
    assert "started 10m ago" in report["services"]["search"]["note"], report["services"]["search"]
    assert posts() == []


def test_an_old_failed_attempt_is_retried(api: str, tmp_path: Path) -> None:
    StubAPI.runs["search"] = [
        {"status": "completed", "conclusion": "failure", "created_at": iso(datetime.now(UTC) - timedelta(hours=3))}
    ]
    report, _ = run(api, tmp_path, "--service", "search")
    assert report["services"]["search"]["started"] is True
    assert posts() == ["/repos/owner/repo/actions/workflows/service-search.yml/dispatches"]


def test_a_broken_listing_still_dispatches(api: str, tmp_path: Path) -> None:
    # A rate limit must not mean "assume it is alive" — the concurrency group
    # queues a duplicate behind the live run, which is the cheap failure.
    StubAPI.fail_listing = True
    report, _ = run(api, tmp_path, "--service", "search")
    assert report["services"]["search"]["started"] is True
    assert posts() == ["/repos/owner/repo/actions/workflows/service-search.yml/dispatches"]


def test_dry_run_touches_nothing(api: str, tmp_path: Path) -> None:
    report, _ = run(api, tmp_path, "--dry-run")
    assert all(entry["needed"] for entry in report["services"].values())
    assert posts() == [] and listings() == []
    assert report["started"] == 0


def test_a_service_between_runs_with_no_address_is_restarted(api: str, tmp_path: Path) -> None:
    publish(tmp_path, "search", url="")
    report, _ = run(api, tmp_path, "--service", "search")
    assert report["services"]["search"]["needed"] is True
    assert report["services"]["search"]["started"] is True


def test_the_human_summary_reads_like_a_sentence(api: str, tmp_path: Path) -> None:
    environment = {
        "GITHUB_TOKEN": "stub",
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_API_URL": api,
        "GITHUB_REF_NAME": "main",
        "PATH": "/usr/bin:/bin",
    }
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path), "--service", "search"],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(ROOT),
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert "search:" in done.stdout
    assert "Traceback" not in done.stdout + done.stderr
