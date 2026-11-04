#!/usr/bin/env python3
"""Small synthetic CPU convolution comparison; not an image latency claim."""
from __future__ import annotations

import statistics
import time
from pathlib import Path

import torch
import torch.nn.functional as functional


def main() -> None:
    torch.set_num_threads(4)
    flags = Path("/proc/cpuinfo").read_text(errors="replace").lower()
    torch.manual_seed(1234)
    x = torch.randn(1, 320, 48, 48)
    weight = torch.randn(320, 320, 3, 3) * 0.01
    xb, wb = x.to(torch.bfloat16), weight.to(torch.bfloat16)
    with torch.inference_mode():
        reference = functional.conv2d(x, weight, padding=1)
        reduced = functional.conv2d(xb, wb, padding=1).float()
        fp32, bf16 = [], []
        for _ in range(4):
            start = time.perf_counter()
            functional.conv2d(x, weight, padding=1)
            fp32.append(time.perf_counter() - start)
            start = time.perf_counter()
            functional.conv2d(xb, wb, padding=1)
            bf16.append(time.perf_counter() - start)
    error = (reference - reduced).abs().mean().item()
    print(f"::notice::Synthetic 320-channel 48x48 conv: FP32 median {statistics.median(fp32):.4f}s; BF16 median {statistics.median(bf16):.4f}s; mean absolute error {error:.6f}; avx512_bf16={'avx512_bf16' in flags}; amx_bf16={'amx_bf16' in flags}. Not a full-model speed test.", flush=True)


if __name__ == "__main__":
    main()
