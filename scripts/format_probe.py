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
from havoc.services.actions import ActionLayer, ToolCall, Turn
from havoc.services.agent import Agent
from havoc.services.ai_provider import AIError, AIMessage, AIProvider
from havoc.services.ai_router import AIRouter
from havoc.services.memory import MemoryStore
from havoc.services.storage import Store
from havoc.services.tools import build_tools
from havoc.services.vision import make_picture
from havoc.utils import glyphs
from havoc.utils.markup import rich

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
            "ok": incoming not in stripped and not stripped.startswith((">", "«")),
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
    # -- 5. her hands: does a real model use one, and does it ask first? ----
    # Two claims are measured here. That she can act at all (a reminder actually
    # lands in the database, after a confirmation she asked for), and that having
    # hands does not change her voice (the same layout/ornament checks the voice
    # section uses, run on an answer written with the tool catalogue in the prompt).
    hands: dict[str, object] = {}
    try:
        from types import SimpleNamespace

        from havoc.database import Database
        from havoc.migrations import apply_migrations

        db = Database(":memory:")
        await db.connect()
        await apply_migrations(db)
        store = Store(db)
        scratch = SimpleNamespace(
            store=store,
            memory=MemoryStore(db),
            logger=logging.getLogger("probe"),
            client=None,
            metrics=None,
            translator=None,
            config=SimpleNamespace(timezone="UTC", data_dir="/tmp"),
        )
        layer = ActionLayer(scratch, build_tools(scratch))
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
        hands.update(
            {
                "tools": [tool.name for tool in layer.tools],
                "asked_first": step.waiting is not None,
                "outcomes": outcomes,
                "said": _head(step.text, 300),
            }
        )
        # The yes. A user account cannot send an inline keyboard, so the confirm
        # is typed — but the *policy* is the same one a button would have used.
        if step.waiting is not None:
            resolved = await layer.resolve(turn.chat_id, approve=True)
            reminders = await store.list_reminders(status="pending", limit=5)
            actions = await layer.recent(turn.chat_id, limit=5)
            hands.update(
                {
                    "after_yes": {"status": resolved.status, "note": _head(resolved.note, 120)},
                    "reminders_stored": len(reminders),
                    "audit_rows": [
                        {"tool": r["tool"], "decision": r["decision"], "status": r["status"]}
                        for r in actions
                    ],
                }
            )

        # The card: what a person actually sees under an answer that did something.
        cards: list[dict[str, object]] = []
        for outcome in step.outcomes:
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
        for line in ("about", "fal page 166", "clock on -- timezone Asia/Tehran"):
            reply = await self_console.run(
                scratch, line, chat_id=1, sender_id=1, language="en"
            )
            commands.append(
                {"line": line, "ok": reply.ok, "kind": reply.kind, "said": _head(reply.note, 120)}
            )
        hands["commands"] = commands

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
            (step.waiting is not None and hands.get("reminders_stored", 0) >= 1)
            or "remind" in [name for name in step.did]
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
            and any(entry["ok"] for entry in hands.get("commands", []) if entry["line"] == "about")
        )
        hands["ok"] = bool(
            layer.tools
            and worked
            and card_ok
            and console_ok
            and hands.get("guest_refused") == "refused"
            and int(hands["voice_chars"]) >= 400
            and hands["voice_glyphs"] >= 1
            and hands["voice_laid_out"]
        )
        await db.close()
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
            answer = await provider.converse(  # type: ignore[attr-defined]
                [
                    AIMessage("system", f"{system_prompt('en')}\n\n{brief}"),
                    AIMessage("user", ask),
                ],
                max_tokens=1200,
            )
            text = str(answer).strip()
            samples.append(text)
            sizes.append(len(text))
            if "\n\n" in text or "\n" in text.strip():
                structured += 1
            if glyphs.collect_faces(text):
                faced += 1
        with_glyph = [text for text in samples if glyphs.collect(text)]
        voice = {
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
            print(f"  guest refused?  {part.get('guest_refused')}")
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
