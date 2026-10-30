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
    """Definitions nobody calls, and scripts nobody runs.

    Grep-level on purpose: an AST pass that is wrong is worse than a list a person
    can check. Functions decorated with ``@command``-style registration are
    reached through the registry, so they are excluded by name pattern.
    """
    import ast

    package = Path(__import__("havoc").__file__).parent
    defined: dict[str, Path] = {}
    for path in package.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("_") or node.name.startswith("test"):
                    continue
                defined.setdefault(node.name, path)
    corpus = "\n".join(path.read_text() for path in package.rglob("*.py"))
    tests = "\n".join(path.read_text() for path in (root / "tests").rglob("*.py"))
    scripts = "\n".join(path.read_text() for path in (root / "scripts").rglob("*.py"))
    everything = corpus + tests + scripts
    orphans = []
    import collections

    words = collections.Counter(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", everything))
    for name, path in sorted(defined.items()):
        # A function is reached by *being named* — a call, a registry tuple, a
        # decorator, a test. The definition itself is one of those mentions.
        if words[name] <= 1:
            orphans.append({"function": name, "file": str(path.relative_to(root))})
    todos = [
        {"file": str(path.relative_to(root)), "line": index, "text": line.strip()[:120]}
        for path in list(package.rglob("*.py"))
        for index, line in enumerate(path.read_text().splitlines(), 1)
        if re.search(r"\b(TODO|FIXME|XXX|HACK)\b", line)
    ]
    bare_excepts = []
    for path in package.rglob("*.py"):
        text = path.read_text()
        for index, line in enumerate(text.splitlines(), 1):
            if re.search(r"except\s*:", line):
                bare_excepts.append({"file": str(path.relative_to(root)), "line": index})
    return {
        "ok": not todos and not bare_excepts,
        "orphans": orphans,
        "todos": todos,
        "bare_excepts": bare_excepts,
    }


async def _schedule(runtime: Any) -> dict[str, Any]:
    """The scheduler is where stale locks live. Ask it what it thinks is due."""
    rows: dict[str, Any] = {}
    try:
        jobs = getattr(runtime.scheduler, "jobs", None)
        rows["jobs"] = len(jobs) if jobs is not None else "unknown"
        rows["running"] = str(getattr(runtime.scheduler, "running", "unknown"))
    except Exception as exc:  # noqa: BLE001
        rows["error"] = f"{type(exc).__name__}: {exc}"
    return rows


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
        record["schedule"] = await _schedule(runtime)
    finally:
        await runtime.db.close()

    record["ok"] = all(
        [
            record["imports"]["ok"],
            record["locales"]["ok"],
            record["features"]["ok"],
            record["actions"]["ok"],
            record["commands"]["ok"],
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
        for entry in record["actions"]["broken"][:10]:
            print(f"  ✗ tool {entry}")
        print(f"overall     {'ok' if record['ok'] else 'FAIL'} · written to {args.json}")
    return 0 if record["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
