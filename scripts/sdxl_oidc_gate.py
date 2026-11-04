#!/usr/bin/env python3
"""Experimental Comfy gateway restricted to trusted GitHub Actions workflows.

Public tunnel points here, not directly at ComfyUI. Only a short list of image
API routes is forwarded, and all except health require a GitHub-signed,
short-lived OIDC token scoped to the configured repo, branch and workflow.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.sdxl_oidc import _OPENER, github_keys, verify  # noqa: E402

MAX_BODY = 8 * 1024 * 1024
MAX_RESPONSE = 16 * 1024 * 1024
#: Loopback only. The public tunnel points *here*, so the thing behind it is
#: this machine's ComfyUI, never another address.
BACKEND = "http://127.0.0.1:8188"
REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")
BRANCH = os.environ.get("GITHUB_REF_NAME", "")
WORKFLOWS = {"auth-and-run.yml", "yura-her.yml"}
_keys: list[dict[str, Any]] = []
_keys_until = 0.0
_key_lock = threading.Lock()


def authorized(header: str) -> bool:
    global _keys, _keys_until
    if not header.startswith("Bearer ") or not REPOSITORY or not BRANCH:
        return False
    try:
        with _key_lock:
            if time.monotonic() >= _keys_until:
                _keys = github_keys()
                _keys_until = time.monotonic() + 600
            keys = _keys
        return verify(header[7:], keys, repository=REPOSITORY, branch=BRANCH, workflows=WORKFLOWS)
    except Exception:  # noqa: BLE001 - auth fails closed on network or parsing errors
        return False


def allowed(method: str, path: str) -> bool:
    parts = urllib.parse.urlsplit(path)
    if parts.scheme or parts.netloc or parts.fragment:
        return False
    clean = parts.path
    if method == "GET":
        if clean in {"/object_info/CheckpointLoaderSimple", "/system_stats"}:
            return not parts.query
        if re.fullmatch(r"/history/[a-zA-Z0-9-]{1,80}", clean):
            return not parts.query
        if clean == "/view":
            query = urllib.parse.parse_qs(parts.query, strict_parsing=True)
            if set(query) - {"filename", "subfolder", "type"} or any(len(v) != 1 for v in query.values()):
                return False
            filename = query.get("filename", [""])[0]
            return bool(re.fullmatch(r"[a-zA-Z0-9_.-]{1,200}\.png", filename)) and query.get("type", ["output"]) == ["output"] and not query.get("subfolder", [""])[0]
    return method == "POST" and not parts.query and clean in {"/prompt", "/queue"}


def permitted_body(path: str, body: bytes) -> bool:
    """Permit only our fixed SDXL graph or deletion of specific pending IDs.

    Possession of a bot job token must not confer arbitrary ComfyUI graph or
    extension execution. A caller cannot alter node types, model, or links.
    """
    try:
        value = json.loads(body)
        if path == "/queue":
            return (isinstance(value, dict) and set(value) == {"delete"} and
                    isinstance(value["delete"], list) and 1 <= len(value["delete"]) <= 4 and
                    all(isinstance(x, str) and re.fullmatch(r"[a-zA-Z0-9-]{1,80}", x) for x in value["delete"]))
        if path != "/prompt" or not isinstance(value, dict) or set(value) != {"prompt"}:
            return False
        graph = value["prompt"]
        if not isinstance(graph, dict) or set(graph) != {str(n) for n in range(3, 11)}:
            return False
        sampler = graph["3"]["inputs"]
        latent = graph["5"]["inputs"]
        positive, negative = graph["6"]["inputs"]["text"], graph["7"]["inputs"]["text"]
        overlap = graph["10"]["inputs"]["overlap"]
        seed = sampler["seed"]
        if (type(seed) is not int or not 0 <= seed < 2**64 or
                not isinstance(positive, str) or not 1 <= len(positive) <= 4000 or
                not isinstance(negative, str) or len(negative) > 4000 or
                type(overlap) is not int or overlap not in {8, 16} or
                type(latent["width"]) is not int or type(latent["height"]) is not int or
                latent["width"] != latent["height"] or latent["width"] not in {512, 768}):
            return False
        from havoc.services.imagegen import ImageGenerator
        generator = ImageGenerator(checkpoint="DreamShaperXL_Lightning.safetensors", width=latent["width"],
                                   height=latent["height"], negative=negative)
        expected, _, _ = generator._builtin(positive, seed, steps=4)
        expected["3"]["inputs"].update({"cfg": 1.0, "scheduler": "sgm_uniform", "model": ["10", 0]})
        expected["10"] = {"class_type": "FourWorkerSDXL", "inputs": {"model": ["4", 0], "overlap": overlap}}
        return graph == expected
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


class Gate(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        pass

    def permitted_body(self, path: str, body: bytes) -> bool:
        """Subclasses may tighten this; the default is the fixed graph."""
        return permitted_body(path, body)

    def do_GET(self):  # noqa: N802
        if self.path == "/healthz":
            data = b'{"ok":true,"service":"sdxl-cluster"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.forward("GET")

    def do_POST(self):  # noqa: N802
        self.forward("POST")

    def forward(self, method: str) -> None:
        try:
            permitted = allowed(method, self.path)
        except ValueError:
            permitted = False
        if not permitted:
            self.send_error(404)
            return
        if not authorized(self.headers.get("Authorization", "")):
            self.send_error(401)
            return
        body = None
        if method == "POST":
            try:
                size = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self.send_error(411)
                return
            if size < 1 or size > MAX_BODY:
                self.send_error(413)
                return
            body = self.rfile.read(size)
            if len(body) != size or not self.permitted_body(self.path, body):
                self.send_error(400)
                return
        req = urllib.request.Request(BACKEND + self.path, method=method, data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with _OPENER.open(req, timeout=300) as response:  # noqa: S310 - fixed loopback; redirects disabled
                data = response.read(MAX_RESPONSE + 1)
                status = response.status
                content_type = response.headers.get("Content-Type", "application/octet-stream")
        except urllib.error.HTTPError as exc:
            # Do not forward upstream error bodies that might contain a prompt.
            self.send_error(502 if 300 <= exc.code < 400 else exc.code)
            return
        except (OSError, urllib.error.URLError):
            self.send_error(502)
            return
        if len(data) > MAX_RESPONSE:
            self.send_error(502)
            return
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    if not REPOSITORY or not BRANCH:
        raise SystemExit("repository and branch scope are required")
    ThreadingHTTPServer(("127.0.0.1", 9009), Gate).serve_forever()
