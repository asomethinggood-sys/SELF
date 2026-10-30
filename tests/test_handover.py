"""Staying alive: the run that queues its own successor.

The bot dies when its workflow ends, so the only thing that keeps it alive is
another run already waiting. These tests fake GitHub's API and check the three
decisions that matter — dispatch when nothing is alive, do nothing when
something is, never count yourself as a successor — plus the wiring: the run
workflow must actually call this, first, and its concurrency group must queue
instead of cancel.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import handover  # noqa: E402  (path wrinkle: scripts/ is not a package)

WORKFLOWS = ROOT / ".github" / "workflows"


class FakeAPI:
    """Stands in for api.github.com and records what was asked."""

    def __init__(self, runs: list[dict] | None = None, dispatch_status: int = 204) -> None:
        self.runs = runs or []
        self.dispatch_status = dispatch_status
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, method, url, token, payload=None, *, timeout=20.0):
        assert token == "t0ken", "the token must be passed through, not rebuilt"
        self.calls.append((method, url, payload))
        if method == "GET":
            return 200, json.dumps({"workflow_runs": self.runs})
        if method == "POST":
            return self.dispatch_status, "" if self.dispatch_status < 300 else '{"message":"nope"}'
        raise AssertionError(method)

    @property
    def dispatched(self) -> bool:
        return any(method == "POST" for method, _, _ in self.calls)


@pytest.fixture
def api(monkeypatch):
    def install(runs=None, *, dispatch_status: int = 204) -> FakeAPI:
        fake = FakeAPI(runs, dispatch_status)
        monkeypatch.setattr(handover, "_request", fake)
        return fake

    return install


def _run(**overrides) -> dict:
    base = {
        "id": 1,
        "status": "in_progress",
        "event": "push",
        "path": ".github/workflows/auth-and-run.yml",
        "head_branch": "arena/b9a136e7-self",
    }
    base.update(overrides)
    return base


def test_it_queues_a_successor_when_nothing_is_alive(api) -> None:
    fake = api([])
    report = handover.ensure_successor("t0ken", "me/repo", "arena/b9a136e7-self")
    assert report["action"] == "dispatched"
    assert report["status"] == 204
    assert fake.dispatched is True
    method, url, payload = fake.calls[-1]
    assert method == "POST"
    assert url.endswith("/actions/workflows/auth-and-run.yml/dispatches")
    assert payload == {"ref": "arena/b9a136e7-self"}


def test_it_does_nothing_when_a_run_is_already_queued(api) -> None:
    fake = api([_run(id=2, status="queued"), _run(id=3, status="completed")])
    report = handover.ensure_successor("t0ken", "me/repo", "arena/b9a136e7-self")
    assert report["action"] == "already-covered"
    assert fake.dispatched is False
    assert [row["id"] for row in report["live"]] == [2]


@pytest.mark.parametrize(
    "status", ["queued", "in_progress", "requested", "waiting", "pending", "accepted"]
)
def test_every_live_status_counts_as_covered(api, status: str) -> None:
    fake = api([_run(status=status)])
    report = handover.ensure_successor("t0ken", "me/repo", "b")
    assert report["action"] == "already-covered"
    assert fake.dispatched is False


def test_this_run_does_not_count_as_its_own_successor(api) -> None:
    """The run asking the question is in progress by definition."""
    fake = api([_run(id=99, status="in_progress")])
    report = handover.ensure_successor("t0ken", "me/repo", "b", self_run_id="99")
    assert report["action"] == "dispatched"
    assert fake.dispatched is True


def test_only_runs_of_our_own_workflow_cover_us(api) -> None:
    fake = api([_run(id=5, path=".github/workflows/ai-probe.yml")])
    report = handover.ensure_successor("t0ken", "me/repo", "b", workflow="auth-and-run.yml")
    assert report["action"] == "dispatched", "an unrelated workflow must not count"
    assert fake.dispatched is True


def test_a_dry_run_asks_and_does_not_dispatch(api) -> None:
    fake = api([])
    report = handover.ensure_successor("t0ken", "me/repo", "b", dry_run=True)
    assert report["action"] == "would-dispatch"
    assert fake.dispatched is False


def test_a_refused_dispatch_is_reported_not_swallowed(api) -> None:
    api([], dispatch_status=403)
    report = handover.ensure_successor("t0ken", "me/repo", "b")
    assert report["action"] == "error"
    assert "403" in report["detail"]


def test_a_failing_listing_does_not_crash(monkeypatch) -> None:
    monkeypatch.setattr(handover, "_request", lambda *a, **k: (500, "boom"))
    report = handover.ensure_successor("t0ken", "me/repo", "b")
    assert report["action"] == "error"
    assert "500" in report["detail"]


# ---------------------------------------------------------------------------
# The wiring — a helper nothing calls keeps nobody alive
# ---------------------------------------------------------------------------
def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def test_the_run_queues_its_successor_before_it_does_anything_else() -> None:
    steps = _load("auth-and-run.yml")["jobs"]["login"]["steps"]
    # The script has to be on disk, so the checkout comes first; nothing else does.
    assert "actions/checkout" in (steps[0].get("uses") or "")
    assert steps[1].get("name") == "Queue the successor", [s.get("name") for s in steps[:4]]
    first = steps[1]
    assert "scripts/handover.py" in first["run"]
    assert first.get("continue-on-error") is True, "a queue failure must not stop the bot starting"
    assert "GITHUB_TOKEN" in first["env"]


def test_a_push_queues_instead_of_cancelling_the_running_bot() -> None:
    data = _load("auth-and-run.yml")
    assert data["concurrency"]["cancel-in-progress"] is False
    assert data["permissions"]["actions"] == "write"


def test_the_run_keeps_re_asserting_the_successor_while_it_runs() -> None:
    script = "\n".join(step.get("run", "") for step in _load("auth-and-run.yml")["jobs"]["login"]["steps"])
    assert script.count("scripts/handover.py") >= 2, "the queue must be re-checked inside the run loop"
    assert "sleep 60" in script


def test_the_supervisor_targets_a_workflow_that_exists() -> None:
    data = _load("supervisor.yml")
    script = "\n".join(step.get("run", "") for step in data["jobs"]["supervise"]["steps"])
    target = data["jobs"]["supervise"]["steps"][1]["env"]["HANDOVER_WORKFLOW"]
    assert target in {path.name for path in WORKFLOWS.glob("*.yml")}, target
    assert "scripts/handover.py" in script
    assert data["permissions"]["actions"] == "write"


def test_the_supervisor_runs_as_often_as_github_allows() -> None:
    trigger = _load("supervisor.yml")[True]
    crons = [entry["cron"] for entry in trigger["schedule"]]
    assert crons == ["*/5 * * * *"], "five minutes is the fastest GitHub promises"


def test_no_workflow_dispatches_a_workflow_that_is_not_there() -> None:
    existing = {path.name for path in WORKFLOWS.glob("*.yml")}
    for path in WORKFLOWS.glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        for name in existing | {"telegram-run.yml"}:
            if f"workflow run {name}" in text or f"/{name}/dispatches" in text:
                assert name in existing, f"{path.name} dispatches missing {name}"


def test_every_step_that_asks_github_about_runs_has_a_token() -> None:
    """`GITHUB_TOKEN` is a secret, not an automatic variable.

    Without it in a step's own `env`, the curl in the run loop got a 401, the
    answer was "unknown", and the bot never stood down — it sat in front of the
    queued deploy for the whole six hours. Every step that talks to the API must
    carry the token itself.
    """
    job = _load("auth-and-run.yml")["jobs"]["login"]
    for step in job["steps"]:
        script = step.get("run", "")
        if "scripts/handover.py" not in script and "GITHUB_TOKEN" not in script:
            continue
        env = step.get("env") or {}
        assert "GITHUB_TOKEN" in env, step.get("name")
        assert env["GITHUB_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}", step.get("name")


def test_restart_can_actually_clear_a_wedged_run() -> None:
    """A stuck run in front of the concurrency group is a case a queue cannot fix."""
    data = _load("restart.yml")
    job = data["jobs"]["restart"]
    assert data["permissions"]["actions"] == "write"
    steps = {step.get("name"): step for step in job["steps"]}
    cancel = steps["Cancel every live run of the bot"]["run"]
    assert "force-cancel" in cancel
    assert "waiting" in cancel and "in_progress" in cancel
    # It waits for the group to be free before dispatching, or the new run just
    # queues behind the corpse it was meant to replace.
    assert "Wait for the group to be free" in steps
    start = steps["Start a fresh run"]["run"]
    assert "auth-and-run.yml/dispatches" in start
    assert 'ref\"' in start or "${GITHUB_REF_NAME}" in start


def test_restart_does_not_use_the_refusing_chain_helper() -> None:
    """handover.py deliberately refuses when something is alive; a restart must not."""
    steps = _load("restart.yml")["jobs"]["restart"]["steps"]
    start = next(step["run"] for step in steps if step.get("name") == "Start a fresh run")
    # The comment may mention it; the code must not call it.
    code = "\n".join(line for line in start.splitlines() if not line.strip().startswith("#"))
    assert "handover.py" not in code
