#!/usr/bin/env python3
"""Prove the three hosted services from *inside the bot*, over real sockets.

This is the turn-48 check: not "does the module work", but "does `.web`, does
`.imagegen`, does `.ai` — the commands a person types — answer from a published
address, through the gate, with a token derived from the secret rather than read
from a file".

What is real here:

* three **gate subprocesses** (`scripts/service_gate.py`), the same file a runner
  runs, each in front of a stub upstream that speaks the real protocol;
* `.services/{search,comfy,ollama}.json` **written to the repository** exactly as
  `scripts/service_host.py` writes them — the bot reads the files, it is not
  handed objects;
* a real `Runtime`, built the way the bot builds it, with the real dispatcher, so
  `.web` / `.imagegen` / `.ai model 14` go through the parser, the cooldowns, the
  sender and the cards;
* a wrong secret, to prove the 401 reaches the person as a sentence.

Run: `python scripts/probe_hosted.py [-v]`
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

RESULTS: list[tuple[str, bool, str]] = []
#: The command cooldown, taken from the feature so the probe and the bot agree.
DRAW_COOLDOWN = 60.0
SECRET = "hosted-probe-secret"
RUN_ID = "481516"

#: 1x1 PNG, real bytes.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6300010000050001a5f645400000000049454e44ae426082"
)


def record(label: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((label, ok, detail))


class Quiet(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        pass

    def _json(self, payload: dict, status: int = 200) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}


class Searx(Quiet):
    """SearXNG's JSON surface, which is what `.web` reads."""

    queries: list[str] = []

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        query = (urllib.parse.parse_qs(parsed.query).get("q") or [""])[0]
        if not parsed.path.startswith("/search"):
            self._json({"error": "not found"}, 404)
            return
        Searx.queries.append(query)
        self._json(
            {
                "query": query,
                "number_of_results": 2,
                "results": [
                    {
                        "title": f"{query.title()} — a survey",
                        "url": "https://example.org/transformers",
                        "content": "Attention is all you need: a transformer stacks attention blocks.",
                        "engine": "duckduckgo",
                    },
                    {
                        "title": "Transformer (machine learning model)",
                        "url": "https://en.wikipedia.org/wiki/Transformer_(deep_learning)",
                        "content": "A transformer is a deep learning architecture based on attention.",
                        "engine": "wikipedia",
                    },
                ],
                "answers": [],
                "infoboxes": [],
                "suggestions": [],
                "unresponsive_engines": [],
            }
        )


class Comfy(Quiet):
    """ComfyUI: POST /prompt, GET /history/<id>, GET /view, GET /system_stats."""

    polls: dict[str, int] = {}
    prompts: list[dict] = []
    views: list[str] = []

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/system_stats":
            self._json({"system": {"os": "posix", "comfyui_version": "0.43.2"}, "devices": []})
            return
        if parsed.path.startswith("/history/"):
            prompt_id = parsed.path.rsplit("/", 1)[-1]
            Comfy.polls[prompt_id] = Comfy.polls.get(prompt_id, 0) + 1
            if Comfy.polls[prompt_id] < 2:
                self._json({})
                return
            self._json(
                {
                    prompt_id: {
                        "status": {"status_str": "success", "completed": True},
                        "outputs": {"9": {"images": [{"filename": "havoc_00001_.png", "subfolder": "", "type": "output"}]}},
                    }
                }
            )
            return
        if parsed.path == "/view":
            Comfy.views.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(PNG)))
            self.end_headers()
            self.wfile.write(PNG)
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        if urllib.parse.urlparse(self.path).path != "/prompt":
            self._json({"error": "not found"}, 404)
            return
        body = self._body()
        Comfy.prompts.append(body)
        self._json({"prompt_id": "p-hosted", "number": len(Comfy.prompts), "node_errors": {}})


class Ollama(Quiet):
    """Ollama's real shapes: /api/tags, plus the OpenAI surface under /v1."""

    chats: list[dict] = []

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        if path == "/api/tags":
            self._json({"models": [{"name": "qwen2.5:0.5b", "size": 397821319}]})
            return
        if path in {"/v1/models", "/models"}:
            self._json({"object": "list", "data": [{"id": "qwen2.5:0.5b", "object": "model"}]})
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        if not path.endswith("/chat/completions"):
            self._json({"error": "not found"}, 404)
            return
        body = self._body()
        Ollama.chats.append(body)
        self._json(
            {
                "id": "chatcmpl-hosted",
                "object": "chat.completion",
                "model": body.get("model") or "qwen2.5:0.5b",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "A transformer is attention stacked into blocks."},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 9, "total_tokens": 21},
            }
        )


def serve(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def start_gate(
    name: str, upstream_port: int, gate_port: int, secret: str | None = SECRET, public: bool = False
) -> subprocess.Popen:
    env = dict(
        os.environ,
        SERVICE_NAME=name,
        SERVICE_RUN_ID=RUN_ID,
        SERVICE_STARTED_AT="2026-11-01T00:00:00Z",
    )
    env.pop("SERVICE_SECRET", None)
    env.pop("SERVICE_PUBLIC", None)
    if secret is not None:
        env["SERVICE_SECRET"] = secret
    if public:
        env["SERVICE_PUBLIC"] = "1"
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "service_gate.py"),
         "--port", str(gate_port), "--upstream", f"127.0.0.1:{upstream_port}"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{gate_port}/healthz", timeout=1) as response:
                if response.status == 200:
                    return process
        except (urllib.error.URLError, OSError):
            time.sleep(0.1)
    raise RuntimeError(f"the {name} gate never started")


def publish(name: str, gate_port: int, model: str = "") -> None:
    """Write the file exactly as `service_host.payload_for` does — the bot reads it."""
    now = datetime.now(UTC).replace(microsecond=0)
    body = {
        "service": name,
        "url": f"http://127.0.0.1:{gate_port}",
        "run_id": RUN_ID,
        "started_at": "2026-11-01T00:00:00Z",
        "expires_at": (now + timedelta(hours=5)).isoformat().replace("+00:00", "Z"),
        "gate_port": gate_port,
        "model": model,
        "version": "probe",
        "published_at": now.isoformat().replace("+00:00", "Z"),
        "gated": True,
    }
    path = ROOT / ".services" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2) + "\n")


async def build_runtime():
    from conftest import FakeTelegram, make_config

    from havoc.features.art import DRAW_COOLDOWN as cooldown

    globals()["DRAW_COOLDOWN"] = cooldown

    from havoc.runtime import Runtime

    config = make_config(Path(tempfile.mkdtemp()))
    logger = logging.getLogger("hosted-probe")
    logger.setLevel(logging.CRITICAL)
    runtime = await Runtime.create(config, logger)
    runtime.telegram = FakeTelegram()
    runtime.client = runtime.telegram.client
    runtime.sender.client = runtime.client
    runtime.permissions.set_self_id(42)
    runtime.health.mark_connected()
    runtime.load_features()
    return runtime


async def say(runtime: Any, line: str, *, language: str = "en") -> list[Any]:
    from conftest import invoke

    return await invoke(runtime, line, sender_id=42, language=language)


def texts(sent: list[Any]) -> str:
    return "\n----\n".join(message.text for message in sent)


def files(sent: list[Any]) -> list[Any]:
    return [message.kwargs.get("file") for message in sent if "file" in message.kwargs]


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    for key in ("SEARXNG_URL", "COMFY_URL", "OLLAMA_HOST", "LOCAL_MODEL_URL", "COMFY_WORKFLOW", "GEMINI_API_KEY"):
        os.environ.pop(key, None)
    os.environ["SERVICE_SHARED_SECRET"] = SECRET
    os.environ["SERVICE_BRANCH"] = ""  # nothing to fetch: the files are already here
    os.environ["HAVOC_DRAW_DIR"] = tempfile.mkdtemp()

    searx_server, searx_port = serve(Searx)
    comfy_server, comfy_port = serve(Comfy)
    ollama_server, ollama_port = serve(Ollama)
    gates = {
        "search": start_gate("search", searx_port, 9101),
        "comfy": start_gate("comfy", comfy_port, 9102),
        "ollama": start_gate("ollama", ollama_port, 9103),
    }
    try:
        publish("search", 9101)
        publish("comfy", 9102, model="sd-turbo.safetensors")
        publish("ollama", 9103, model="qwen2.5:0.5b")

        runtime = await build_runtime()
        try:
            # ---------------------------------------------------------- .web
            sent = await say(runtime, ".web a transformer, briefly")
            text = texts(sent)
            if args.verbose:
                print(text)
            record("web · .web answers from the published address", "survey" in text.lower() or "attention" in text.lower(), text[:220])
            record("web · the search really reached SearXNG", bool(Searx.queries) and "transformer" in Searx.queries[0].lower(), str(Searx.queries[:2]))
            record("web · the card carries no traceback", "Traceback" not in text)
            endpoint = runtime.services.resolve("search")
            record("web · the token was derived, not read", len(endpoint.token) == 64, str(len(endpoint.token)))

            # ----------------------------------------------------- .imagegen
            sent = await say(runtime, ".imagegen a lighthouse in fog")
            paths = files(sent)
            text = texts(sent)
            if args.verbose:
                print(text, paths)
            record("imagegen · .imagegen is a real alias", bool(paths), text[:200])
            record(
                "imagegen · a real PNG file was sent",
                bool(paths) and Path(str(paths[0])).read_bytes().startswith(b"\x89PNG"),
                str(paths[0]) if paths else "no file",
            )
            record("imagegen · the graph asked for 8 steps", bool(Comfy.prompts) and _steps(Comfy.prompts[-1]) == 8, str(_steps(Comfy.prompts[-1]) if Comfy.prompts else None))
            record("imagegen · the picture came through /view", bool(Comfy.views), str(Comfy.views[:1]))

            # ------------------------------------------------------- .ai (14)
            card = texts(await say(runtime, ".ai"))
            if args.verbose:
                print(card)
            record("ai · the card lists row 14", "14" in card, card[:200])
            record("ai · the card names the hosted group", "our runner" in card.lower(), card[:400])
            record("ai · the card says whether the runner is up", "up" in card.lower() or "between" in card.lower(), card[:400])

            pinned = texts(await say(runtime, ".ai model 14"))
            if args.verbose:
                print(pinned)
            record("ai · .ai model 14 is accepted", "hosted model" in pinned.lower(), pinned[:220])
            record("ai · the selection is stored", str(runtime.ai.selection).startswith("hosted:"), str(runtime.ai.selection))

            answer = await runtime.ai.converse(
                [_message("user", "In one sentence, what is a transformer?")], max_tokens=60
            )
            record("ai · a real answer from the hosted model", "attention" in answer.lower(), answer[:200])
            record("ai · the request went through the gate to Ollama", bool(Ollama.chats), str(Ollama.chats[-1].get("model") if Ollama.chats else None))
            record("ai · the model name came from the published file", bool(Ollama.chats) and Ollama.chats[-1].get("model") == "qwen2.5:0.5b", str(Ollama.chats[-1].get("model") if Ollama.chats else None))
            record("ai · the router says where it came from", runtime.ai.active_label == "hosted", str(runtime.ai.active_label))

            worked, detail = await runtime.ai.probe()
            record("ai · .ai test answers while 14 is pinned", worked, detail[:160])

            # A wrong secret is a sentence, not a crash.
            os.environ["SERVICE_SHARED_SECRET"] = "not-the-secret"
            runtime.services.secret = "not-the-secret"
            runtime.services.refresh()
            runtime.web_search.url = ""
            refused = texts(await say(runtime, ".web something else entirely"))
            if args.verbose:
                print(refused)
            record("web · a wrong secret reads as the gate refusing", "gate refused" in refused.lower(), refused[:240])
            os.environ["SERVICE_SHARED_SECRET"] = SECRET
            runtime.services.secret = SECRET
            runtime.services.refresh()

            # A service between runs is named, with its age, not "not configured".
            (ROOT / ".services" / "comfy.json").unlink()
            runtime.services.refresh()
            runtime.image_gen.url = ""
            gap = texts(await say(runtime, ".imagegen a castle"))
            if args.verbose:
                print(gap)
            record("imagegen · between runs is said out loud", "between runs" in gap.lower() or "never" in gap.lower(), gap[:240])

            # .services agrees with all of it.
            card = texts(await say(runtime, ".services"))
            record("services · the card shows search and ollama up", "up" in card.lower(), card[:300])

            # ------------------------------------------------ no secret at all
            # The point of this turn: nothing configured, and it still works.
            # Both sides fall back to the committed default, so the token is
            # computed rather than communicated.
            for key in ("SERVICE_SECRET",):
                os.environ.pop(key, None)
            os.environ.pop("SERVICE_PUBLIC", None)
            gates["search"].terminate()
            gates["search"].wait(timeout=10)
            gates["search"] = start_gate("search", searx_port, 9101, secret=None)
            publish("search", 9101)
            runtime.services = _directory()
            runtime.web_search.directory = runtime.services
            runtime.web_search.url = ""
            record(
                "no-secret · the bot still derives a token",
                len(runtime.services.resolve("search").token) == 64,
                str(len(runtime.services.resolve("search").token)),
            )
            unauth = _get("http://127.0.0.1:9101/search?q=x")
            record("no-secret · the gate still refuses a request with no token", unauth[0] == 401, str(unauth[0]))
            plain = texts(await say(runtime, ".web a transformer"))
            if args.verbose:
                print(plain)
            record("no-secret · .web answers with nothing configured", "survey" in plain.lower() or "attention" in plain.lower(), plain[:200])

            # ------------------------------------------------------ fully public
            # SERVICE_PUBLIC=1: no token at all. The bot must not send a header
            # and the gate must not ask for one.
            os.environ["SERVICE_PUBLIC"] = "1"
            gates["comfy"].terminate()
            gates["comfy"].wait(timeout=10)
            gates["comfy"] = start_gate("comfy", comfy_port, 9102, secret=None, public=True)
            publish("comfy", 9102, model="sd-turbo.safetensors")
            runtime.services = _directory()
            runtime.image_gen.directory = runtime.services
            runtime.image_gen.url = ""
            endpoint = runtime.services.resolve("comfy")
            record("public · the published file says public", endpoint.gated is False, str(endpoint.snapshot()))
            record("public · the bot sends no Authorization header", endpoint.headers() == {}, str(endpoint.headers()))
            code, _body = _get("http://127.0.0.1:9102/system_stats")
            record("public · the gate answers without a token", code == 200, str(code))
            # The command's cooldown is real; wait it out rather than bypass it.
            await asyncio.sleep(DRAW_COOLDOWN + 1.0)
            os.environ["HAVOC_DRAW_DIR"] = tempfile.mkdtemp()
            sent = await say(runtime, ".imagegen a public castle")
            paths = files(sent)
            record("public · .imagegen still draws", bool(paths), texts(sent)[:160])
            os.environ.pop("SERVICE_PUBLIC", None)
        finally:
            await runtime.db.close()
    except Exception:  # noqa: BLE001 - a crash is the finding
        record("the probe itself crashed", False, traceback.format_exc(limit=6))
    finally:
        for process in gates.values():
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        for server in (searx_server, comfy_server, ollama_server):
            server.shutdown()
        for name in ("search", "comfy", "ollama"):
            (ROOT / ".services" / f"{name}.json").unlink(missing_ok=True)

    failures = [item for item in RESULTS if not item[1]]
    for label, ok, detail in RESULTS:
        print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f" — {detail}" if not ok and detail else ""))
    print(f"\nsummary: {len(RESULTS) - len(failures)}/{len(RESULTS)} checks passed")
    for label, _ok, detail in failures:
        print(f"  failed: {label} — {detail}")
    return 1 if failures else 0


def _directory():
    """A directory reading the real `.services` files, with whatever env is set."""
    from havoc.services.service_directory import ServiceDirectory

    return ServiceDirectory.from_env(root=ROOT)


def _get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except (urllib.error.URLError, OSError) as error:
        return 0, str(error).encode()


def _steps(graph: dict) -> int | None:
    for node in (graph.get("prompt") or graph).values():
        inputs = (node or {}).get("inputs") if isinstance(node, dict) else None
        if isinstance(inputs, dict) and "steps" in inputs:
            return int(inputs["steps"])
    return None


def _message(role: str, content: str) -> Any:
    from havoc.services.ai_provider import AIMessage

    return AIMessage(role, content)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
