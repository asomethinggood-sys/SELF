#!/usr/bin/env python3
"""Hosted live HTTP preflight for the experimental authenticated gateway."""
from __future__ import annotations

import json
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

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.do_GET()

    def log_message(self, _format, *_args):
        pass


def call(path: str, token: str = "", body: dict | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request("http://127.0.0.1:19009" + path,
                                     data=json.dumps(body).encode() if body is not None else None,
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
        from havoc.services.imagegen import ImageGenerator
        generator = ImageGenerator(checkpoint="DreamShaperXL_Lightning.safetensors", width=768, height=768)
        graph, _, _ = generator._builtin("a lighthouse", 1234, steps=4)
        graph["3"]["inputs"].update({"cfg": 1.0, "scheduler": "sgm_uniform", "model": ["10", 0]})
        graph["10"] = {"class_type": "FourWorkerSDXL", "inputs": {"model": ["4", 0], "overlap": 8}}
        good_graph = {"prompt": graph}
        bad_graph = {"prompt": {"1": {"class_type": "CustomNode", "inputs": {}}}}
        checks = (
            call("/system_stats", token) == (200, b"stub-comfy"),
            call("/system_stats")[0] == 401,
            call("/system_stats", token + "x")[0] == 401,
            call("/queue", token)[0] == 404,
            call("/sdxl-worker/predict", token)[0] == 404,
            call("/prompt", token, good_graph) == (200, b"stub-comfy"),
            call("/prompt", token, bad_graph)[0] == 400,
            call("/queue", token, {"clear": True})[0] == 400,
            call("/view?filename=../../secrets.png", token)[0] == 404,
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
