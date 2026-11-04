"""Experimental bounded, non-pickle tensor protocol for SDXL worker calls.

Only JSON and safetensors are accepted. Never log payloads: conditioning tensors
are derived from a user's prompt. Transport and peer authentication are the
caller's responsibility.
"""
from __future__ import annotations

import base64
import json
import math
from typing import Any

MAX_BODY = 16 * 1024 * 1024
MAX_TENSOR_BYTES = 12 * 1024 * 1024
MAX_TENSORS = 64
MAX_DEPTH = 12


def encode(value: Any) -> bytes:
    import torch
    from safetensors.torch import save

    tensors: dict[str, Any] = {}

    def walk(item: Any, depth: int) -> Any:
        if depth > MAX_DEPTH:
            raise ValueError("structure too deep")
        if isinstance(item, torch.Tensor):
            if len(tensors) >= MAX_TENSORS:
                raise ValueError("too many tensors")
            name = f"t{len(tensors)}"
            tensor = item.detach().to("cpu").contiguous()
            if tensor.numel() * tensor.element_size() > MAX_TENSOR_BYTES:
                raise ValueError("tensor too large")
            tensors[name] = tensor
            return {"$tensor": name}
        if item is None or isinstance(item, (bool, int, str)):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("non-finite number")
            return item
        if isinstance(item, (list, tuple)):
            return [walk(v, depth + 1) for v in item]
        if isinstance(item, dict):
            if any(not isinstance(k, str) or k.startswith("$") for k in item):
                raise ValueError("invalid key")
            return {k: walk(v, depth + 1) for k, v in item.items()}
        raise ValueError(f"unsupported value: {type(item).__name__}")

    structure = walk(value, 0)
    raw = save(tensors) if tensors else b""
    if len(raw) > MAX_TENSOR_BYTES:
        raise ValueError("tensor bundle too large")
    body = json.dumps({"version": 1, "structure": structure, "tensors": base64.b64encode(raw).decode("ascii")}, separators=(",", ":"), allow_nan=False).encode()
    if len(body) > MAX_BODY:
        raise ValueError("message too large")
    return body


def decode(body: bytes) -> Any:
    from safetensors.torch import load

    if len(body) > MAX_BODY:
        raise ValueError("message too large")
    message = json.loads(body)
    if not isinstance(message, dict) or message.get("version") != 1:
        raise ValueError("unsupported message")
    raw = base64.b64decode(message.get("tensors", ""), validate=True)
    if len(raw) > MAX_TENSOR_BYTES:
        raise ValueError("tensor bundle too large")
    tensors = load(raw) if raw else {}
    if len(tensors) > MAX_TENSORS:
        raise ValueError("too many tensors")
    used: set[str] = set()

    def walk(item: Any, depth: int) -> Any:
        if depth > MAX_DEPTH:
            raise ValueError("structure too deep")
        if isinstance(item, dict):
            if set(item) == {"$tensor"}:
                key = item["$tensor"]
                if not isinstance(key, str) or key not in tensors:
                    raise ValueError("missing tensor")
                used.add(key)
                return tensors[key]
            if any(not isinstance(k, str) or k.startswith("$") for k in item):
                raise ValueError("invalid key")
            return {k: walk(v, depth + 1) for k, v in item.items()}
        if isinstance(item, list):
            return [walk(v, depth + 1) for v in item]
        if item is None or isinstance(item, (bool, int, str)):
            return item
        if isinstance(item, float) and math.isfinite(item):
            return item
        raise ValueError("invalid value")

    result = walk(message.get("structure"), 0)
    if used != set(tensors):
        raise ValueError("unexpected tensors")
    return result
