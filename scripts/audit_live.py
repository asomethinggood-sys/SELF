#!/usr/bin/env python3
"""Walk the whole project and report what is broken — by running it.

Static checks miss the bugs the owner actually feels: a command that answers with
its own locale key, a feature that cannot be switched on, a handler that throws on
an empty argument, a locale string that exists in one language and not the other.
So this script *builds the runtime*, enables every feature, and then dispatches
every command in the registry with no arguments, with ``--help``, and with a set
of plausible arguments, recording what came back.

It writes ``.audit.json`` and prints a short summary. Exit code is 1 when
anything failed, so CI can hold the line.

Usage:
    python scripts/audit_live.py [--json PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging
import pkgutil
import re
import sys
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

#: Output that means a bug, whatever the command was trying to do.
BAD_PATTERNS = {
    "locale_key_leak": re.compile(r"^(?:\W*)([a-z][a-z0-9_]+(?:\.[a-z0-9_]+){1,})(?:\W*)$"),
    "unformatted_placeholder": re.compile(r"\{[a-z_][a-z0-9_]*\}"),
    "python_none": re.compile(r"\bNone\b|\bTrue\b|\bFalse\b"),
    "traceback": re.compile(r"Traceback \(most recent call last\)|ZeroDivisionError|KeyError|AttributeError|TypeError|NameError|IndexError|ValueError"),
    "double_escaped": re.compile(r"&amp;(?:lt|gt|amp|quot);"),
}

#: A few commands want an argument shaped like something specific. Anything not
#: listed still gets the general battery (nothing, `--help`, `yes`).
EXTRA_ARGS: dict[str, tuple[str, ...]] = {
    "remember": ("the bridge is on the north side",),
    "note": ("the bridge is on the north side",),
    "forget": ("the bridge",),
    "remind": ("1h drink water",),
    "translate": ("hello",),
    "lang": ("en",),
    "language": ("en",),
    "prefix": ("!",),
    "clock": ("status",),
    "fal": ("page 166",),
    "poem": ("hafez",),
    "weather": ("Berlin",),
    "feature": ("poetry on",),
    "yura": ("status",),
    "profile": ("show",),
    "help": ("clock",),
}


def _head(text: str, limit: int = 300) -> str:
    flat = " ".join(str(text or "").split())
    return flat[:limit]


def _verdicts(text: str) -> list[str]:
    findings: list[str] = []
    stripped = str(text or "").strip()
    if not stripped:
        findings.append("empty_reply")
        return findings
    if stripped.lower().startswith("error:") or "not implemented" in stripped.lower():
        findings.append("error_sentence")
    for name, pattern in BAD_PATTERNS.items():
        if name == "locale_key_leak":
            for line in stripped.splitlines():
                candidate = line.strip().strip("*_`").strip()
                if pattern.match(candidate) and not candidate.startswith((".", "/")):
                    findings.append(f"{name}:{candidate[:60]}")
                    break
        elif pattern.search(stripped):
            findings.append(name)
    return findings


async def _runtime(tmp: Path, logger: logging.Logger):
    from conftest import FakeTelegram, make_config  # type: ignore

    from havoc.runtime import Runtime

    config = make_config(tmp)
    runtime = await Runtime.create(config, logger)
    runtime.telegram = FakeTelegram()
    runtime.client = runtime.telegram.client
    runtime.sender.client = runtime.client
    runtime.permissions.set_self_id(7542695649)
    runtime.health.mark_connected()
    runtime.load_features()
    # Not dry-run: a dry run swallows the reply before the fake client sees it, and
    # a command that answers nothing then looks broken when it is not. The fake
    # client is already offline.
    return runtime


def _imports() -> dict[str, Any]:
    """Import every module. An import error is a bug, not a warning."""
    problems: list[dict[str, str]] = []
    count = 0
    import havoc

    for info in pkgutil.walk_packages(havoc.__path__, prefix="havoc."):
        name = info.name
        if ".features." in name or ".services." in name or ".utils." in name or name.count(".") <= 1:
            try:
                importlib.import_module(name)
                count += 1
            except Exception as exc:  # noqa: BLE001 - the point is to catch it
                problems.append(
                    {"module": name, "error": f"{type(exc).__name__}: {exc}", "where": _head(traceback.format_exc(), 400)}
                )
    return {"ok": not problems, "modules": count, "problems": problems}


async def _locales(runtime: Any) -> dict[str, Any]:
    """Every ``t("…")`` in the source must exist in both languages."""
    import havoc as pkg

    root = Path(pkg.__file__).parent
    en = json.loads((root / "locales" / "en.json").read_text())
    fa = json.loads((root / "locales" / "fa.json").read_text())
    used: set[str] = set()
    pattern = re.compile(r"(?:\bt|ctx\.t|locales\.t|runtime\.t)\(\s*[\"']([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)[\"']")
    for path in root.rglob("*.py"):
        used |= set(pattern.findall(path.read_text()))
    missing_en = sorted(key for key in used if key not in en)
    missing_fa = sorted(key for key in used if key not in fa)
    # Unused strings are dead weight, not a failure; report them so they can rot
    # in plain sight rather than silently.
    keyed = {
        key
        for key in set(en) | set(fa)
        if re.match(r"^(cmd|actions|errors|help|yura|feature|clock|fal|poem|remind|profile)\.", key)
    }
    unused = sorted(key for key in keyed if key not in used)
    return {
        "ok": not missing_en and not missing_fa,
        "used": len(used),
        "missing_en": missing_en[:40],
        "missing_fa": missing_fa[:40],
        "unused_sample": unused[:30],
        "unused": len(unused),
    }


async def _commands(runtime: Any) -> dict[str, Any]:
    from conftest import invoke

    results: list[dict[str, Any]] = []
    for command in runtime.registry.visible():
        name = command.name
        batteries: list[tuple[str, str]] = [("bare", f".{name}"), ("help", f".{name} --help")]
        for extra in EXTRA_ARGS.get(name, ()):
            batteries.append((f"args:{extra}", f".{name} {extra}"))
        # Read-only subcommands are cheap to exercise and the place where a stale
        # name (a renamed setting, a dropped table) actually shows up.
        for sub in command.subcommands[:3]:
            if any(ord(char) > 0x400 for char in sub):
                continue
            if sub in {"delete", "rm", "remove", "clear", "reset", "cancel", "cleanup", "stop", "off"}:
                continue
            batteries.append((f"sub:{sub}", f".{name} {sub}"))
        for label, line in batteries:
            entry: dict[str, Any] = {"command": name, "case": label, "line": line}
            try:
                sent = await invoke(runtime, line, sender_id=7542695649)
                text = "\n".join(message.text for message in sent)
                entry["replies"] = len(sent)
                entry["said"] = _head(text, 240)
                findings = _verdicts(text)
                # A locale key printed as the whole answer is the interesting one.
                if findings:
                    entry["findings"] = findings
            except Exception as exc:  # noqa: BLE001 - a crash is the finding
                entry["error"] = f"{type(exc).__name__}: {exc}"
                entry["where"] = _head(traceback.format_exc(), 500)
            finally:
                # `.stop` is a real switch and the battery can trip it; every later
                # command would then be refused for the wrong reason. Resume, so a
                # failure in the list is about that command.
                if await runtime.store.emergency_stop():
                    await runtime.feature_manager.resume()
                await runtime.store.clear_interactive(7542695649)
            results.append(entry)
    silent = [entry for entry in results if "empty_reply" in entry.get("findings", [])]
    broken = [
        entry
        for entry in results
        if entry.get("error")
        or any(
            finding.split(":")[0]
            in {"locale_key_leak", "unformatted_placeholder", "traceback", "double_escaped"}
            for finding in entry.get("findings", [])
        )
    ]
    return {
        "ok": not broken,
        "cases": len(results),
        "broken": broken,
        "silent": silent,
    }


async def _features(runtime: Any) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for name in sorted(runtime.feature_manager.names() if hasattr(runtime.feature_manager, "names") else []):
        entry: dict[str, Any] = {"feature": name}
        missing = runtime.feature_manager.missing_dependencies(name)
        entry["missing"] = list(missing)
        try:
            await runtime.feature_manager.set_enabled(name, True)
            entry["on"] = await runtime.feature_manager.is_enabled(name)
            await runtime.feature_manager.set_enabled(name, False)
            entry["off"] = not await runtime.feature_manager.is_enabled(name)
            await runtime.feature_manager.set_enabled(name, True)
        except Exception as exc:  # noqa: BLE001
            if not missing:
                # A declared dependency that is not configured (no AI key, no
                # translator) is a fact about the deployment, not a bug.
                entry["error"] = f"{type(exc).__name__}: {exc}"
        results.append(entry)
    broken = [
        entry
        for entry in results
        if entry.get("error") or entry.get("on") is False or entry.get("off") is False
    ]
    return {"ok": not broken, "count": len(results), "broken": broken}


async def _actions(runtime: Any) -> dict[str, Any]:
    """Every tool: schema sane, and a guest can run none of them."""
    from havoc.services.actions import ToolCall, Turn

    rows: list[dict[str, Any]] = []
    for tool in runtime.actions.tools:
        row: dict[str, Any] = {"tool": tool.name, "scope": tool.scope, "risk": tool.risk}
        try:
            schema = tool.schema
            row["schema_ok"] = schema["type"] == "object" and bool(schema["properties"])
        except Exception as exc:  # noqa: BLE001
            row["schema_ok"] = False
            row["error"] = f"{type(exc).__name__}: {exc}"
        guest = Turn(chat_id=1, actor_id=999, is_owner=False)
        outcome = await runtime.actions.execute(ToolCall(tool.name, {}), guest)
        row["guest"] = outcome.status
        rows.append(row)
    broken = [row for row in rows if not row.get("schema_ok") or row.get("guest") != "refused"]
    return {"ok": not broken, "tools": rows, "broken": broken}


def _dead_code(root: Path) -> dict[str, Any]:
    """Definitions nothing *reaches*, and scripts nobody runs.

    This is an AST pass, not a grep: a name in a comment or a docstring is not a
    caller, and counting it as one is how a project keeps a hundred helpers nobody
    uses. Three kinds of definition are exempt by construction, because Python or a
    plugin reaches them without naming them: dunders (``__len__``, ``__iter__``),
    ``__post_init__``/``__getattr__`` hooks, and callbacks a plugin API hands out.
    """
    import ast

    from havoc.services.self_console import BLOCKED  # noqa: F401 - import check

    package = Path(__import__("havoc").__file__).parent
    sources = list(package.rglob("*.py")) + list((root / "tests").rglob("*.py")) + list(
        (root / "scripts").rglob("*.py")
    )
    loaded: dict[str, int] = {}
    defined: list[tuple[str, str, str]] = []
    trees = []
    for path in sources:
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        trees.append((path, tree))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                loaded[node.id] = loaded.get(node.id, 0) + 1
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                loaded[node.attr] = loaded.get(node.attr, 0) + 1
    for path, tree in trees:
        if not str(path).startswith(str(package)):
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.append((node.name, str(path.relative_to(root)), "function"))
            elif isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        defined.append((sub.name, str(path.relative_to(root)), f"{node.name}.method"))
    #: Public API a plugin or a test reaches by string, not by name.
    hooks = {
        "register_command",
        "add_strings",
        "register_hook",
        "has_raqm",
        # Read by the dispatcher with getattr ("did this handler answer?"), so
        # the AST never sees a plain attribute load for it.
        "sent_count",
    }
    orphans = []
    for name, where, kind in sorted(defined):
        if name.startswith("__") or name in hooks:
            continue
        if loaded.get(name, 0) == 0:
            orphans.append({"function": name, "file": where, "kind": kind})
    todos = [
        {"file": str(path.relative_to(root)), "line": index, "text": line.strip()[:120]}
        for path in list(package.rglob("*.py"))
        for index, line in enumerate(path.read_text().splitlines(), 1)
        if re.search(r"\b(TODO|FIXME|XXX|HACK)\b", line)
    ]
    bare_excepts = []
    for path in package.rglob("*.py"):
        for index, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"except\s*:", line):
                bare_excepts.append({"file": str(path.relative_to(root)), "line": index})
    return {
        # An orphan is rot: a helper nobody calls is a promise nobody keeps. The
        # sweep that emptied this list is only permanent if the day a new one
        # appears is a red build.
        "ok": not todos and not bare_excepts and not orphans,
        "orphans": orphans,
        "todos": todos,
        "bare_excepts": bare_excepts,
    }


async def _sql(runtime: Any) -> dict[str, Any]:
    """Every SQL statement in the code, prepared against the real schema.

    A column renamed by a migration and still selected by an old handler is the
    quietest kind of rot: nothing fails at import, nothing fails in a test that
    does not touch that table, and the user sees "something went wrong". Preparing
    the statement is the cheapest way to ask SQLite itself.
    """
    import re as _re

    root = Path(__import__("havoc").__file__).parent
    db = runtime.store.db
    bad: list[dict[str, str]] = []
    checked = 0
    for path in sorted(root.rglob("*.py")):
        text = path.read_text()
        blocks = [m.group(2) for m in _re.finditer(r'("""|\'\'\')(.*?)\1', text, _re.S)]
        blocks += _re.findall(r'"((?:SELECT|INSERT|UPDATE|DELETE)[^"\n]{10,600})"', text)
        for sql in blocks:
            statement = sql.strip()
            if not _re.match(r"^(SELECT|INSERT|UPDATE|DELETE)\b", statement, _re.I):
                continue
            if "{" in statement:
                continue  # built at runtime; those have their own tests
            probe = _re.sub(r"\?", "NULL", statement)
            try:
                await db.fetch_all("EXPLAIN " + probe)
                checked += 1
            except Exception as exc:  # noqa: BLE001
                reason = str(exc)
                # A string split across a concatenation is not a schema problem.
                if "incomplete input" in reason or "syntax error" in reason:
                    continue
                bad.append(
                    {
                        "file": str(path.relative_to(root.parent)),
                        "sql": statement.splitlines()[0][:80],
                        "error": reason[:120],
                    }
                )
    return {"ok": not bad, "checked": checked, "broken": bad}


async def _schedule(runtime: Any) -> dict[str, Any]:
    """Does a due piece of work actually fire, and does a missed one do what it says?

    A scheduler is where stale locks live: a row marked running forever, a job that
    fires twice, a reminder left ``pending`` after its moment. This runs the real
    tick and the real recovery, then reads the database.
    """
    from datetime import timedelta

    from havoc.utils.time import utc_now

    problems: list[str] = []
    store = runtime.store
    scheduler = runtime.scheduler
    fired: list[str] = []

    async def _on_reminder(row: Any) -> None:
        fired.append(str(row["text"] if "text" in row.keys() else row))

    scheduler.on_reminder = _on_reminder
    ran: list[str] = []

    async def _job(_payload: Any) -> None:
        ran.append("job")

    scheduler.register_job("audit_noop", _job)
    reminder_id = await store.add_reminder(100, "audit reminder", utc_now() - timedelta(seconds=5))
    scheduled_id = await store.add_schedule(chat_id=100, text="audit schedule", run_at=utc_now() - timedelta(seconds=5))
    job_id = await scheduler.enqueue("audit_noop", utc_now() - timedelta(seconds=5))
    summary = await scheduler.tick()
    row = await store.get_reminder(reminder_id)
    if row["status"] != "done":
        problems.append(f"reminder stayed {row['status']}")
    if fired != ["audit reminder"]:
        problems.append(f"reminder fired {len(fired)} time(s)")
    schedule_row = await store.db.fetch_one("SELECT enabled FROM schedules WHERE id = ?", (scheduled_id,))
    if schedule_row and int(schedule_row["enabled"]) not in (0, 1):
        problems.append("schedule left in an unknown state")
    job_row = await store.db.fetch_one("SELECT status FROM jobs WHERE id = ?", (job_id,))
    if job_row and str(job_row["status"]) == "pending":
        problems.append("a due job is still pending after a tick")
    if ran != ["job"]:
        problems.append(f"the due job ran {len(ran)} time(s)")
    if summary.get("errors"):
        problems.append(f"tick reported {summary['errors']} error(s)")

    # A second tick must not deliver it again — that is the double-send bug.
    fired.clear()
    await scheduler.tick()
    if fired:
        problems.append("a delivered reminder fired twice")

    # A missed job under each policy: run_once keeps it, skip drops it, ask reports.
    policies: dict[str, str] = {}
    for policy in ("run_once", "skip", "ask"):
        scheduler.missed_policy = policy
        missed_id = await scheduler.enqueue(f"audit_missed_{policy}", utc_now() - timedelta(hours=1))
        result = await scheduler.recover()
        row = await store.db.fetch_one("SELECT status FROM jobs WHERE id = ?", (missed_id,))
        policies[policy] = str(row["status"] if row else "gone")
        if policy == "run_once" and policies[policy] != "pending":
            problems.append(f"run_once turned a missed job into {policies[policy]}")
        if policy in {"skip", "ask"} and policies[policy] == "pending":
            problems.append(f"{policy} left a missed job pending")
        if policy == "ask" and not result.get("jobs_missed"):
            problems.append("ask did not count the job it dropped")

    return {"ok": not problems, "problems": problems, "policies": policies, "summary": summary}


def _boot() -> dict[str, Any]:
    """Does the thing actually start?

    The suite checks the boot in-process; this runs the same probe the repository
    ships, in a subprocess, with its own temporary database — the closest thing to
    `python -m havoc run` that a CI box with no network can do. 1.7 died here and
    nothing noticed, so it is now part of the verdict.
    """
    import subprocess

    probe = ROOT / "scripts" / "boot_probe.py"
    try:
        result = subprocess.run(
            [sys.executable, str(probe), "--json"],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=ROOT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "problems": [f"could not run the probe: {exc}"], "exit_code": None}
    # The probe prints one JSON document; the log lines stay on stderr. Anything
    # that does not parse is a broken probe, not a pass.
    start = result.stdout.find("{")
    evidence: dict[str, Any] = {}
    if start >= 0:
        try:
            evidence = json.loads(result.stdout[start:])
        except ValueError:
            evidence = {}
    problems = list(evidence.get("problems") or [])
    if not evidence:
        problems.append(f"the probe printed no verdict: {result.stdout[-300:]}{result.stderr[-300:]}")
    elif evidence.get("ok") is not True:
        problems.append("the probe did not report a healthy boot")
    if result.returncode != 0 and not problems:
        problems.append(f"the probe returned {result.returncode}: {result.stderr[-400:]}")
    return {
        "ok": not problems,
        "problems": problems,
        "exit_code": evidence.get("exit_code"),
        "handlers": evidence.get("handlers"),
        "owner_told": evidence.get("owner_told"),
        "said": (evidence.get("said") or "")[:200],
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", default=str(ROOT / ".audit.json"))
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.CRITICAL)
    logger = logging.getLogger("audit")
    logger.setLevel(logging.CRITICAL)

    import tempfile

    record: dict[str, Any] = {"imports": _imports(), "dead_code": _dead_code(ROOT)}

    tmp = Path(tempfile.mkdtemp())
    runtime = await _runtime(tmp, logger)
    try:
        record["locales"] = await _locales(runtime)
        record["features"] = await _features(runtime)
        record["actions"] = await _actions(runtime)
        record["commands"] = await _commands(runtime)
        record["sql"] = await _sql(runtime)
        record["schedule"] = await _schedule(runtime)
        record["boot"] = _boot()
    finally:
        await runtime.db.close()

    record["ok"] = all(
        [
            record["imports"]["ok"],
            record["locales"]["ok"],
            record["features"]["ok"],
            record["actions"]["ok"],
            record["commands"]["ok"],
            record["sql"]["ok"],
            record["schedule"]["ok"],
            record["boot"]["ok"],
            record["dead_code"]["ok"],
        ]
    )
    Path(args.json).write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")

    if not args.quiet:
        print(f"imports     {'ok' if record['imports']['ok'] else 'FAIL'} ({record['imports']['modules']} modules)")
        print(f"locales     {'ok' if record['locales']['ok'] else 'FAIL'} · used {record['locales']['used']} · "
              f"missing en {len(record['locales']['missing_en'])} fa {len(record['locales']['missing_fa'])} · unused {record['locales']['unused']}")
        print(f"features    {'ok' if record['features']['ok'] else 'FAIL'} ({record['features']['count']})")
        print(f"actions     {'ok' if record['actions']['ok'] else 'FAIL'} ({len(record['actions']['tools'])} tools)")
        print(f"commands    {'ok' if record['commands']['ok'] else 'FAIL'} ({record['commands']['cases']} cases, "
              f"{len(record['commands']['broken'])} broken)")
        print(f"sql         {'ok' if record['sql']['ok'] else 'FAIL'} ({record['sql']['checked']} statements prepared)")
        print(f"boot        {'ok' if record['boot']['ok'] else 'FAIL'} · "
              f"exit {record['boot']['exit_code']} · {record['boot']['handlers']} handlers · "
              f"awake {'yes' if record['boot']['owner_told'] else 'no'}")
        for problem in record["boot"]["problems"][:10]:
            print(f"  ! boot: {problem}")
        print(f"dead code   {'ok' if record['dead_code']['ok'] else 'FAIL'} · orphans {len(record['dead_code']['orphans'])} · "
              f"todos {len(record['dead_code']['todos'])} · bare excepts {len(record['dead_code']['bare_excepts'])}")
        for entry in record["commands"]["broken"][:20]:
            print(f"  ✗ .{entry['command']} [{entry['case']}] {entry.get('error') or entry.get('findings')}")
            if entry.get("said"):
                print(f"      said: {entry['said'][:120]}")
        for entry in record["locales"]["missing_en"][:10]:
            print(f"  ✗ locale missing (en): {entry}")
        for entry in record["locales"]["missing_fa"][:10]:
            print(f"  ✗ locale missing (fa): {entry}")
        for entry in record["dead_code"]["orphans"][:15]:
            print(f"  ✗ unreferenced: {entry['file']} :: {entry['function']}")
        for entry in record["sql"]["broken"][:10]:
            print(f"  ✗ {entry['file']}: {entry['sql']} — {entry['error']}")
        for entry in record["actions"]["broken"][:10]:
            print(f"  ✗ tool {entry}")
        print(f"overall     {'ok' if record['ok'] else 'FAIL'} · written to {args.json}")
    return 0 if record["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
