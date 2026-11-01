"""Pictures she draws herself, on a server he runs.

ComfyUI is a graph runner with a small HTTP API: you POST a workflow by node id,
watch ``/history/<prompt_id>`` until it has an output, then fetch the file from
``/view``. It has no key, no per-image bill and no content filter to argue with.
The cost is that it needs a machine with a GPU to be pleasant, which is exactly
why it belongs next to the bot rather than inside the request path.

Two shapes, because ComfyUI installs differ:

* **A workflow file** (``COMFY_WORKFLOW``). The JSON exported from the ComfyUI
  UI, with a ``__PROMPT__`` placeholder somewhere in it — usually inside a
  ``CLIPTextEncode`` node. This is the honest path: the owner's model, sampler
  and resolution are whatever they set up.
* **Nothing but a URL** (``COMFY_URL``). A minimal built-in graph: checkpoint
  name from ``COMFY_CKPT``, 512×512, 20 steps, one image. It works on a fresh
  ``ComfyUI`` checkout with a single model in ``models/checkpoints``.

Two ways to find the server:

* ``COMFY_URL`` — your own machine, your own port. Ungated, unchanged.
* **the service directory** — when ``COMFY_URL`` is unset, the address comes from
  ``.services/comfy.json``, published by the ``service-comfy`` workflow on every
  restart, and every request carries the bearer token the gate in front of it
  wants. That is how a picture survives the runner that drew the last one: the
  URL moves, the bot reads it, nobody reconfigures anything.

When a hosted service is between runs, ``available`` is false and ``.draw`` says
"between runs, last seen 40 minutes ago" — a sentence, not a traceback.

Either way the result is a PNG on disk; :mod:`havoc.features.art` is what sends
it. When nothing is configured, ``available`` is false and ``.draw`` explains
what to run instead of pretending to think about it.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .http import HttpError, get_bytes, post_json, request

DEFAULT_URL = "http://127.0.0.1:8188"
#: A 512×512, 20-step image on a mid-range GPU takes 3-10 seconds; a CPU-only
#: runner takes minutes. The ceiling is the owner's patience for a command.
DEFAULT_TIMEOUT = 180.0
POLL_INTERVAL = 1.5


class ImageError(Exception):
    """A picture that could not be made, with a sentence for the owner."""

    def __init__(self, message: str, *, kind: str = "failed") -> None:
        self.kind = kind
        super().__init__(message)


@dataclass
class ImageGenerator:
    """One ComfyUI instance, one workflow, one picture at a time."""

    url: str = ""
    workflow_path: str = ""
    checkpoint: str = "v1-5-pruned-emaonly.safetensors"
    width: int = 512
    height: int = 512
    steps: int = 20
    timeout: float = DEFAULT_TIMEOUT
    negative: str = "text, watermark, signature, blurry, low quality"
    logger: Any = None
    #: The directory to ask when ``url`` is empty; ``None`` means no hosting.
    directory: Any = None
    made: int = 0
    failures: int = 0
    last_error: str = ""
    _workflow: dict[str, Any] | None = field(default=None, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    name = "comfyui"

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls, *, logger: Any = None, directory: Any = None) -> ImageGenerator | None:
        url = (os.environ.get("COMFY_URL") or "").strip()
        workflow = (os.environ.get("COMFY_WORKFLOW") or "").strip()
        if not url and directory is None:
            return None
        if not url:
            # Hosted: the object exists even while the service is down, so the
            # command can explain the state instead of claiming nothing is set up.
            return cls(
                url="",
                workflow_path=workflow,
                checkpoint=(os.environ.get("COMFY_CKPT") or "v1-5-pruned-emaonly.safetensors").strip(),
                width=int(os.environ.get("COMFY_WIDTH") or 512),
                height=int(os.environ.get("COMFY_HEIGHT") or 512),
                steps=int(os.environ.get("COMFY_STEPS") or 8),
                timeout=float(os.environ.get("COMFY_TIMEOUT") or DEFAULT_TIMEOUT),
                negative=(os.environ.get("COMFY_NEGATIVE") or "").strip()
                or "text, watermark, signature, blurry, low quality",
                logger=logger,
                directory=directory,
            )
        return cls(
            url=url.rstrip("/") or DEFAULT_URL,
            workflow_path=workflow,
            checkpoint=(os.environ.get("COMFY_CKPT") or "v1-5-pruned-emaonly.safetensors").strip(),
            width=int(os.environ.get("COMFY_WIDTH") or 512),
            height=int(os.environ.get("COMFY_HEIGHT") or 512),
            steps=int(os.environ.get("COMFY_STEPS") or 20),
            timeout=float(os.environ.get("COMFY_TIMEOUT") or DEFAULT_TIMEOUT),
            negative=(os.environ.get("COMFY_NEGATIVE") or "").strip()
            or "text, watermark, signature, blurry, low quality",
            logger=logger,
        )

    def _target(self) -> tuple[str, dict[str, str], str]:
        """``(base_url, headers, reason)`` — resolved per call, so restarts are seen."""
        if self.url:
            return self.url, {}, ""
        if self.directory is None:
            return "", {}, "no image server is configured"
        endpoint = self.directory.resolve("comfy")
        if not getattr(endpoint, "ok", False):
            return "", {}, getattr(endpoint, "note", "") or "the picture server is between runs"
        return endpoint.base_url, endpoint.headers(), ""

    @property
    def available(self) -> bool:
        if self.url:
            return True
        if self.directory is not None:
            return bool(getattr(self.directory.resolve("comfy"), "ok", False))
        return False

    #: Steps used when the owner did not ask for a path. A CPU render at 20 steps
    #: is 1-3 minutes; at 8 it is 30-50 seconds, which is the difference between
    #: a command someone uses and one they stop using.
    FAST_STEPS = 8
    QUALITY_STEPS = 20

    def describe(self) -> dict[str, Any]:
        return {
            "provider": "comfyui" if self.available else "none",
            "url": self.url,
            "workflow": Path(self.workflow_path).name if self.workflow_path else "built-in",
            "checkpoint": self.checkpoint if not self.workflow_path else "",
            "made": self.made,
            "failures": self.failures,
            "last_error": self.last_error,
        }

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------
    def _graph(
        self, prompt: str, *, seed: int | None = None, steps: int | None = None
    ) -> tuple[dict[str, Any], str, str]:
        """``(graph, positive_id, filename_prefix_node)`` for this prompt."""
        seed = int(seed if seed is not None else random.randint(0, 2**31 - 1))
        if self.workflow_path:
            graph = self._load_workflow()
            if graph is not None:
                filled = _fill(graph, prompt, self.negative, seed)
                return filled, "", ""
        return self._builtin(prompt, seed, steps=steps)

    def _load_workflow(self) -> dict[str, Any] | None:
        if self._workflow is not None:
            return self._workflow
        path = Path(self.workflow_path)
        if not path.exists():
            self.last_error = f"the workflow file is missing: {path.name}"
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.last_error = f"the workflow file could not be read: {exc}"
            return None
        # ComfyUI exports either the graph itself or ``{"prompt": {...}}``.
        if isinstance(data, dict) and isinstance(data.get("prompt"), dict):
            data = data["prompt"]
        if not isinstance(data, dict):
            self.last_error = "the workflow file is not a ComfyUI graph"
            return None
        self._workflow = data
        return data

    def _builtin(self, prompt: str, seed: int, *, steps: int | None = None) -> tuple[dict[str, Any], str, str]:
        """A small, working text-to-image graph.

        Node ids are the ones ComfyUI's own examples use (``3`` checkpoint,
        ``6`` positive, ``7`` negative, ``5`` latent, ``3`` sampler...) so a
        workflow the owner later exports lines up with what they see here.
        """
        graph = {
            "3": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": seed,
                    "steps": int(steps if steps is not None else self.steps),
                    "cfg": 7.0,
                    "sampler_name": "euler",
                    "scheduler": "normal",
                    "denoise": 1.0,
                    "model": ["4", 0],
                    "positive": ["6", 0],
                    "negative": ["7", 0],
                    "latent_image": ["5", 0],
                },
            },
            "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": self.checkpoint}},
            "5": {
                "class_type": "EmptyLatentImage",
                "inputs": {"width": self.width, "height": self.height, "batch_size": 1},
            },
            "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["4", 1]}},
            "7": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": self.negative, "clip": ["4", 1]},
            },
            "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
            "9": {
                "class_type": "SaveImage",
                "inputs": {"filename_prefix": "havoc", "images": ["8", 0]},
            },
        }
        return graph, "6", "9"

    # ------------------------------------------------------------------
    # Generate
    # ------------------------------------------------------------------
    async def generate(
        self,
        prompt: str,
        *,
        outdir: Path | None = None,
        seed: int | None = None,
        quality: bool = False,
    ) -> Path:
        """Queue one picture, wait for it, save it, return the file."""
        text = " ".join(str(prompt or "").split())
        if not text:
            raise ImageError("there is nothing to draw", kind="empty")
        base, headers, reason = self._target()
        if not base:
            raise ImageError(reason or "no image server is configured", kind="disabled")
        graph, _positive, _save = self._graph(
            text, seed=seed, steps=self.QUALITY_STEPS if quality else self.FAST_STEPS
        )
        started = time.monotonic()
        try:
            payload = await post_json(f"{base}/prompt", {"prompt": graph}, timeout=30.0, headers=headers)
        except HttpError as exc:
            self.failures += 1
            self.last_error = str(exc)[:200]
            hint = ""
            if exc.status == 401:
                raise ImageError(
                    "the gate refused me — the shared secret does not match this run",
                    kind="unauthorized",
                ) from exc
            if exc.status == 400:
                hint = " (the checkpoint or a node is missing on the server)"
            raise ImageError(f"the image server refused the job: {exc}{hint}", kind="refused") from exc
        prompt_id = str((payload or {}).get("prompt_id") or "") if isinstance(payload, dict) else ""
        if not prompt_id:
            self.failures += 1
            raise ImageError("the image server did not return a job id", kind="bad_response")
        image = await self._wait(prompt_id, base=base, headers=headers, timeout=self.timeout)
        data = await self._download(image, base=base, headers=headers)
        folder = Path(outdir) if outdir is not None else Path(os.environ.get("TMPDIR", "/tmp"))
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"havoc-draw-{int(time.time())}-{random.randint(100, 999)}.png"
        path.write_bytes(data)
        self.made += 1
        if self.logger is not None:
            self.logger.info("comfyui: drew a %dx%d image in %.1fs", self.width, self.height, time.monotonic() - started)
        return path

    async def _wait(
        self, prompt_id: str, *, base: str, headers: dict[str, str], timeout: float
    ) -> dict[str, Any]:
        """Poll ``/history`` until the job has an output (or clearly failed)."""
        deadline = time.monotonic() + max(5.0, timeout)
        while time.monotonic() < deadline:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                payload = await request("GET", f"{base}/history/{prompt_id}", timeout=15.0, headers=headers)
            except HttpError as exc:
                # A 404 here just means "not finished yet" on some builds.
                if exc.status == 404:
                    continue
                self.failures += 1
                raise ImageError(f"the image server stopped answering: {exc}", kind="network") from exc
            except Exception as exc:  # noqa: BLE001
                self.failures += 1
                raise ImageError(f"the image server stopped answering: {exc}", kind="network") from exc
            try:
                history = payload.json()
            except HttpError:
                continue
            entry = (history or {}).get(prompt_id) if isinstance(history, dict) else None
            if not isinstance(entry, dict):
                continue
            status = entry.get("status") or {}
            if str(status.get("status_str") or "") == "error":
                self.failures += 1
                raise ImageError("the workflow failed on the server (a missing model?)", kind="node_error")
            images = _images_of(entry)
            if images:
                return images[0]
        self.failures += 1
        raise ImageError(
            f"the image took longer than {int(timeout)}s — a bigger GPU, or fewer steps",
            kind="timeout",
        )

    async def _download(self, image: dict[str, Any], *, base: str, headers: dict[str, str]) -> bytes:
        query = urllib.parse.urlencode(
            {
                "filename": image.get("filename", ""),
                "subfolder": image.get("subfolder", ""),
                "type": image.get("type", "output"),
            }
        )
        try:
            return await get_bytes(f"{base}/view?{query}", timeout=60.0, headers=headers)
        except HttpError as exc:
            self.failures += 1
            raise ImageError(f"the picture was made but could not be fetched: {exc}", kind="network") from exc


def _images_of(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Every image node output in one history entry, in node order."""
    outputs = entry.get("outputs") or {}
    found: list[dict[str, Any]] = []
    if isinstance(outputs, dict):
        for node in outputs.values():
            images = (node or {}).get("images") if isinstance(node, dict) else None
            if isinstance(images, list):
                found.extend(item for item in images if isinstance(item, dict) and item.get("filename"))
    return found


def _fill(graph: dict[str, Any], prompt: str, negative: str, seed: int) -> dict[str, Any]:
    """Put the words into an exported workflow, without touching anything else.

    ``__PROMPT__`` and ``__NEGATIVE__`` are the placeholders; a graph without
    them gets the prompt written into the first positive ``CLIPTextEncode`` and
    the negative into the second, which is what the export looks like in
    practice. Seeds are replaced wherever a node has one.
    """
    filled: dict[str, Any] = {}
    positives = 0
    for node_id, node in graph.items():
        copy = json.loads(json.dumps(node)) if isinstance(node, dict) else node
        if isinstance(copy, dict):
            inputs = copy.get("inputs")
            if isinstance(inputs, dict):
                for key, value in list(inputs.items()):
                    if isinstance(value, str):
                        if "__PROMPT__" in value:
                            inputs[key] = value.replace("__PROMPT__", prompt)
                        elif "__NEGATIVE__" in value:
                            inputs[key] = value.replace("__NEGATIVE__", negative)
                        elif copy.get("class_type") == "CLIPTextEncode" and key == "text":
                            if positives == 0:
                                inputs[key] = prompt
                            elif positives == 1:
                                inputs[key] = negative
                            positives += 1
                    elif key in {"seed", "noise_seed"} and isinstance(value, int):
                        inputs[key] = seed
        filled[str(node_id)] = copy
    return filled


def from_env(*, logger: Any = None, directory: Any = None) -> ImageGenerator | None:
    return ImageGenerator.from_env(logger=logger, directory=directory)


__all__ = ["DEFAULT_URL", "ImageError", "ImageGenerator", "from_env"]
