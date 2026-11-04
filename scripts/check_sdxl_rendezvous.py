#!/usr/bin/env python3
"""Exercise rendezvous retry over an actual local HTTP server, not the GitHub API."""
from __future__ import annotations

import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["GITHUB_TOKEN"] = "synthetic-test-token"
os.environ["WORKER_INDEX"] = "1"
from scripts import sdxl_cluster_test as harness  # noqa: E402


class Flaky(BaseHTTPRequestHandler):
    requests = 0

    def do_GET(self):  # noqa: N802
        type(self).requests += 1
        if self.headers.get("Authorization") != "Bearer synthetic-test-token":
            self.send_error(401)
            return
        if self.requests <= 2:
            self.send_error(504)
            return
        data = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, _format, *_args):
        pass


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Flaky)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        harness.API = f"http://127.0.0.1:{server.server_port}"
        status, payload = harness.github("GET", "leader.json")
        if (status, payload, Flaky.requests) != (200, {"ok": True}, 3):
            raise RuntimeError("rendezvous retry did not recover")
        print("::notice::Real HTTP rendezvous retry recovered after two simulated 504 responses")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
