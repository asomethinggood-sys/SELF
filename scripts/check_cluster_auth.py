#!/usr/bin/env python3
"""Live OIDC preflight; print only verdicts, never token contents."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.sdxl_oidc import get_token, github_keys, verify  # noqa: E402


def main() -> int:
    token = get_token()
    keys = github_keys()
    repo, branch = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_REF_NAME"]
    scope = {"repository": repo, "branch": branch, "workflows": {"cluster-auth-test.yml"}}
    if not verify(token, keys, **scope):
        print("::error::Genuine scoped GitHub OIDC token failed verification")
        return 1
    if verify(token, keys, **{**scope, "workflows": {"auth-and-run.yml"}}):
        print("::error::Token was accepted for an unrelated workflow")
        return 1
    if verify(token, keys, **{**scope, "branch": branch + "-wrong"}):
        print("::error::Token was accepted for another branch")
        return 1
    if verify(token + "x", keys, **scope):
        print("::error::Tampered token was accepted")
        return 1
    print("::notice::GitHub OIDC verification passed; wrong workflow, branch and tampered token were refused")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"::error::OIDC preflight failed: {type(exc).__name__}")
        raise SystemExit(1) from None
