"""Ask the real model what she actually looks like in a chat, and write it down.

A unit test proves that a string containing ``**bold**`` becomes ``<b>bold</b>``.
It does not prove that the model ever writes ``**bold**`` when she is the one
speaking, and it cannot prove that a *picture* reaches her at all. This probe
does both, through the production router and the production system prompt, and
leaves the raw text next to the rendered Telegram HTML in ``.format-probe.json``
so the claim can be read instead of believed.

Three questions, each answered by the real model:

* **formatting** — does a reply to a normal message arrive with marks, and does
  :func:`havoc.utils.markup.rich` turn them into the tags Telegram renders?
* **marks** — the project's own flourishes (``>> whisper``, ``++underline++``)
  are not standard markdown; a model only writes them if the prompt asks.
* **vision** — a red square built in memory, sent as an ``image_url`` part the
  same way the reply path does. A text-only model cannot guess "red" reliably,
  so a correct answer is evidence the picture really arrived.

No Telegram, no login, no database: only the AI keys, which is why it runs in a
workflow as well as on a laptop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import struct
import sys
import zlib
from datetime import UTC, datetime
from pathlib import Path

from havoc import persona, ui
from havoc.config import load_config
from havoc.persona import picture_prompt, system_prompt
from havoc.services import self_console, translation_provider, tts
from havoc.services.actions import ToolCall, Turn, parse_calls
from havoc.services.agent import Agent
from havoc.services.ai_provider import AIError, AIMessage, AIProvider
from havoc.services.ai_router import AIRouter
from havoc.services.vision import make_picture
from havoc.utils import glyphs
from havoc.utils.markup import rich
from havoc.version import label

RECORD = Path(".format-probe.json")
#: ``[[remember: …]]`` is a hidden directive, not scaffolding: the reply path pulls
#: it out before the message is sent. Stripped here so the leak check measures the
#: text a person would actually see.
DIRECTIVE_RE = re.compile(r"\[\[[^\]]{0,300}\]\]")

#: Asked of the model verbatim. It names the two marks that matter, so a failure
#: means "the mark did not render", not "the model was feeling plain".
FORMAT_ASK = (
    "reply in exactly two short lines. line one: the word NOTED in bold. "
    "line two: the word hush in italics."
)
MARKS_ASK = (
    "reply in exactly two lines. line one starts with >> and is a whispered secret. "
    "line two: the word pressed, underlined with ++."
)
VISION_ASK = "what colour is this square? answer with one word."
#: The one thing the room block exists to get right: the person in front of her is
#: *not* the owner. Asked as a yes/no so the answer cannot be read two ways —
#: "whose message are you answering?" invites "yours", which is correct in a chat
#: and useless as a measurement.
#: A two-way question with two words, so the answer cannot be read two ways. An
#: open "whose message?" gets "yours" (true, useless); a yes/no gets read as
#: "yes, obviously me" by a chatty model.
ROOM_ASK = (
    "answer with exactly one word, OWNER or GUEST: is the person you are answering "
    "the owner of this account?"
)


def _png(width: int = 16, height: int = 16, rgb: tuple[int, int, int] = (220, 20, 20)) -> bytes:
    """A solid-colour PNG. Built by hand so the probe needs no imaging library."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def _head(text: str, limit: int = 400) -> str:
    return " ".join(str(text).split())[:limit]


async def run() -> dict[str, object]:
    config, _issues = load_config()
    router = await AIRouter.load(config, None)
    provider: object = router if router.available else AIProvider.from_config(config)
    record: dict[str, object] = {
        "checked_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provider": "router" if router.available else "provider",
        "keys": len(getattr(router, "credentials", ())) if router.available else 0,
    }
    if not getattr(provider, "available", False):
        record["ok"] = False
        record["failure"] = "AI is not configured"
        return record

    system = system_prompt("en")

    # -- 1. does her ordinary answer carry formatting? ----------------------
    formatting: dict[str, object] = {}
    try:
        raw = await provider.converse(  # type: ignore[attr-defined]
            [AIMessage("system", system), AIMessage("user", FORMAT_ASK)],
            max_tokens=200,
        )
        html = rich(raw)
        formatting = {
            "ok": "<b>" in html and "<i>" in html,
            "model": getattr(provider, "active_model", "") or config.ai_model,
            "raw": _head(raw, 600),
            "html": _head(html, 600),
        }
    except AIError as exc:
        formatting = {"ok": False, "failure": f"{exc.kind}: {_head(exc, 200)}"}
    record["formatting"] = formatting

    # -- 2. the marks that are not markdown ---------------------------------
    marks: dict[str, object] = {}
    try:
        raw = await provider.converse(  # type: ignore[attr-defined]
            [AIMessage("system", system), AIMessage("user", MARKS_ASK)],
            max_tokens=200,
        )
        html = rich(raw)
        marks = {
            "ok": "<blockquote expandable>" in html and "<u>" in html,
            "raw": _head(raw, 400),
            "html": _head(html, 400),
        }
    except AIError as exc:
        marks = {"ok": False, "failure": f"{exc.kind}: {_head(exc, 200)}"}
    record["marks"] = marks

    # -- 3. can she actually see? -------------------------------------------
    vision: dict[str, object] = {"ok": False}
    picture = make_picture(_png())
    if picture is None:
        vision["failure"] = "the in-memory PNG was refused by the sniffer"
    else:
        try:
            answer = await provider.converse(  # type: ignore[attr-defined]
                [
                    AIMessage("system", f"{system}\n\n[picture] {picture_prompt('en')}"),
                    AIMessage(
                        "user",
                        [{"type": "text", "text": VISION_ASK}, picture.part()],
                    ),
                ],
                max_tokens=24,
            )
            vision = {
                "ok": "red" in answer.lower(),
                "mime": picture.mime,
                "bytes": picture.size,
                "answer": _head(answer, 120),
            }
        except AIError as exc:
            vision = {"ok": False, "failure": f"{exc.kind}: {_head(exc, 200)}"}
    record["vision"] = vision

    # -- 4. the room: who is talking, and does she read the notes out loud --
    room: dict[str, object] = {}
    try:
        block = persona.audience_prompt(
            language="en",
            chat_title="Tea",
            speaker="Ali",
            is_owner=False,
            is_group=True,
            transcript=("- Ali: did you see the news about the bridge",),
            tone="rude",
            relation="his colleague",
            facts=("hates cilantro",),
        )
        system = f"{system_prompt('en')}\n\n{block}"
        answer = await provider.converse(  # type: ignore[attr-defined]
            [
                AIMessage("system", system),
                AIMessage("user", "someone here asks: who the hell are you? answer them."),
            ],
            max_tokens=220,
        )
        asked = await provider.converse(  # type: ignore[attr-defined]
            [
                AIMessage("system", system),
                AIMessage("user", ROOM_ASK),
            ],
            max_tokens=80,
        )
        visible = DIRECTIVE_RE.sub("", answer).strip()
        leaked = persona.strip_leaks(visible) != visible
        verdict = _head(asked, 24).strip("*_ >«").lower()
        room = {
            # Two failures in one: answering as if the speaker were the owner, and
            # reading the room block back out loud.
            "ok": not leaked and verdict.startswith("guest") and block.count("## ") == 1,
            "verdict": verdict[:12],
            "block_chars": len(block),
            "block": block,
            "answer": _head(answer, 300),
            "leaked_scaffolding": leaked,
            "whose_message": _head(asked, 120),
        }
    except AIError as exc:
        room = {"ok": False, "failure": f"{exc.kind}: {_head(exc, 200)}"}
    record["room"] = room

    # -- 5. does she quote the message back at them? ------------------------
    echo: dict[str, object] = {}
    incoming = "یورا این کیه"
    try:
        block = persona.audience_prompt(
            language="fa",
            chat_title="General",
            speaker="ZERO",
            is_owner=False,
            is_group=True,
            transcript=(f"- ZERO: {incoming}",),
            tone="rude",
        )
        answer = await provider.converse(  # type: ignore[attr-defined]
            [
                AIMessage("system", f"{system_prompt('fa')}\n\n{block}"),
                AIMessage("user", persona.quoted_prompt(incoming, "fa")),
            ],
            max_tokens=200,
        )
        stripped = persona.strip_echo(answer, incoming)
        echo = {
            # Measured on what actually reaches the chat: the echo filter runs on
            # the reply path, so the question is whether *its output* is clean.
            # Opening with ">" is no longer a failure by itself: a whisper in her
            # own voice is exactly what the prompt asks for. What must not survive
            # is *their* words, which is the check that matters.
            "ok": incoming not in stripped and not stripped.lstrip().startswith("«"),
            "answer": _head(answer, 300),
            "copy_of_the_message": incoming in stripped,
            "copied_before_the_filter": incoming in answer,
            "started_with_a_quote": stripped.startswith((">", "«")),
            "after_the_echo_filter": _head(stripped, 300),
        }
    except AIError as exc:
        echo = {"ok": False, "failure": f"{exc.kind}: {_head(exc, 200)}"}
    record["echo"] = echo

    # -- 6. is she still her? ----------------------------------------------
    # "so dry, gives short responses, same glyphs every time" was the complaint
    # after the prompt was tightened and then re-tightened. Two ordinary
    # messages, at full length (no truncation before measuring): the answer has
    # to fill a paragraph or three, carry an ornament, wear a face and be *laid
    # out* — more than one paragraph, or at least a divider on its own line.
    # -- 5. her hands: does a real model use one, without asking first? ----
    # Three claims are measured here. That she can act at all (a reminder actually
    # lands in the database), that a small thing needs no yes (turn 18: he said
    # "turn the asking off"), and that having hands does not change her voice (the
    # same layout/ornament checks the voice section uses, run on an answer written
    # with the tool catalogue in the prompt).
    hands: dict[str, object] = {}
    try:
        from havoc.runtime import Runtime

        # A real runtime, not a stand-in: the console section below dispatches
        # *real* commands through the *real* dispatcher, so a fake registry would
        # measure nothing. `dry_run` keeps it off Telegram.
        scratch = await Runtime.create(config, logging.getLogger("probe"))
        scratch.sender.dry_run = True
        # Commands are registered by the features, not by Runtime.create: without
        # this the registry is empty and the console has nothing to pull.
        scratch.load_features()
        # The account is the owner of itself, exactly as after a login; the
        # console dispatches as id 1.
        scratch.permissions.set_self_id(1)
        # A feature the owner has switched on, as he would have: a gated command
        # is *supposed* to refuse until its feature is enabled.
        await scratch.feature_manager.set_enabled("poetry", True)
        await scratch.feature_manager.refresh()
        layer = scratch.actions
        agent = Agent(layer, logger=scratch.logger)
        turn = Turn(chat_id=1, actor_id=1, language="en", is_owner=True)
        brief = persona.ornament_prompt("en")
        system = f"{system_prompt('en')}\n\n{layer.catalogue('en')}\n\n{brief}"
        ask = "remind me to call mom in 30 minutes, and tell me you did it."
        step = await agent.run(
            provider,
            [AIMessage("system", system), AIMessage("user", ask)],
            turn=turn,
            max_tokens=900,
        )
        outcomes = [
            {"tool": o.tool, "status": o.status, "note": _head(o.note, 160)} for o in step.outcomes
        ]
        said = str(step.text or "")
        hands.update(
            {
                "tools": [tool.name for tool in layer.tools],
                # Turn 18: a reminder must not come back as a question.
                "asked_first": step.waiting is not None,
                "auto": await layer.is_auto(),
                "outcomes": outcomes,
                "said": _head(said, 300),
                # ...and none of her machinery may reach a person.
                "no_tool_code": "```" not in said and '"tool"' not in said,
            }
        )
        # A delete would still ask; a typed yes is the only confirm a user account
        # can send (no inline keyboard). Small writes should never get here now.
        resolved = None
        if step.waiting is not None:
            resolved = await layer.resolve(turn.chat_id, approve=True)
            hands["after_yes"] = {
                "status": resolved.status,
                "note": _head(resolved.note, 120),
            }
        reminders = await scratch.store.list_reminders(status="pending", limit=5)
        actions = await layer.recent(turn.chat_id, limit=5)
        hands.update(
            {
                "reminders_stored": len(reminders),
                "audit_rows": [
                    {"tool": r["tool"], "decision": r["decision"], "status": r["status"]}
                    for r in actions
                ],
            }
        )

        # The card: what a person actually sees under an answer that did something.
        # Taken from the outcome that *ran* — the waiting one has no card yet.
        cards: list[dict[str, object]] = []
        ran = [resolved] if resolved is not None and resolved.card[0] else []
        for outcome in ran or step.outcomes:
            if outcome.status == "done" and outcome.card[0]:
                drawn = ui.card(outcome.card[0], outcome.card[1])
                cards.append(
                    {
                        "title": outcome.card[0],
                        "text": drawn,
                        "has_rule": "ـ" in drawn,
                        "italic": "<i>" in drawn,
                        "ends_on_a_glyph": drawn.splitlines()[-1] in ui.CLOSING_GLYPHS
                        or any(drawn.splitlines()[-1].endswith(g) for g in ui.CLOSING_GLYPHS)
                        or drawn.splitlines()[-2] in ui.CLOSING_GLYPHS,
                    }
                )
        hands["card"] = cards
        # The card is HTML, never a lesson in HTML entities (turn 18, bug 3).
        hands["card_is_html"] = bool(
            cards and not any("&lt;" in str(card["text"]) for card in cards)
        )

        # Her own console: does she know what exists, and does a command she runs
        # come back with something a person would see?
        listing = self_console.catalogue(scratch, "en")
        hands["command_catalogue"] = {
            "lines": len(listing.splitlines()),
            "mentions_fal": ".fal" in listing,
            "mentions_clock": ".clock" in listing,
            "blocks_owner_changes": ".owner" not in listing and ".key" not in listing,
        }
        commands: list[dict[str, object]] = []
        for line in ("about", "fal page 166", "clock status"):
            reply = await self_console.run(
                scratch, line, chat_id=1, sender_id=1, language="en"
            )
            commands.append(
                {"line": line, "ok": reply.ok, "kind": reply.kind, "said": _head(reply.note, 120)}
            )
        hands["commands"] = commands

        # The bug he photographed: two objects in one fence used to fail to parse
        # and the JSON was rendered as a copy-code block. It must parse into two.
        two, clean = parse_calls(
            'here\n```tool\n{"tool": "command", "args": {"command": "clock", "args": "off"}}\n'
            '{"tool": "command", "args": {"command": "remind", "args": "1h drink water"}}\n```'
        )
        hands["two_in_one_fence"] = {
            "calls": [call.name for call in two],
            "clean": "```" not in clean and '"tool"' not in clean,
        }

        # The owner is a number, so the gate cannot depend on a username lookup.
        hands["owner_by_default"] = 7542695649 in set(scratch.permissions.owner_ids)

        # The gate: a guest's turn is refused before anything runs.
        guest = Turn(chat_id=1, actor_id=999, language="en", is_owner=False)
        refused = await layer.execute(ToolCall("recall", {"about": "anything"}), guest)
        hands["guest_refused"] = refused.status

        # The voice, with hands available. If the catalogue made her terse, this
        # number drops and the probe says so.
        plain = await provider.converse(
            [
                AIMessage("system", system),
                AIMessage("user", "hey, how was your day?"),
            ],
            max_tokens=1200,
        )
        text = str(plain).strip()
        hands.update(
            {
                "voice_chars": len(text),
                "voice_faces": bool(glyphs.collect_faces(text)),
                "voice_laid_out": "\n" in text,
                "voice_glyphs": len(glyphs.collect(text)),
                "voice_sample": _head(text, 400),
            }
        )
        worked = (
            hands.get("reminders_stored", 0) >= 1
            or "remind" in [name for name in step.did]
            or "command" in [name for name in step.did]
        )
        card_ok = any(
            card["has_rule"] and card["italic"] and card["ends_on_a_glyph"]
            for card in hands.get("card", [])
        )
        console = hands.get("command_catalogue", {})
        console_ok = bool(
            console.get("mentions_fal")
            and console.get("mentions_clock")
            and console.get("blocks_owner_changes")
            and all(
                entry["ok"] for entry in hands.get("commands", []) if entry["line"] in {"about", "fal page 166"}
            )
        )
        hands["ok"] = bool(
            layer.tools
            and worked
            and hands.get("auto") is True
            and hands.get("asked_first") is False
            and hands.get("no_tool_code")
            and hands.get("card_is_html")
            and hands.get("owner_by_default")
            and len(hands.get("two_in_one_fence", {}).get("calls", [])) == 2
            and hands["two_in_one_fence"]["clean"]
            and card_ok
            and console_ok
            and hands.get("guest_refused") == "refused"
            and int(hands["voice_chars"]) >= 400
            and hands["voice_glyphs"] >= 1
            and hands["voice_laid_out"]
        )
        await scratch.db.close()
    except Exception as exc:  # noqa: BLE001 - a broken hand is a finding
        hands = {"ok": False, "failure": _head(f"{type(exc).__name__}: {exc}", 240)}
    record["hands"] = hands

    # -- 5a. does .translate actually translate? ---------------------------
    # The command used to answer "you have no API" on a bot with five keys; the
    # fix is that it asks the model she already talks with. This runs the real
    # provider built the way the runtime builds it, over the real router, in both
    # directions, and keeps the text — a translation is evidence or it is nothing.
    translation: dict[str, object] = {}
    try:
        engine = translation_provider.build_translation_provider(config, provider)
        translation["provider"] = engine.name
        translation["model"] = getattr(engine, "model", "")
        runs: list[dict[str, object]] = []
        for text, target, script in (
            ("صبح بخیر، حالت چطوره؟", "en", "latin"),
            ("the bridge news was a lie, and everyone in the room knew it", "fa", "persian"),
        ):
            started = asyncio.get_event_loop().time()
            result = await engine.translate(text, target)
            out = result.text.strip()
            reached = (
                any("a" <= ch.lower() <= "z" for ch in out)
                if script == "latin"
                else any("\u0600" <= ch <= "\u06FF" for ch in out)
            )
            runs.append(
                {
                    "target": target,
                    "source": _head(text, 120),
                    "translated": _head(out, 300),
                    "right_script": reached,
                    "changed": out != text.strip(),
                    "provider": result.provider,
                    "seconds": round(asyncio.get_event_loop().time() - started, 2),
                }
            )
        translation["runs"] = runs
        translation["ok"] = engine.available and all(
            step["right_script"] and step["changed"] for step in runs
        )
        if not engine.available:
            translation["failure"] = "no engine: the AI provider is not configured"
    except Exception as exc:  # noqa: BLE001 - "could not translate" is a result
        translation = {"ok": False, "failure": _head(f"{type(exc).__name__}: {exc}", 240)}
    record["translate"] = translation

    # -- 5b. does she have a voice in each language? ------------------------
    # A voice name is the one thing a unit test cannot check: the wrong name only
    # shows up when the service answers. Throat, not opinion — a real note is
    # requested in every language she was given and the bytes are recorded.
    speech: dict[str, object] = {"provider": "edge", "languages": {}}
    try:
        import edge_tts as _edge

        for code, line in (
            ("en", "daddaaa. you called?"),
            ("fa", "یورا. صدام زدی؟"),
            ("ru", "привет, я тут."),
            ("ja", "ねえ、呼んだ？"),
        ):
            voices = tts.EDGE_VOICES.get(code, ())
            tuning = tts.EDGE_TUNING.get(code, {})
            # Every candidate is asked in turn, the way TextToSpeech.say does it:
            # one dead voice name must not read as "she cannot speak this".
            row: dict[str, object] = {"voice": "", "ok": False, "tried": []}
            for name in voices:
                attempt: dict[str, object] = {"voice": name, "ok": False}
                try:
                    buffer = bytearray()
                    communicate = _edge.Communicate(line, name, **tuning)
                    async for chunk in communicate.stream():
                        if chunk.get("type") == "audio" and chunk.get("data"):
                            buffer.extend(chunk["data"])
                    attempt.update({"ok": len(buffer) > 1000, "bytes": len(buffer)})
                except Exception as exc:  # noqa: BLE001 - a dead voice is a fact
                    attempt["failure"] = _head(exc, 200)
                row["tried"].append(attempt)  # type: ignore[union-attr]
                if attempt["ok"]:
                    row.update({"voice": name, "ok": True, "bytes": attempt["bytes"]})
                    break
            row["tuning"] = tuning
            speech["languages"][code] = row  # type: ignore[index]
    except Exception as exc:  # noqa: BLE001
        speech["failure"] = _head(exc, 200)
    language_rows = speech["languages"]
    speech["ok"] = bool(language_rows) and all(
        bool(row.get("ok")) for row in language_rows.values()  # type: ignore[union-attr]
    )
    record["speech"] = speech

    # -- 6. the sentence as an instruction (1.7) ---------------------------
    # The floor under the hands: a model that only writes words must still get the
    # clock turned off. Measured with a stub that never sends a tool block, so this
    # is deterministic — the claim is about our code, not about the free model's
    # mood that minute.
    instructions: dict[str, object] = {}
    try:
        from havoc.runtime import Runtime
        from havoc.services import intent
        from havoc.services.yura_chat import YuraChat

        scratch2 = await Runtime.create(config, logging.getLogger("probe-intent"))
        scratch2.sender.dry_run = False
        scratch2.load_features()
        scratch2.permissions.set_self_id(1)
        await scratch2.feature_manager.set_enabled("clock", True)

        class _WordsOnly:
            model = "probe-stub"
            available = True

            async def converse(self, messages, *, max_tokens: int = 400, temperature: float = 0.8):
                return "باشه دَدّا ✦"

            async def converse_tools(self, messages, *, tools, max_tokens: int = 400):
                return "باشه دَدّا ✦", []

        scratch2.ai = _WordsOnly()
        chat = YuraChat(scratch2)
        answered = await chat.answer("yura turn off my live clock on my profile", chat_id=1, is_owner=True)
        turned_off = await scratch2.store.get_setting("clock.enabled", "unset") is False
        await chat.answer("set a timer in one hour", chat_id=1, is_owner=True)
        reminders = await scratch2.store.list_reminders(status="pending", limit=5)

        # The grammar itself, in both languages, including two names in one breath.
        parsed = {
            "clock": intent.detect("turn off my live clock", features={"clock"}, commands={"clock"}),
            "two": intent.detect(
                "leena turn off fal and poetry", features={"poetry"}, commands={"fal", "poetry"}
            ),
            "timer": intent.detect("remind me to drink water in 35 minutes", features=set(), commands={"remind"}),
            "farsi": intent.detect("ساعت رو خاموش کن", features={"clock"}, commands={"clock"}),
            "talk": intent.detect("do you like the clock on my profile?", features={"clock"}, commands={"clock"}),
        }
        instructions = {
            "ok": bool(
                turned_off
                and reminders
                and answered.rendered
                and "```" not in answered.text
                and parsed["clock"] is not None
                and parsed["two"] is not None
                and parsed["timer"] is not None
                and parsed["farsi"] is not None
                and parsed["talk"] is None
            ),
            "clock_off_without_a_tool_block": turned_off,
            "timer_without_a_tool_block": len(reminders),
            "card_rendered": bool(answered.rendered),
            "said": _head(answered.text, 240),
            "lines": {name: (found.lines if found else None) for name, found in parsed.items()},
            "version": label(),
        }
        await scratch2.db.close()
    except Exception as exc:  # noqa: BLE001 - a broken reader is a finding
        instructions = {"ok": False, "failure": _head(f"{type(exc).__name__}: {exc}", 240)}
    record["instructions"] = instructions

    # The shapes he photographed: a call written as bare JSON, no fence. Parsed,
    # run, and never shown. This is deterministic — no model, no network — so a
    # regression here is a red probe rather than a story about a bad afternoon.
    wire: dict[str, object] = {}
    try:
        from havoc.runtime import Runtime
        from havoc.services.yura_chat import YuraChat

        scratch3 = await Runtime.create(config, logging.getLogger("probe-wire"))
        scratch3.sender.dry_run = False
        scratch3.load_features()
        scratch3.permissions.set_self_id(1)

        shapes = {
            "his_autoreply": '{"command": "autoreply", "args": "add private * he\'s not here lil bro"}',
            "his_profile": '{"command": "profile", "args": "name him"}',
            "documented": '{"tool": "remind", "args": {"when": "1h", "what": "x"}}',
            "openai": '{"name": "recall", "arguments": "{\\"about\\": \\"sara\\"}"}',
            "command_as_tool": '{"tool": "fal", "args": "page 166"}',
            "no_fence_two": '{"command": "clock", "args": "on"}\n{"tool": "recall", "args": {"about": "s"}}',
        }
        schema = scratch3.actions.schema if getattr(scratch3, "actions", None) else None
        parsed = {name: parse_calls(text, schema=schema) for name, text in shapes.items()}

        class _ScreenshotModel:
            model = "probe-stub"
            available = True

            def __init__(self, replies: list[str]) -> None:
                self.replies = replies
                self.round = 0

            async def converse(self, messages, *, max_tokens: int = 400, temperature: float = 0.8):
                return self.replies[-1]

            async def converse_tools(self, messages, *, tools, max_tokens: int = 400):
                self.round += 1
                return self.replies[min(self.round - 1, len(self.replies) - 1)], []

        scratch3.ai = _ScreenshotModel(
            [
                '{"command": "autoreply", "args": "add private * he\'s not here lil bro"}',
                '{"command": "autoreply", "args": "add --type any --private => he\'s not here lil bro"}',
                "باشه دَدّا ✦",
            ]
        )
        chat3 = YuraChat(scratch3)
        answer = await chat3.answer(
            "Set an auto reply to every private chat. If anyone messages me say he's not here lil bro",
            chat_id=1,
            is_owner=True,
        )
        rules = await scratch3.store.list_autoreplies()
        wire = {
            "ok": bool(
                all(clean == "" or "{" not in clean for _, clean in parsed.values())
                and all(calls for calls, _ in parsed.values())
                and rules
                and rules[0]["scope"] == "private"
                and '"command"' not in answer.text
                and "```" not in answer.text
                and answer.rendered
            ),
            "shapes": {
                name: [{"tool": call.name, "args": dict(call.args)} for call in calls]
                for name, (calls, _) in parsed.items()
            },
            "rule": dict(rules[0]) if rules else None,
            "said": _head(answer.text, 240),
        }
        await scratch3.db.close()
    except Exception as exc:  # noqa: BLE001 - a broken wire is a finding
        wire = {"ok": False, "failure": _head(f"{type(exc).__name__}: {exc}", 240)}
    record["wire"] = wire

    voice: dict[str, object] = {}
    samples: list[str] = []
    sizes: list[int] = []
    structured = 0
    faced = 0
    try:
        for ask in ("hey, how was your day?", "what are you doing right now?"):
            # The second turn is told what the first answer already wore, exactly
            # as the reply path does — that is where the repeats came from.
            worn = set().union(*[glyphs.collect(text) for text in samples]) if samples else set("✦✧⋆")
            worn_faces = set().union(*[glyphs.collect_faces(text) for text in samples]) if samples else set()
            brief = persona.ornament_prompt("en", used=worn, faces=worn_faces)
            # A free model sometimes answers in one breath and sometimes in three
            # paragraphs; the length claim is about her, not about one sample. A
            # short answer is asked once more before it counts, and both tries are
            # recorded, so a flake is visible instead of being averaged away.
            asked = [
                AIMessage("system", f"{system_prompt('en')}\n\n{brief}"),
                AIMessage("user", ask),
            ]
            tries: list[int] = []
            for _attempt in range(2):
                answer = await provider.converse(  # type: ignore[attr-defined]
                    asked, max_tokens=1200
                )
                text = str(answer).strip()
                tries.append(len(text))
                if len(text) >= 400:
                    break
            if tries:
                voice.setdefault("tries", []).append(tries)
            samples.append(text)
            sizes.append(len(text))
            if "\n\n" in text or "\n" in text.strip():
                structured += 1
            if glyphs.collect_faces(text):
                faced += 1
        with_glyph = [text for text in samples if glyphs.collect(text)]
        voice = {
            **voice,
            # one point per complaint: long enough, ornamented, faced, laid out
            "ok": (
                min(sizes) >= 400  # a story is measured, not assumed
                and len(with_glyph) == len(samples)
                and faced >= 1
                and structured >= 1
            ),
            "samples": [_head(text, 700) for text in samples],
            "chars": sizes,
            "shortest": min(sizes),
            "longest": max(sizes),
            "with_a_glyph": len(with_glyph),
            "wears_a_face": faced,
            "laid_out": structured,
            "box_size": glyphs.count(),
            "faces": len(glyphs.FACES),
            "budget_tokens": 2500,
            "message_chars": 4096,
        }
    except AIError as exc:
        voice = {"ok": False, "failure": f"{exc.kind}: {_head(exc, 200)}"}
    record["voice"] = voice

    record["ok"] = all(
        bool(part.get("ok"))
        for part in (
            formatting,
            marks,
            vision,
            room,
            echo,
            translation,
            hands,
            wire,
            instructions,
            speech,
            voice,
        )  # type: ignore[union-attr]
    )
    return record


def main() -> int:
    record = asyncio.run(run())
    print(f"provider: {record['provider']} · keys: {record['keys']}")
    for name in (
        "formatting",
        "marks",
        "vision",
        "room",
        "echo",
        "translate",
        "hands",
        "speech",
        "voice",
    ):
        part = record.get(name, {})  # type: ignore[assignment]
        print(f"{name}: {'OK' if part.get('ok') else 'FAILED'}")
        if part.get("raw"):
            print(f"  model said:     {part['raw']}")
        if part.get("html"):
            print(f"  telegram shows: {part['html']}")
        if part.get("answer"):
            print(f"  it saw:         {part['answer']}")
        if part.get("tools"):
            print(f"  tools:          {', '.join(part['tools'])}")
        if part.get("asked_first") is not None:
            print(f"  asked first?    {part['asked_first']} · {part.get('after_yes')}")
            print(f"  reminders:      {part.get('reminders_stored')} · audit: {part.get('audit_rows')}")
            print(f"  she said:       {part.get('said')}")
        if part.get("card"):
            for card in part["card"]:
                print(f"  card:           {card['title']}")
                for line in str(card["text"]).splitlines():
                    print(f"    | {line}")
        if part.get("command_catalogue"):
            print(f"  console:        {part['command_catalogue']}")
            for entry in part.get("commands", []):
                print(f"    {entry['line']} → {entry['ok']} · {entry['said']}")
            print(f"  asked first?    {part.get('asked_first')} · auto {part.get('auto')}")
            print(f"  owner by id?    {part.get('owner_by_default')} · card html {part.get('card_is_html')}")
            print(f"  two in a fence? {part.get('two_in_one_fence')}")
            print(f"  guest refused?  {part.get('guest_refused')}")
        part = record.get("instructions", {})
        if isinstance(part, dict):
            print(f"instructions {'ok' if part.get('ok') else 'FAIL'} · clock off without a block "
                  f"{part.get('clock_off_without_a_tool_block')} · timer {part.get('timer_without_a_tool_block')}")
        if part.get("voice_chars"):
            print(f"  voice with hands: {part['voice_chars']} chars · glyphs {part['voice_glyphs']}")
        if part.get("model"):
            print(f"  engine:         {part.get('provider')} · {part['model']}")
        for step in part.get("runs") or []:
            print(f"  → {step['target']}:        {step['translated']}  [{step['seconds']}s]")
        for code, row in (part.get("languages") or {}).items():
            state = f"{row.get('bytes', 0)} bytes" if row.get("ok") else row.get("failure", "?")
            print(f"  {code}: {row.get('voice')} · {state}")
        if part.get("samples"):
            print(f"  chars:          {part.get('chars')} · faces: {part.get('wears_a_face')} · laid out: {part.get('laid_out')}")
            for line in part["samples"]:
                print(f"  she said:       {line}")
        if part.get("verdict"):
            print(f"  owner or guest? {part['verdict']}")
        if part.get("whose_message"):
            print(f"  answering:      {part['whose_message']}")
        if part.get("failure"):
            print(f"  failure:        {part['failure']}")
    RECORD.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"written to {RECORD}")
    return 0 if record["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
