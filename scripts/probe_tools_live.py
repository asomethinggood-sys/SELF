#!/usr/bin/env python3
"""Ask the real model for something that needs her hands, and watch them move.

Why this exists
---------------
``.ai-probe.json`` proves the provider answers ``2+2``. That is the *plain*
case: no tool catalogue, no execution, no second request. Almost everything the
owner actually wants is the other case — a sentence that is not a command, where
the model has to choose a tool from the catalogue, the action layer has to
authorize and run it, and the reply has to describe what happened without
leaking the scaffolding.

What already proves that, and what this adds
--------------------------------------------
``scripts/format_probe.py`` already drives the real agent loop, and on a hosted
runner ``.auth/config.json`` carries real credentials — so its ``hands`` section
is live evidence, not plumbing. ``.format-probe.json`` from 2026-11-03T09:54:51Z
records ``provider=router`` with 4 keys, ``remind`` chosen and ``done``, one row
in ``reminders``, an audit row of ``allowed``/``done``, and no tool code in the
reply. It runs on every push to ``havoc/**``, so that path is covered
continuously — in English.

What it does not cover is the same sentence in Persian. Half the owner's life is
in Farsi, the catalogue is translated per language, and a model that picks the
right tool in English can still fail to in the other one. This probe asks in
whichever language it is told, writes its own ``.tool-probe.json``, and fails
loudly when the reply leaks the call even though the tool ran.

Two scripts are *not* substitutes for either: ``scripts/try_message.py`` runs the
real runtime behind a ``QuietProvider`` that, in its own words, "never runs
anything", and ``scripts/probe_ai.py`` talks to the gateway directly and never
builds a ``Turn``, so it cannot select a tool at all.

Cost, and why it is gated
-------------------------
A tool call is two model requests: the ask, and the ask again with the result
folded in. The free allowance is 50 a day, so ``ai-probe.yml`` only runs this on
a dispatch with ``tools: true`` — never on a push, and never on the schedule.

Nothing here prints the key. Every string that reaches the record or the
terminal goes through :func:`havoc.utils.security.redact_text`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # allow `python scripts/probe_tools_live.py`
    sys.path.insert(0, str(ROOT))

from havoc import persona  # noqa: E402
from havoc.config import load_config  # noqa: E402
from havoc.persona import system_prompt  # noqa: E402
from havoc.runtime import Runtime  # noqa: E402
from havoc.services.actions import Turn  # noqa: E402
from havoc.services.agent import Agent  # noqa: E402
from havoc.services.ai_provider import AIMessage, AIProvider  # noqa: E402
from havoc.services.ai_router import AIRouter  # noqa: E402
from havoc.utils.security import redact_text  # noqa: E402

RECORD = Path(".tool-probe.json")

#: One non-plain sentence per language. Neither is a command, neither names a
#: tool, and both are impossible to satisfy by talking: either a reminder exists
#: afterwards or she lied. ``format_probe.py`` uses the English one for the same
#: reason — a reminder must not come back as a question.
ASKS = {
    "en": "remind me to call mom in 30 minutes, and tell me you did it.",
    "fa": "یورا، یادت باشه نیم ساعت دیگه به مامان زنگ بزنم. بگو که انجامش دادی.",
}

#: The features whose tools the default asks need. A gated tool is *supposed* to
#: refuse until its feature is on, so the probe turns them on as the owner would.
FEATURES = ("poetry", "scheduling")


def _head(text: Any, limit: int = 300) -> str:
    """One line, no newlines, short enough for a log line and a JSON record."""
    return " ".join(str(text or "").split())[:limit]


async def build_provider(config: Any) -> tuple[Any, dict[str, Any]]:
    """The same choice the bot makes: the rotating router, or the lone provider."""
    router = await AIRouter.load(config, None)
    provider: Any = router if router.available else AIProvider.from_config(config)
    meta = {
        "provider": "router" if router.available else "provider",
        "keys": len(getattr(router, "credentials", ())) if router.available else 0,
        "model": getattr(provider, "active_model", "") or config.ai_model,
    }
    return provider, meta


async def prepare(runtime: Runtime) -> Runtime:
    """Make a real runtime safe and able.

    ``dry_run`` is the whole safety argument: the action layer runs for real and
    writes to a scratch database, but the sender never touches Telegram. The
    account is the owner of itself, exactly as it is after a login, and the
    features whose tools the ask needs are on — a gated tool is *supposed* to
    refuse until its owner switches it on.
    """
    runtime.sender.dry_run = True
    runtime.load_features()
    runtime.permissions.set_self_id(1)
    for feature in FEATURES:
        try:
            await runtime.feature_manager.set_enabled(feature, True)
        except Exception:  # noqa: BLE001 - an unknown feature name is not a dead probe
            pass
    await runtime.feature_manager.refresh()
    return runtime


async def build_runtime(config: Any, logger: logging.Logger) -> Runtime:
    """A real runtime that cannot reach a person."""
    return await prepare(await Runtime.create(config, logger))


async def run_turn(
    runtime: Runtime,
    provider: Any,
    ask: str,
    language: str,
    *,
    max_tokens: int = 900,
) -> dict[str, Any]:
    """One sentence through the real agent loop: ask → tool → result → answer."""
    layer = runtime.actions
    agent = Agent(layer, logger=runtime.logger)
    turn = Turn(chat_id=1, actor_id=1, language=language, is_owner=True)
    system = (
        f"{system_prompt(language)}\n\n{layer.catalogue(language)}\n\n"
        f"{persona.ornament_prompt(language)}"
    )
    step = await agent.run(
        provider,
        [AIMessage("system", system), AIMessage("user", ask)],
        turn=turn,
        max_tokens=max_tokens,
    )
    said = redact_text(str(step.text or ""))
    outcomes = [
        {"tool": o.tool, "status": o.status, "note": redact_text(_head(o.note, 160))}
        for o in step.outcomes
    ]
    did = list(step.did)
    # Tool scaffolding in the owner's face is a bug even when the tool ran: the
    # chat must read as an answer, not as a transcript of the call.
    leaked = "```" in said or '"tool"' in said
    return {
        "ok": bool(did) and not leaked,
        "failure": "" if did else "the model chose no tool that ran",
        "ask": ask,
        "language": language,
        "tools_offered": len(layer.tools),
        "outcomes": outcomes,
        "did": did,
        "asked_first": step.waiting is not None,
        "said": _head(said, 400),
        "leaked_tool_code": leaked,
    }


async def probe(
    ask: str,
    language: str,
    *,
    max_tokens: int = 900,
    logger: logging.Logger | None = None,
    config: Any = None,
) -> dict[str, Any]:
    """Build the real provider and runtime, run one turn, and report it.

    ``config`` is injectable so a test can prove the "no provider" verdict
    without this script ever reading the repository's real credentials — a test
    that reached ``.auth`` would make the suite depend on the network.
    """
    log = logger or logging.getLogger("tool-probe")
    if config is None:
        config, _issues = load_config()
    record: dict[str, Any] = {
        "checked_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kind": "tool-probe",
    }
    provider, meta = await build_provider(config)
    record.update(meta)
    if not getattr(provider, "available", False):
        record.update(
            ok=False,
            failure="AI is not configured",
            ask=ask,
            language=language,
            tools_offered=0,
            outcomes=[],
            did=[],
            asked_first=False,
            said="",
            leaked_tool_code=False,
        )
        return record

    runtime = await build_runtime(config, log)
    try:
        record.update(await run_turn(runtime, provider, ask, language, max_tokens=max_tokens))
    except Exception as exc:  # noqa: BLE001 - a probe reports, it never raises
        record.update(ok=False, failure=redact_text(f"{type(exc).__name__}: {exc}", [])[:300])
    finally:
        await runtime.db.close()
    return record


def readable(record: dict[str, Any]) -> str:
    """The part a human reads, without the log archive."""
    lines = [
        f"provider  {record.get('provider', '?')} · keys {record.get('keys', 0)}"
        f" · model {record.get('model') or 'auto'}",
        f"ask       {record.get('language', '?')}: {record.get('ask', '')}",
        f"catalogue {record.get('tools_offered', 0)} tool(s) offered",
        f"ran       {', '.join(record.get('did') or []) or '— nothing ran —'}",
    ]
    for outcome in record.get("outcomes") or []:
        lines.append(f"  · {outcome['tool']}: {outcome['status']} — {outcome['note']}")
    lines.append(f"asked first  {record.get('asked_first')}")
    lines.append(f"leaked tool code  {record.get('leaked_tool_code')}")
    lines.append(f"said      {record.get('said') or '(nothing)'}")
    lines.append(f"verdict   {'ok' if record.get('ok') else record.get('failure') or 'not ok'}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="one non-plain sentence through the real model and the real action layer"
    )
    parser.add_argument("--ask", default="", help="the sentence (default: the language's own)")
    parser.add_argument("--lang", default="en", choices=tuple(ASKS), help="which language to ask in")
    parser.add_argument("--max-tokens", type=int, default=900, help="budget for the answer")
    parser.add_argument("--json-out", default=str(RECORD), help="where the record is written")
    parser.add_argument("--no-write", action="store_true", help="print only, write no file")
    args = parser.parse_args(argv)

    ask = args.ask or ASKS[args.lang]
    record = asyncio.run(probe(ask, args.lang, max_tokens=args.max_tokens))
    if not args.no_write:
        path = Path(args.json_out)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(readable(record))
    # A green tick must mean her hands moved. Anything else is a failure worth
    # seeing, including "the model talked about the tool instead of using it".
    return 0 if record.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
