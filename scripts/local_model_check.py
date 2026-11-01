#!/usr/bin/env python3
"""Start a model on *this* machine and make it answer, or say why it did not.

Every other check in this repository talks to a stub. This one is the opposite:
it runs a real ``llama-server`` binary on a real ``.gguf`` file, waits for the
port to open, and asks the model things through the exact code path a chat
message takes — ``LocalModel.start()`` → ``converse()`` → ``AIRouter`` fallback.

    python scripts/local_model_check.py                     # the defaults below
    python scripts/local_model_check.py --binary ./llama-server --model q.gguf
    python scripts/local_model_check.py --url http://127.0.0.1:8080/v1
    python scripts/local_model_check.py --json local-model.json

Exit code 0 means an answer came back from a model that was not on the network.
Anything else prints why: a missing binary, a missing file, a server that never
opened its port, or a model that answered with nothing.

Why a GGUF and not an API: the point of the local model is that a spent key
allowance, a rate limit, or a total absence of keys stops meaning silence. This
script is the only place in the repo where that claim is *executed*.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHECKS: list[tuple[str, bool, str]] = []
ANSWERS: list[str] = []
#: Set by ``main``; written by ``_finish`` whatever the outcome. A check that
#: only writes its report on success is a check whose failures are invisible in
#: a run log nobody can open.
REPORT_PATH: Path | None = None


def record(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))


def write_report() -> None:
    """Publish the result — passed, failed, or never started."""
    if REPORT_PATH is None:
        return
    try:
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(
            json.dumps(
                {
                    "checks": [{"name": name, "ok": ok, "detail": detail} for name, ok, detail in CHECKS],
                    "answers": ANSWERS,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError as exc:  # pragma: no cover - a full disk, nothing to do
        print(f"could not write {REPORT_PATH}: {exc}", file=sys.stderr)


class Turn:
    """The smallest thing a chat call needs: a role, words, and no picture."""

    def __init__(self, role: str, content: str) -> None:
        self.role = role
        self.content = content
        self.has_image = False


async def run(args: argparse.Namespace) -> int:
    from havoc.services.local_model import LocalModel

    model = LocalModel(
        base_url=args.url,
        model=args.name,
        binary=args.binary,
        weights=args.model,
        timeout=args.timeout,
        flags=args.flags,
    )
    record("local · something is configured", model.configured, model.describe() and str(model.base_url))

    if args.binary and not shutil.which(args.binary) and not Path(args.binary).exists():
        record("local · the llama.cpp binary exists", False, f"{args.binary} not found")
    if args.model and not Path(args.model).exists():
        record("local · the weights exist", False, f"{args.model} not found")

    started = time.monotonic()
    ready = await model.start()
    start_seconds = time.monotonic() - started
    record("local · start() reaches the endpoint", ready, f"{start_seconds:.1f}s · {model.last_error or model.base_url}")
    if not ready:
        return _finish(args)

    questions = [
        "In one sentence: what are you?",
        "Write a single line of poetry about rain.",
        "Answer with one word: two plus two?",
    ]
    for question in questions:
        began = time.monotonic()
        try:
            reply = await model.converse([Turn("user", question)], max_tokens=120, temperature=0.7)
        except Exception as exc:  # noqa: BLE001 - the failure is the result
            record(f"local · answered: {question[:34]}", False, f"{type(exc).__name__}: {exc}")
            continue
        took = time.monotonic() - began
        ANSWERS.append({"question": question, "reply": reply})
        record(f"local · answered: {question[:34]}", bool(reply.strip()), f"{took:.1f}s · {reply.strip()[:80]}")
        if took > args.timeout:
            record("local · answered inside the timeout", False, f"{took:.1f}s > {args.timeout:.0f}s")

    # The router's fallback, with no credentials at all: the whole point.
    from havoc.services.ai_router import AIRouter

    router = AIRouter([])
    router.local = model
    try:
        reply = await router.converse([Turn("user", "say hello, briefly")], max_tokens=60)
        record("local · the router answers with zero keys", bool(reply.strip()), reply.strip()[:80])
        record(
            "local · the router labels it local",
            router.active_label in {"local", "hosted"},
            str(router.active_label),
        )
    except Exception as exc:  # noqa: BLE001
        record("local · the router answers with zero keys", False, f"{type(exc).__name__}: {exc}")

    description = model.describe()
    record("local · diagnostics describe it", bool(description.get("model")), json.dumps(description, default=str)[:200])

    await model.stop()
    record("local · the server stops cleanly", True, "stopped")
    return _finish(args)


def _finish(args: argparse.Namespace) -> int:
    write_report()
    for name, ok, detail in CHECKS:
        mark = "ok  " if ok else "FAIL"
        print(f"  {mark} {name}" + (f"\n       ↳ {detail}" if detail and not ok else ""))
    failures = [item for item in CHECKS if not item[1]]
    print(f"\nsummary: {len(CHECKS) - len(failures)}/{len(CHECKS)} checks passed")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("LOCAL_MODEL_URL") or "http://127.0.0.1:8080/v1")
    parser.add_argument("--binary", default=os.environ.get("LOCAL_MODEL_BIN", "llama-server"))
    parser.add_argument("--model", default=os.environ.get("LOCAL_MODEL_GGUF", ""))
    parser.add_argument("--name", default=os.environ.get("LOCAL_MODEL_NAME", "local"))
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("LOCAL_MODEL_TIMEOUT") or 120))
    parser.add_argument("--flags", default=os.environ.get("LOCAL_MODEL_FLAGS", ""))
    parser.add_argument("--json", default="")
    args = parser.parse_args()
    global REPORT_PATH
    REPORT_PATH = Path(args.json) if args.json else None
    try:
        return asyncio.run(run(args))
    finally:
        # Even a crash writes what it knew: "asyncio died here" is a finding.
        write_report()


if __name__ == "__main__":
    raise SystemExit(main())
