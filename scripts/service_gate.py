#!/usr/bin/env python3
"""The gate every hosted service sits behind.

ComfyUI has no login. SearXNG can be told to have one, Ollama cannot. All three
end up on a public tunnel because GitHub's runners have no inbound port, and a
bare tunnel would publish "draw me anything, no account needed" to the internet
with our repository's name in the URL.

So each service runs on loopback and this gate is the only thing the tunnel
points at:

* **Bearer token**, derived per run (``havoc/services/service_token.py``), never
  stored in the repository. Wrong or missing → ``401`` with a sentence.
* **``/healthz``** with no auth, so the bot can ask "are you there" without
  spending a token or a slot.
* **A per-minute request cap** and an **8 MB body cap**, so a mistake in the bot
  or a stranger with the URL cannot turn a runner into a traffic relay.
* **One upstream**, one port, no path rewriting: whatever ComfyUI, SearXNG or
  Ollama expects to see, they see.

Run it in front of a service on 127.0.0.1:8188:

    SERVICE_NAME=comfy SERVICE_SECRET=... SERVICE_RUN_ID=123 \
    SERVICE_STARTED_AT=2026-11-01T20:00:00Z \
    python scripts/service_gate.py --port 9000 --upstream 8188

Exit code 0 only on a clean shutdown; anything else prints why.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from havoc.services.service_token import (  # noqa: E402  (path set up above)
    matches,
    parse_authorization,
    token_for,
)

#: 8 MB. A ComfyUI PNG is 0.5-2 MB, a search response is 40 KB, a chat completion
#: is 4 KB. Anything bigger is a mistake or an attack, and neither needs proxying.
MAX_BODY = 8 * 1024 * 1024
#: Per-minute request budget. A drawing polls ``/history`` about once a second for
#: two minutes, so a single picture costs ~120 requests; 600 leaves room for five
#: pictures plus search and chat in the same minute and stops a runaway loop.
DEFAULT_RATE = 600
#: How long one proxied request may take. ComfyUI's ``/view`` is instant, but the
#: bot may keep a connection open while a 20-step CPU render finishes.
DEFAULT_TIMEOUT = 300.0


class GateState:
    """What the gate needs to know, in one place so it can be tested."""

    def __init__(
        self,
        *,
        name: str,
        secret: str,
        run_id: str,
        started_at: str,
        upstream: str,
        rate: int = DEFAULT_RATE,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.name = name
        self.secret = secret
        self.run_id = run_id
        self.started_at = started_at
        self.upstream = upstream.rstrip("/")
        self.rate = max(1, int(rate))
        self.timeout = float(timeout)
        self.started = time.monotonic()
        self.requests = 0
        self.authorised = 0
        self.refused = 0
        self.errors = 0
        self.recent: deque[float] = deque(maxlen=8192)
        self.lock = threading.Lock()

    @property
    def token(self) -> str:
        return token_for(self.name, self.run_id, self.started_at, self.secret)

    def allow(self) -> bool:
        """One slot from the per-minute window, or ``False`` when it is spent."""
        now = time.monotonic()
        cutoff = now - 60.0
        with self.lock:
            while self.recent and self.recent[0] < cutoff:
                self.recent.popleft()
            if len(self.recent) >= self.rate:
                return False
            self.recent.append(now)
            self.requests += 1
            return True

    def check(self, header: str | None) -> bool:
        """Does this request carry the token for *this* run?"""
        offered = parse_authorization(header)
        ok = matches(self.token, offered)
        with self.lock:
            self.authorised += 1 if ok else 0
            self.refused += 0 if ok else 1
        return ok

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "service": self.name,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "uptime_seconds": round(time.monotonic() - self.started, 1),
            "upstream": self.upstream,
            "requests": self.requests,
            "authorised": self.authorised,
            "refused": self.refused,
            "errors": self.errors,
            "rate_per_minute": self.rate,
        }


def make_handler(state: GateState) -> type[BaseHTTPRequestHandler]:
    """The request handler, closed over one state object."""

    class Gate(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "YuraGate/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - stdlib name
            # One line per request, on stdout, so the workflow log is the record.
            sys.stdout.write(f"{time.strftime('%H:%M:%S')} {self.command} {self.path.split('?')[0]} {fmt % args}\n")
            sys.stdout.flush()

        # -- replies -----------------------------------------------------
        def _json(self, status: int, payload: dict[str, Any], *, headers: dict[str, str] | None = None) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _refuse(self, status: int, message: str, **extra: Any) -> None:
            self._json(
                status,
                {
                    "error": message,
                    "service": state.name,
                    "hint": "this endpoint needs the token for the current run "
                    "(Authorization: Bearer …)",
                    **extra,
                },
            )

        # -- routing -----------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            if self.path.split("?")[0].rstrip("/") in {"/healthz", ""}:
                self._json(200, state.health())
                return
            self._proxy()

        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            self._proxy()

        def do_HEAD(self) -> None:  # noqa: N802 - stdlib naming
            self._proxy()

        # -- the proxy ---------------------------------------------------
        def _proxy(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._refuse(413, f"body too large ({length} bytes, {MAX_BODY} allowed)")
                return
            body = self.rfile.read(length) if length else None

            if not state.check(self.headers.get("Authorization")):
                self._refuse(401, "unauthorized")
                return
            if not state.allow():
                self._refuse(429, "rate limited", retry_after=60)
                return

            target = f"{state.upstream}{self.path}"
            request = urllib.request.Request(target, data=body, method=self.command)
            for key, value in self.headers.items():
                if key.lower() in {"host", "authorization", "connection", "content-length", "accept-encoding"}:
                    continue
                request.add_header(key, value)
            try:
                with urllib.request.urlopen(request, timeout=state.timeout) as response:  # noqa: S310 - loopback only
                    payload = response.read(MAX_BODY + 1)
                    status = response.status
                    content_type = response.headers.get("Content-Type", "application/octet-stream")
            except urllib.error.HTTPError as exc:
                payload = exc.read(MAX_BODY)
                status = exc.code
                content_type = exc.headers.get("Content-Type", "application/json") if exc.headers else "application/json"
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                with state.lock:
                    state.errors += 1
                reason = getattr(exc, "reason", exc)
                self._json(
                    502,
                    {
                        "error": "upstream_unreachable",
                        "service": state.name,
                        "detail": str(reason)[:200],
                        "hint": "the service itself is down or still starting",
                    },
                )
                return

            if len(payload) > MAX_BODY:
                self._json(502, {"error": "upstream_too_large", "service": state.name})
                return
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            if content_type.startswith("image/"):
                # Pictures are the one thing worth caching in the bot: a re-send
                # of the same drawing should not cost another minute of CPU.
                self.send_header("Cache-Control", "private, max-age=600")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

    return Gate


def _free(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def serve(state: GateState, *, port: int, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    handler = make_handler(state)
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="a token gate in front of one hosted service")
    parser.add_argument("--port", type=int, default=int(os.environ.get("SERVICE_GATE_PORT") or 9000))
    parser.add_argument("--upstream", default=os.environ.get("SERVICE_UPSTREAM") or "127.0.0.1:8188")
    parser.add_argument("--host", default=os.environ.get("SERVICE_GATE_HOST") or "127.0.0.1")
    parser.add_argument("--rate", type=int, default=int(os.environ.get("SERVICE_GATE_RATE") or DEFAULT_RATE))
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("SERVICE_GATE_TIMEOUT") or DEFAULT_TIMEOUT))
    args = parser.parse_args(argv)

    name = (os.environ.get("SERVICE_NAME") or "").strip()
    secret = (os.environ.get("SERVICE_SECRET") or "").strip()
    run_id = (os.environ.get("SERVICE_RUN_ID") or "local").strip()
    started_at = (os.environ.get("SERVICE_STARTED_AT") or "local").strip()
    if not name:
        print("service_gate: SERVICE_NAME is not set", file=sys.stderr)
        return 2
    if not secret:
        # Refusing to start is the safe failure: an unauthenticated gate on a
        # public tunnel is exactly what this file exists to prevent.
        print("service_gate: SERVICE_SECRET is not set — refusing to expose an open proxy", file=sys.stderr)
        return 2
    upstream = args.upstream
    if not upstream.startswith("http"):
        upstream = f"http://{upstream}"

    state = GateState(
        name=name,
        secret=secret,
        run_id=run_id,
        started_at=started_at,
        upstream=upstream,
        rate=args.rate,
        timeout=args.timeout,
    )
    server = serve(state, port=args.port, host=args.host)
    print(
        f"service_gate: {name} on {args.host}:{args.port} -> {upstream} "
        f"(rate {state.rate}/min, body cap {MAX_BODY // 1024 // 1024} MB, run {run_id})",
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("service_gate: stopping", flush=True)
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
