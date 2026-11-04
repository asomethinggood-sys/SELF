"""Experimental ComfyUI model patch for four-way tiled denoising.

This node must be explicitly included in a graph; normal ComfyUI jobs are never
modified. One tile runs locally and three run on authenticated remote workers.
A worker error aborts the image rather than silently mixing an incomplete step.
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
                "optional": {"global_weight": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 1.0, "step": 0.05})}}

    RETURN_TYPES = ("MODEL",)
    FUNCTION = "patch"
    CATEGORY = "experimental/distributed"

    def patch(self, model: Any, overlap: int, global_weight: float = 0.0) -> tuple[Any]:
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

            elapsed: dict[int, float] = {}
            global_prediction: Any = None

            def run_tile(tile: Any, data: Any) -> Any:
                nonlocal global_prediction
                start = time.monotonic()
                try:
                    if tile.index == 0:
                        import torch
                        with torch.inference_mode():
                            tile_prediction = apply_model(data, sigma, **conditions)
                            if global_weight:
                                # The global pass runs alongside remote tiles on
                                # the coordinator, not as another full-size UNet.
                                # It is an approximation, not full attention.
                                import torch.nn.functional as functional
                                scaled = functional.interpolate(x, scale_factor=0.5, mode="bilinear", align_corners=False)
                                coarse = apply_model(scaled, sigma, **conditions)
                                global_prediction = functional.interpolate(coarse, size=x.shape[-2:], mode="bilinear", align_corners=False)
                            return tile_prediction
                    worker = workers[tile.index - 1]
                    return predict(worker["url"], worker["token"], data, sigma, portable)
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
            if global_weight:
                import torch
                if global_prediction is None or global_prediction.shape != x.shape or not torch.isfinite(global_prediction).all():
                    raise ValueError("invalid coarse global prediction")
                result = result.lerp(global_prediction, global_weight)
            print("SDXL_TILE_STATS step_seconds=" + f"{time.monotonic() - start:.2f}" +
                  " tile_seconds=" + ",".join(f"{elapsed[i]:.2f}" for i in range(4)), flush=True)
            return result

        patched.set_model_unet_function_wrapper(wrapper)
        return (patched,)


NODE_CLASS_MAPPINGS = {"FourWorkerSDXL": FourWorkerSDXL}
NODE_DISPLAY_NAME_MAPPINGS = {"FourWorkerSDXL": "Experimental four-worker SDXL"}
