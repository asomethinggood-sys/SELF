"""Prove the two claims that cannot be proven by a unit test.

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

    record["ok"] = bool(formatting.get("ok")) and bool(vision.get("ok"))
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
    RECORD.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"written to {RECORD}")
    return 0 if record["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
