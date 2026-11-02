"""The second Yura: her account, her own workflow, and no shared moving parts.

Two bots in one repository is a wiring problem before it is a code problem. Every
file that a run writes, restores or dispatches is a place where one instance can
quietly take the other's place — the session, the database, the snapshot, the
status file, the handover chain, the concurrency group. Each of those is pinned
here, against the workflow YAML itself, so a later edit that "simplifies" the file
turns this red instead of turning her Yura into a copy of his.

The tests also hold the two things that are deliberately shared: the Telegram app
and the AI block in `.auth/config.json`, so one key and one model serve both.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from havoc import persona
from havoc.config import load_config

ROOT = Path(__file__).resolve().parents[1]
HER = "yura-her.yml"
MAIN = "auth-and-run.yml"
HER_PHONE = "+989309823681"


def workflow(name: str) -> dict:
    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))


def run_scripts(document: dict) -> str:
    """Every ``run:`` block of a workflow, joined — the shell it will execute."""
    job = document["jobs"]["run"] if "run" in document["jobs"] else next(iter(document["jobs"].values()))
    return "\n".join(str(step.get("run") or "") for step in job["steps"])


def everything(document: dict) -> str:
    """The whole file as text — for things that live in keys, caches or env."""
    return yaml.safe_dump(document, sort_keys=False)


def run_env(document: dict) -> str:
    """Every ``env:`` block of a workflow, flattened — the settings a step runs with."""
    job = document["jobs"]["run"] if "run" in document["jobs"] else next(iter(document["jobs"].values()))
    parts = [json.dumps(job.get("env") or {})]
    for step in job["steps"]:
        parts.append(json.dumps(step.get("env") or {}))
    return "\n".join(parts)


def step_named(document: dict, fragment: str) -> dict:
    job = document["jobs"]["run"] if "run" in document["jobs"] else next(iter(document["jobs"].values()))
    for step in job["steps"]:
        if fragment.lower() in str(step.get("name", "")).lower():
            return step
    raise AssertionError(f"no step named like {fragment!r}")


# ---------------------------------------------------------------------------
# The file exists, parses, and is a repeating run
# ---------------------------------------------------------------------------
def test_her_workflow_is_a_valid_repeating_run() -> None:
    document = workflow(HER)
    assert document["name"] == "Her Yura"
    # Repeating: the account lives in short jobs, so each run queues its own
    # successor and the chain never depends on a schedule firing.
    assert "push" in document[True] or "push" in document["on"]
    env = document["jobs"]["run"]["steps"]
    assert any(step.get("env", {}).get("HANDOVER_WORKFLOW") == HER for step in env), (
        "her run never queues its own successor"
    )
    assert "scripts/handover.py --workflow yura-her.yml" in run_scripts(document)
    assert "timeout-minutes" in document["jobs"]["run"]
    assert "timeout --signal=INT" in run_scripts(document)


def test_nothing_in_her_workflow_can_collide_with_his() -> None:
    hers, his = workflow(HER), workflow(MAIN)
    her_scripts, his_scripts = run_scripts(hers), run_scripts(his)

    # Concurrency: two bots, two groups, or one waits six hours for the other.
    assert hers["concurrency"]["group"] == "havoc-her"
    assert his["concurrency"]["group"] != hers["concurrency"]["group"]

    # Session and database caches are separate universes.
    her_text = everything(hers)
    assert "havoc-her-session-" in her_text
    assert "havoc-session-" not in her_text.replace("havoc-her-session-", "")
    assert "havoc-her-data-" in her_text
    assert "havoc-data-" not in her_text.replace("havoc-her-data-", "")

    # Her files, never his.
    assert ".runtime/her/data" in her_text
    assert ".data/her-snapshot.json" in run_env(hers)
    assert "--path her-bot-status.json" in her_scripts
    assert "bot-status.json" not in her_scripts.replace("her-bot-status.json", "")

    # And his run never adopts her session file by accident.
    assert ".auth/her/session.txt" not in his_scripts


def test_both_bots_share_one_app_and_one_brain() -> None:
    """The api credentials and the AI block live in his config, once."""
    her_scripts = run_scripts(workflow(HER))
    assert ".auth/config.json" in her_scripts
    assert '"ai"' in her_scripts or "ai_api_key" in her_scripts
    assert "AI_API_KEY" in everything(workflow(HER))
    # Nothing about her account is a new secret: the phone is a committed file.
    assert ".auth/her/config.json" in her_scripts


# ---------------------------------------------------------------------------
# Who she is
# ---------------------------------------------------------------------------
def test_her_phone_is_the_number_he_gave() -> None:
    document = json.loads((ROOT / ".auth" / "her" / "config.json").read_text(encoding="utf-8"))
    assert document["phone"] == HER_PHONE
    assert document.get("enabled", True) is True
    # The person who paid for the account is still able to fix it for her.
    assert 7542695649 in document["owner_ids"]


def test_she_owns_her_bot_and_he_keeps_his() -> None:
    """The logged-in account is always an owner; the list is who *else* is."""
    from havoc.permissions import Permissions

    class Store:
        async def get_setting(self, *_args, **_kwargs):
            return {}

    config, _problems = load_config(
        {
            "API_ID": "2040",
            "API_HASH": "x",
            "OWNER_IDS": "7542695649",
        }
    )
    permissions = Permissions(config, Store())
    permissions.set_self_id(880)  # her account
    assert permissions.is_owner(880), "the account that runs the bot is not an owner"
    assert permissions.is_owner(7542695649), "he cannot command her bot"


def test_his_bot_learns_her_id_from_her_login() -> None:
    """`.auth/her/owner.json` is written by her login and read by his run."""
    his = run_scripts(workflow(MAIN))
    assert ".auth/her/owner.json" in his
    assert "steps.owners.outputs.owner_ids" in run_env(workflow(MAIN))


# ---------------------------------------------------------------------------
# Her login: ask Telegram, then finish on its own
# ---------------------------------------------------------------------------
def test_her_login_asks_for_a_code_and_finishes_without_a_human_at_the_runner() -> None:
    scripts = run_scripts(workflow(HER))
    assert "python -m havoc login --request --state .auth/her/login-state.json" in scripts
    assert '.auth/her/code.txt' in scripts, "the waiting step does not look for the code"
    assert "--export-session .auth/her/session.txt" in scripts
    assert "TELEGRAM_2FA_PASSWORD" in json.dumps(workflow(HER)), "no way to supply a 2FA password"


def test_only_a_person_asks_for_her_code() -> None:
    """A deploy must never text her: the code is asked for by a dispatch, once."""
    scripts = run_scripts(workflow(HER))
    assert 'if [ -z "$code" ] && [ "${IN_LOGIN:-}" != "true" ] && [ ! -f .auth/her/ASK ]; then' in scripts
    # …and the ask file is spent in the same step, so it cannot fire twice.
    assert "git rm -q --cached .auth/her/ASK" in scripts
    # No marker file decides this any more: a file on the branch could be stale,
    # and a deploy that read it as "ask for a code" would text her every time.
    assert not (ROOT / ".auth" / "her" / "REQUEST").exists(), "a stale request marker came back"


def test_a_run_without_a_session_never_starts_a_repeating_chain() -> None:
    """A successor that cannot sign in would chain itself all night asking for codes."""
    scripts = run_scripts(workflow(HER))
    assert 'if [ ! -s .auth/her/session.txt ] && [ "${IN_LOGIN:-}" != "true" ] && [ ! -f .auth/her/ASK ]; then' in scripts
    assert "nothing is queued" in scripts
    # The session has to be restored before that decision is taken.
    order = [step.get("name") or step.get("uses") for step in workflow(HER)["jobs"]["run"]["steps"]]
    assert order.index("Restore her session") < order.index("Queue the successor")


def test_a_login_code_does_not_stay_in_the_repository() -> None:
    scripts = run_scripts(workflow(HER))
    assert "git rm -q --cached .auth/her/code.txt" in scripts


# ---------------------------------------------------------------------------
# Her prompt says "her"
# ---------------------------------------------------------------------------
def test_the_prompt_grammar_follows_the_owner() -> None:
    assert persona.with_owner_pronoun("be soft with him", "him") == "be soft with him"
    assert persona.with_owner_pronoun("be soft with him", "her") == "be soft with her"
    assert persona.with_owner_pronoun("his own account", "her") == "her own account"
    assert persona.with_owner_pronoun("that is his bot himself", "her") == "that is her bot herself"
    # A word that merely contains "his" is not the owner.
    assert persona.with_owner_pronoun("this history is this", "her") == "this history is this"


def test_her_run_tells_her_bot_which_pronoun_to_use() -> None:
    assert "HAVOC_OWNER_PRONOUN" in run_env(workflow(HER))
    assert "her" in run_env(workflow(HER))


def test_the_pronoun_is_a_config_setting_with_his_default() -> None:
    config, _ = load_config({"API_ID": "2040", "API_HASH": "x"})
    assert config.owner_pronoun == "him"
    hers, _ = load_config({"API_ID": "2040", "API_HASH": "x", "HAVOC_OWNER_PRONOUN": "Her"})
    assert hers.owner_pronoun == "her"


# ---------------------------------------------------------------------------
# The status file, so a quiet bot is visible
# ---------------------------------------------------------------------------
def test_her_status_is_published_separately(runtime) -> None:
    from havoc.features import persona as persona_module

    assert persona_module is not None  # the module that publishes is importable
    assert (ROOT / "scripts" / "publish_status.py").exists()
    scripts = run_scripts(workflow(HER))
    assert "--extra \"account=her\"" in scripts


@pytest.mark.parametrize("name", ["Her Yura", "yura-her.yml"])
def test_the_workflow_is_discoverable_by_name(name: str) -> None:
    text = (ROOT / ".github" / "workflows" / HER).read_text(encoding="utf-8")
    assert name in text


def test_a_stale_code_is_never_redeemed() -> None:
    """A code from an older attempt is dead: it is dropped, and a fresh one asked for."""
    scripts = run_scripts(workflow(HER))
    assert "rm -f .auth/her/code.txt" in scripts
    assert "Ignoring a login code left on the branch" in scripts
    # Only a commit newer than the request may carry the code.
    assert 'base=$(git rev-parse FETCH_HEAD' in scripts
    assert '[ "$remote" != "${base:-}" ]' in scripts


def test_the_login_code_is_removed_from_the_branch_afterwards() -> None:
    """The code file was committed from her phone, so it is not in the checkout:
    the cleanup step has to start from the branch to delete it, not from the tree."""
    scripts = run_scripts(workflow(HER))
    assert "git reset -q --mixed FETCH_HEAD" in scripts
    assert "git ls-files --error-unmatch .auth/her/code.txt" in scripts
    assert "git rm -q --cached .auth/her/code.txt" in scripts
    assert "rm -f .auth/her/code.txt" in scripts


def test_the_login_code_is_never_echoed() -> None:
    scripts = run_scripts(workflow(HER))
    assert 'echo "::add-mask::$code"' in scripts
    # The session string is written to a file and cached, never printed.
    assert "cat .auth/her/session.txt" not in scripts.split("### Logged in")[0]
