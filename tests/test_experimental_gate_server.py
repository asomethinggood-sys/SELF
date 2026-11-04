"""End-to-end test of the experimental gate: a real HTTP server, real signatures.

The gate is the only thing between a public tunnel and a ComfyUI that will draw
anything for anyone. So it is exercised as a server: signed tokens from the
right workflow are forwarded, everything else is refused, and the graph is
checked byte for byte even when the caller is signed.
"""
from __future__ import annotations

import base64
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from scripts import sdxl_experimental_gate as gate
from scripts.sdxl_oidc import AUDIENCE, ISSUER

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 512


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _key_json(private: rsa.RSAPrivateKey, kid: str) -> dict:
    numbers = private.public_key().public_numbers()
    return {
        "kid": kid,
        "kty": "RSA",
        "e": _b64(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
        "n": _b64(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
    }


def _token(private: rsa.RSAPrivateKey, kid: str, *, now: float, workflow: str = "auth-and-run.yml",
           repository: str = "owner/repo", branch: str = "arena/b9a136e7-self", audience: str = AUDIENCE) -> str:
    claims = {
        "iss": ISSUER, "aud": audience, "repository": repository,
        "ref": f"refs/heads/{branch}",
        "workflow_ref": f"{repository}/.github/workflows/{workflow}@refs/heads/{branch}",
        "iat": int(now), "nbf": int(now) - 5, "exp": int(now) + 60,
    }
    head = _b64(json.dumps({"alg": "RS256", "kid": kid}, separators=(",", ":")).encode())
    payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{head}.{payload}".encode()
    return f"{head}.{payload}.{_b64(private.sign(signing_input, padding.PKCS1v15(), hashes.SHA256()))}"


class Comfy(BaseHTTPRequestHandler):
    prompts: list[dict] = []
    queue: dict = {"queue_running": [], "queue_pending": []}

    def log_message(self, *_args):
        pass

    def _send(self, body: bytes, status: int = 200, content: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/system_stats":
            return self._send(b'{"system":{}}')
        if self.path == "/history/job-1":
            return self._send(json.dumps({"job-1": {
                "status": {"status_str": "success"},
                "outputs": {"9": {"images": [{"filename": "out.png"}]}}}}).encode())
        if self.path.startswith("/view?"):
            return self._send(PNG, content="image/png")
        self.send_error(404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        if self.path == "/prompt":
            Comfy.prompts.append(json.loads(body))
            return self._send(json.dumps({"prompt_id": "job-1"}).encode())
        if self.path == "/queue":
            return self._send(json.dumps(Comfy.queue).encode())
        self.send_error(404)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture()
def stack(monkeypatch):
    from scripts import sdxl_oidc_gate as base

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key = _key_json(private, "test-key")
    monkeypatch.setattr(base, "github_keys", lambda: [key])
    monkeypatch.setattr(base, "REPOSITORY", "owner/repo")
    monkeypatch.setattr(base, "BRANCH", "arena/b9a136e7-self")
    monkeypatch.setattr(base, "WORKFLOWS", {"auth-and-run.yml", "yura-her.yml"})
    # The gate caches GitHub's keys for ten minutes; each test mints a new
    # keypair, so the cache must not survive into the next test.
    monkeypatch.setattr(base, "_keys", [])
    monkeypatch.setattr(base, "_keys_until", 0.0)

    backend = ThreadingHTTPServer(("127.0.0.1", _free_port()), Comfy)
    threading.Thread(target=backend.serve_forever, daemon=True).start()
    Comfy.prompts = []
    Comfy.queue = {"queue_running": [], "queue_pending": []}
    monkeypatch.setattr(base, "BACKEND", f"http://127.0.0.1:{backend.server_address[1]}")

    service = ThreadingHTTPServer(("127.0.0.1", _free_port()), gate.ExperimentalGate)
    threading.Thread(target=service.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{service.server_address[1]}"
    try:
        yield SimpleNamespace(private=private, key=key, base=base, backend=backend, comfy=Comfy)
    finally:
        service.shutdown()
        backend.shutdown()


def _call(stack, method: str, path: str, *, body: dict | None = None, token: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(stack.base + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_gate_forwards_a_signed_valid_graph(stack):
    from tests.test_experimental_sdxl import graph

    token = _token(stack.private, stack.key["kid"], now=time.time())
    status, body = _call(stack, "POST", "/prompt", body={"prompt": graph("a lighthouse", 7)}, token=token)
    assert status == 200
    assert json.loads(body)["prompt_id"] == "job-1"
    assert stack.comfy.prompts[-1]["prompt"] == graph("a lighthouse", 7)


@pytest.mark.parametrize("token", [None, "Bearer", "Bearer not-a-jwt", "Bearer x.y.z"])
def test_gate_rejects_unsigned_requests(stack, token):
    from tests.test_experimental_sdxl import graph

    assert _call(stack, "POST", "/prompt", body={"prompt": graph()}, token=token)[0] == 401
    assert stack.comfy.prompts == []


@pytest.mark.parametrize("kwargs", [
    {"branch": "other-branch"},
    {"workflow": "evil.yml"},
    {"repository": "other/repo"},
])
def test_gate_rejects_a_token_outside_its_scope(stack, kwargs):
    from tests.test_experimental_sdxl import graph

    token = _token(stack.private, stack.key["kid"], now=time.time(), **kwargs)
    assert _call(stack, "POST", "/prompt", body={"prompt": graph()}, token=token)[0] == 401
    assert stack.comfy.prompts == []


def test_gate_refuses_a_tampered_graph_even_when_signed(stack):
    from tests.test_experimental_sdxl import graph

    built = graph()
    built["3"]["inputs"]["cfg"] = 7.0
    token = _token(stack.private, stack.key["kid"], now=time.time())
    assert _call(stack, "POST", "/prompt", body={"prompt": built}, token=token)[0] == 400
    assert stack.comfy.prompts == []


def test_gate_returns_429_when_two_jobs_are_already_queued(stack):
    from tests.test_experimental_sdxl import graph

    stack.comfy.queue = {"queue_running": ["a"], "queue_pending": ["b"]}
    try:
        token = _token(stack.private, stack.key["kid"], now=time.time())
        assert _call(stack, "POST", "/prompt", body={"prompt": graph()}, token=token)[0] == 429
        assert stack.comfy.prompts == []
    finally:
        stack.comfy.queue = {"queue_running": [], "queue_pending": []}


def test_gate_lets_the_caller_clear_its_own_pending_job(stack):
    token = _token(stack.private, stack.key["kid"], now=time.time())
    assert _call(stack, "POST", "/queue", body={"delete": ["job-1"]}, token=token)[0] == 200


def test_gate_does_not_answer_unlisted_paths(stack):
    token = _token(stack.private, stack.key["kid"], now=time.time())
    assert _call(stack, "GET", "/queue")[0] == 404
    assert _call(stack, "GET", "/system_stats", token=token)[0] == 200
