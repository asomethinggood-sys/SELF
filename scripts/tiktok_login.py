#!/usr/bin/env python3
"""Open TikTok's login page on *this machine* and save the resulting session.

This is the TikTok twin of ``havoc login``: the only step that needs a human at
a keyboard, and the only step whose output the workflow depends on.

    python scripts/tiktok_login.py            # window opens; sign in yourself
    python scripts/tiktok_login.py --check    # afterwards: what did we get?

Nothing is typed by this script. TikTok's login guard (ticket/D PoP) is designed
to stop scripted sign-ins, and a human handshake is both the honest way and the
one that keeps working. What is stored when you are done is the cookie jar —
the same class of secret as the Telegram session string, revocable from
TikTok's own device list.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from havoc.tiktok import SessionError, TikTokSessionStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default=".auth/tiktok", help="where to keep the jar")
    parser.add_argument("--account", default="default")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--check", action="store_true", help="report the saved session instead of logging in")
    args = parser.parse_args()

    store = TikTokSessionStore(Path(args.dir), account=args.account)

    if args.check:
        try:
            account = store.load()
        except SessionError as exc:
            print(f"{exc.kind}: {exc}")
            if exc.hint:
                print(f"  → {exc.hint}")
            return 1
        print(f"jar      : {store.path} ({len(account.cookies)} cookies)")
        print(f"handle   : @{account.handle or '(unknown)'}")
        print(f"age      : {account.age_days():.1f} days")
        return 0

    try:
        from havoc.tiktok.browser import browser_available, install_hint
    except Exception as exc:  # noqa: BLE001
        print(f"cannot import the browser layer: {exc}")
        return 1
    if not browser_available():
        print("the browser engine is not installed.\n  " + install_hint())
        return 1

    from havoc.tiktok.browser import TikTokBrowser
    from havoc.tiktok.limits import Pace

    async def _go() -> int:
        print("A Chromium window will open on TikTok's login page.")
        print("Sign in yourself — password or QR. This script types nothing and sees no password.\n")
        browser = TikTokBrowser(store, headless=False, pace=Pace())
        try:
            await browser.start()
            result = await browser.login(timeout=args.timeout)
        finally:
            await browser.close()
        if not result.ok:
            print(f"\nlogin failed: {result.detail}")
            return 1
        account = store.load()
        print(f"\nlogged in as @{account.handle or '(handle unreadable)'}")
        print(f"jar saved to {store.path}  ({len(account.cookies)} cookies)")
        print("\ncommit that file so the workflow can restore it:")
        print(f"  git add {args.dir}/{args.account}.json && git commit -m 'tiktok: session'")
        return 0

    return asyncio.run(_go())


if __name__ == "__main__":
    raise SystemExit(main())
