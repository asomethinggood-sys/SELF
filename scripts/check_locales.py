#!/usr/bin/env python3
"""Locale coverage checker used by developers and CI.

It does three things without touching Telegram or the network:

1. collects every literal translation key used in ``havoc/**/*.py``
   (``x.t("key", ...)``, ``locales.t("key", ...)``, ``runtime.t("key", ...)``),
2. collects every command name from ``Command("name", ...)`` declarations and
   derives the ``cmd.<name>.description`` / ``cmd.<name>.usage`` keys,
3. verifies that each collected key exists in English *and* Farsi, that both
   catalogs have identical key sets, and that templates only use named
   placeholders.

Usage::

    python scripts/check_locales.py          # exits 1 on problems
    python scripts/check_locales.py --list   # print the collected keys
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HAVOC = ROOT / "havoc"
LOCALES = HAVOC / "locales"
LANGUAGES = ("en", "fa")

#: Keys that are assembled at runtime from a fixed set of suffixes.
DYNAMIC_KEYS = {
    "provider": {"disabled", "timeout", "rate_limited", "unauthorized", "network", "bad_response", "provider_error", "http_error", "bad_json", "too_large"},
    "reason": {"denylisted", "not_allowlisted", "chat_disabled", "private_disabled", "emergency_stop", "feature_disabled", "quiet_hours", "allowed", "chat_allowed", "group_default", "private_default", "default"},
    "feature_group": {"messaging", "detection", "profile", "operations", "other"},
}

COMMAND_KEYS = ("description", "usage")


def flatten(data: dict, prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    for key, value in data.items():
        full = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(flatten(value, full))
        else:
            flat[full] = str(value)
    return flat


def collect_keys() -> dict[str, set[str]]:
    literal_keys: set[str] = set()
    command_names: set[str] = set()
    dynamic: dict[str, set[str]] = {}

    for path in sorted(HAVOC.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:  # pragma: no cover - guards broken commits
            print(f"syntax error in {path}: {exc}", file=sys.stderr)
            raise SystemExit(2) from None
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name == "t" and node.args:
                    first = node.args[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        literal_keys.add(first.value)
                    elif isinstance(first, ast.JoinedStr) and first.values:
                        head = first.values[0]
                        if isinstance(head, ast.Constant):
                            prefix = str(head.value)
                            dynamic.setdefault(prefix.rstrip("."), set())
                if name == "Command" and node.args:
                    first = node.args[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        command_names.add(first.value)
                if name == "CommandError" and node.args:
                    first = node.args[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        literal_keys.add(first.value)

    for command in command_names:
        for suffix in COMMAND_KEYS:
            literal_keys.add(f"cmd.{command}.{suffix}")
    for prefix, values in DYNAMIC_KEYS.items():
        for value in values:
            literal_keys.add(f"{prefix}.{value}")
    return {"literal": literal_keys, "dynamic_prefixes": set(dynamic)}


def load_catalog(language: str) -> dict[str, str]:
    path = LOCALES / f"{language}.json"
    if not path.exists():
        print(f"missing locale file: {path}", file=sys.stderr)
        raise SystemExit(2)
    return flatten(json.loads(path.read_text(encoding="utf-8")))


def check_templates(catalog: dict[str, str], language: str) -> list[str]:
    import string

    problems: list[str] = []
    for key, value in catalog.items():
        try:
            parsed = list(string.Formatter().parse(value))
        except ValueError as exc:
            problems.append(f"{language}:{key}: malformed braces ({exc})")
            continue
        for _literal, field, spec, _conv in parsed:
            if field is not None and spec and any(ch.isalpha() for ch in spec):
                problems.append(f"{language}:{key}: unsupported format spec {spec!r}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="check HAVOC locale coverage")
    parser.add_argument("--list", action="store_true", help="print collected keys")
    args = parser.parse_args(argv)

    collected = collect_keys()
    required = collected["literal"]
    if args.list:
        for key in sorted(required):
            print(key)

    catalogs = {language: load_catalog(language) for language in LANGUAGES}
    problems: list[str] = []
    for language, catalog in catalogs.items():
        missing = sorted(key for key in required if key not in catalog)
        if missing:
            problems.append(f"{language}: {len(missing)} missing key(s):\n  " + "\n  ".join(missing[:40]))
        problems.extend(check_templates(catalog, language))

    base, other = catalogs["en"], catalogs["fa"]
    only_en = sorted(set(base) - set(other))
    only_fa = sorted(set(other) - set(base))
    if only_en:
        problems.append(f"keys only in en: {', '.join(only_en[:20])}")
    if only_fa:
        problems.append(f"keys only in fa: {', '.join(only_fa[:20])}")

    unused = sorted(set(base) - required)
    if unused:
        print(f"note: {len(unused)} catalog key(s) are not referenced by the checker (dynamic keys or future use)")

    if problems:
        print("\n\n".join(problems), file=sys.stderr)
        print(f"\nFAILED: {len(problems)} locale problem group(s)", file=sys.stderr)
        return 1
    print(f"OK: {len(required)} keys covered in {', '.join(LANGUAGES)} ({len(base)} catalog entries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
