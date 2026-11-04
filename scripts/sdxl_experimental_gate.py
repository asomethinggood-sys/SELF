#!/usr/bin/env python3
"""OIDC-protected fixed-graph gateway for the temporary SDXL experiment."""
from __future__ import annotations

import json
import re
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import sdxl_oidc_gate as gate  # noqa: E402

CHECKPOINT = "DreamShaperXL_Lightning.safetensors"
MAX_QUEUE = 2
_queue_lock = threading.Lock()


def permitted_body(path: str, body: bytes) -> bool:
    if path == "/queue":
        try:
            value = json.loads(body)
            return (isinstance(value, dict) and set(value) == {"delete"} and
                    isinstance(value["delete"], list) and 1 <= len(value["delete"]) <= 4 and
                    all(isinstance(x, str) and re.fullmatch(r"[a-zA-Z0-9-]{1,80}", x) for x in value["delete"]))
        except (ValueError, TypeError):
            return False
    try:
        value = json.loads(body)
        if path != "/prompt" or not isinstance(value, dict) or set(value) != {"prompt"}:
            return False
        graph = value["prompt"]
        if not isinstance(graph, dict) or set(graph) != {str(i) for i in range(3, 10)}:
            return False
        seed = graph["3"]["inputs"]["seed"]
        prompt = graph["6"]["inputs"]["text"]
        negative = graph["7"]["inputs"]["text"]
        if type(seed) is not int or not 0 <= seed < 2**64:
            return False
        if not isinstance(prompt, str) or not 1 <= len(prompt) <= 1500:
            return False
        if negative != "text, watermark, signature, blurry, low quality":
            return False
        from havoc.services.imagegen import ImageGenerator
        generator = ImageGenerator(checkpoint=CHECKPOINT, width=768, height=768, negative=negative)
        expected, _, _ = generator._builtin(prompt, seed, steps=4)
        expected["3"]["inputs"].update({"cfg": 1.0, "scheduler": "sgm_uniform"})
        return graph == expected
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


class ExperimentalGate(gate.Gate):
    # The base gate validates the fixed tiled graph; this one validates the
    # fixed single-runner graph instead. Both are checked before ComfyUI sees
    # the body, so a signed caller still cannot change the model or the sampler.
    def permitted_body(self, path: str, body: bytes) -> bool:
        return permitted_body(path, body)

    def do_POST(self):  # noqa: N802
        if self.path != "/prompt":
            return self.forward("POST")
        if not gate.authorized(self.headers.get("Authorization", "")):
            self.send_error(401)
            return
        with _queue_lock:
            try:
                # ComfyUI lists its queue with GET and deletes from it with POST.
                request = urllib.request.Request(gate.BACKEND + "/queue", method="GET")
                with gate._OPENER.open(request, timeout=5) as response:  # noqa: S310 - fixed loopback
                    raw = response.read(65537)
                if len(raw) > 65536:
                    raise ValueError("oversize queue response")
                queue = json.loads(raw)
                running = queue.get("queue_running")
                pending = queue.get("queue_pending")
                if not isinstance(running, list) or not isinstance(pending, list):
                    raise ValueError("invalid queue response")
                if len(running) + len(pending) >= MAX_QUEUE:
                    self.send_error(429)
                    return
            except (ValueError, TypeError, OSError, urllib.error.URLError):
                self.send_error(503)
                return
            self.forward("POST")


if __name__ == "__main__":
    if not gate.REPOSITORY or not gate.BRANCH:
        raise SystemExit("repository and branch scope are required")
    gate.ThreadingHTTPServer(("127.0.0.1", 9009), ExperimentalGate).serve_forever()
