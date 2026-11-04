"""A tunnel redirect must never receive worker credentials or tensor payloads."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

pytest.importorskip("aiohttp")

from scripts.sdxl_cluster import client


def test_worker_refuses_redirect_without_leaking_credentials(monkeypatch):
    received = []

    class Sink(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            received.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        do_POST = do_GET

        def log_message(self, *args):
            pass

    sink = ThreadingHTTPServer(("127.0.0.1", 0), Sink)

    class Redirect(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.send_response(307)
            self.send_header("Location", f"http://127.0.0.1:{sink.server_port}/steal")
            self.end_headers()

        def log_message(self, *args):
            pass

    source = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    for server in (sink, source):
        Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(client.wire, "encode", lambda _: b"private tensors")
        with pytest.raises(client.WorkerError):
            client.predict(f"http://127.0.0.1:{source.server_port}", "a" * 32, None, None, {}, local_test=True)
        assert received == []
    finally:
        for server in (source, sink):
            server.shutdown()
            server.server_close()


def test_worker_rejects_nonstandard_external_port():
    with pytest.raises(ValueError):
        client.validate_worker("https://example.trycloudflare.com:1234", "a" * 32)
