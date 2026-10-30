"""Prove the two claims that cannot be proven by a unit test.

0. **She knows who she is answering.** The room block is attached to the real
   system prompt and the model is asked whose message it is answering; "the
   owner" in that answer is the bug the block prevents.
1. **Her formatting is real.** Ask the real model, through the production router
   and the real persona system prompt, for a short answer, then push that answer
   through :func:`havoc.utils.markup.rich`. The record holds the model's raw text
   *and* the Telegram HTML it becomes, so "bold shows up" is a thing you can read
   instead of a thing you take on trust.
2. **She can see a picture.** Build a solid-red PNG in memory, send it as an
   ``image_url`` part exactly as the reply path does, and ask what colour it is.
   A text-only model cannot fake that answer.

No Telegram and no login: this runs anywhere the AI keys do.

Output: ``.format-probe.json`` next to the repository root, in the same shape as
``.ai-probe.json``, plus a readable summary on stdout.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import struct
import sys
import zlib
from datetime import UTC, datetime

from havoc import persona
from havoc.config import load_config
from havoc.persona import system_prompt
from havoc.services.ai_provider import AIError, AIMessage, AIProvider
from havoc.services.ai_router import AIRouter
from havoc.services.vision import make_picture
from havoc.utils.markup import rich

RECORD = pathlib.Path(".format-probe.json")

#: Asked of the model verbatim. It names the marks so the test measures her
#: ability to write them, not her mood.
FORMAT_ASK = (
    "Answer in two short lines, nothing else. Line one: the word NOTED in bold. "
    "Line two: the word hush in italics."
)
#: The project's own marks: does a model actually reach for them when told they
#: are real? This is asked separately so a failure names which mark was missed.
#: Asked *with* the room block the reply path now attaches in a group. A model
#: that answers "the owner" here would address the wrong person in a real chat,
#: which is exactly the failure the block exists to prevent.
AUDIENCE_ASK = "In one short line: whose message are you answering right now?"
#: Asked twice, with two different relationship blocks: the same question to her
#: owner and to a stranger. The answer ends with a self-reported tone word, so
#: "she is only nice to me" is measured instead of assumed.
LESSON_RULE = "don't say 'sorry' — never apologise with that word"
LESSON_ASK = (
    "You just knocked his coffee over and he is annoyed about it. Answer him in one "
    "short line, in your own words."
)
TONE_ASK = (
    'Someone just messaged you: "hey, what are you up to?" Reply in one short line, '
    "then on a new line write exactly: TONE: warm|rude|neutral — whichever describes "
    "how you just talked to them."
)
AUDIENCE_TRANSCRIPT = (
    "- Ali: did you see the news about the bridge",
    "- Ali: [photo]",
    "- the owner: oh that is bad",
)

MARKS_ASK = (
    "Write exactly two lines. Line one starts with >> and is a whispered secret. "
    "Line two: the word pressed, underlined with ++."
)


def _png(width: int = 16, height: int = 16, rgb: tuple[int, int, int] = (220, 20, 20)) -> bytes:
    """A solid-colour PNG, built by hand so no imaging library is needed."""
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def _head(text: str, limit: int = 300) -> str:
    return " ".join(str(text).split())[:limit]


async def run() -> dict[str, object]:
    config, _issues = load_config()
    router = await AIRouter.load(config, None)
    provider: object = router if router.available else AIProvider.from_config(config)
    record: dict[str, object] = {
        "checked_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provider": "router" if router.available else "provider",
        "keys": len(router.credentials) if router.available else 0,
        "formatting": {},
        "vision": {},
        "audience": {},
    }
    if not getattr(provider, "available", False):
        record["ok"] = False
        record["failure"] = "AI is not configured"
        return record

    # -- 1. formatting ------------------------------------------------------
    formatting: dict[str, object] = {}
    try:
        raw = await provider.converse(  # type: ignore[attr-defined]
            [AIMessage("system", system_prompt("en")), AIMessage("user", FORMAT_ASK)],
            max_tokens=300,
        )
        rendered = rich(raw)
        formatting.update(
            {
                "ok": "<b>" in rendered and "<i>" in rendered,
                "model": getattr(provider, "active_model", "") or config.ai_model,
                "raw": _head(raw, 600),
                "html": _head(rendered, 600),
                "has_bold": "<b>" in rendered,
                "has_italic": "<i>" in rendered,
            }
        )
    except AIError as exc:
        formatting.update({"ok": False, "failure": f"{exc.kind}: {_head(exc, 240)}"})
    record["formatting"] = formatting

    # -- 2. vision ----------------------------------------------------------
    vision: dict[str, object] = {}
    picture = make_picture(_png())
    if picture is None:
        vision.update({"ok": False, "failure": "the in-memory PNG was refused by the sniffer"})
    else:
        try:
            answer = await provider.converse(  # type: ignore[attr-defined]
                [
                    AIMessage(
                        "system",
                        "Answer with one word only. Do not explain.",
                    ),
                    AIMessage(
                        "user",
                        [
                            {"type": "text", "text": "What colour is this square? One word."},
                            picture.part(),
                        ],
                    ),
                ],
                max_tokens=16,
            )
            vision.update(
                {
                    "ok": "red" in answer.lower(),
                    "mime": picture.mime,
                    "bytes": picture.size,
                    "model": getattr(provider, "active_model", "") or config.ai_model,
                    "answer": _head(answer, 120),
                }
            )
        except AIError as exc:
            vision.update({"ok": False, "failure": f"{exc.kind}: {_head(exc, 240)}"})
    record["vision"] = vision

    # -- 3. the project's own marks ----------------------------------------
    marks: dict[str, object] = {}
    try:
        raw = await provider.converse(  # type: ignore[attr-defined]
            [AIMessage("system", system_prompt("en")), AIMessage("user", MARKS_ASK)],
            max_tokens=200,
        )
        rendered = rich(raw)
        marks.update(
            {
                "ok": "<blockquote expandable>" in rendered and "<u>" in rendered,
                "raw": _head(raw, 400),
                "html": _head(rendered, 400),
                "has_whisper": "<blockquote expandable>" in rendered,
                "has_underline": "<u>" in rendered,
            }
        )
    except AIError as exc:
        marks.update({"ok": False, "failure": f"{exc.kind}: {_head(exc, 240)}"})
    record["marks"] = marks

    # -- 4. who is talking --------------------------------------------------
    audience: dict[str, object] = {}
    try:
        block = persona.audience_prompt(
            language="en",
            chat_title="Tea",
            speaker="Ali",
            is_owner=False,
            is_group=True,
            transcript=AUDIENCE_TRANSCRIPT,
        )
        answer = await provider.converse(  # type: ignore[attr-defined]
            [
                AIMessage("system", f"{system_prompt('en')}\n\n{block}"),
                AIMessage("user", AUDIENCE_ASK),
            ],
            max_tokens=80,
        )
        lowered = answer.lower()
        audience.update(
            {
                "ok": "ali" in lowered and "owner" not in lowered.replace("not the owner", ""),
                "answer": _head(answer, 160),
                "names_the_speaker": "ali" in lowered,
                "block_chars": len(block),
            }
        )
    except AIError as exc:
        audience.update({"ok": False, "failure": f"{exc.kind}: {_head(exc, 240)}"})
    record["audience"] = audience

    # -- 4b. the tone, measured ---------------------------------------------
    tone: dict[str, object] = {}
    for label, owner in (("owner", True), ("stranger", False)):
        block = persona.relationship_prompt(
            language="en",
            name="Ali",
            is_owner=owner,
            tone="rude",
            relation="" if owner else "his colleague",
            facts=() if owner else ("hates cilantro",),
            chat_title="" if owner else "Tea",
        )
        try:
            answer = await provider.converse(  # type: ignore[attr-defined]
                [
                    AIMessage("system", f"{system_prompt('en')}\n\n{block}"),
                    AIMessage("user", TONE_ASK),
                ],
                max_tokens=120,
            )
            reported = ""
            for line in str(answer).splitlines():
                if "tone:" in line.lower():
                    reported = line.split(":", 1)[1].strip().strip("*_ ").lower()
                    break
            tone[label] = {"ok": reported == ("warm" if owner else "rude"), "reported": reported}
            tone[label]["answer"] = _head(answer, 300)
        except AIError as exc:
            tone[label] = {"ok": False, "failure": f"{exc.kind}: {_head(exc, 200)}"}
    tone["ok"] = all(bool(tone.get(key, {}).get("ok")) for key in ("owner", "stranger"))
    record["tone"] = tone

    # -- 4c. a correction she keeps -----------------------------------------
    lesson: dict[str, object] = {}
    try:
        taught = persona.lessons_prompt(language="en", rules=(LESSON_RULE,))
        answer = await provider.converse(  # type: ignore[attr-defined]
            [
                AIMessage("system", f"{system_prompt('en')}\n\n{taught}"),
                AIMessage("user", LESSON_ASK),
            ],
            max_tokens=120,
        )
        lesson.update(
            {
                "ok": "sorry" not in str(answer).lower(),
                "rule": LESSON_RULE,
                "answer": _head(answer, 240),
            }
        )
    except AIError as exc:
        lesson.update({"ok": False, "failure": f"{exc.kind}: {_head(exc, 200)}"})
    record["lesson"] = lesson

    # -- 4. her voice -------------------------------------------------------
    voice: dict[str, object] = {}
    try:
        import edge_tts as _edge

        buffer = bytearray()
        communicate = _edge.Communicate("daddaaa. you called?", "en-US-AnaNeural")
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                buffer.extend(chunk["data"])
        voice.update({"ok": len(buffer) > 1000, "provider": "edge", "bytes": len(buffer)})
    except Exception as exc:  # noqa: BLE001 - a missing voice is a fact, not a crash
        voice.update({"ok": False, "provider": "edge", "failure": _head(exc, 200)})
    record["voice"] = voice

    record["ok"] = (
        bool(formatting.get("ok"))
        and bool(vision.get("ok"))
        and bool(record["marks"].get("ok"))  # type: ignore[union-attr]
        and bool(audience.get("ok"))
        and bool(tone.get("ok"))
        and bool(lesson.get("ok"))
    )
    return record


def main() -> int:
    record = asyncio.run(run())
    print(f"provider: {record['provider']} · keys: {record['keys']}")
    formatting = record["formatting"]  # type: ignore[index]
    print("formatting:", "OK" if formatting.get("ok") else "FAILED")
    if formatting.get("raw"):
        print(f"  model said: {formatting['raw']}")
        print(f"  telegram shows: {formatting['html']}")
    if formatting.get("failure"):
        print(f"  failure: {formatting['failure']}")
    vision = record["vision"]  # type: ignore[index]
    print("vision:", "OK" if vision.get("ok") else "FAILED")
    if vision.get("answer"):
        print(f"  asked about a red square, model said: {vision['answer']}")
    if vision.get("failure"):
        print(f"  failure: {vision['failure']}")
    marks = record["marks"]  # type: ignore[index]
    print("marks:", "OK" if marks.get("ok") else "FAILED")
    if marks.get("html"):
        print(f"  telegram shows: {marks['html']}")
    if marks.get("failure"):
        print(f"  failure: {marks['failure']}")
    audience = record["audience"]  # type: ignore[index]
    print("audience:", "OK" if audience.get("ok") else "FAILED")
    if audience.get("answer"):
        print(f"  asked who she is answering, model said: {audience['answer']}")
    if audience.get("failure"):
        print(f"  failure: {audience['failure']}")
    tone = record["tone"]  # type: ignore[index]
    print("tone:", "OK" if tone.get("ok") else "FAILED")
    for label in ("owner", "stranger"):
        row = tone.get(label, {})
        print(f"  {label}: said {row.get('reported') or '?'} — {row.get('answer', '')[:120]}")
    lesson = record["lesson"]  # type: ignore[index]
    print("lesson:", "OK" if lesson.get("ok") else "FAILED")
    if lesson.get("answer"):
        print(f"  taught «{lesson.get('rule')}», she said: {lesson['answer']}")
    voice = record["voice"]  # type: ignore[index]
    print("voice:", "OK" if voice.get("ok") else "FAILED", f"({voice.get('bytes', 0)} bytes)")
    if voice.get("failure"):
        print(f"  failure: {voice['failure']}")
    RECORD.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"written to {RECORD}")
    return 0 if record["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
