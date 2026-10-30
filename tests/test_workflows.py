"""The GitHub workflows are code too: they must parse and stay honest.

A broken workflow is invisible until someone needs it in a hurry, so the cheap
checks live here: the YAML parses, the triggers exist, secrets are referenced
only through ``${{ secrets.* }}``, and no step echoes a credential.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"

EXPECTED = {"ci.yml", "telegram-login.yml", "telegram-run.yml", "auth-and-run.yml", "supervisor.yml"}


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def test_expected_workflows_exist() -> None:
    assert EXPECTED <= {path.name for path in WORKFLOWS.glob("*.yml")}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_workflow_structure(name: str) -> None:
    data = _load(name)
    assert data["name"]
    assert data[True] or data.get("on"), f"{name} has no trigger"
    jobs = data["jobs"]
    assert jobs, f"{name} has no jobs"
    for job_name, job in jobs.items():
        assert job["runs-on"], job_name
        steps = job["steps"]
        assert any("uses" in step or "run" in step for step in steps), job_name


def test_login_workflow_is_manual_only() -> None:
    trigger = _load("telegram-login.yml")[True]
    assert list(trigger) == ["workflow_dispatch"]
    phases = trigger["workflow_dispatch"]["inputs"]["phase"]["options"]
    assert phases == ["request", "verify"]


def test_run_workflow_is_time_bounded() -> None:
    job = _load("telegram-run.yml")["jobs"]["run"]
    # GitHub kills a job at six hours; the job must not be scheduled for longer.
    assert job["timeout-minutes"] <= 360
    script = "\n".join(step.get("run", "") for step in job["steps"])
    assert "havoc run" in script
    assert "timeout" in script


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_no_secret_is_printed(name: str) -> None:
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("echo", "printf", "run:")):
            continue
        assert not re.search(r"\$\{\{\s*secrets\.", stripped), f"{name}: {stripped}"


def test_ci_runs_the_documented_checks() -> None:
    steps = _load("ci.yml")["jobs"]["test"]["steps"]
    commands = "\n".join(step.get("run", "") for step in steps)
    assert "pytest" in commands
    assert "scripts/check_locales.py" in commands
    assert "scripts/generate_docs.py --check" in commands
    lint = "\n".join(step.get("run", "") for step in _load("ci.yml")["jobs"]["lint"]["steps"])
    assert "ruff" in lint


def test_login_phases_share_the_session_file() -> None:
    """Telegram binds a login code to the auth key that requested it.

    If the second phase started from a fresh session file, ``sign_in`` would be
    rejected as an expired code — the two phases must share one session path and
    the artifact must carry it along with the challenge.
    """
    job = _load("telegram-login.yml")["jobs"]["login"]
    paths = {}
    for step in job["steps"]:
        env = step.get("env") or {}
        if isinstance(env, dict) and "SESSION_PATH" in env:
            paths[step["name"]] = env["SESSION_PATH"]
    assert len(paths) == 2, paths
    assert len(set(paths.values())) == 1, paths

    upload = next(
        step for step in job["steps"] if step.get("name") == "Keep the challenge for the next step"
    )
    assert upload["with"]["path"] == ".login"
    download = next(
        step for step in job["steps"] if step.get("name") == "Restore the pending challenge"
    )
    assert download["with"] == {"name": "login-state", "path": ".login"}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_every_run_block_is_valid_shell(name: str) -> None:
    """A typo in a workflow only shows up when someone needs it in a hurry."""
    import shutil
    import subprocess
    import tempfile

    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover - Windows runners
        pytest.skip("bash not available")

    data = _load(name)
    for job in data["jobs"].values():
        for step in job["steps"]:
            script = step.get("run")
            if not script:
                continue
            # GitHub expressions are not shell: neutralise them before parsing.
            cleaned = re.sub(r"\$\{\{[^}]*\}\}", "EXPR", script)
            with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
                handle.write(cleaned)
                path = handle.name
            result = subprocess.run([bash, "-n", path], capture_output=True, text=True)
            assert result.returncode == 0, f"{name} / {step.get('name')}: {result.stderr}"


def test_credentials_can_be_passed_as_run_inputs() -> None:
    """Trying a candidate api_id must not require a secret or a commit."""
    data = _load("telegram-login.yml")
    inputs = data[True]["workflow_dispatch"]["inputs"]
    for name in ("api_id", "api_hash", "phone"):
        assert name in inputs, name
        assert inputs[name]["required"] is False

    # Every step that needs credentials prefers the input and falls back to the secret.
    for step in data["jobs"]["login"]["steps"]:
        env = step.get("env")
        if not isinstance(env, dict):
            continue
        for key, var in (("API_ID", "api_id"), ("API_HASH", "api_hash"), ("PHONE", "phone")):
            if key in env:
                assert env[key] == f"${{{{ inputs.{var} || secrets.{key} }}}}", (step["name"], key)


def test_missing_credentials_message_names_the_form() -> None:
    job = _load("telegram-login.yml")["jobs"]["login"]
    step = next(s for s in job["steps"] if s.get("name") == "Check credentials are configured")
    script = step["run"]
    assert "Run workflow" in script
    # The check must accept inputs as well as secrets.
    for var in ("INPUT_API_ID", "SECRET_API_ID", "INPUT_PHONE", "SECRET_PHONE"):
        assert var in step["env"]


# ---------------------------------------------------------------------------
# auth-and-run: the one-run login that survives GitHub's job ceiling
# ---------------------------------------------------------------------------
def test_auth_and_run_is_triggerable_without_dispatch_permission() -> None:
    """A push must be enough: workflow_dispatch needs a permission this token lacks."""
    trigger = _load("auth-and-run.yml")[True]
    assert "push" in trigger
    paths = trigger["push"]["paths"]
    assert ".auth/config.json" in paths
    assert ".github/workflows/auth-and-run.yml" in paths


def test_delivering_the_code_does_not_start_a_second_run() -> None:
    """The waiting job polls for the code; a trigger would race it with a cold login."""
    paths = _load("auth-and-run.yml")[True]["push"]["paths"]
    assert ".auth/code.txt" not in paths


def test_auth_and_run_restarts_on_a_schedule() -> None:
    trigger = _load("auth-and-run.yml")[True]
    assert any("cron" in item for item in trigger["schedule"])
    job = _load("auth-and-run.yml")["jobs"]["login"]
    # The job must fit under GitHub's six-hour ceiling with room for the login.
    assert job["timeout-minutes"] <= 360


def test_the_code_is_polled_from_the_remote() -> None:
    job = _load("auth-and-run.yml")["jobs"]["login"]
    step = next(s for s in job["steps"] if s.get("name") == "Wait for the code, then log in")
    script = step["run"]
    assert "git fetch" in script
    assert ".auth/code.txt" in script
    # A missing code is a warning, not a failed job: the user may simply be slow.
    assert "exit 0" in script


def test_the_code_and_phone_are_masked_in_logs() -> None:
    job = _load("auth-and-run.yml")["jobs"]["login"]
    scripts = "\n".join(step.get("run", "") for step in job["steps"])
    assert "::add-mask::$code" in scripts
    assert "::add-mask::$phone" in scripts


def test_the_session_is_cached_and_never_committed() -> None:
    text = (WORKFLOWS / "auth-and-run.yml").read_text(encoding="utf-8")
    job = _load("auth-and-run.yml")["jobs"]["login"]
    cache = next(step for step in job["steps"] if str(step.get("uses", "")).startswith("actions/cache"))
    assert cache["with"]["path"] == ".auth/session.txt"
    # Nothing may git-add the session file.
    for step in job["steps"]:
        script = step.get("run") or ""
        assert "git add .auth/session" not in script
        assert "add .auth/session" not in script
    assert "chmod 600 .auth/session.txt" in text


def test_housekeeping_cannot_skip_the_bot() -> None:
    """GitHub implicitly ANDs a step's `if` with success().

    A failing cleanup step therefore skipped the bot step in the first live run,
    which is exactly the expensive mistake this test locks out.
    """
    job = _load("auth-and-run.yml")["jobs"]["login"]
    by_name = {step.get("name"): step for step in job["steps"]}

    cleanup = by_name["Clear the used code"]
    assert cleanup["continue-on-error"] is True
    assert str(cleanup["if"]).startswith("always()")

    run = by_name["Run the userbot"]
    assert str(run["if"]).startswith("always()")

    save = by_name["Save the session for later runs"]
    assert str(save["if"]).startswith("always()")
    assert save["uses"] == "actions/cache/save@v4"


def test_cleanup_pushes_to_an_explicit_branch() -> None:
    """actions/checkout leaves a detached HEAD, so a bare `git push` fails."""
    job = _load("auth-and-run.yml")["jobs"]["login"]
    script = next(s for s in job["steps"] if s.get("name") == "Clear the used code")["run"]
    assert "git push" in script
    assert "refs/heads/" in script


# ---------------------------------------------------------------------------
# The deploy loop: change code, push, bot runs the new code, no re-login
# ---------------------------------------------------------------------------
def test_a_code_push_triggers_a_deploy() -> None:
    paths = _load("auth-and-run.yml")[True]["push"]["paths"]
    assert "havoc/**" in paths
    assert ".auth/config.json" in paths


def test_docs_and_tests_do_not_restart_the_bot() -> None:
    """Restarting a live userbot costs uptime; prose should not do that."""
    paths = _load("auth-and-run.yml")[True]["push"]["paths"]
    assert "docs/**" not in paths
    assert "tests/**" not in paths
    assert "README.md" not in paths


def test_a_deploy_queues_instead_of_cancelling_the_running_bot() -> None:
    """Cancelling on every push is how the bot was left dead.

    A push used to cancel the running job; if the replacement then failed there
    was nothing running and nothing queued. Now the push waits, and the running
    bot stands itself down only once it has confirmed the replacement exists
    (see ``test_the_bot_only_stands_down_for_a_run_that_exists``).
    """
    assert _load("auth-and-run.yml")["concurrency"]["cancel-in-progress"] is False


def test_the_restored_session_skips_the_login() -> None:
    """Iterating must never require the user to type another login code."""
    job = _load("auth-and-run.yml")["jobs"]["login"]
    steps = {step.get("name"): step for step in job["steps"]}

    restore = steps["Restore a previous session"]
    assert restore["with"]["path"] == ".auth/session.txt"
    # ``restore-keys`` is a block scalar (a string), not a list.
    assert "havoc-session-" in str(restore["with"]["restore-keys"])

    # Both login steps are skipped when a session was restored.
    assert "have_session != 'yes'" in str(steps["Request a login code"]["if"])
    assert "have_session != 'yes'" in str(steps["Wait for the code, then log in"]["if"])
    # ...and the bot still starts.
    assert "have_session == 'yes'" in str(steps["Run the userbot"]["if"])


def test_every_deploy_announces_its_commit() -> None:
    job = _load("auth-and-run.yml")["jobs"]["login"]
    step = next(s for s in job["steps"] if s.get("name") == "Announce the deploy")
    assert "GITHUB_SHA" in step["run"]


def test_the_bot_steps_aside_for_a_newer_deploy() -> None:
    """GitHub will not start the queued run while this one holds the group.

    Cancellation is not available to every token (this one gets a 403), so the
    running bot has to notice a newer commit on its own and exit.
    """
    job = _load("auth-and-run.yml")["jobs"]["login"]
    script = next(s for s in job["steps"] if s.get("name") == "Run the userbot")["run"]

    assert "kill -0" in script            # liveness check on the bot process
    assert "git fetch" in script          # watch the branch
    assert "GITHUB_SHA" in script         # compare against the deployed commit
    assert "kill -INT" in script          # stop gracefully, not with SIGKILL


def test_only_deployable_changes_trigger_the_handover() -> None:
    """A docs push must not make the running bot step aside."""
    job = _load("auth-and-run.yml")["jobs"]["login"]
    script = next(s for s in job["steps"] if s.get("name") == "Run the userbot")["run"]
    assert "havoc/" in script
    assert "pyproject" in script
    assert "docs/" not in script


def test_the_bot_is_started_in_the_background_so_it_can_be_watched() -> None:
    job = _load("auth-and-run.yml")["jobs"]["login"]
    script = next(s for s in job["steps"] if s.get("name") == "Run the userbot")["run"]
    assert "python -m havoc run &" in script
    assert "wait \"$bot\"" in script


def test_the_database_survives_a_deploy() -> None:
    """Every deploy restarts the process; reminders and bookmarks must not vanish."""
    job = _load("auth-and-run.yml")["jobs"]["login"]
    steps = job["steps"]
    names = [step.get("name") for step in steps]

    restore = steps[names.index("Restore the database")]
    assert restore["with"]["path"] == ".runtime/data"
    assert "havoc-data-" in str(restore["with"]["restore-keys"])

    save = steps[names.index("Save the database")]
    assert save["uses"] == "actions/cache/save@v4"
    assert str(save["if"]).startswith("always()")

    # Restore before the bot starts, save after it stops.
    assert names.index("Restore the database") < names.index("Run the userbot")
    assert names.index("Save the database") > names.index("Run the userbot")


def test_the_wal_is_folded_in_before_saving_the_cache() -> None:
    """Caching a database plus a live WAL is fragile; caching a checkpointed one is not."""
    job = _load("auth-and-run.yml")["jobs"]["login"]
    names = [step.get("name") for step in job["steps"]]

    assert "Fold the write-ahead log into the database" in names
    assert "wal_checkpoint(TRUNCATE)" in "\n".join(
        step.get("run", "") for step in job["steps"]
    )
    assert names.index("Fold the write-ahead log into the database") < names.index(
        "Save the database"
    )
    # And it must still run when the bot step failed or was cancelled.
    checkpoint = job["steps"][names.index("Fold the write-ahead log into the database")]
    assert str(checkpoint["if"]).startswith("always()")


def test_a_boot_reports_what_the_database_contains() -> None:
    """Data loss has to be visible: every boot reports the restored contents."""
    job = _load("auth-and-run.yml")["jobs"]["login"]
    step = next(s for s in job["steps"] if s.get("name") == "What the database contains")
    assert "havoc data" in step["run"]
    # The boot counter is the only thing that proves persistence across runs,
    # because it is the one value that can only grow if the cache round-trips.
    assert "--boot-probe" in step["run"]


def test_a_failed_data_read_does_not_stop_the_bot() -> None:
    job = _load("auth-and-run.yml")["jobs"]["login"]
    step = next(s for s in job["steps"] if s.get("name") == "What the database contains")
    assert "exit 0" in step["run"]
    assert "::warning::" in step["run"]



def test_the_data_status_is_published_to_the_repository() -> None:
    """The boot counter is only evidence if it leaves the runner."""
    job = _load("auth-and-run.yml")["jobs"]["login"]
    names = [step.get("name") for step in job["steps"]]
    assert "Record the data status" in names
    step = job["steps"][names.index("Record the data status")]
    assert "--json-out data-status.json" in "\n".join(
        s.get("run", "") for s in job["steps"]
    )
    assert "git push" in step["run"]
    # A push from inside the job must not start a deploy of its own.
    assert "[skip ci]" in step["run"]
    # Failing to publish is cosmetic; it must never fail the run.
    assert step["continue-on-error"] is True


def test_the_bot_only_stands_down_for_a_run_that_exists() -> None:
    """Losing a bot because a push started no run is the worst failure mode."""
    job = _load("auth-and-run.yml")["jobs"]["login"]
    step = next(s for s in job["steps"] if s.get("name") == "Run the userbot")
    script = step["run"]

    assert "replacement_queued" in script
    # It asks GitHub which runs exist for the newer commit.
    assert "/actions/runs?head_sha=" in script
    # Standing down is conditional on that answer being yes.
    handover = script.index("kill -INT")
    assert script.index("confirmed") < handover
    assert 'confirmed="yes"' in script
    # And an unconfirmed deploy leaves the bot running: the guard sits before
    # the only place that stops it.
    guard = script.index("no run is queued for it; staying up")
    assert guard < handover
    assert "continue" in script[guard:handover]


def test_asking_github_about_runs_and_queuing_one_needs_permission() -> None:
    workflow = _load("auth-and-run.yml")
    # Write, because the run dispatches its own successor, not just reads.
    assert workflow["permissions"].get("actions") == "write"
    # Still only writing what it already writes.
    assert workflow["permissions"].get("contents") == "write"


def test_every_step_that_touches_data_uses_the_same_paths() -> None:
    """A probe on the default path reads nothing and reports a first boot."""
    job = _load("auth-and-run.yml")["jobs"]["login"]
    for step in job["steps"]:
        run = step.get("run", "")
        if "havoc data" not in run:
            continue
        env = step.get("env") or {}
        for name in ("DATA_DIR", "DATABASE_PATH", "LOG_DIR"):
            assert name in env, f"{step.get('name')} does not set {name}"


def test_a_missing_status_report_is_reported() -> None:
    job = _load("auth-and-run.yml")["jobs"]["login"]
    step = next(s for s in job["steps"] if s.get("name") == "Record the data status")
    assert "::warning::No data-status.json" in step["run"]


def test_the_running_bot_can_publish_snapshots() -> None:
    """The cache is a cache; the bot also needs a token to reach the API."""
    job = _load("auth-and-run.yml")["jobs"]["login"]
    step = next(s for s in job["steps"] if s.get("name") == "Run the userbot")
    env = step["env"]
    assert env["HAVOC_SNAPSHOT_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
    assert env["HAVOC_SNAPSHOT_MINUTES"].isdigit()
    # Writing a file through the contents API needs contents:write, which the
    # workflow already grants.
    assert _load("auth-and-run.yml")["permissions"]["contents"] == "write"


def test_publishing_the_status_survives_a_moving_branch() -> None:
    """The bot commits snapshots while the job runs, so a plain push is fragile."""
    job = _load("auth-and-run.yml")["jobs"]["login"]
    step = next(s for s in job["steps"] if s.get("name") == "Record the data status")
    run = step["run"]
    assert "git fetch" in run and "git rebase" in run
    assert run.index("git rebase") < run.index("git push")
    assert "for _ in 1 2 3" in run  # it retries instead of giving up silently
