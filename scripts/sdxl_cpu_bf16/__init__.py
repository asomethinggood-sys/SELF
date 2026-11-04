"""Opt-in BF16 CPU experiment for runners with native BF16 instructions.

ComfyUI deliberately disables BF16 CPU compute by default. This experimental
extension changes its capability check only when explicitly enabled, and only
for CPUs advertising hardware BF16. It is never installed on the live service.
"""
from __future__ import annotations

import os
from pathlib import Path

NODE_CLASS_MAPPINGS = {}

if os.environ.get("SDXL_ALLOW_CPU_BF16") == "1":
    import comfy.model_management as management

    flags = Path("/proc/cpuinfo").read_text(errors="replace").lower()
    if "avx512_bf16" not in flags and "amx_bf16" not in flags:
        raise RuntimeError("native CPU BF16 is required for this experiment")
    previous = management.should_use_bf16

    def should_use_bf16(device=None, *args, **kwargs):
        if device is not None and management.is_device_cpu(device):
            return True
        return previous(device, *args, **kwargs)

    management.should_use_bf16 = should_use_bf16
