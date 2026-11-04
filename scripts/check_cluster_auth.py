#!/usr/bin/env python3
"""Check whether this job's GitHub installation token proves repo access.

Prints only a pass/fail verdict, never credentials, headers or API responses.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    expect_readonly = "--expect-readonly" in sys.argv
    if not token or not repo:
        print("::error::Missing job token or repository")
        return 1
    request = urllib.request.Request("https://api.github.com/installation/repositories?per_page=100", headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - fixed GitHub URL
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        if expect_readonly and exc.code in {401, 403, 404}:
            print("::notice::Read-only token was refused by the installation API")
            return 0
        print(f"::error::Installation token verification failed: HTTP {exc.code}")
        return 1
    except (OSError, ValueError, urllib.error.URLError) as exc:
        print(f"::error::Installation token verification failed: {type(exc).__name__}")
        return 1
    matches = [item for item in data.get("repositories", []) if item.get("full_name", "").lower() == repo.lower()]
    writable = bool(matches and matches[0].get("permissions", {}).get("push"))
    if writable == expect_readonly:
        print("::error::Token's reported access did not match its expected permission")
        return 1
    print("::notice::Token's reported repository access matched the expected permission")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
