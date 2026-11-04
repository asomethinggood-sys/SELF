"""Opt-in timing of real ComfyUI attention operations; never logs tensors or prompts."""
from __future__ import annotations

import json
import time
from typing import Any


class ProfileSDXLAttention:
    @classmethod
    def INPUT_TYPES(cls) -> dict:
        return {"required": {"model": ("MODEL",)}}

    RETURN_TYPES = ("MODEL",)
    FUNCTION = "patch"
    CATEGORY = "experimental/profiling"

    def patch(self, model: Any) -> tuple[Any]:
        patched = model.clone()
        grouped: dict[tuple, list[float]] = {}

        def attention(func: Any, q: Any, k: Any, v: Any, heads: int, *extra: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            result = func(q, k, v, heads, *extra, **kwargs)
            elapsed = time.perf_counter() - start
            key = (tuple(q.shape), tuple(k.shape), heads, str(q.dtype))
            grouped.setdefault(key, []).append(elapsed)
            return result

        patched.model_options.setdefault("transformer_options", {})["optimized_attention_override"] = attention

        def wrapper(apply_model: Any, args: dict[str, Any]) -> Any:
            grouped.clear()
            start = time.perf_counter()
            result = apply_model(args["input"], args["timestep"], **args["c"])
            rows = [{"q": q, "k": k, "heads": heads, "dtype": dtype, "calls": len(times),
                     "seconds": round(sum(times), 4)}
                    for (q, k, heads, dtype), times in grouped.items()]
            print("SDXL_ATTENTION_PROFILE " + json.dumps({"unet_seconds": round(time.perf_counter() - start, 4),
                  "shapes": rows}, separators=(",", ":")), flush=True)
            return result

        patched.set_model_unet_function_wrapper(wrapper)
        return (patched,)


NODE_CLASS_MAPPINGS = {"ProfileSDXLAttention": ProfileSDXLAttention}
NODE_DISPLAY_NAME_MAPPINGS = {"ProfileSDXLAttention": "Experimental SDXL attention profiling"}
