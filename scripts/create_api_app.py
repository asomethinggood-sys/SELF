#!/usr/bin/env python3
"""Create a Telegram API application without guessing what the form got wrong.

``my.telegram.org`` answers a refused submission with a bare ``ERROR`` page and no
reason. In practice there are exactly three causes, and they need very different
responses: the application already exists, Telegram is rate-limiting the number,
or the submission was rejected because the IP's country does not match the phone
number's country. This script distinguishes them and stops before making things
worse — retrying a rate-limit is what pushes people into a 24-hour lockout.

It performs the same request the website performs when you press
*Create application*: one ``GET`` of the form (to pick up the hidden ``hash``
input and confirm the session) and one ``POST``. No browser automation.

Usage
-----
1. Log in at https://my.telegram.org in a browser and open
   https://my.telegram.org/apps
2. Open developer tools → Application → Cookies → copy the value of
   ``stel_token``.
3. Run:

       export TELEGRAM_STEL_TOKEN='the value you copied'
       python scripts/create_api_app.py --title "HAVOC Userbot" --short-name havocbot7k

The token is a short-lived session cookie for that website only; it is never
logged and never written to disk.

If the account has an application already, the script prints it instead of
creating a second one. If Telegram replies with a rate-limit, it exits
immediately with the wait advice.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

BASE = "https://my.telegram.org"
APPS_URL = f"{BASE}/apps"
CREATE_URL = f"{BASE}/apps/create"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

#: Platform values the site accepts, in the order worth trying. ``desktop`` is
#: the honest choice for a userbot; the others exist because the country check
#: sometimes passes for one value and refuses another with identical fields.
PLATFORMS = ("desktop", "android", "web")

#: Strings that mean "wait", not "try again differently".
FLOOD_MARKERS = ("too many", "flood", "try again later", "limit")

#: Strings that mean "this IP is in the wrong country" (Telegram never says so).
COUNTRY_HINT = (
    "If this keeps failing, the usual cause is that the country of your IP does\n"
    "not match the country of the phone number. Use the mobile network of the\n"
    "number's own country, turn VPNs off, and submit exactly once."
)


@dataclass
class Outcome:
    kind: str  # "ok" | "flood" | "refused" | "unparsed"
    api_id: str = ""
    api_hash: str = ""
    detail: str = ""

    @property
    def succeeded(self) -> bool:
        return self.kind == "ok"


def extract_hash(html: str) -> str:
    """Pull the hidden ``hash`` input the form posts back."""
    marker = 'name="hash"'
    index = html.find(marker)
    if index == -1:
        return ""
    tail = html[index : index + 400]
    for quote in ('"', "'"):
        for pattern in (f"value={quote}", f'value="{quote}'):
            start = tail.find(pattern)
            if start != -1:
                start += len(pattern)
                end = tail.find(quote, start)
                if end > start:
                    return tail[start:end].strip()
    return ""


def parse_response(text: str, status: int = 200) -> Outcome:
    """Classify what ``/apps/create`` answered.

    Success is JSON carrying ``app_id``/``api_id``; a refusal is the literal
    body ``ERROR`` (or an ``[object Object]``-shaped JSON error).
    """
    body = (text or "").strip()
    lowered = body.lower()

    if status == 429 or any(marker in lowered for marker in FLOOD_MARKERS):
        return Outcome("flood", detail=body[:200] or f"HTTP {status}")

    if body.startswith("{") or body.startswith("["):
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            api_id = str(payload.get("app_id") or payload.get("api_id") or "")
            api_hash = str(payload.get("app_hash") or payload.get("api_hash") or "")
            if api_id and api_hash:
                return Outcome("ok", api_id=api_id, api_hash=api_hash)
            error = payload.get("error") or payload.get("message")
            if error:
                return Outcome("refused", detail=str(error)[:200])
            return Outcome("unparsed", detail=body[:200])

    if lowered in {"error", "[object object]", "true", "false", ""}:
        return Outcome("refused", detail=lowered or "(empty reply)")

    # A successful submission has been seen to answer with the app page itself.
    api_id = _find_between(body, '"app_id":', ",") or _find_between(body, "app_id: ", ",")
    api_hash = _find_between(body, '"app_hash":', '"') or _find_between(body, '"api_hash":', '"')
    if api_id and api_hash:
        return Outcome("ok", api_id=api_id.strip('" '), api_hash=api_hash.strip('" '))
    return Outcome("unparsed", detail=body[:200])


def _find_between(text: str, start_marker: str, end_marker: str) -> str:
    start = text.find(start_marker)
    if start == -1:
        return ""
    start += len(start_marker)
    end = text.find(end_marker, start)
    if end == -1:
        return ""
    return text[start:end].strip()


def _cell_value(html: str, marker: str) -> str:
    """Read the value that follows an html marker, e.g. ``id="app_id">123<``."""
    raw = _find_between(html, marker, "<")
    return raw.lstrip("> \t\r\n\"'").strip()


def existing_app(html: str) -> Outcome | None:
    """Detect an already-created application on the ``/apps`` page."""
    api_id = _cell_value(html, 'id="app_id"') or _cell_value(html, "App api_id:")
    api_hash = _cell_value(html, 'id="app_hash"') or _cell_value(html, "App api_hash:")
    if api_id and api_hash:
        return Outcome("ok", api_id=api_id, api_hash=api_hash)
    return None


def form_fields(platform: str, title: str, short_name: str, url: str, description: str) -> dict[str, str]:
    # Mirrors the website's own field names, including the odd spacing.
    return {
        "MIME Type": " application/x-www-form-urlencoded; charset=UTF-8",
        "app_title": title,
        "app_shortname": short_name,
        "app_url": url,
        "app_platform": platform,
        "app_desc": description,
    }


def _request(url: str, *, token: str, data: dict[str, str] | None = None) -> tuple[int, str]:
    payload = urllib.parse.urlencode(data).encode() if data is not None else None
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "User-Agent": USER_AGENT,
            "Cookie": f"stel_token={token}",
            "Origin": BASE,
            "Referer": APPS_URL,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def create_app(
    *,
    token: str,
    title: str,
    short_name: str,
    url: str,
    description: str,
    platforms: tuple[str, ...] = PLATFORMS,
    pause: float = 10.0,
    sleep=time.sleep,
) -> Outcome:
    status, html = _request(APPS_URL, token=token)
    if status in (301, 302, 401, 403):
        return Outcome("refused", detail=f"the token was rejected (HTTP {status}); copy stel_token again")
    hidden = extract_hash(html)
    found = existing_app(html)
    if found is not None:
        return found

    last = Outcome("unparsed", detail="no attempt was made")
    for index, platform in enumerate(platforms):
        if index:
            sleep(pause)
        fields = form_fields(platform, title, short_name, url, description)
        if hidden:
            fields["hash"] = hidden
        status, body = _request(CREATE_URL, token=token, data=fields)
        outcome = parse_response(body, status)
        if outcome.succeeded:
            return outcome
        if outcome.kind == "flood":
            return outcome
        last = outcome
    return last


def validate_short_name(value: str) -> str:
    """The rule the site enforces silently: 5+ lowercase alphanumerics."""
    cleaned = value.strip().lower()
    if not cleaned.isalnum() or not cleaned.isascii() or len(cleaned) < 5:
        raise SystemExit(
            "invalid --short-name: use 5 or more lowercase letters/digits, no spaces\n"
            "(a short name that is too short or already taken makes the site answer ERROR)"
        )
    return cleaned


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a Telegram API application and explain refusals")
    parser.add_argument("--token", default=os.environ.get("TELEGRAM_STEL_TOKEN"), help="the stel_token cookie (or TELEGRAM_STEL_TOKEN)")
    parser.add_argument("--title", default="HAVOC Userbot")
    parser.add_argument("--short-name", default="havocuserbot", help="5+ lowercase alphanumeric characters, unique")
    parser.add_argument("--url", default="https://github.com/asomethinggood-sys/SELF")
    parser.add_argument("--description", default="Personal bilingual Telegram userbot")
    parser.add_argument(
        "--platforms",
        default=",".join(PLATFORMS),
        help=f"platform values to try in order (default: {','.join(PLATFORMS)})",
    )
    parser.add_argument("--pause", type=float, default=10.0, help="seconds between attempts (default: 10)")
    args = parser.parse_args(argv)

    if not args.token:
        print(
            "Missing the session cookie.\n"
            "  Log in at https://my.telegram.org, open /apps, then copy the\n"
            "  'stel_token' cookie from developer tools into TELEGRAM_STEL_TOKEN.",
            file=sys.stderr,
        )
        return 2

    platforms = tuple(part.strip().lower() for part in args.platforms.split(",") if part.strip())
    outcome = create_app(
        token=args.token.strip(),
        title=args.title,
        short_name=validate_short_name(args.short_name),
        url=args.url,
        description=args.description,
        platforms=platforms,
        pause=args.pause,
    )

    if outcome.succeeded:
        print("Application is ready.\n")
        print(f"API_ID={outcome.api_id}")
        print(f"API_HASH={outcome.api_hash}")
        print("\nKeep API_HASH private; it identifies the app, not the account.")
        return 0

    if outcome.kind == "flood":
        print("Telegram is rate-limiting this number.", file=sys.stderr)
        print("  Stop now: every further attempt restarts the 24-hour window.", file=sys.stderr)
        print(f"  Server said: {outcome.detail}", file=sys.stderr)
        return 3

    print("Telegram refused the submission.", file=sys.stderr)
    print(f"  Reply: {outcome.detail}", file=sys.stderr)
    print(f"\n{COUNTRY_HINT}", file=sys.stderr)
    print("\nAlso check, in this order:", file=sys.stderr)
    print("  1. Reload https://my.telegram.org/apps — the app may already exist.", file=sys.stderr)
    print("  2. This account may already have its one application.", file=sys.stderr)
    print("  3. A different --short-name (it may be taken).", file=sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
