#!/usr/bin/env python3
"""Narrow gate for the SDXL experiment; never expose ComfyUI's other routes."""
from __future__ import annotations

import hmac
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 16 * 1024 * 1024
TOKEN = os.environ.get("SDXL_WORKER_TOKEN", "")


class Gate(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        # No paths, prompts, tokens, or request contents in logs.
        pass

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/sdxl-worker/predict":
            self.send_error(404)
            return
        if not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {TOKEN}"):
            self.send_error(401)
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.send_error(411)
            return
        if length < 1 or length > MAX_BODY:
            self.send_error(413)
            return
        body = self.rfile.read(length)
        if len(body) != length:
            self.send_error(400)
            return
        req = urllib.request.Request("http://127.0.0.1:8188/sdxl-worker/predict", data=body,
                                     headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300) as upstream:  # noqa: S310 - fixed loopback
                response = upstream.read(MAX_BODY + 1)
                status = upstream.status
            if len(response) > MAX_BODY:
                self.send_error(502)
                return
        except (OSError, urllib.error.URLError):
            self.send_error(502)
            return
        print("SDXL_GATE_PREDICT ok", flush=True)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


if __name__ == "__main__":
    if len(TOKEN) < 32 or len(TOKEN) > 256:
        raise SystemExit("an ephemeral worker token is required")
    ThreadingHTTPServer(("127.0.0.1", 9008), Gate).serve_forever()
