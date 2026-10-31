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

from ..utils.text import emoji_in
from ..utils.time import TimeParseError, format_datetime, parse_when, utc_now
from .actions import MAX_CALLS_PER_TURN, ActionResult, Tool, Turn
from .self_console import run as run_command
from .translation_provider import TranslationError

__all__ = ["build_tools"]


#: The ways a sentence says "the room I am standing in".
_HERE_WORDS = {
    "here",
    "this chat",
    "this group",
    "this room",
    "this channel",
    "the group",
    "the chat",
    "current chat",
    "this one",
    "این گروه",
    "همین گروه",
    "اینجا",
    "این چت",
}


def first_emoji(text: str) -> str:
    """The one emoji in a sentence — ``قلب`` is not one, ``❤️`` is.

    The rule lives in :func:`havoc.utils.text.emoji_in` so the sentence reader and
    the tool agree on what a reaction is; a word or a Chinese character is not
    one, and sending one to Telegram fails.
    """
    return emoji_in(text)


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

    # ------------------------------------------------------------------
    # Reaching people, and sequences over time
    # ------------------------------------------------------------------
    # These are the tools the "no send_message" rule in the module docstring was
    # waiting for: a send that resolves the target, refuses a stranger, is paced,
    # is recorded in the outbox and is verified after the fact. The old refusal was
    # right — a bare send_message would have been a spam cannon. This is not bare.
    async def send_to(turn: Turn, args: dict[str, str]) -> ActionResult:
        who = (args.get("to") or "").strip()
        body = (args.get("text") or "").strip()
        if not who or not body:
            return ActionResult(False, detail="both 'to' and 'text' are needed", kind="bad_args")
        target = None
        if who.lower().strip(" .,!") in _HERE_WORDS:
            # "send hi in 10 different messages in this group" — the model has no
            # id to give, and the room it is speaking in is the answer.
            from .reach import Target

            target = Target(id=int(turn.chat_id), kind="unknown", title="this chat")
        if target is None:
            target = await runtime.reach.resolve(who)
        if target is None:
            near = await runtime.reach.candidates(who, limit=4)
            hint = ", ".join(f"{item.label} ({item.id})" for item in near) if near else ""
            return ActionResult(
                False,
                detail=f"no chat called {who!r}" + (f"; did you mean {hint}?" if hint else ""),
                kind="unknown_target",
            )
        report = await runtime.reach.deliver(
            target,
            body,
            spoiler_hidden=bool(args.get("spoiler")),
            repeat=int(args.get("repeat") or 1),
            delete_after=float(args["delete_after"]) if args.get("delete_after") else None,
            at=args.get("at") or None,
        )
        if not report.ok:
            return ActionResult(False, detail=report.error or "the send failed", kind="failed")
        return ActionResult(
            True,
            note=f"sent to {target.label} ({target.id}): {report.summary}",
            title=_t(runtime, turn, "reach.sent_header", count=1),
            lines=(f"{target.label} · {report.summary}",),
        )

    async def plan_it(turn: Turn, args: dict[str, str]) -> ActionResult:
        sentence = (args.get("sentence") or args.get("text") or "").strip()
        if not sentence:
            return ActionResult(False, detail="a sentence is needed", kind="bad_args")
        from . import intent as intent_module
        from .plans import build_plan

        plan = build_plan(sentence, intent=intent_module)
        if plan.empty:
            return ActionResult(False, detail="could not read that as a timeline", kind="failed")
        result = await runtime.plans.create(plan, said=sentence, owner_id=turn.actor_id)
        return ActionResult(
            True,
            note=f"plan #{result['id']} with {result['steps']} steps",
            title=_t(runtime, turn, "plan.header", count=1),
            lines=tuple(plan.describe()),
        )

    async def react_to(turn: Turn, args: dict[str, str]) -> ActionResult:
        """React to the message he pointed at, or to the one he typed."""
        emoji = first_emoji(args.get("emoji") or "")
        if not emoji:
            return ActionResult(False, detail="one emoji is needed", kind="bad_args")
        try:
            message_id = int(args.get("message_id") or 0)
        except ValueError:
            message_id = 0
        # "react to his message" = the bubble he answered. Only when there is no
        # reply does it fall back to his own line, which is still a real target.
        message_id = message_id or int(turn.reply_to or turn.message_id or 0)
        if not message_id:
            return ActionResult(False, detail="there is no message to react to", kind="bad_args")
        ok = await runtime.reach.react(turn.chat_id, int(message_id), emoji)
        if not ok:
            return ActionResult(False, detail="telegram refused the reaction", kind="failed")
        return ActionResult(True, note=f"reacted {emoji}", title=_t(runtime, turn, "reach.reacted", emoji=emoji))

    tools.append(
        Tool(
            name="send_message",
            summary=(
                "send a message to a named person or chat, with repeats, a spoiler, "
                "self-destruct or a later time"
            ),
            properties={
                "to": "@handle, a name from his chats, an id, or 'saved'",
                "text": "exactly what to send, in his words",
                "repeat": "how many times (default 1)",
                "delete_after": "seconds after which it deletes itself",
                "spoiler": "true to hide it behind a tap",
                "at": "a time like 21:00 or 30m, instead of now",
            },
            required=("to", "text"),
            run=send_to,
            scope="write",
            risk="medium",
        )
    )
    async def read_chat(turn: Turn, args: dict[str, str]) -> ActionResult:
        """The last few lines of one conversation, for answering "what did she say"."""
        who = (args.get("who") or "").strip()
        if not who:
            return ActionResult(False, detail="a chat is needed", kind="bad_args")
        target = await runtime.reach.resolve(who)
        if target is None:
            return ActionResult(False, detail=f"no chat called {who!r}", kind="unknown_target")
        try:
            limit = max(1, min(int(args.get("limit") or 12), 50))
        except ValueError:
            limit = 12
        getter = getattr(runtime.client, "get_messages", None)
        if getter is None:
            return ActionResult(False, detail="no Telegram connection", kind="offline")
        try:
            rows = list(await getter(target.id, limit=limit) or [])
        except Exception as exc:  # noqa: BLE001 - the note carries it
            return ActionResult(False, detail=str(exc)[:200], kind="failed")
        if not rows:
            return ActionResult(True, note=f"nothing in {target.label} yet")
        lines = []
        for message in reversed(rows):
            text = (
                getattr(message, "raw_text", None)
                or getattr(message, "text", None)
                or f"[{getattr(message, 'media', None) and 'media' or 'empty'}]"
            )
            who_said = "them" if not getattr(message, "out", False) else "him"
            lines.append(f"{who_said}: {str(text)[:200]}")
        return ActionResult(
            True,
            note=f"{target.label}: " + " | ".join(lines[-12:]),
            title=_t(runtime, turn, "inbox.read_header", name=target.label, count=len(lines)),
            lines=tuple(line[:200] for line in lines[-3:]),
        )

    tools.append(
        Tool(
            name="read_chat",
            summary="read the last few messages of one conversation (for 'what did Sara say')",
            properties={
                "who": "@handle, a name from his chats, saved, or an id",
                "limit": "how many messages (default 12, max 50)",
            },
            required=("who",),
            run=read_chat,
            scope="read",
        )
    )

    tools.append(
        Tool(
            name="plan",
            summary="turn a whole sequence with times into scheduled steps",
            properties={"sentence": "the sentence he said, verbatim"},
            required=("sentence",),
            run=plan_it,
            scope="write",
            risk="medium",
        )
    )
    tools.append(
        Tool(
            name="react",
            summary="react to a message with one emoji",
            properties={"emoji": "one emoji", "message_id": "the message number; defaults to his"},
            required=("emoji",),
            run=react_to,
            scope="write",
        )
    )

    async def send_poll(turn: Turn, args: dict[str, str]) -> ActionResult:
        """A real poll, built and serialised by the service before it is sent."""
        question = (args.get("question") or "").strip()
        raw = (args.get("options") or "").strip()
        options = [part.strip() for part in re.split(r"[|;]", raw) if part.strip()]
        if not question or len(options) < 2:
            return ActionResult(False, detail="a poll needs a question and two options", kind="bad_args")
        extras = getattr(runtime, "tg", None)
        if extras is None:
            return ActionResult(False, detail="no Telegram connection", kind="offline")
        correct: int | None = None
        if (args.get("correct") or "").strip():
            try:
                correct = int((args.get("correct") or "").strip())
            except ValueError:
                return ActionResult(False, detail="correct must be the option number", kind="bad_args")
        try:
            await extras.send_poll(
                turn.chat_id,
                question,
                options[:10],
                quiz=bool(correct),
                correct=correct,
            )
        except Exception as exc:  # noqa: BLE001 - the refusal is the answer
            return ActionResult(False, detail=str(exc)[:200], kind="send")
        return ActionResult(
            True,
            note=f"poll posted: {question} ({len(options[:10])} options)",
            title=_t(runtime, turn, "actions.card_poll", question=question[:80]),
            lines=tuple(options[:10]),
        )

    tools.append(
        Tool(
            name="send_poll",
            summary="post a real poll with a question and options people can tap",
            properties={
                "question": "the question",
                "options": "the choices, separated by | or ;",
                "correct": "for a quiz, the number of the right option (optional)",
            },
            required=("question", "options"),
            run=send_poll,
            scope="write",
        )
    )

    async def send_location(turn: Turn, args: dict[str, str]) -> ActionResult:
        extras = getattr(runtime, "tg", None)
        if extras is None:
            return ActionResult(False, detail="no Telegram connection", kind="offline")
        try:
            lat = float((args.get("lat") or "").strip())
            lon = float((args.get("lon") or "").strip())
        except ValueError:
            return ActionResult(False, detail="lat and lon must be numbers", kind="bad_args")
        try:
            await extras.send_location(turn.chat_id, lat, lon)
        except Exception as exc:  # noqa: BLE001
            return ActionResult(False, detail=str(exc)[:200], kind="send")
        return ActionResult(
            True,
            note=f"location sent: {lat:.5f}, {lon:.5f}",
            title=_t(runtime, turn, "actions.card_location"),
            lines=(f"{lat:.5f}, {lon:.5f}",),
        )

    tools.append(
        Tool(
            name="send_location",
            summary="send a map point here",
            properties={"lat": "latitude", "lon": "longitude"},
            required=("lat", "lon"),
            run=send_location,
            scope="write",
        )
    )

    async def contacts(turn: Turn, args: dict[str, str]) -> ActionResult:
        extras = getattr(runtime, "tg", None)
        if extras is None:
            return ActionResult(False, detail="no Telegram connection", kind="offline")
        query = (args.get("about") or "").strip().lower()
        try:
            rows = await extras.contacts(limit=500)
        except Exception as exc:  # noqa: BLE001
            return ActionResult(False, detail=str(exc)[:200], kind="read")
        if query:
            rows = [
                row
                for row in rows
                if query in str(row.get("name", "")).lower()
                or query in str(row.get("username", "")).lower()
            ]
        if not rows:
            return ActionResult(
                True,
                note="no contact matched",
                title=_t(runtime, turn, "actions.card_contacts", what=query or "all"),
                lines=(_t(runtime, turn, "actions.card_no_contact"),),
            )
        lines = [
            f"{row.get('name') or '—'} — {('@' + row['username']) if row.get('username') else row.get('id')}"
            for row in rows[:12]
        ]
        return ActionResult(
            True,
            note=" | ".join(lines[:12]),
            title=_t(runtime, turn, "actions.card_contacts", what=query or "all"),
            lines=tuple(lines[:4]),
        )

    tools.append(
        Tool(
            name="contacts",
            summary="look through his address book, optionally by name",
            properties={"about": "a name or handle to filter by (optional)"},
            run=contacts,
            scope="read",
        )
    )

    return tools
