#!/usr/bin/env python3
"""Ask Telegram whether a given api_id/api_hash pair is usable.

Why this exists: the published credentials of Telegram's own clients are widely
copied into tutorials, and whether they still work is folklore that changes.
This script settles it with two harmless, unauthenticated probes and reports a
verdict per pair:

* ``help.GetConfigRequest`` — is the pair accepted at all?
* ``auth.SendCodeRequest`` to a syntactically valid but non-existent number —
  this is the call that raises ``API_ID_PUBLISHED_FLOOD`` for a public api_id.
  Because the number does not exist, no code is sent to anybody.

Never point the second probe at a real phone number: it costs that number a
login-code request, and every retry restarts Telegram's 24-hour window.

The verdicts:

``works``           the pair is accepted; Telegram did not call it published
``published_flood`` Telegram recognises it as a public client id and refuses
``invalid``         the id/hash pair is wrong, or the hash does not match the id
``network``         Telegram was unreachable (proxy/blocked network)
``unknown``         something else — the raw error is included in the report
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import asdict, dataclass

#: A syntactically valid number that cannot receive anything. ``auth.sendCode``
#: answers PHONE_NUMBER_INVALID, which is the success signal for the probe.
PROBE_PHONE = "+9996615550000"

#: The pairs that tutorials copy from Telegram's own clients. Recorded here so
#: the answer is reproducible rather than remembered.
PUBLISHED_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("telegram-desktop", "2040", "b18441a1ff607e10a989891a5462e627"),
    ("telegram-android", "6", "eb06d4abfb49dc3eeb1aeb98ae0f581e"),
    ("telegram-ios", "8", "7245de8e747a0d6fbe11f7cc14fcc0bb"),
    ("telegram-macos", "9", "3975f648bb682ee889f35483bc618d1c"),
)


@dataclass
class Verdict:
    label: str
    api_id: str
    verdict: str
    detail: str = ""

    @property
    def usable(self) -> bool:
        """Proven to work, not merely accepted."""
        return self.verdict == "works"

    @property
    def conclusive(self) -> bool:
        return self.verdict in {"works", "published_flood", "invalid"}


def _error_blob(error: BaseException) -> str:
    """Normalise an exception to something marker matching can rely on.

    Telethon names its errors in camel case (``PhoneNumberInvalidError``) while
    the wire format uses shouting snake case (``PHONE_NUMBER_INVALID``). Both
    have to match, or a clear signal gets reported as "unknown".
    """
    name = type(error).__name__
    snake = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    return f"{snake} {str(error)}".upper()


def classify(error: BaseException | None, *, stage: str) -> tuple[str, str]:
    """Map an exception from a probe call to (verdict, detail)."""
    if error is None:
        return "ok", ""
    name = type(error).__name__
    text = str(error)
    blob = _error_blob(error)

    if "API_ID_PUBLISHED_FLOOD" in blob or "API_ID_PUBLISHED" in blob:
        return "published_flood", f"{name}: {text}"[:300]
    if "API_ID_INVALID" in blob:
        return "invalid", f"{name}: {text}"[:300]
    if "PHONE_NUMBER_INVALID" in blob:
        # The api_id was accepted and the throwaway number was rejected. That is
        # as far as a probe can go: Telegram checks the number before it decides
        # whether the api_id is a published one, so this proves the pair is
        # structurally valid but not that it can log in.
        return "accepted", f"{name}: {text}"[:300]
    if any(marker in blob for marker in ("FLOOD", "TOO_MANY")):
        return "flood", f"{name}: {text}"[:300]
    if any(
        marker in blob
        for marker in ("CONNECT", "TIMEOUT", "INCOMPLETEREAD", "OSERROR", "NETWORK", "PROXY")
    ):
        return "network", f"{name}: {text}"[:300]
    if "AUTH_KEY" in blob or "UNAUTHORIZED" in blob:
        # The id is fine; this session is simply not logged in (expected).
        return "ok", f"{name}: {text}"[:300]
    return "unknown", f"{stage}: {name}: {text}"[:300]


async def probe_pair(label: str, api_id: int, api_hash: str, *, timeout: float = 20.0) -> Verdict:
    """Run the two probes against one credential pair."""
    try:
        from telethon import TelegramClient, functions
        from telethon.sessions import MemorySession
        from telethon.tl.types import CodeSettings
    except ImportError:  # pragma: no cover - environment dependent
        return Verdict(label, str(api_id), "network", "telethon is not installed")

    client = TelegramClient(MemorySession(), api_id, api_hash, connection_retries=2, timeout=timeout)
    try:
        await client.connect()
    except Exception as exc:  # noqa: BLE001 - classified below
        verdict, detail = classify(exc, stage="connect")
        if verdict == "ok":  # pragma: no cover - a connect failure is never "ok"
            verdict = "network"
        return Verdict(label, str(api_id), verdict if verdict != "ok" else "network", detail)

    try:
        # Probe 1: does Telegram accept the pair for a plain API call?
        try:
            await client(functions.help.GetConfigRequest())
            first = Verdict(label, str(api_id), "ok")
        except Exception as exc:  # noqa: BLE001
            verdict, detail = classify(exc, stage="help.getConfig")
            if verdict == "network":
                return Verdict(label, str(api_id), "network", detail)
            return Verdict(label, str(api_id), verdict, detail)

        # Probe 2: the call that identifies a published client id. The number is
        # deliberately not real: a real one would cost its owner a code request.
        try:
            await client(
                functions.auth.SendCodeRequest(
                    phone_number=PROBE_PHONE,
                    api_id=api_id,
                    api_hash=api_hash,
                    settings=CodeSettings(),
                )
            )
            # If this ever succeeds the number exists; nothing was sent to it
            # because Telegram refuses unknown numbers, so treat it as "ok".
            second = Verdict(label, str(api_id), "ok", "sendCode accepted (unexpected)")
        except Exception as exc:  # noqa: BLE001
            verdict, detail = classify(exc, stage="auth.sendCode")
            if verdict == "ok":
                verdict = "accepted"
            second = Verdict(label, str(api_id), verdict, detail)

        # The stricter answer wins: a pair that passes getConfig but is refused
        # for sendCode is not usable for logging in.
        return second if not second.usable or first.usable else first
    finally:
        try:
            await client.disconnect()
        except Exception:  # pragma: no cover - best effort
            pass


async def probe_all(pairs=PUBLISHED_PAIRS, *, timeout: float = 20.0) -> list[Verdict]:
    verdicts: list[Verdict] = []
    for label, api_id, api_hash in pairs:
        verdicts.append(await probe_pair(label, int(api_id), api_hash, timeout=timeout))
    return verdicts


def summarise(verdicts: list[Verdict]) -> str:
    """A short table plus the one-line conclusion."""
    lines = ["| credential | api_id | verdict | detail |", "| --- | --- | --- | --- |"]
    for verdict in verdicts:
        detail = verdict.detail.replace("|", "\\|").replace("\n", " ")[:160]
        lines.append(f"| {verdict.label} | {verdict.api_id} | **{verdict.verdict}** | {detail} |")
    usable = [verdict.label for verdict in verdicts if verdict.usable]
    accepted = [verdict.label for verdict in verdicts if verdict.verdict == "accepted"]
    lines.append("")
    if usable:
        lines.append(f"Proven to work: {', '.join(usable)}.")
        lines.append(
            "This is a stopgap, not a home: Telegram can start refusing a public id at "
            "any time, and its documented position is that you should register your own."
        )
    elif accepted:
        lines.append(
            f"Accepted by Telegram but not proven: {', '.join(accepted)}. "
            "The pair is structurally valid and was not rejected as published, but a "
            "probe cannot say whether a real login would pass — Telegram validates the "
            "phone number before it evaluates the api_id, and this probe deliberately "
            "uses a number that cannot exist."
        )
        lines.append(
            "The only decisive test is a real code request to your own number, which is "
            "what 'Telegram login' does with api_id/api_hash filled in as inputs."
        )
    else:
        lines.append("None of the pairs is usable. Register your own application.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check whether an api_id/api_hash pair is usable")
    parser.add_argument("--api-id", help="check one pair instead of the published list")
    parser.add_argument("--api-hash")
    parser.add_argument("--label", default="custom")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--json-out", help="write the verdicts as JSON to this path")
    parser.add_argument("--markdown-out", help="write a markdown summary to this path")
    args = parser.parse_args(argv)

    if bool(args.api_id) != bool(args.api_hash):
        print("--api-id and --api-hash must be given together", file=sys.stderr)
        return 2

    pairs = (
        ((args.label, args.api_id, args.api_hash),) if args.api_id else PUBLISHED_PAIRS
    )
    verdicts = asyncio.run(probe_all(pairs, timeout=args.timeout))
    report = summarise(verdicts)

    print(report)
    if args.markdown_out:
        with open(args.markdown_out, "w", encoding="utf-8") as handle:
            handle.write(report + "\n")
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump([asdict(verdict) for verdict in verdicts], handle, indent=2)

    # Exit status: 0 when at least one pair works, 1 when none do, 2 on a hard
    # failure, so a workflow can branch on the result without parsing text.
    if any(verdict.verdict == "network" for verdict in verdicts) and not any(
        verdict.usable for verdict in verdicts
    ):
        return 2
    return 0 if any(verdict.usable for verdict in verdicts) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
