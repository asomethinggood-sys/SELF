"""Experimental ComfyUI model patch for four-way tiled denoising.

This node must be explicitly included in a graph; normal ComfyUI jobs are never
modified. One tile runs locally and three run on authenticated remote workers.
A worker error aborts the image by default. An explicitly enabled fallback
reruns the entire step locally; partial tile predictions are never mixed.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from scripts.experimental_tiled_denoise import predict_four

from .client import WorkerError, predict, validate_worker


def _workers() -> list[dict[str, str]]:
    data = json.loads(os.environ.get("SDXL_COORDINATOR_WORKERS", "[]"))
    if not isinstance(data, list) or len(data) != 3:
        raise ValueError("exactly three remote workers are required")
    workers = []
    for row in data:
        if not isinstance(row, dict) or set(row) != {"url", "token"}:
            raise ValueError("invalid worker configuration")
        validate_worker(row["url"], row["token"])
        workers.append({"url": row["url"], "token": row["token"]})
    if len({row["url"] for row in workers}) != 3:
        raise ValueError("worker URLs must be distinct")
    return workers


def _crop_conditioning(conditions: dict[str, Any], top: int, left: int) -> dict[str, Any]:
    """Experiment with SDXL crop metadata; preserve the full-frame size.

    SDXL's ADM vector contains the pooled CLIP embedding followed by six
    256-dimensional size/crop embeddings. This is not positional attention and
    may not improve composition. Never guess how to modify other model types or
    already-cropped inputs: reject them instead.
    """
    import torch
    from comfy.ldm.modules.diffusionmodules.openaimodel import Timestep

    y = conditions.get("y")
    if not isinstance(y, torch.Tensor) or y.ndim != 2 or y.shape[1] != 2816:
        raise ValueError("crop-aware tiling requires a standard SDXL ADM vector")
    embed = Timestep(256)
    zero = embed(torch.tensor([0.0])).to(device=y.device, dtype=y.dtype)
    if not torch.allclose(y[:, 1792:2048], zero.expand(y.shape[0], -1), rtol=1e-4, atol=1e-4):
        raise ValueError("nonzero input crop is not supported")
    if not torch.allclose(y[:, 2048:2304], zero.expand(y.shape[0], -1), rtol=1e-4, atol=1e-4):
        raise ValueError("nonzero input crop is not supported")
    replacement = y.clone()
    replacement[:, 1792:2048] = embed(torch.tensor([float(top * 8)])).to(device=y.device, dtype=y.dtype)
    replacement[:, 2048:2304] = embed(torch.tensor([float(left * 8)])).to(device=y.device, dtype=y.dtype)
    return {**conditions, "y": replacement}


def _portable_conditions(conditions: dict[str, Any]) -> dict[str, Any]:
    """Fail closed for graph features whose semantics we cannot distribute."""
    if set(conditions) - {"c_crossattn", "y", "transformer_options"}:
        raise ValueError("unsupported conditioning for distributed sampler")
    options = conditions.get("transformer_options", {})
    if not isinstance(options, dict):
        raise ValueError("invalid transformer options")
    forbidden = {"patches", "patches_replace", "wrappers", "control"}
    if any(options.get(key) for key in forbidden):
        raise ValueError("model patches or controls are not supported")
    return {
        **{k: v for k, v in conditions.items() if k != "transformer_options"},
        "transformer_options": {k: options[k] for k in ("cond_or_uncond", "sigmas", "sample_sigmas") if k in options},
    }


class FourWorkerSDXL:
    @classmethod
    def INPUT_TYPES(cls) -> dict:
        return {"required": {"model": ("MODEL",), "overlap": ("INT", {"default": 8, "min": 0, "max": 48, "step": 8})},
                "optional": {"global_weight": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 1.0, "step": 0.05}),
                             "global_first_only": ("BOOLEAN", {"default": False}),
                             "global_full_res": ("BOOLEAN", {"default": False}),
                             "spatial_crops": ("BOOLEAN", {"default": False})}}

    RETURN_TYPES = ("MODEL",)
    FUNCTION = "patch"
    CATEGORY = "experimental/distributed"

    def patch(self, model: Any, overlap: int, global_weight: float = 0.0,
              global_first_only: bool = False, global_full_res: bool = False,
              spatial_crops: bool = False) -> tuple[Any]:
        if not 0.0 <= global_weight <= 1.0:
            raise ValueError("global weight must be between zero and one")
        workers = _workers()
        patched = model.clone()
        fallback_enabled = os.environ.get("SDXL_FALLBACK_LOCAL", "").lower() in {"1", "true", "yes"}
        degraded = False

        def wrapper(apply_model: Any, args: dict[str, Any]) -> Any:
            nonlocal degraded
            x, sigma, conditions = args["input"], args["timestep"], args["c"]
            if degraded:
                import torch
                with torch.inference_mode():
                    return apply_model(x, sigma, **conditions)
            portable = _portable_conditions(conditions)
            active_global = global_weight
            if global_weight and global_first_only:
                import torch
                samples = conditions.get("transformer_options", {}).get("sample_sigmas")
                if not isinstance(samples, torch.Tensor) or not samples.numel():
                    raise ValueError("first-step global guidance requires the sampler sigma schedule")
                if not torch.isclose(sigma.flatten()[0], samples.flatten()[0].to(sigma), rtol=0.0001):
                    active_global = 0.0

            if active_global == 1.0 and global_full_res:
                # The exact full-frame pass entirely determines this step. Do
                # not waste three network/model calls computing tiles whose
                # mathematical contribution would be zero.
                import torch
                start = time.monotonic()
                with torch.inference_mode():
                    result = apply_model(x, sigma, **conditions)
                print(f"SDXL_GLOBAL_FULL step_seconds={time.monotonic() - start:.2f}", flush=True)
                return result

            elapsed: dict[int, float] = {}
            global_prediction: Any = None

            def run_tile(tile: Any, data: Any) -> Any:
                nonlocal global_prediction
                start = time.monotonic()
                try:
                    tile_conditions = _crop_conditioning(conditions, tile.top, tile.left) if spatial_crops else conditions
                    if tile.index == 0:
                        import torch
                        with torch.inference_mode():
                            tile_prediction = apply_model(data, sigma, **tile_conditions)
                            if active_global:
                                if global_full_res:
                                    # One full UNet pass retains global attention,
                                    # at extra cost. Remote tile work still runs
                                    # concurrently and contributes to the blend.
                                    global_prediction = apply_model(x, sigma, **conditions)
                                else:
                                    # Cheaper coarse approximation, not full attention.
                                    import torch.nn.functional as functional
                                    scaled = functional.interpolate(x, scale_factor=0.5, mode="bilinear", align_corners=False)
                                    coarse = apply_model(scaled, sigma, **conditions)
                                    global_prediction = functional.interpolate(coarse, size=x.shape[-2:], mode="bilinear", align_corners=False)
                            return tile_prediction
                    worker = workers[tile.index - 1]
                    tile_portable = _portable_conditions(tile_conditions) if spatial_crops else portable
                    return predict(worker["url"], worker["token"], data, sigma, tile_portable)
                finally:
                    elapsed[tile.index] = time.monotonic() - start

            start = time.monotonic()
            try:
                result = predict_four(x, run_tile, overlap=overlap)
            except (WorkerError, ValueError):
                if not fallback_enabled:
                    raise
                # Never use partial predictions. Finish this and subsequent
                # steps on the coordinator until this patched model is rebuilt.
                degraded = True
                print("SDXL_DEGRADED switching to full-frame local inference", flush=True)
                import torch
                with torch.inference_mode():
                    return apply_model(x, sigma, **conditions)
            if active_global:
                import torch
                if global_prediction is None or global_prediction.shape != x.shape or not torch.isfinite(global_prediction).all():
                    raise ValueError("invalid coarse global prediction")
                result = result.lerp(global_prediction, active_global)
            print("SDXL_TILE_STATS step_seconds=" + f"{time.monotonic() - start:.2f}" +
                  " tile_seconds=" + ",".join(f"{elapsed[i]:.2f}" for i in range(4)), flush=True)
            return result

        patched.set_model_unet_function_wrapper(wrapper)
        return (patched,)


NODE_CLASS_MAPPINGS = {"FourWorkerSDXL": FourWorkerSDXL}
NODE_DISPLAY_NAME_MAPPINGS = {"FourWorkerSDXL": "Experimental four-worker SDXL"}
