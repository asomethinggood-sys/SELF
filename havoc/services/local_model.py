"""A model that lives on this machine: no key, no quota, no rate limit.

The free keys are a miracle and a bottleneck: Gemma's daily allowance is generous
right up until it is spent, and then the whole account goes quiet until
midnight. A small model served locally — llama.cpp behind an OpenAI-compatible
``/v1`` — removes both the quota and the network from the conversation. It is
slower per word and much less clever, which is exactly the trade it should be:
the keys answer first, and this answers when there is no key left.

Two ways in, and the difference matters:

* **A server already running** (set ``LOCAL_MODEL_URL``). Anything that speaks
  OpenAI chat completions works: llama.cpp, Ollama, vLLM, LM Studio. We do not
  care how it got there.
* **A server we start** (``LOCAL_MODEL_BIN`` + ``LOCAL_MODEL_GGUF``). In a
  GitHub runner there is no daemon to rely on, so the process is ours: start it,
  wait for ``/health``, stop it when the bot stops. If either path is missing the
  class reports itself unavailable in one sentence and nothing else changes.

The class duck-types :class:`havoc.services.ai_provider.AIProvider`, so the rest
of the codebase treats it as one more provider in the rotation.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .http import HttpError, post_json, request

#: Where a local server usually listens when it was started by hand.
DEFAULT_URL = "http://127.0.0.1:8080/v1"
#: A small model on a CPU runner produces a paragraph in a few seconds. Longer
#: than this and the caller is better off with the offline bank.
DEFAULT_TIMEOUT = 60.0
#: How long to wait for a freshly started server to answer ``/health``. A 1B
#: quantised model loads from disk in about ten seconds; thirty is generous.
START_TIMEOUT = 90.0


@dataclass
class LocalModel:
    """An OpenAI-compatible endpoint on this machine, started if necessary."""

    base_url: str = ""
    model: str = ""
    binary: str = ""
    weights: str = ""
    timeout: float = DEFAULT_TIMEOUT
    #: Extra llama-server flags (context size, threads). Kept as a string so an
    #: environment variable can shape it without a schema change.
    flags: str = ""
    logger: Any = None
    enabled: bool = True
    requests: int = 0
    failures: int = 0
    last_error: str = ""
    _process: Any = field(default=None, repr=False)
    #: The server's own words, kept because "exited while loading" is not a
    #: diagnosis. A llama-server that dies on a missing shared library or an
    #: unknown flag says so on stderr, and that line is the whole fix.
    _log: Any = field(default=None, repr=False)
    _ready: bool = False
    _checked_at: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    name = "local"
    #: The directory to ask when ``base_url`` is empty; ``None`` means no hosting.
    directory: Any = None
    #: True when the endpoint came from ``.services/ollama.json`` behind a gate.
    gated: bool = False
    #: The same words a credential uses in the UI, so `.ai` reads the same
    #: whether the answer came from a key or from this machine.
    active_label: str = "local"

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls, *, logger: Any = None, directory: Any = None) -> LocalModel | None:
        """Build one from the environment, or ``None`` when nothing is configured.

        With nothing in the environment but a service directory, the endpoint is
        whatever the last ``service-ollama`` run published — the same spare tyre,
        reached through a gate instead of a local port.
        """
        url = (os.environ.get("LOCAL_MODEL_URL") or "").strip()
        binary = (os.environ.get("LOCAL_MODEL_BIN") or "").strip()
        weights = (os.environ.get("LOCAL_MODEL_GGUF") or "").strip()
        if not url and not (binary and weights):
            if directory is None:
                return None
            return cls(
                base_url="",
                model=(os.environ.get("LOCAL_MODEL_NAME") or "").strip(),
                timeout=float(os.environ.get("LOCAL_MODEL_TIMEOUT") or DEFAULT_TIMEOUT),
                logger=logger,
                directory=directory,
                gated=True,
            )
        model = (os.environ.get("LOCAL_MODEL_NAME") or "").strip()
        return cls(
            base_url=_normalise(url or DEFAULT_URL),
            model=model or "local",
            binary=binary,
            weights=weights,
            timeout=float(os.environ.get("LOCAL_MODEL_TIMEOUT") or DEFAULT_TIMEOUT),
            flags=(os.environ.get("LOCAL_MODEL_FLAGS") or "").strip(),
            logger=logger,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> bool:
        """Make an endpoint exist. True when something answers afterwards.

        Failing here is not an error the owner needs to see: the keys are the
        product, and this is the spare tyre. It is logged once, plainly, and the
        router simply does not include it.
        """
        async with self._lock:
            if await self._healthy():
                self._ready = True
                return True
            if self.directory is not None and not self.base_url:
                # Hosted: there is no process for us to start. Not being there
                # yet is a state (`between runs`), not a failure to report.
                self.last_error = self.last_error or "the hosted model is between runs"
                return False
            if self._process is not None:
                return False
            if not (self.binary and Path(self.weights).exists()):
                self.last_error = "no local server and no weights to start one with"
                return False
            binary = shutil.which(self.binary) or self.binary
            if not Path(binary).exists():
                self.last_error = f"the llama.cpp binary is missing: {self.binary}"
                return False
            command = [
                binary,
                "--model", self.weights,
                "--host", "127.0.0.1",
                "--port", str(_port_of(self.base_url)),
                "--ctx-size", "2048",
                "--threads", str(max(1, (os.cpu_count() or 2) - 1)),
                "--no-webui",
            ]
            command.extend(self.flags.split())
            try:
                # A file, not a pipe: an unread pipe fills and deadlocks the
                # server mid-load, which would look exactly like a hang.
                self._log = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed in stop()
                    prefix="havoc-llama-", suffix=".log", delete=False
                )
                self._process = subprocess.Popen(  # noqa: S603 - our own binary, our own args
                    command,
                    stdout=self._log,
                    stderr=subprocess.STDOUT,
                )
            except Exception as exc:  # noqa: BLE001 - a missing server is not fatal
                self.last_error = f"could not start the local model: {exc}"
                self._process = None
                return False
            deadline = time.monotonic() + START_TIMEOUT
            while time.monotonic() < deadline:
                if self._process.poll() is not None:
                    self.last_error = f"the local model exited while loading: {self._tail()}"
                    self._close_log()
                    self._process = None
                    return False
                if await self._healthy():
                    self._ready = True
                    if self.logger is not None:
                        self.logger.info(
                            "local model ready at %s (%s)", self.base_url, Path(self.weights).name
                        )
                    return True
                await asyncio.sleep(1.0)
            self.last_error = f"the local model did not answer within {START_TIMEOUT:.0f}s"
            await self.stop()
            return False

    def _tail(self, lines: int = 4) -> str:
        """The last few lines the server wrote, or a plain 'no output'."""
        path = getattr(self._log, "name", "")
        if not path:
            return "no output"
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                body = handle.read()
        except OSError:
            return "no output"
        kept = [line.strip() for line in body.strip().splitlines() if line.strip()][-lines:]
        return " · ".join(kept)[:400] or "no output"

    def _close_log(self) -> None:
        handle, self._log = self._log, None
        if handle is not None:
            try:
                handle.close()
            except Exception:  # noqa: BLE001 - teardown never raises
                pass

    async def stop(self) -> None:
        process, self._process = self._process, None
        self._ready = False
        self._close_log()
        if process is None:
            return
        try:
            process.terminate()
            for _ in range(20):
                if process.poll() is not None:
                    return
                await asyncio.sleep(0.1)
            process.kill()
        except Exception:  # noqa: BLE001 - teardown never raises
            pass

    async def _healthy(self) -> bool:
        """Cheap liveness check: does ``/models`` answer?"""
        base, headers, _reason = self._target()
        if not base:
            return False
        try:
            response = await request("GET", f"{base}/models", timeout=5.0, headers=headers)
            return response.status == 200
        except (HttpError, Exception):  # noqa: BLE001
            return False

    # ------------------------------------------------------------------
    # AIProvider surface
    # ------------------------------------------------------------------
    def _target(self) -> tuple[str, dict[str, str], str]:
        """``(base_url, headers, reason)`` — re-resolved, so a restart is noticed."""
        if self.base_url:
            return self.base_url, ({"Authorization": f"Bearer {self._token}"} if self.gated and self._token else {}), ""
        if self.directory is None:
            return "", {}, "no local model is configured"
        endpoint = self.directory.resolve("ollama")
        if not getattr(endpoint, "ok", False):
            return "", {}, getattr(endpoint, "note", "") or "the local model is between runs"
        self._token = getattr(endpoint, "token", "")
        self.model = self.model or str(getattr(endpoint, "model", "") or "local")
        # Ollama's OpenAI-compatible surface lives under `/v1`, and the published
        # address is the root of the tunnel. Without this the client asks
        # `/chat/completions` and gets a 404 that looks like "the model is down".
        base = endpoint.base_url
        if not base.rstrip("/").endswith("/v1"):
            base = base.rstrip("/") + "/v1"
        return base, endpoint.headers(), ""

    _token: str = ""

    @property
    def configured(self) -> bool:
        """True when the owner asked for a local model, whether or not it is up.

        Features are enabled at boot, before ``start()`` has finished waiting for
        the endpoint, so "is one configured" has to be answerable without a
        request. Being configured and being ready are different questions and
        this pair keeps them apart.
        """
        if not self.enabled:
            return False
        if self.base_url:
            return True
        if self.directory is None:
            return False
        # Hosted: it counts only when a run has actually published an address
        # that is still fresh. A directory with no published file is not a
        # provider — it is a *place to look*, and saying otherwise would turn a
        # missing service into "the AI is configured", which then fails at the
        # first sentence instead of at the card.
        return bool(getattr(self.directory.resolve("ollama"), "ok", False))

    @property
    def available(self) -> bool:
        """True when a request would actually be answered right now."""
        if self.directory is not None and not self.base_url:
            return bool(getattr(self.directory.resolve("ollama"), "ok", False))
        return bool(self.configured and self._ready)

    @property
    def active_model(self) -> str:
        return self.model or "local"

    def selected_spec(self) -> None:
        """Duck-typing for the `.ai` screen: a local model has no catalogue row."""
        return None

    def describe(self) -> dict[str, object]:
        return {
            "provider": "local",
            "model": self.active_model if self.available else None,
            "ready": self.available,
            "url": self.base_url,
            "weights": Path(self.weights).name if self.weights else "",
            "requests": self.requests,
            "failures": self.failures,
            "last_error": self.last_error,
        }

    async def converse(
        self,
        messages: Any,
        *,
        max_tokens: int = 400,
        temperature: float = 0.85,
    ) -> str:
        base, headers, reason = self._target()
        if not base:
            raise _error(reason or "the local model is not running", "disabled")
        thread = [
            {"role": str(getattr(message, "role", "user")), "content": str(getattr(message, "content", ""))}
            for message in messages
        ]
        body = {
            "model": self.active_model,
            "messages": thread,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        started = time.monotonic()
        try:
            payload = await post_json(
                f"{base}/chat/completions", body, timeout=self.timeout, headers=headers
            )
        except HttpError as exc:
            self.failures += 1
            self.last_error = str(exc)[:200]
            if exc.status == 401:
                raise _error("local model: the gate refused me", "unauthorized") from exc
            kind = "timeout" if exc.kind == "timeout" else "network"
            raise _error(f"local model: {exc}", kind) from exc
        except (TimeoutError, Exception) as exc:  # noqa: BLE001
            self.failures += 1
            self.last_error = str(exc)[:200]
            raise _error(f"local model: {exc}", "network") from exc
        text = _reply_text(payload)
        if not text:
            self.failures += 1
            self.last_error = "the local model answered with nothing"
            raise _error("local model: empty answer", "bad_response")
        self.requests += 1
        self._checked_at = started
        return text

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 300,
        temperature: float = 0.8,
    ) -> str:
        from .ai_provider import AIMessage

        thread = []
        if system:
            thread.append(AIMessage("system", system))
        thread.append(AIMessage("user", prompt))
        return await self.converse(thread, max_tokens=max_tokens, temperature=temperature)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def sentence(self, language: str = "en") -> str:
        """One honest line for a card."""
        name = Path(self.weights).name if self.weights else self.base_url
        if language == "fa":
            if self.available:
                return f"مدل محلی آماده است · {name}"
            return f"مدل محلی خاموش · {self.last_error or 'بدون بار'}"
        if self.available:
            return f"local model ready · {name}"
        return f"local model off · {self.last_error or 'not configured'}"


def _normalise(url: str) -> str:
    text = url.strip().rstrip("/")
    if not text:
        return DEFAULT_URL
    if not text.endswith("/v1") and "/v1" not in text:
        text = f"{text}/v1"
    return text


def _port_of(url: str) -> int:
    try:
        tail = url.split("//", 1)[-1].split("/", 1)[0]
        return int(tail.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return 8080


def _reply_text(payload: Any) -> str:
    """Pull the assistant text out of an OpenAI-shaped response."""
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices") or []
    if not choices:
        return ""
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") or {}
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    text = first.get("text")
    return text.strip() if isinstance(text, str) else ""


def _error(message: str, kind: str) -> Exception:
    """The typed error the router expects, imported lazily to avoid a cycle."""
    from .ai_provider import AIError

    return AIError(message, kind=kind)


__all__ = ["LocalModel", "DEFAULT_URL", "START_TIMEOUT"]
