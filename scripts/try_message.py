#!/usr/bin/env python3
"""Send one message to Yura and see exactly what she would do.

No Telegram, no network, no credentials: the same runtime the bot boots with,
behind fake Telegram objects, so this prints *every* message, reaction, sticker,
media file and scheduled job a real message would produce.

    python scripts/try_message.py "یورا فال بگیر"
    python scripts/try_message.py "say I am a good girl and repeat 3 times" --lang en
    python scripts/try_message.py ".note list" --command
    python scripts/try_message.py "به پیامش قلب واکنش بده" --reply-to 77
    python scripts/try_message.py "یورا من چطورم" --ai "خوبی دَدّا"

``--ai`` is what the model answers; the point of this tool is to see what she
*does* beyond talking, so the default model says nothing useful on purpose.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import logging
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from conftest import FakeMessage, FakeTelegram, incoming, invoke, make_config  # noqa: E402

from havoc.runtime import Runtime  # noqa: E402

OWNER_ID = 42


class QuietProvider:
    """A model that only talks — the case where abilities used to be lost."""

    model = "try-message"
    available = True

    def __init__(self, reply: str) -> None:
        self.reply = reply

    async def converse(self, messages: Any, *, max_tokens: int = 400, temperature: float = 0.85) -> str:
        return self.reply


def _plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", str(text or "")))


def _describe(message: Any) -> str:
    text = _plain(getattr(message, "text", "") or "").strip()
    kind = ""
    media = getattr(message, "media", None) or (getattr(message, "kwargs", {}) or {}).get("file")
    if media:
        kind = f"[file: {Path(str(media)).name}]"
    parts = [part for part in (kind, text) if part]
    if not parts:
        parts = ["[no text]"]
    return f"chat {getattr(message, 'chat_id', '?')} · " + " · ".join(parts).replace("\n", " / ")


async def run(args: argparse.Namespace) -> int:
    workspace = Path(tempfile.mkdtemp(prefix="try-message-"))
    config = make_config(workspace)
    runtime = await Runtime.create(config, logging.getLogger("try-message"))
    runtime.load_features()
    runtime.telegram = FakeTelegram()
    runtime.client = runtime.telegram.client
    runtime.sender.client = runtime.client
    runtime.permissions.set_self_id(OWNER_ID)
    runtime.ai = QuietProvider(args.ai)
    # Persona on, so the answer path (the one he actually talks to) is the path
    # being exercised, not a bare command.
    await runtime.store.set_feature("persona", True)
    await runtime.feature_manager.refresh()
    runtime.client.history.append(FakeMessage(id=args.reply_to or 0, chat_id=args.chat, sender_id=5, text="…"))

    before = len(runtime.client.sent)
    reply_to = None
    if args.reply_to:
        reply_to = FakeMessage(id=args.reply_to, chat_id=args.chat, sender_id=5, text="…")

    text = args.text
    is_command = args.command or text.startswith(runtime.prefix)
    if args.private and not is_command:
        # In his own DM the auto-reply gate is deliberately closed ("that was
        # your own side of a chat"), so a private test goes through `.yura` —
        # which is how he actually calls her there.
        text = f"{runtime.prefix}yura {text}"
        is_command = True
    if is_command:
        # The router runs prefixed lines; `invoke` is that same path without a
        # network in the middle.
        produced = await invoke(
            runtime,
            text,
            chat_id=args.chat,
            reply=reply_to,
            is_private=args.private,
        )
    else:
        # Everything else is a message she is meant to *answer*: the persona
        # path, in a group, with the model talking and her hands doing.
        await incoming(
            runtime,
            FakeMessage(
                id=901,
                chat_id=args.chat,
                sender_id=OWNER_ID,
                text=text,
                is_private=args.private,
                is_group=not args.private,
                reply=reply_to,
            ),
        )
        produced = runtime.client.sent[before:]

    print(f"\n  said  {args.text}")
    print(f"  chat  {args.chat} ({'private' if args.private else 'group'}) · language {args.lang}\n")
    if produced:
        for index, message in enumerate(produced, 1):
            print(f"  {index:>2}. {_describe(message)}")
    else:
        print("  (nothing was sent)")

    reactions = getattr(runtime.client, "reactions", []) or []
    if reactions:
        print("\n  reactions")
        for chat_id, message_id, emoji in reactions[-5:]:
            print(f"   · {emoji} on message {message_id} in {chat_id}")

    plans = await runtime.db.fetch_all("SELECT label, status FROM plans") if args.db else []
    if plans:
        print("\n  plans")
        for row in plans:
            print(f"   · {row['label']} ({row['status']})")

    jobs = await runtime.db.fetch_all("SELECT kind, run_at FROM jobs ORDER BY id DESC LIMIT 50")
    if jobs:
        print(f"\n  scheduled ({len(jobs)})")
        for row in jobs[:6]:
            print(f"   · {row['kind']} at {row['run_at']}")
        if len(jobs) > 6:
            print(f"   … and {len(jobs) - 6} more")

    reminders = await runtime.db.fetch_all("SELECT text, remind_at FROM reminders")
    if reminders:
        print("\n  reminders")
        for row in reminders:
            print(f"   · {row['text']} at {row['remind_at']}")

    print(f"\n  database kept at {config.database_path}\n")
    await runtime.db.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Send one message to Yura, offline, and watch her hands.")
    parser.add_argument("text", help="the sentence, exactly as he would type it")
    parser.add_argument("--lang", default="en", choices=("en", "fa"), help="the language she answers in")
    parser.add_argument("--chat", type=int, default=OWNER_ID, help="the chat it arrives in")
    parser.add_argument("--private", action="store_true", help="a direct message (default: a group)")
    parser.add_argument("--reply-to", type=int, default=0, help="pretend he used Telegram's reply on this id")
    parser.add_argument("--command", action="store_true", help="treat the text as a .command line")
    parser.add_argument("--ai", default="باشه.", help="what the model answers (it never runs anything)")
    parser.add_argument("--db", action="store_true", help="also list plans")
    args = parser.parse_args()
    if args.private is False and args.chat == OWNER_ID:
        # The owner's own id is a private chat: pick a group id instead so the
        # "in a group" default is honest.
        args.chat = -1001234567890
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
