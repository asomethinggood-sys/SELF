"""ComfyUI extension for experimental private, bounded tile inference.

This route is disabled unless an explicit random per-run token is provided.
No committed default and no public mode. A coordinator must authenticate over
HTTPS; this module never publishes a URL or token. It is not a production node.
"""
from __future__ import annotations

import asyncio
import hmac
import os
from typing import Any

from aiohttp import web

from . import wire

_model: Any = None
_lock: asyncio.Lock | None = None


def _load_model() -> Any:
    import comfy.model_management
    import comfy.sd
    import folder_paths
    import torch

    filename = os.environ.get("SDXL_WORKER_CHECKPOINT", "DreamShaperXL_Lightning.safetensors")
    if filename != os.path.basename(filename):
        raise ValueError("checkpoint must be a filename")
    path = folder_paths.get_full_path_or_raise("checkpoints", filename)
    patcher = comfy.sd.load_checkpoint_guess_config(
        path, output_vae=False, output_clip=False, output_clipvision=False,
    )[0]
    if patcher is None:
        raise RuntimeError("checkpoint has no diffusion model")
    comfy.model_management.load_model_gpu(patcher)
    torch.set_num_threads(max(1, min(4, int(os.environ.get("SDXL_WORKER_THREADS", "4")))))
    return patcher


def _run_inference(payload: Any) -> Any:
    import torch

    global _model
    if not isinstance(payload, dict) or set(payload) != {"latent", "sigma", "conditioning"}:
        raise ValueError("invalid payload")
    x, sigma, cond = payload["latent"], payload["sigma"], payload["conditioning"]
    if not isinstance(x, torch.Tensor) or x.ndim != 4 or x.shape[0] > 2 or x.shape[1] != 4:
        raise ValueError("invalid latent")
    if not 8 <= x.shape[2] <= 160 or not 8 <= x.shape[3] <= 160:
        raise ValueError("invalid latent size")
    if not isinstance(sigma, torch.Tensor) or sigma.numel() > 2 or not isinstance(cond, dict):
        raise ValueError("invalid conditioning")
    if set(cond) - {"c_crossattn", "y", "transformer_options"}:
        raise ValueError("unsupported conditioning")
    opts = cond.get("transformer_options", {})
    if not isinstance(opts, dict) or set(opts) - {"cond_or_uncond", "sigmas", "sample_sigmas"}:
        raise ValueError("unsupported transformer options")
    if not torch.isfinite(x).all() or not torch.isfinite(sigma).all():
        raise ValueError("non-finite latent")
    if _model is None:
        _model = _load_model()
    with torch.inference_mode():
        output = _model.model.apply_model(x, sigma, **cond)
    if output.shape != x.shape or not torch.isfinite(output).all():
        raise ValueError("invalid prediction")
    return {"prediction": output.to("cpu")}


def register_routes() -> None:
    token = os.environ.get("SDXL_WORKER_TOKEN", "")
    if len(token) < 32 or len(token) > 256:
        return
    from server import PromptServer
    if PromptServer.instance is None:
        raise RuntimeError("ComfyUI server not ready")
    global _lock
    _lock = asyncio.Lock()

    @PromptServer.instance.routes.post("/sdxl-worker/warm")
    async def warm(request: web.Request) -> web.Response:
        global _model
        if not hmac.compare_digest(request.headers.get("Authorization", ""), f"Bearer {token}"):
            return web.Response(status=401, text="unauthorized")
        try:
            async with _lock:
                if _model is None:
                    _model = await asyncio.to_thread(_load_model)
            return web.Response(text="ready")
        except Exception:  # noqa: BLE001 - never expose server internals
            return web.Response(status=500, text="worker failed")

    @PromptServer.instance.routes.post("/sdxl-worker/predict")
    async def predict(request: web.Request) -> web.Response:
        offered = request.headers.get("Authorization", "")
        if not hmac.compare_digest(offered, f"Bearer {token}"):
            return web.Response(status=401, text="unauthorized")
        if request.content_length is not None and request.content_length > wire.MAX_BODY:
            return web.Response(status=413, text="too large")
        chunks = bytearray()
        async for chunk in request.content.iter_chunked(65536):
            chunks.extend(chunk)
            if len(chunks) > wire.MAX_BODY:
                return web.Response(status=413, text="too large")
        body = bytes(chunks)
        try:
            payload = wire.decode(body)
            async with _lock:
                result = await asyncio.to_thread(_run_inference, payload)
            return web.Response(body=wire.encode(result), content_type="application/json")
        except (ValueError, TypeError):
            return web.Response(status=400, text="invalid request")
        except Exception:  # noqa: BLE001 - never reveal model or request details to clients
            return web.Response(status=500, text="worker failed")
