#!/usr/bin/env python3
"""Boot the bot for real, with only Telegram replaced, and report what happened.

Why this file exists
--------------------
1.7 shipped a build that logged in and died nine seconds later: the runtime still
called a method that had been deleted along with the queue it belonged to. Every
unit test passed. No test started the thing.

The boot path is the seam between `main`, `Runtime`, the event engine, the
scheduler, the locales and the feature loader — and seams are where a project
dies quietly. So the boot is now something you can run on its own:

    python scripts/boot_probe.py            # a verdict, in one line
    python scripts/boot_probe.py --json     # the same, machine-readable

It uses a temporary database, a stub client, and no network. It asserts what a
user would notice: the process returns success, handlers are registered, the
owner is told she is awake, the banner is a sentence rather than a locale key,
and shutdown is clean.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OWNER_ID = 7542695649


class _Message:
    id = 1


class _Info:
    user_id = OWNER_ID
    username = "Edited_by_nyx"
    first_name = "Owner"
    last_name = ""


class StubClient:
    """Records what the bot sends; accepts the handlers it registers."""

    def __init__(self) -> None:
        self.sent: list[tuple[Any, str]] = []
        self.handlers: list[Any] = []
        self.connected = False

    def add_event_handler(self, callback: Any, event: Any = None) -> None:  # noqa: ARG002 - telethon shape
        self.handlers.append(callback)

    async def send_message(self, chat_id: Any, text: str, **_: Any) -> _Message:
        self.sent.append((chat_id, text))
        return _Message()

    async def send_file(self, chat_id: Any, *_: Any, **__: Any) -> _Message:
        self.sent.append((chat_id, "<file>"))
        return _Message()

    async def get_entity(self, value: Any) -> Any:
        return value

    async def get_messages(self, *_: Any, **__: Any) -> list[Any]:
        return []

    async def disconnect(self) -> None:
        self.connected = False


class StubTelegram:
    """Stands in for `havoc.client.TelegramService` — no network, no login."""

    def __init__(self, config: Any = None, logger: Any = None) -> None:
        self.config = config
        self.logger = logger
        self.client = StubClient()
        self.ran = False
        self.disconnected = False

    async def authorize(self, interactive: bool = False) -> _Info:  # noqa: ARG002 - parity
        self.client.connected = True
        return _Info()

    async def run_until_disconnected(self) -> None:
        self.ran = True
        # The connection "drops" at once: the harshest honest test of the exit
        # path, without waiting a single second.
        await asyncio.sleep(0)

    async def disconnect(self) -> None:
        self.disconnected = True
        self.client.connected = False

    async def resolve_entity(self, value: Any) -> Any:
        return value

    async def get_messages(self, *_: Any, **__: Any) -> list[Any]:
        return []

    async def get_reply_message(self, message: Any) -> Any:
        return None


def prepare_environment(data: Path) -> None:
    os.environ.update(
        API_ID="2040",
        API_HASH="0123456789abcdef0123456789abcdef",
        SESSION_STRING="1" + "A" * 300,
        DATA_DIR=str(data),
        DATABASE_PATH=str(data / "havoc.db"),
        LOG_DIR=str(data / "logs"),
        BACKUP_DIR=str(data / "backups"),
        TIMEZONE="UTC",
        DEFAULT_LANGUAGE="en",
        HAVOC_SNAPSHOT_PUSH="0",
        OWNER_IDS=str(OWNER_ID),
    )


async def boot(telegram_factory: type | None = None) -> dict[str, Any]:
    """Run `havoc run` once. Returns the evidence, never raises for a bad boot."""
    import argparse as ap

    from havoc import main as entry

    factory = telegram_factory or StubTelegram
    created: list[Any] = []

    def make(config: Any = None, logger: Any = None) -> Any:
        instance = factory(config, logger)
        created.append(instance)
        return instance

    entry.TelegramService = make
    evidence: dict[str, Any] = {}
    try:
        code = await entry.command_run(ap.Namespace(no_plugins=False))
        evidence["exit_code"] = code
        evidence["expected_exit_code"] = entry.EXIT_OK
    except Exception as exc:  # noqa: BLE001 - a boot that raises is the finding
        evidence["exit_code"] = None
        evidence["expected_exit_code"] = entry.EXIT_OK
        evidence["failure"] = f"{type(exc).__name__}: {exc}"
    telegram = created[0] if created else None
    if telegram is not None:
        sent = getattr(telegram.client, "sent", [])
        evidence["handlers"] = len(getattr(telegram.client, "handlers", []))
        evidence["ran"] = bool(getattr(telegram, "ran", False))
        evidence["disconnected"] = bool(getattr(telegram, "disconnected", False))
        evidence["said"] = (sent[0][1][:400] if sent else "")
        evidence["owner_told"] = bool(sent) and sent[0][0] == OWNER_ID
    else:
        evidence["handlers"] = 0
        evidence["ran"] = False
        evidence["disconnected"] = False
        evidence["said"] = ""
        evidence["owner_told"] = False
    banner = evidence["said"]
    problems: list[str] = []
    if evidence.get("failure"):
        problems.append(f"the boot raised: {evidence['failure']}")
    if evidence["exit_code"] != evidence["expected_exit_code"]:
        problems.append(f"`havoc run` returned {evidence['exit_code']}")
    if evidence["handlers"] < 5:
        problems.append(f"only {evidence['handlers']} handlers registered")
    if not evidence["ran"]:
        problems.append("the run loop never started")
    if not evidence["disconnected"]:
        problems.append("the client was not disconnected on the way out")
    if not evidence["owner_told"]:
        problems.append("the owner was never told she is awake")
    if "startup." in banner or "{" in banner:
        problems.append(f"the banner is not a sentence: {banner!r}")
    evidence["problems"] = problems
    evidence["ok"] = not problems
    return evidence


async def _main() -> int:
    parser = argparse.ArgumentParser(description="boot the bot with Telegram stubbed")
    parser.add_argument("--json", action="store_true", help="print the evidence as JSON")
    args = parser.parse_args()
    data = Path(tempfile.mkdtemp()) / "data"
    prepare_environment(data)
    evidence = await boot()
    if args.json:
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
    else:
        print(f"boot        {'ok' if evidence['ok'] else 'FAIL'}")
        print(f"  exit      {evidence['exit_code']} (wanted {evidence['expected_exit_code']})")
        print(f"  handlers  {evidence['handlers']}")
        print(f"  awake     {'yes' if evidence['owner_told'] else 'no'}")
        for problem in evidence["problems"]:
            print(f"  ! {problem}")
    return 0 if evidence["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(asyncio.run(_main()))
