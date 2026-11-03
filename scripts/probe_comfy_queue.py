#!/usr/bin/env python3
"""Opt-in hosted diagnostic: report service health and queue sizes, never prompts."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from havoc.services.service_directory import ServiceDirectory  # noqa: E402


def main() -> int:
    directory = ServiceDirectory.from_env()
    directory.pull_now()
    endpoint = directory.resolve("comfy", force=True)
    if not endpoint.ok:
        print(f"::error::Endpoint state: {endpoint.state}")
        return 1
    print(f"::notice::Endpoint run: {endpoint.run_id}")
    for path in ("/healthz", "/system_stats", "/queue"):
        req = urllib.request.Request(endpoint.base_url + path, headers=endpoint.headers())
        try:
            with urllib.request.urlopen(req, timeout=20) as response:  # noqa: S310
                data = json.load(response)
            if path == "/queue":
                print(f"::notice::Queue: running={len(data.get('queue_running', []))} pending={len(data.get('queue_pending', []))}")
            elif path == "/healthz":
                print(f"::notice::Gate: requests={data.get('requests')} authorised={data.get('authorised')} refused={data.get('refused')} errors={data.get('errors')}")
            else:
                print("::notice::Comfy system endpoint responds")
        except (OSError, ValueError, urllib.error.URLError) as exc:
            print(f"::error::{path} failed: {type(exc).__name__}")
            return 1
    return 0


if __name__ == "__main__":
    os.environ.setdefault("SERVICE_BRANCH", os.environ.get("GITHUB_REF_NAME", ""))
    sys.exit(main())
