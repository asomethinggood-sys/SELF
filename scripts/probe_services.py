#!/usr/bin/env python3
"""Prove the *external service* commands against real servers, not mocks.

``pytest`` proves that ``.web`` renders a card. It cannot prove that the card
comes from a SearXNG-shaped JSON document over HTTP, that the query string is
the one SearXNG expects, or that ``.draw`` really polls ``/history`` and really
downloads a PNG. Those failures live in the wire format, and the wire format is
the part that was never exercised.

So this script starts a **real HTTP server** on a loopback port for each service
(a small stdlib server that speaks the same shapes as SearXNG and ComfyUI),
points the environment at it, boots a runtime, and drives the commands through
the real dispatcher:

    python scripts/probe_services.py            # human summary, exit 1 on failure
    python scripts/probe_services.py -v         # print every card

Nothing here touches the internet, and nothing is mocked inside ``havoc``: the
only fake is the server on the other end of a socket.

SearXNG is a real dependency of this check in one direction only — the stub
speaks *its* protocol, so if ``havoc/services/websearch.py`` starts sending the
wrong parameters or reading the wrong field, this fails with the server's honest
complaint. The same for ComfyUI: ``/prompt`` -> ``/history/<id>`` -> ``/view``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

#: A 1×1 transparent PNG. Real bytes: the point is that the file on disk is a
#: picture, not that it is pretty.
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6300010000050001a5f645400000000049454e44ae426082"
)

RESULTS: list[tuple[str, bool, str]] = []

#: Imported from the feature so the probe and the bot cannot disagree about it.
from havoc.features.art import DRAW_COOLDOWN  # noqa: E402


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))


# ---------------------------------------------------------------------------
# Stub servers
# ---------------------------------------------------------------------------
class _Quiet(BaseHTTPRequestHandler):
    def log_message(self, *_args: Any) -> None:  # noqa: D102 - silence
        return

    def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send(status, json.dumps(payload).encode())


class SearxngStub(_Quiet):
    """The subset of SearXNG the bot uses: ``GET /search?q=..&format=json``."""

    seen: list[dict[str, list[str]]] = []

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        SearxngStub.seen.append(query)
        if parsed.path.rstrip("/") not in {"", "/search"}:
            self._json({"error": "not found"}, status=404)
            return
        if query.get("format", [""])[0] != "json":
            # SearXNG answers HTML (or 403) unless `json` is enabled in its
            # settings.yml; that is the mistake this check must catch.
            self._send(403, b"json format is not enabled")
            return
        words = (query.get("q", [""])[0] or "").strip()
        if not words:
            self._json({"results": []})
            return
        self._json(
            {
                "query": words,
                "number_of_results": 3,
                "results": [
                    {
                        "title": f"{words} — a serious answer",
                        "url": f"https://example.org/{urllib.parse.quote(words)}",
                        "content": "A paragraph of the result body, with <b>markup</b> that must be escaped.",
                        "engine": "duckduckgo",
                        "score": 3.1,
                    },
                    {
                        "title": "Second <i>result</i> & nonsense",
                        "url": "https://example.net/second?a=1&b=2",
                        "content": "",
                        "engine": "brave",
                    },
                    # Duplicates and empties are what a real engine returns.
                    {
                        "title": "Second <i>result</i> & nonsense",
                        "url": "https://example.net/second?a=1&b=2",
                        "content": "",
                    },
                    {"title": "", "url": "", "content": "no title, no link"},
                ],
            }
        )


class ComfyStub(_Quiet):
    """ComfyUI's three calls: queue, poll, fetch — plus a failure mode."""

    prompt_ids: list[str] = []
    polls: dict[str, int] = {}
    fail_prompt = False
    view_requests: list[str] = []

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        if self.path.rstrip("/") != "/prompt":
            self._json({"error": "not found"}, status=404)
            return
        if ComfyStub.fail_prompt:
            self._json({"error": {"type": "prompt_outputs_failed_validation"}}, status=400)
            return
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "bad json"}, status=400)
            return
        graph = body.get("prompt") or {}
        if not isinstance(graph, dict) or not graph:
            self._json({"error": "empty prompt"}, status=400)
            return
        # A real ComfyUI validates node classes; a graph with no KSampler is a
        # client bug worth failing loudly.
        if not any(node.get("class_type") == "KSampler" for node in graph.values() if isinstance(node, dict)):
            self._json({"error": "no KSampler in graph"}, status=400)
            return
        prompt_id = f"p{len(ComfyStub.prompt_ids) + 1}"
        ComfyStub.prompt_ids.append(prompt_id)
        self._json({"prompt_id": prompt_id, "number": len(ComfyStub.prompt_ids)})

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urllib.parse.urlparse(self.path)
        parts = [part for part in parsed.path.split("/") if part]
        if parts[:1] == ["history"] and len(parts) == 2:
            prompt_id = parts[1]
            seen = ComfyStub.polls.get(prompt_id, 0) + 1
            ComfyStub.polls[prompt_id] = seen
            if seen < 2:  # not finished on the first poll, exactly like the real one
                self._json({})
                return
            self._json(
                {
                    prompt_id: {
                        "status": {"status_str": "success", "completed": True},
                        "outputs": {
                            "9": {
                                "images": [
                                    {
                                        "filename": "havoc_00001_.png",
                                        "subfolder": "",
                                        "type": "output",
                                    }
                                ]
                            }
                        },
                    }
                }
            )
            return
        if parts[:1] == ["view"]:
            ComfyStub.view_requests.append(self.path)
            self._send(200, PNG_BYTES, content_type="image/png")
            return
        self._json({"error": "not found"}, status=404)


class LlamaStub(_Quiet):
    """llama.cpp's ``llama-server``: ``/v1/models`` + ``/v1/chat/completions``."""

    calls: list[dict[str, Any]] = []
    fail = False

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path.rstrip("/").endswith("/v1/models") or self.path.rstrip("/") == "/models":
            self._json({"object": "list", "data": [{"id": "qwen2.5-3b-instruct", "object": "model"}]})
            return
        self._json({"error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._json({"error": "not found"}, status=404)
            return
        if LlamaStub.fail:
            self._json({"error": "the model is loading"}, status=503)
            return
        LlamaStub.calls.append(body)
        self._json(
            {
                "id": "chatcmpl-local-1",
                "object": "chat.completion",
                "model": body.get("model") or "local",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "I am here, on this machine, with no key at all."},
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 12, "total_tokens": 32},
            }
        )


def _serve(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


def _closed_port() -> str:
    """A URL nothing is listening on — the "server is down" case."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
async def build_runtime():
    from conftest import FakeTelegram, make_config

    from havoc.runtime import Runtime

    config = make_config(Path(tempfile.mkdtemp()))
    logger = logging.getLogger("services-probe")
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


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------
async def probe_web(searx_url: str, verbose: bool) -> None:
    os.environ["SEARXNG_URL"] = searx_url
    runtime = await build_runtime()
    try:
        service = runtime.web_search
        record("web · the service was built from the environment", service is not None and service.available)

        sent = await say(runtime, ".web how do transformer models work")
        card = texts(sent)
        if verbose:
            print(card)
        record("web · one message, not a placeholder dance", len(sent) == 1, f"{len(sent)} messages")
        record("web · the query reached the engine", any(q.get("q") == ["how do transformer models work"] for q in SearxngStub.seen))
        record("web · json was requested explicitly", all(q.get("format") == ["json"] for q in SearxngStub.seen[-1:]))
        record("web · two results survived the duplicates", card.count("example.org") == 1 and card.count("example.net") == 1)
        record("web · markup from the engine is escaped", "&lt;b&gt;" in card and "&lt;i&gt;" in card, card[:160])
        record("web · the ampersand in a URL is escaped", "&amp;b=2" in card, card[-200:])
        record("web · the tally is at the bottom", "results" in card.splitlines()[-1].lower() or "2" in card.splitlines()[-1])

        info = texts(await say(runtime, ".web info"))
        record("web · .web info names the engine", searx_url in info, info[:120])

        # The engine dying mid-conversation is the normal Tuesday case.
        dead = _closed_port()
        runtime.web_search.url = dead
        failed = texts(await say(runtime, ".web anything"))
        record("web · a dead engine is a sentence, not a traceback", "traceback" not in failed.lower() and failed.strip() != "", failed[:160])
        runtime.web_search.url = searx_url

        # Persian must work through the same path.
        fa = texts(await say(runtime, ".web مدل زبان", language="fa"))
        record("web · Persian query is passed through", "مدل" in fa, fa[:160])
    finally:
        await runtime.db.close()


async def probe_draw(comfy_url: str, verbose: bool) -> None:
    os.environ["COMFY_URL"] = comfy_url
    runtime = await build_runtime()
    folder = Path(tempfile.mkdtemp())
    os.environ["HAVOC_DRAW_DIR"] = str(folder)
    try:
        generator = runtime.image_gen
        record("draw · the generator was built from the environment", generator is not None and generator.available)

        info = texts(await say(runtime, ".draw info"))
        record("draw · .draw info names the server", comfy_url in info, info[:120])

        sent = await say(runtime, ".draw a lighthouse in fog, pen and ink")
        paths = files(sent)
        if verbose:
            print(texts(sent), paths)
        record("draw · a file was sent", bool(paths), str(paths))
        record("draw · the file is a real PNG", bool(paths) and Path(str(paths[0])).read_bytes().startswith(b"\x89PNG"),
               str(paths[0]) if paths else "no file")
        record("draw · the poll loop ran twice before the picture", ComfyStub.polls.get("p1", 0) >= 2, str(ComfyStub.polls))
        record("draw · the file was fetched from /view", any("havoc_00001_.png" in item for item in ComfyStub.view_requests))
        record("draw · the caption carries the prompt", "lighthouse" in texts(sent).lower(), texts(sent)[:120])
        record("draw · the placeholder did not stay behind", "drawing" not in texts(sent).lower() or len(sent) == 1, f"{len(sent)} messages")
        record("draw · the picture is not in the chat twice", len(paths) == 1)

        # The cooldown is real and per chat; wait it out rather than disable it.
        await asyncio.sleep(DRAW_COOLDOWN + 0.4)
        # The seed flag must reach the graph, not be swallowed.
        import havoc.services.imagegen as imagegen_module

        seen_seeds: list[int] = []
        seen_steps: list[int] = []
        original = imagegen_module.ImageGenerator._graph

        def spy(self, prompt, *, seed=None, steps=None):
            seen_seeds.append(-1 if seed is None else seed)
            seen_steps.append(int(steps or 0))
            return original(self, prompt, seed=seed, steps=steps)

        imagegen_module.ImageGenerator._graph = spy  # type: ignore[method-assign]
        try:
            await say(runtime, ".draw a cat --seed 1234")
            await asyncio.sleep(DRAW_COOLDOWN + 0.4)
            await say(runtime, ".draw a cat --quality")
        finally:
            imagegen_module.ImageGenerator._graph = original  # type: ignore[method-assign]
        record("draw · --seed reaches the graph", 1234 in seen_seeds, str(seen_seeds))
        # The fast/quality split has to be real on the wire, not just a constant.
        record("draw · the default is 8 steps and --quality is 20", seen_steps[:2] == [8, 20], str(seen_steps))

        # A server that refuses the job (model missing) must read like a sentence.
        await asyncio.sleep(DRAW_COOLDOWN + 0.4)
        ComfyStub.fail_prompt = True
        try:
            refused = texts(await say(runtime, ".draw something"))
        finally:
            ComfyStub.fail_prompt = False
        record("draw · a refused job is explained", "refused" in refused.lower() or "checkpoint" in refused.lower(), refused[:200])

        # Server down entirely.
        await asyncio.sleep(DRAW_COOLDOWN + 0.4)
        dead = _closed_port()
        runtime.image_gen.url = dead
        down = texts(await say(runtime, ".draw an island"))
        record("draw · a dead server is a sentence", down.strip() != "" and "Traceback" not in down, down[:200])
    finally:
        await runtime.db.close()


async def probe_gate(searx_url: str, verbose: bool) -> None:
    """The gate, the resolver and a real 401 — over real sockets.

    Everything here runs as it does on a runner: `scripts/service_gate.py` is
    started as a **subprocess** in front of the SearXNG stub, the bot is pointed
    at it through `.services/search.json` (an override, so no file is written),
    and the token is derived the same way on both sides. What this proves that a
    unit test cannot: the bytes that leave the bot are the bytes the gate accepts.
    """
    from datetime import datetime, timedelta

    from havoc.services.service_directory import (
        STATE_BETWEEN_RUNS,
        STATE_EXPIRED,
        ServiceDirectory,
    )

    secret = "probe-shared-secret"
    run_id = "424242"
    started_at = "2026-11-01T00:00:00Z"
    port = int(_closed_port().rsplit(":", 1)[1])
    upstream = searx_url.replace("http://", "")
    env = dict(
        os.environ,
        SERVICE_NAME="search",
        SERVICE_SECRET=secret,
        SERVICE_RUN_ID=run_id,
        SERVICE_STARTED_AT=started_at,
    )
    process = subprocess.Popen(
        [sys.executable, "scripts/service_gate.py", "--port", str(port), "--upstream", upstream],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(ROOT),
    )
    first_line = ""
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            line = process.stdout.readline() if process.stdout else ""
            if line:
                first_line = line.strip()
                if first_line.startswith("service_gate:"):
                    break
            if process.poll() is not None:
                break
            time.sleep(0.05)
        record("gate · the gate started and said where it listens", "service_gate:" in first_line, first_line or "no output")

        base = f"http://127.0.0.1:{port}"
        now = datetime.now(UTC)
        published = {
            "service": "search",
            "url": base,
            "run_id": run_id,
            "started_at": started_at,
            "published_at": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "expires_at": (now + timedelta(hours=6)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "model": "",
            "gated": True,
        }

        # 1. A wrong token is refused at the gate, in JSON, over a real socket.
        wrong = urllib.request.Request(
            f"{base}/search?q=transformers", headers={"Authorization": "Bearer " + "0" * 64}
        )
        code = 0
        body = ""
        try:
            with urllib.request.urlopen(wrong, timeout=10) as response:
                code = response.status
                body = response.read().decode()
        except urllib.error.HTTPError as error:
            code = error.code
            body = error.read().decode()
        record("gate · a wrong token is a 401", code == 401, f"{code} {body[:120]}")
        record("gate · the 401 is a readable JSON error", '"unauthorized"' in body, body[:160])

        # 2. /healthz needs no token: that is what the tunnel check uses.
        with urllib.request.urlopen(f"{base}/healthz", timeout=10) as response:
            health = json.loads(response.read().decode())
        record("gate · /healthz answers without a token", response.status == 200 and health.get("service") == "search", str(health)[:160])

        # 3. The bot, told the same secret, gets results *through* the gate.
        for key in ("SEARXNG_URL", "SEARXNG_ENGINES"):
            os.environ.pop(key, None)
        runtime = await build_runtime()
        try:
            directory = ServiceDirectory(override={"search": published}, secret=secret, branch="")
            runtime.services = directory
            if runtime.web_search is not None:
                runtime.web_search.directory = directory
                runtime.web_search.url = ""
            record("gate · the directory resolves the published file", directory.resolve("search").ok,
                   directory.resolve("search").note)
            record("gate · the token is derived, not read", len(directory.resolve("search").token) == 64,
                   str(len(directory.resolve("search").token)))

            here = len(SearxngStub.seen)
            answer = texts(await say(runtime, ".web a transformer, briefly"))
            if verbose:
                print(answer)
            record("gate · .web answers through the gate", "transform" in answer.lower() or "attention" in answer.lower(), answer[:200])
            record("gate · the query reached the upstream instance", len(SearxngStub.seen) > here, str(SearxngStub.seen[-1:]))

            # 4. A checkout with the wrong secret is "the gate refused me", not a crash.
            wrong_dir = ServiceDirectory(override={"search": published}, secret="not-the-secret", branch="")
            runtime.services = wrong_dir
            runtime.web_search.directory = wrong_dir
            refused = texts(await say(runtime, ".web something else entirely"))
            record("gate · a mismatched secret reads like a sentence", "gate refused" in refused.lower(), refused[:240])

            # 5. Between runs and expired are different sentences, and neither is
            #    "not configured". A service spends minutes a day in the first one.
            between = dict(published, url="")
            runtime.services = ServiceDirectory(override={"search": between}, secret=secret, branch="")
            runtime.web_search.directory = runtime.services
            runtime.web_search.url = ""
            gap = texts(await say(runtime, ".web anything"))
            record("gate · between runs says so, with the age", "between runs" in gap.lower(), gap[:240])
            assert runtime.services.resolve("search").state == STATE_BETWEEN_RUNS

            old = dict(
                published,
                published_at=(now - timedelta(hours=7)).isoformat().replace("+00:00", "Z"),
                expires_at=(now - timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
            )
            runtime.services = ServiceDirectory(override={"search": old}, secret=secret, branch="")
            runtime.web_search.directory = runtime.services
            expired = texts(await say(runtime, ".web anything at all"))
            record("gate · an expired address is named as expired", runtime.services.resolve("search").state == STATE_EXPIRED,
                   runtime.services.resolve("search").state)
            record("gate · an expired service still explains itself", "between runs" in expired.lower() or "past" in expired.lower(), expired[:240])

            # 6. `.services` shows the truth about all three, reachable from help.
            runtime.services = ServiceDirectory(override={"search": published, "comfy": between}, secret=secret, branch="")
            card = texts(await say(runtime, ".services"))
            if verbose:
                print(card)
            record("gate · .services names the reachable one", "search" in card.lower() and "up" in card.lower(), card[:240])
            record("gate · .services names the one between runs", "between runs" in card.lower(), card[:240])
            record("gate · .services names the one that never ran", "never" in card.lower() or "not" in card.lower(), card[:240])
            help_text = texts(await say(runtime, ".help"))
            record("gate · .services is in the help", "services" in help_text.lower())
            # Plain language reaches it through the model's hands: the tool layer
            # must offer a `services` tool whose answer is the same card.
            layer = getattr(runtime, "actions", None)
            offered = [tool.name for tool in layer.offered()] if layer is not None else []
            record("gate · the model is offered a services tool", "services" in offered, str(offered)[:200])
            tool = layer.get("services") if layer is not None else None
            if tool is not None:
                from havoc.services.actions import Turn

                turn = Turn(chat_id=42, actor_id=42, message_id=1, language="en")
                outcome = await tool.run(turn, {})
                record(
                    "gate · the tool answers with the same states",
                    bool(outcome.ok) and "comfy" in (outcome.note or ""),
                    (outcome.note or outcome.detail or "")[:200],
                )
            else:
                record("gate · the tool answers with the same states", False, "no services tool")
        finally:
            await runtime.db.close()
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        print(f"gate · stopped (exit {process.returncode})") if False else None


async def probe_unconfigured() -> None:
    """With no services at all, the commands must teach instead of fail."""
    for key in ("SEARXNG_URL", "COMFY_URL", "COMFY_WORKFLOW"):
        os.environ.pop(key, None)
    runtime = await build_runtime()
    try:
        record("unconfigured · no web service object", runtime.web_search is None or not runtime.web_search.available)
        web = texts(await say(runtime, ".web what is a transformer"))
        record("unconfigured · .web explains the setup", "SEARXNG_URL" in web, web[:160])
        draw = texts(await say(runtime, ".draw a castle"))
        record("unconfigured · .draw explains the setup", "COMFY_URL" in draw, draw[:160])
        # Both must be reachable from the help screen, or nobody finds them.
        help_text = texts(await say(runtime, ".help"))
        record("unconfigured · .web is in the help", "web" in help_text)
        record("unconfigured · .draw is in the help", "draw" in help_text)
    finally:
        await runtime.db.close()



async def probe_local_model(llama_url: str, verbose: bool) -> None:
    """The spare tyre: no keys, one model on this machine, an answer anyway."""
    os.environ["LOCAL_MODEL_URL"] = f"{llama_url}/v1"
    os.environ["LOCAL_MODEL_NAME"] = "qwen2.5-3b-instruct"
    runtime = await build_runtime()
    try:
        model = runtime.local_model
        record("local · the model was built from the environment", model is not None and model.base_url.endswith("/v1"))
        ready = await model.start()
        record("local · start() finds the endpoint", ready is True and model.available is True, str(model.describe()))

        from havoc.services.ai_router import AIRouter

        router = runtime.ai
        if isinstance(router, AIRouter):
            router.local = model
        reply = await router.converse([_Turn("user", "who are you?")])
        if verbose:
            print(reply)
        record("local · the router answers with no keys at all", "on this machine" in reply, reply[:120])
        record("local · the router admits it was the local model", router.active_label == "local", str(router.active_label))
        record("local · the request was a real chat completion", bool(LlamaStub.calls) and "who are you?" in json.dumps(LlamaStub.calls[-1]))

        # A model still loading must fall back rather than hang or lie.
        LlamaStub.fail = True
        try:
            from havoc.services.ai_router import AIError

            try:
                await router.converse([_Turn("user", "hello")])
                record("local · a failing model raises", False, "no error raised")
            except AIError as exc:
                record("local · a failing model raises a typed error", exc.kind in {"network", "disabled", "rate_limited"}, str(exc)[:120])
        finally:
            LlamaStub.fail = False

        # And the health surface must show it, or the owner cannot tell.
        snapshot = runtime.ai.describe() if hasattr(runtime.ai, "describe") else {}
        record("local · diagnostics name the local model", "local" in json.dumps(snapshot, default=str), json.dumps(snapshot, default=str)[:160])
    finally:
        await runtime.db.close()


class _Turn:
    """The smallest thing that looks like a chat message to the router."""

    def __init__(self, role: str, content: str) -> None:
        self.role = role
        self.content = content
        self.has_image = False



async def probe_bulk(verbose: bool) -> None:
    """A mass send is a *job*: one row, progress, a cursor, a resume.

    None of this can be judged from a unit test. The tick has to return in
    milliseconds while twenty messages are still going out, a reminder due at the
    same moment must not wait behind them, a cancel must park the row where it
    stopped, and a runner started over the same database must continue from the
    cursor instead of starting over.
    """
    import havoc.services.reach as reach_module

    original_gap = reach_module.MIN_GAP
    # The bot's 1.2s floor is a rate limit, not a design constant; the shape of
    # the job is what is under test here.
    reach_module.MIN_GAP = 0.05
    runtime = await build_runtime()

    def body_count(body: str) -> int:
        return sum(1 for message in runtime.client.sent if message.text.strip() == body)

    async def run_until_idle(seconds: float = 30.0) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            await runtime.scheduler.tick()
            if not runtime.jobs.snapshot():
                return
            await asyncio.sleep(0.05)

    try:
        # ---- a job that runs to completion --------------------------------
        started = len(runtime.client.sent)
        # Something due *while* the job runs: the complaint was that a reminder
        # used to wait behind a mass send.
        await say(runtime, ".remind 2s stretch")
        reply = texts(await say(runtime, ".send --here hello there --repeat 20"))
        if verbose:
            print(reply)

        rows = await runtime.db.fetch_all("SELECT * FROM jobs WHERE kind = 'reach_bulk' ORDER BY id DESC LIMIT 5")
        record("bulk · twenty repeats made exactly one job row", len(rows) == 1, f"{len(rows)} rows")
        job = rows[0] if rows else None
        payload = json.loads(job["payload"]) if job else {}
        record(
            "bulk · the row carries the work, not a stub",
            job is not None and int(payload.get("total") or 0) == 19,
            str(payload)[:160],
        )
        record("bulk · she says a job id, not just 'queued'", bool(job) and str(job["id"]) in reply, reply[:160])

        clock_start = time.monotonic()
        fired = await runtime.scheduler.tick()
        tick_seconds = time.monotonic() - clock_start
        record("bulk · the tick returns without sending twenty messages", tick_seconds < 1.5, f"{tick_seconds:.2f}s")
        record("bulk · the tick reports it deferred the job", fired.get("deferred") == 1, str(fired))
        running = runtime.jobs.snapshot()
        record(
            "bulk · the runner shows live progress",
            bool(running) and running[0]["kind"] == "reach_bulk" and running[0]["total"] == 19,
            str(running)[:200],
        )

        reminder_during_job = False
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            await runtime.scheduler.tick()
            if "stretch" in " ".join(message.text for message in runtime.client.sent) and runtime.jobs.snapshot():
                reminder_during_job = True
            if not runtime.jobs.snapshot():
                break
            await asyncio.sleep(0.05)
        record("bulk · a reminder due mid-job is delivered mid-job", reminder_during_job)
        final = await runtime.db.fetch_all("SELECT * FROM jobs WHERE id = ?", (job["id"],)) if job else []
        record(
            "bulk · the job finished as done",
            bool(final) and final[0]["status"] == "done",
            final[0]["status"] if final else "no row",
        )
        # One message is sent immediately and nineteen are the job's work.
        record("bulk · twenty copies arrived in all", body_count("hello there") == 20, f"{body_count('hello there')} bodies")
        record("bulk · nothing was sent twice", len(runtime.client.sent) - started <= 23, f"{len(runtime.client.sent) - started} messages")

        # ---- a crash: the task dies, the row must survive ---------------
        reach_module.MIN_GAP = 0.08
        before_crash = body_count("again")
        await say(runtime, ".send --here again --repeat 60")
        await runtime.scheduler.tick()
        rows = await runtime.db.fetch_all("SELECT * FROM jobs WHERE kind = 'reach_bulk' AND status = 'running' ORDER BY id DESC")
        crashed = rows[0] if rows else None
        record("bulk · the second job is running", crashed is not None)
        await asyncio.sleep(0.5)
        crash_id = int(crashed["id"]) if crashed else 0
        task = runtime.jobs._tasks.get(crash_id)  # noqa: SLF001 - a probe may look inside
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await asyncio.sleep(0.2)
        parked = await runtime.db.fetch_all("SELECT * FROM jobs WHERE id = ?", (crash_id,)) if crash_id else []
        state = parked[0]["status"] if parked else "no row"
        stored = json.loads(parked[0]["payload"]) if parked else {}
        cursor = int(stored.get("cursor") or 0)
        record("bulk · a killed task leaves the row resumable, not failed", state == "pending", state)
        record("bulk · the cursor remembers how far it got", 0 < cursor < 59, f"cursor={cursor}")
        sent_before = body_count("again") - before_crash

        # ---- a restart: a brand-new runner over the same database -------
        from havoc.services.jobs import JobRunner

        fresh = JobRunner(store=runtime.store, logger=runtime.logger, scheduler=runtime.scheduler)
        fresh.register("reach_bulk", runtime.reach.send_bulk)
        reach_module.MIN_GAP = 0.02
        claimed = await fresh.start(parked[0]) if parked else False
        record("bulk · a fresh runner claims the parked row", claimed is True)
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline and fresh.snapshot():
            await asyncio.sleep(0.05)
        after = await runtime.db.fetch_all("SELECT * FROM jobs WHERE id = ?", (crash_id,)) if crash_id else []
        record(
            "bulk · the resumed job finishes",
            bool(after) and after[0]["status"] == "done",
            after[0]["status"] if after else "no row",
        )
        sent_after = body_count("again") - before_crash - sent_before
        # 59 job sends plus the one the command made itself; a restart that began
        # again from zero would show 60 more, not (59 - cursor).
        record(
            "bulk · it resumed instead of starting over",
            sent_after <= 60 - sent_before,
            f"{sent_after} more, {sent_before} already sent",
        )
        record("bulk · every repeat arrived exactly once", body_count("again") == 60, f"{body_count('again')} of 60")
        await fresh.shutdown()

        # ---- a user cancel is a decision, not a crash -------------------
        reach_module.MIN_GAP = 0.08
        await say(runtime, ".send --here stop me --repeat 40")
        await runtime.scheduler.tick()
        rows = await runtime.db.fetch_all("SELECT * FROM jobs WHERE kind = 'reach_bulk' AND status = 'running' ORDER BY id DESC")
        cancelled_row = rows[0] if rows else None
        await asyncio.sleep(0.4)
        # What `.tasks` says *while* it runs is the owner's only window into a
        # job he walked away from.
        live = texts(await say(runtime, ".tasks"))
        record(
            "bulk · .tasks shows the job while it runs",
            bool(cancelled_row) and str(cancelled_row["id"]) in live and "%" in live,
            live[:240],
        )
        record("bulk · .tasks cancel finds a running job", runtime.jobs.cancel(int(cancelled_row["id"])) if cancelled_row else False)
        await asyncio.sleep(0.4)
        stopped = await runtime.db.fetch_all("SELECT * FROM jobs WHERE id = ?", (cancelled_row["id"],)) if cancelled_row else []
        record(
            "bulk · a user cancel is recorded as cancelled",
            bool(stopped) and stopped[0]["status"] == "cancelled",
            stopped[0]["status"] if stopped else "no row",
        )
        stopped_payload = json.loads(stopped[0]["payload"]) if stopped else {}
        record("bulk · a cancel keeps the count it reached", int(stopped_payload.get("sent") or 0) > 0, str(stopped_payload)[:120])

        spent = texts(await say(runtime, ".tasks"))
        record("bulk · the cancelled job is no longer running", "%" not in spent or "Pending tasks (0)" in spent, spent[:160])
        await runtime.jobs.shutdown()
    finally:
        reach_module.MIN_GAP = original_gap
        await runtime.db.close()



async def probe_lanes(verbose: bool) -> None:
    """Two lanes, one budget: a reply must not queue behind a mass send.

    Driven through the real :class:`Pacer` with the real clock and a *small*
    budget, so the arithmetic that the production path uses is the arithmetic on
    show: six sends a minute, two of them reserved for whoever is waiting.
    """
    from havoc.services.pacer import Lane, Pacer
    from havoc.services.rate_limiter import RateLimiter

    limiter = RateLimiter(outbound_limit=6, outbound_window=60.0)
    pacer = limiter.pacer
    assert pacer is not None
    record("lanes · the limiter owns a pacer", True, str(pacer.snapshot()["reserve"]))
    record("lanes · bulk may not take the whole budget", pacer.interactive_reserve == 2, str(pacer.interactive_reserve))

    # Spend four slots in the interactive lane. The account just spoke, so bulk
    # is held at the reserve line: four used, ceiling four.
    for _ in range(4):
        await limiter.acquire_outbound(lane=Lane.INTERACTIVE)
    record("lanes · four of six slots are gone", pacer.counter.used() == 4, str(pacer.counter.used()))
    record("lanes · bulk is held at the reserve line", pacer._bulk_ceiling(pacer.clock()) == 4, str(pacer._bulk_ceiling(pacer.clock())))  # noqa: SLF001

    bulk_waits: list[float] = []

    async def bulk_send(index: int) -> None:
        start = time.monotonic()
        try:
            await pacer.acquire(Lane.BULK, timeout=0.6)
            bulk_waits.append(time.monotonic() - start)
        except TimeoutError:
            bulk_waits.append(-1.0)

    bulk_task = asyncio.create_task(bulk_send(1))
    await asyncio.sleep(0.25)
    record("lanes · bulk is refused while the reserve is a person's", bulk_waits == [-1.0] or (bulk_waits and bulk_waits[0] < 0), str(bulk_waits))

    start = time.monotonic()
    await limiter.acquire_outbound(lane=Lane.INTERACTIVE)
    first_wait = time.monotonic() - start
    record("lanes · an interactive send is not queued behind bulk", first_wait < 0.25, f"{first_wait:.3f}s")
    start = time.monotonic()
    await limiter.acquire_outbound(lane=Lane.INTERACTIVE)
    second_wait = time.monotonic() - start
    record("lanes · the reserve is really reserved", second_wait < 0.25, f"{second_wait:.3f}s")
    await bulk_task

    # A quiet account: nobody has said anything for a while, so bulk gets nearly
    # everything — but the last slot is still kept, which is the guarantee that
    # a person arriving mid-job waits one gap and not one window.
    quiet = Pacer(per_minute=6, handover=0.2)
    quiet._last_interactive_at = quiet.clock() - 1.0  # noqa: SLF001 - a quiet minute
    record("lanes · a quiet account lets bulk run near full speed", quiet._bulk_ceiling(quiet.clock()) == 5, str(quiet._bulk_ceiling(quiet.clock())))  # noqa: SLF001
    for _ in range(5):
        await quiet.acquire(Lane.BULK, timeout=1.0)
    start = time.monotonic()
    await quiet.acquire(Lane.INTERACTIVE, timeout=1.0)
    arriving = time.monotonic() - start
    record("lanes · a person arriving mid-drain is served at once", arriving < 0.2, f"{arriving:.3f}s")

    # FIFO inside a lane: two bulk waiters are served in arrival order.
    from havoc.services.cache import RateCounter

    # A one-second window so the second waiter is served by the next window
    # rather than by tomorrow: the *order* is what is under test.
    fifo = Pacer(per_minute=2, reserve=0.0, counter=RateCounter(2, 1.0))
    order: list[int] = []

    async def waiter(index: int) -> None:
        await fifo.acquire(Lane.BULK, timeout=5)
        order.append(index)

    tasks = [asyncio.create_task(waiter(1)), asyncio.create_task(waiter(2))]
    await asyncio.sleep(0.05)
    while any(not task.done() for task in tasks):
        await asyncio.sleep(0.05)
    record("lanes · waiters inside a lane are served in order", order == [1, 2], str(order))
    record("lanes · the budget itself never changed", limiter.outbound_limit == 6, str(limiter.outbound_limit))
    if verbose:
        print(pretty(limiter.snapshot()))


async def probe_lag(verbose: bool) -> None:
    """The loop itself has to be measurable, or "slow" is a guess."""
    runtime = await build_runtime()
    try:
        lag = runtime.lag
        record("lag · the runtime carries a monitor", lag is not None)
        lag.start(runtime.logger)
        # Block the loop the way a big export or a synchronous DB call does: one
        # long stall, long enough that a two-second heartbeat cannot miss it.
        await asyncio.sleep(0.1)
        time.sleep(2.6)
        await asyncio.sleep(2.2)
        worst = lag.worst_ms if hasattr(lag, "worst_ms") else lag.snapshot().get("worst_ms", 0)
        record("lag · a blocked loop is recorded", float(worst) >= 300, f"worst={worst}")
        snapshot = await runtime.snapshot()
        record(
            "lag · diagnostics carry the number",
            "loop_lag" in json.dumps(snapshot, default=str),
            json.dumps(snapshot, default=str)[:200],
        )
        diagnose = texts(await say(runtime, ".diagnose"))
        record("lag · .diagnose mentions the loop", "loop" in diagnose.lower(), diagnose[:200])
        if verbose:
            print(diagnose[:400])
    finally:
        if hasattr(runtime.lag, "stop"):
            await runtime.lag.stop()
        await runtime.db.close()


def pretty(payload: Any) -> str:
    return json.dumps(payload, indent=2, default=str)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    searx_server, searx_url = _serve(SearxngStub)
    comfy_server, comfy_url = _serve(ComfyStub)
    llama_server, llama_url = _serve(LlamaStub)
    try:
        scenarios = [
            ("web", probe_web(searx_url, args.verbose)),
            ("draw", probe_draw(comfy_url, args.verbose)),
            ("local", probe_local_model(llama_url, args.verbose)),
            ("bulk", probe_bulk(args.verbose)),
            ("lanes", probe_lanes(args.verbose)),
            ("lag", probe_lag(args.verbose)),
            ("unconfigured", probe_unconfigured()),
            ("gate", probe_gate(searx_url, args.verbose)),
        ]
        for name, coro in scenarios:
            print(f"=== {name.upper()} ===")
            start = len(RESULTS)
            try:
                await coro
            except Exception:  # noqa: BLE001 - a crash is the finding
                record(f"{name} · the probe itself crashed", False, traceback.format_exc(limit=4))
            for label, ok, detail in RESULTS[start:]:
                mark = "ok  " if ok else "FAIL"
                line = f"  {mark} {label}"
                if not ok and detail:
                    line += f"\n       ↳ {detail}"
                print(line)
    finally:
        searx_server.shutdown()
        comfy_server.shutdown()
        llama_server.shutdown()

    failures = [item for item in RESULTS if not item[1]]
    print(f"\nsummary: {len(RESULTS) - len(failures)}/{len(RESULTS)} checks passed")
    for label, _ok, detail in failures:
        print(f"  failed: {label} — {detail}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
