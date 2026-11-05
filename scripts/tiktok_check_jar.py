#!/usr/bin/env python3
"""Exit 0 only when a saved TikTok jar actually authenticates.

Used by the login workflow after the streamed sign-in: the file existing is not
proof of anything, and a jar with no ``sessionid`` is the difference between a
chain that works tomorrow and one that boots logged out. Prints one line a human
can read; never prints a cookie value.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    path = Path(argv[1] if len(argv) > 1 else ".auth/tiktok/default.json")
    if not path.exists():
        print(f"no jar at {path}")
        return 1
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"unreadable: {exc}")
        return 1
    cookies = data.get("cookies") or []
    names = {str(item.get("name", "")) for item in cookies}
    authed = bool(names & {"sessionid", "sessionid_ss"})
    print(
        f"{len(cookies)} cookies · handle={data.get('handle') or '(unknown)'} · "
        f"authenticating cookie={'yes' if authed else 'NO'}"
    )
    return 0 if authed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
