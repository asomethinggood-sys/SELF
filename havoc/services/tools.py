"""The tools she is actually given in phase 0 — all of them already exist.

Nothing here is new capability dressed up as an agent: fetching a fact, searching
a chat, translating a line, remembering something, forgetting something, setting a
reminder. What is new is that she decides *when* one is needed instead of waiting
for a command, and that every use goes through :mod:`havoc.services.actions`.

Two deliberate omissions:

* no ``send_message`` — she already speaks, and a tool that can address an
  arbitrary chat is a spam cannon with a personality;
* no money, no identity, nothing that logs in anywhere. Those scopes are refused
  by the layer itself, so a future connector cannot quietly add one.
"""

from __future__ import annotations

import re
from typing import Any

from ..utils.time import TimeParseError, format_datetime, parse_when, utc_now
from .actions import MAX_CALLS_PER_TURN, ActionResult, Tool, Turn
from .self_console import run as run_command
from .translation_provider import TranslationError

__all__ = ["build_tools"]


def _t(runtime: Any, turn: Turn, key: str, **variables: Any) -> str:
    """A localized card phrase, never a crash if a locale is stripped."""
    try:
        return str(runtime.t(key, turn.language, **variables))
    except Exception:  # noqa: BLE001 - a missing key must not silence a card
        return ""


def build_tools(runtime: Any) -> list[Tool]:
    """The toolbox for this runtime. Missing services simply drop their tools."""

    tools: list[Tool] = []

    async def recall(turn: Turn, args: dict[str, str]) -> ActionResult:
        about = (args.get("about") or "").strip()
        rows = await runtime.memory.recall(turn.chat_id, limit=200)
        lines = [row.line() for row in rows]
        if about:
            words = [word for word in about.lower().split() if len(word) > 2]
            hits = [line for line in lines if any(word in line.lower() for word in words)]
            if not hits:
                return ActionResult(
                    True,
                    note=f"nothing stored about {about}",
                    title=_t(runtime, turn, "actions.card_looked_up", what=about),
                    lines=(_t(runtime, turn, "actions.card_nothing_kept"),),
                )
            return ActionResult(
                True,
                note=" | ".join(hits[:6]),
                title=_t(runtime, turn, "actions.card_looked_up", what=about),
                lines=tuple(hit[:160] for hit in hits[:3]),
            )
        if not lines:
            return ActionResult(
                True,
                note="nothing stored yet",
                title=_t(runtime, turn, "actions.card_looked_up", what="memory"),
                lines=(_t(runtime, turn, "actions.card_nothing_kept"),),
            )
        return ActionResult(
            True,
            note=" | ".join(lines[:12]),
            title=_t(runtime, turn, "actions.card_looked_up", what="memory"),
            lines=tuple(line[:160] for line in lines[:3]),
        )

    tools.append(
        Tool(
            name="recall",
            summary="look through what you have been told and kept (facts, names, promises)",
            properties={"about": "a word or phrase to look for; empty lists the newest"},
            run=recall,
            scope="read",
        )
    )

    async def search(turn: Turn, args: dict[str, str]) -> ActionResult:
        query = args.get("query", "").strip()
        try:
            limit = max(1, min(10, int(args.get("limit", "5") or 5)))
        except ValueError:
            limit = 5
        client = getattr(runtime, "client", None)
        if client is None:
            return ActionResult(False, detail="telegram is not connected", kind="no_client")
        try:
            messages = await client.get_messages(
                turn.chat_id, limit=limit, search=query or None
            )
        except Exception as exc:  # noqa: BLE001 - a search that fails is a sentence
            return ActionResult(False, detail=f"search failed: {type(exc).__name__}", kind="search")
        if not messages:
            return ActionResult(
            True,
            note=f"no messages in this chat match {query!r}",
            title=_t(runtime, turn, "actions.card_searched", what=query),
            lines=(_t(runtime, turn, "actions.card_nothing_found"),),
        )
        rows = []
        for message in messages:
            text = " ".join(str(getattr(message, "message", "") or getattr(message, "text", "")).split())
            if not text:
                continue
            rows.append(text[:180])
        if not rows:
            return ActionResult(True, note="only empty messages matched")
        return ActionResult(
            True,
            note=" ⏎ ".join(rows),
            title=_t(runtime, turn, "actions.card_searched", what=query),
            lines=tuple(rows[:3]),
        )

    tools.append(
        Tool(
            name="search",
            summary="search this chat's own history for what was said about something",
            properties={
                "query": "what to search for",
                "limit": "how many messages, 1-10 (optional)",
            },
            required=("query",),
            run=search,
            scope="read",
        )
    )

    async def translate(turn: Turn, args: dict[str, str]) -> ActionResult:
        text = args.get("text", "").strip()
        to = (args.get("to") or ("en" if turn.language == "fa" else "fa")).strip()
        translator = getattr(runtime, "translator", None)
        if translator is None or not getattr(translator, "available", False):
            return ActionResult(False, detail="no translation engine", kind="disabled")
        try:
            result = await translator.translate(text, to)
        except TranslationError as exc:
            return ActionResult(False, detail=f"could not translate: {exc.kind}", kind=exc.kind)
        return ActionResult(
            True,
            note=f"{to}: {result.text[:900]}",
            title=_t(runtime, turn, "actions.card_translated", to=to.upper()),
            lines=(result.text[:300],),
        )

    tools.append(
        Tool(
            name="translate",
            summary="translate a piece of text into another language",
            properties={"text": "the text", "to": "target language code, e.g. en, fa, ru"},
            required=("text",),
            run=translate,
            scope="read",
        )
    )

    async def remember(turn: Turn, args: dict[str, str]) -> ActionResult:
        fact = " ".join(args.get("fact", "").split())
        if not fact:
            return ActionResult(False, detail="nothing to remember", kind="bad_args")
        stored = await runtime.memory.remember(turn.chat_id, fact, kind="fact")
        if not stored:
            return ActionResult(
                True,
                note="already known",
                title=_t(runtime, turn, "actions.card_remembered"),
                lines=(_t(runtime, turn, "actions.card_already_kept"),),
            )
        return ActionResult(
            True,
            note="kept",
            title=_t(runtime, turn, "actions.card_remembered"),
            lines=(fact[:240],),
        )

    tools.append(
        Tool(
            name="remember",
            summary="keep a durable fact about the owner or this chat",
            properties={"fact": "one sentence, in his own words"},
            required=("fact",),
            run=remember,
            scope="write",
        )
    )

    async def forget(turn: Turn, args: dict[str, str]) -> ActionResult:
        query = args.get("about", "").strip()
        removed = await runtime.memory.forget_matching(turn.chat_id, query)
        if not removed:
            return ActionResult(
                True,
                note=f"nothing matched {query!r}",
                title=_t(runtime, turn, "actions.card_forgotten"),
                lines=(_t(runtime, turn, "actions.card_nothing_found"),),
            )
        return ActionResult(
            True,
            note=f"removed {removed}",
            title=_t(runtime, turn, "actions.card_forgotten"),
            lines=(query[:240],),
        )

    tools.append(
        Tool(
            name="forget",
            summary="delete a stored fact — only when he actually asked you to forget it",
            properties={"about": "the fact, or a phrase from it"},
            required=("about",),
            run=forget,
            scope="delete",
            risk="medium",
        )
    )

    async def remind(turn: Turn, args: dict[str, str]) -> ActionResult:
        when_raw = args.get("when", "").strip()
        what = " ".join(args.get("what", "").split())
        tz = getattr(runtime.config, "timezone", "UTC")
        try:
            when = parse_when(when_raw, tz)
        except TimeParseError as exc:
            return ActionResult(False, detail=f"could not read the time: {exc}", kind="bad_args")
        if when <= utc_now():
            return ActionResult(False, detail="that time has already passed", kind="bad_args")
        reminder_id = await runtime.store.add_reminder(
            turn.chat_id,
            what,
            when,
            timezone_name=tz,
            created_by=turn.actor_id or None,
            meta={"language": turn.language, "source": "action"},
        )
        human = format_datetime(when, tz, turn.language)
        return ActionResult(
            True,
            note=f"#{reminder_id} at {human}",
            title=_t(runtime, turn, "actions.card_reminder", when=human),
            lines=(_t(runtime, turn, "actions.card_reminder_note"),),
        )

    tools.append(
        Tool(
            name="remind",
            summary="set a real reminder that will be delivered in this chat",
            properties={
                "when": "30m, tomorrow 09:00, 21:15 — the time he said",
                "what": "what to remind him about, in his words",
            },
            required=("when", "what"),
            run=remind,
            scope="write",
        )
    )

    async def command(turn: Turn, args: dict[str, str]) -> ActionResult:
        """Any of her own commands, dispatched exactly as if he had typed it.

        One call may carry several lines — a New York model writing a compound
        request ("clock off, then remind me") uses newlines or ``;`` — and each
        line is dispatched on its own so one bad line does not swallow the rest.
        """
        raw = " ".join(
            part for part in (args.get("command", ""), args.get("args", "")) if part
        ).strip()
        script = [part.strip() for part in re.split(r"[\n;]+|\s+&&\s+", raw) if part.strip()]
        if not script:
            return ActionResult(False, detail="no command given", kind="bad_args")
        notes: list[str] = []
        all_ok = True
        for line in script[:MAX_CALLS_PER_TURN]:
            result = await run_command(
                runtime,
                line,
                chat_id=turn.chat_id,
                sender_id=turn.actor_id or 0,
                message_id=turn.message_id,
                language=turn.language,
            )
            if not result.ok and result.kind in {"blocked", "unknown"}:
                return ActionResult(False, detail=result.note, kind=result.kind)
            if not result.ok:
                all_ok = False
                # The model guessed a syntax. Give it the line it should have
                # written, so the next round can repair it instead of apologising.
                name = (result.command or line.split(" ")[0]).strip().lower()
                record = runtime.registry.resolve(name) if getattr(runtime, "registry", None) else None
                usage = ""
                if record is not None:
                    usage = " ".join(str(getattr(record, "usage", "") or "").split())
                hint = f"`{runtime.prefix}{name} {usage}`".replace("  ", " ").strip()
                notes.append(f"{line}: {result.note}" + (f" — the form is {hint}" if usage else ""))
            else:
                notes.append(f"{line}: {result.note}")
        if all_ok:
            return ActionResult(
                True,
                note=" · ".join(notes)[:400],
                title=_t(runtime, turn, "actions.card_ran_command", command=raw[:80]),
                lines=tuple(notes[:3]),
            )
        return ActionResult(
            False,
            detail="; ".join(notes)[:400],
            note="; ".join(notes)[:400],
            kind="failed",
        )

    tools.append(
        Tool(
            name="command",
            summary=(
                "run one of her own commands for him (everything in .help): "
                "'fal page 166', 'clock on -- timezone Asia/Tehran', 'remind 30m call mom'"
            ),
            properties={
                "command": "the command name without the prefix, e.g. fal",
                "args": "the rest of the line, exactly as he would type it",
            },
            required=("command",),
            run=command,
            scope="write",
            risk="medium",
        )
    )

    return tools
