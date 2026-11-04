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

from .client import predict, validate_worker


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
        return {"required": {"model": ("MODEL",), "overlap": ("INT", {"default": 8, "min": 0, "max": 48, "step": 8})}}

    RETURN_TYPES = ("MODEL",)
    FUNCTION = "patch"
    CATEGORY = "experimental/distributed"

    def patch(self, model: Any, overlap: int) -> tuple[Any]:
        workers = _workers()
        patched = model.clone()

        def wrapper(apply_model: Any, args: dict[str, Any]) -> Any:
            x, sigma, conditions = args["input"], args["timestep"], args["c"]
            portable = _portable_conditions(conditions)

            elapsed: dict[int, float] = {}

            def run_tile(tile: Any, data: Any) -> Any:
                start = time.monotonic()
                try:
                    if tile.index == 0:
                        import torch
                        with torch.inference_mode():
                            return apply_model(data, sigma, **conditions)
                    worker = workers[tile.index - 1]
                    return predict(worker["url"], worker["token"], data, sigma, portable)
                finally:
                    elapsed[tile.index] = time.monotonic() - start

            start = time.monotonic()
            result = predict_four(x, run_tile, overlap=overlap)
            print("SDXL_TILE_STATS step_seconds=" + f"{time.monotonic() - start:.2f}" +
                  " tile_seconds=" + ",".join(f"{elapsed[i]:.2f}" for i in range(4)), flush=True)
            return result

        patched.set_model_unet_function_wrapper(wrapper)
        return (patched,)


NODE_CLASS_MAPPINGS = {"FourWorkerSDXL": FourWorkerSDXL}
NODE_DISPLAY_NAME_MAPPINGS = {"FourWorkerSDXL": "Experimental four-worker SDXL"}
