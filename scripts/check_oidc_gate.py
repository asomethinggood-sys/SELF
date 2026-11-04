#!/usr/bin/env python3
"""Hosted live HTTP preflight for the experimental authenticated gateway."""
from __future__ import annotations

import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import sdxl_oidc_gate as gate  # noqa: E402
from scripts.sdxl_oidc import get_token  # noqa: E402


class Upstream(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.headers.get("Authorization"):
            self.send_error(500)
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"stub-comfy")

    def log_message(self, _format, *_args):
        pass


def call(path: str, token: str = "") -> tuple[int, bytes]:
    request = urllib.request.Request("http://127.0.0.1:19009" + path,
                                     headers={"Authorization": "Bearer " + token} if token else {})
    try:
        with urllib.request.urlopen(request, timeout=25) as response:  # noqa: S310 - loopback only
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def main() -> int:
    gate.REPOSITORY = os.environ["GITHUB_REPOSITORY"]
    gate.BRANCH = os.environ["GITHUB_REF_NAME"]
    gate.WORKFLOWS = {"cluster-auth-test.yml"}
    upstream = ThreadingHTTPServer(("127.0.0.1", 8188), Upstream)
    proxy = ThreadingHTTPServer(("127.0.0.1", 19009), gate.Gate)
    for server in (upstream, proxy):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        token = get_token()
        checks = (
            call("/system_stats", token) == (200, b"stub-comfy"),
            call("/system_stats")[0] == 401,
            call("/system_stats", token + "x")[0] == 401,
            call("/queue", token)[0] == 404,
            call("/sdxl-worker/predict", token)[0] == 404,
        )
        if not all(checks):
            print("::error::Scoped gateway HTTP preflight failed")
            return 1
        print("::notice::Scoped gateway accepted a real job identity, stripped credentials, and rejected invalid and forbidden requests")
        return 0
    finally:
        proxy.shutdown()
        upstream.shutdown()
        proxy.server_close()
        upstream.server_close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"::error::Gateway preflight failed: {type(exc).__name__}")
        raise SystemExit(1) from None
