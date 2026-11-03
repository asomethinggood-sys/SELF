#!/usr/bin/env python3
"""Exercise the experimental four-worker blend with real torch tensors."""
from __future__ import annotations

import threading

import torch
from experimental_tiled_denoise import four_tiles, predict_four


def main() -> None:
    torch.set_num_threads(1)
    image = torch.arange(4 * 64 * 64, dtype=torch.float32).reshape(1, 4, 64, 64)
    barrier = threading.Barrier(4, timeout=10)
    seen: set[int] = set()
    lock = threading.Lock()

    def identity(tile, data):
        with lock:
            seen.add(tile.index)
        barrier.wait()
        return data

    result = predict_four(image, identity, overlap=8)
    if seen != {0, 1, 2, 3} or not torch.allclose(result, image, atol=0.002, rtol=1e-6):
        raise AssertionError("concurrent identity tiles did not reconstruct the latent")
    try:
        four_tiles(64, 64, 32)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid overlap accepted")

    def fail(tile, data):
        if tile.index == 2:
            raise RuntimeError("simulated worker failure")
        return data

    try:
        predict_four(image, fail)
    except RuntimeError as exc:
        if str(exc) != "simulated worker failure":
            raise
    else:
        raise AssertionError("worker failure did not abort the step")
    print("::notice::Four-way local torch tile dispatch, overlap reconstruction and failure propagation passed")


if __name__ == "__main__":
    main()
