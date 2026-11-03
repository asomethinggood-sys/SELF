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
                pending = data.get("queue_pending", [])
                print(f"::notice::Queue: running={len(data.get('queue_running', []))} pending={len(pending)}")
                if os.environ.get("REMOVE_OUR_DRAW_PROBES") == "1":
                    # Delete only queued synthetic prompts from this diagnostic, never
                    # running work or other people's prompts. Do not log the graph.
                    ids = []
                    for job in pending:
                        if not isinstance(job, list) or len(job) < 3 or not isinstance(job[2], dict):
                            continue
                        graph = job[2]
                        if any(
                            isinstance(node, dict)
                            and node.get("class_type") == "CLIPTextEncode"
                            and node.get("inputs", {}).get("text") == "a small red lighthouse by the sea"
                            for node in graph.values()
                        ):
                            ids.append(job[1])
                    if ids:
                        body = json.dumps({"delete": ids}).encode()
                        headers = {**endpoint.headers(), "Content-Type": "application/json"}
                        delete = urllib.request.Request(endpoint.base_url + "/queue", data=body, headers=headers)
                        with urllib.request.urlopen(delete, timeout=20) as response:  # noqa: S310
                            response.read()
                    print(f"::notice::Removed {len(ids)} queued synthetic test jobs; no other work was removed")
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
