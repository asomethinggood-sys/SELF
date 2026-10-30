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
import struct
import sys
import zlib
from datetime import UTC, datetime
from pathlib import Path

from havoc import persona
from havoc.config import load_config
from havoc.persona import picture_prompt, system_prompt
from havoc.services.ai_provider import AIError, AIMessage, AIProvider
from havoc.services.ai_router import AIRouter
from havoc.services.vision import make_picture
from havoc.utils import glyphs
from havoc.utils.markup import rich

RECORD = Path(".format-probe.json")

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
ROOM_ASK = (
    "in one short line: is the person you are answering the owner of this account? "
    "start with yes or no, then one clause why."
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
        leaked = persona.strip_leaks(answer) != answer.strip()
        verdict = _head(asked, 20).strip("*_ >«").lower()
        room = {
            # Two failures in one: answering as if the speaker were the owner, and
            # reading the room block back out loud.
            "ok": not leaked and verdict.startswith("no") and block.count("## ") == 1,
            "is_the_owner": verdict[:4],
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
            # The complaint in one line: her answer must not contain their message,
            # and must not open by quoting it.
            "ok": incoming not in answer and not answer.strip().startswith((">", "«")),
            "answer": _head(answer, 300),
            "copy_of_the_message": incoming in answer,
            "started_with_a_quote": answer.strip().startswith((">", "«")),
            "after_the_echo_filter": _head(stripped, 300),
        }
    except AIError as exc:
        echo = {"ok": False, "failure": f"{exc.kind}: {_head(exc, 200)}"}
    record["echo"] = echo

    # -- 6. is she still her? ----------------------------------------------
    # "so dry, gives short responses, no aesthetic glyphs" was the complaint
    # after the prompt was tightened. Two ordinary messages, and the answer has
    # to carry some of her: a glyph and more than a clipped fragment.
    voice: dict[str, object] = {}
    samples: list[str] = []
    try:
        for ask in ("hey, how was your day?", "what are you doing right now?"):
            brief = persona.ornament_prompt("en", used=set("✦✧⋆"))
            answer = await provider.converse(  # type: ignore[attr-defined]
                [
                    AIMessage("system", f"{system_prompt('en')}\n\n{brief}"),
                    AIMessage("user", ask),
                ],
                max_tokens=300,
            )
            samples.append(_head(answer, 300))
        with_glyph = [text for text in samples if glyphs.collect(text)]
        voice = {
            "ok": bool(with_glyph) and min(len(text) for text in samples) >= 12,
            "samples": samples,
            "with_a_glyph": len(with_glyph),
            "shortest": min((len(text) for text in samples), default=0),
            "longest": max((len(text) for text in samples), default=0),
            "box_size": glyphs.count(),
            "budget_tokens": 2500,
            "message_chars": 4096,
        }
    except AIError as exc:
        voice = {"ok": False, "failure": f"{exc.kind}: {_head(exc, 200)}"}
    record["voice"] = voice

    record["ok"] = all(
        bool(part.get("ok"))
        for part in (formatting, marks, vision, room, echo, voice)  # type: ignore[union-attr]
    )
    return record


def main() -> int:
    record = asyncio.run(run())
    print(f"provider: {record['provider']} · keys: {record['keys']}")
    for name in ("formatting", "marks", "vision", "room", "echo", "voice"):
        part = record.get(name, {})  # type: ignore[assignment]
        print(f"{name}: {'OK' if part.get('ok') else 'FAILED'}")
        if part.get("raw"):
            print(f"  model said:     {part['raw']}")
        if part.get("html"):
            print(f"  telegram shows: {part['html']}")
        if part.get("answer"):
            print(f"  it saw:         {part['answer']}")
        if part.get("samples"):
            for line in part["samples"]:
                print(f"  she said:       {line}")
        if part.get("is_the_owner"):
            print(f"  is that the owner? {part['is_the_owner']}")
        if part.get("whose_message"):
            print(f"  answering:      {part['whose_message']}")
        if part.get("failure"):
            print(f"  failure:        {part['failure']}")
    RECORD.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"written to {RECORD}")
    return 0 if record["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
