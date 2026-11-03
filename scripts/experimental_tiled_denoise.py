"""Experimental four-way *approximate* latent tiling; not wired into .draw.

A diffusion scheduler must stay on the coordinator. At each denoising step it
sends overlapping slices of the same latent and the same timestep/conditioning
to four workers, blends their noise predictions, and advances the scheduler once.
This module implements only that slice/dispatch/blend operation. Worker transport,
model loading, authorization and generation orchestration are deliberately not
included. A tile's UNet attention sees less context than a full-image UNet, so
this is an approximation, not numerically equivalent to unpartitioned inference.
"""
from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Tile:
    index: int
    top: int
    bottom: int
    left: int
    right: int


def four_tiles(height: int, width: int, overlap: int) -> tuple[Tile, ...]:
    """Make four overlapping latent-space rectangles; never leave a gap."""
    if height < 16 or width < 16 or height % 16 or width % 16:
        raise ValueError("latent dimensions must be positive multiples of 16")
    if overlap < 0 or overlap % 8 or overlap >= min(height, width) // 2:
        raise ValueError("overlap must be a multiple of 8 and less than half a dimension")
    mid_y, mid_x = height // 2, width // 2
    ys = ((0, mid_y + overlap), (mid_y - overlap, height))
    xs = ((0, mid_x + overlap), (mid_x - overlap, width))
    return tuple(Tile(row * 2 + col, top, bottom, left, right)
                 for row, (top, bottom) in enumerate(ys)
                 for col, (left, right) in enumerate(xs))


def predict_four(
    latent: Any, predict: Callable[[Tile, Any], Any], *, overlap: int = 8
) -> Any:
    """Call four predictors concurrently, then feather-blend their results.

    ``latent`` is a CPU torch tensor [batch, channels, height, width]. ``predict``
    must preserve tile shape and dtype and must arrange bounded/authenticated
    transport itself. Any worker failure aborts the step; partial data is never
    used to advance the scheduler. This is not a complete image generator.
    """
    import torch

    if not isinstance(latent, torch.Tensor) or latent.ndim != 4 or latent.device.type != "cpu":
        raise ValueError("expected a four-dimensional CPU torch tensor")
    height, width = latent.shape[-2:]
    tiles = four_tiles(height, width, overlap)
    outputs: dict[int, Any] = {}
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="tile-worker") as pool:
        futures = {
            pool.submit(predict, tile, latent[..., tile.top:tile.bottom, tile.left:tile.right].clone()): tile
            for tile in tiles
        }
        for future in as_completed(futures):
            tile = futures[future]
            output = future.result()
            expected = (*latent.shape[:-2], tile.bottom - tile.top, tile.right - tile.left)
            if not isinstance(output, torch.Tensor) or tuple(output.shape) != expected:
                raise ValueError(f"worker {tile.index} returned an invalid shape")
            if output.device.type != "cpu" or output.dtype != latent.dtype or not torch.isfinite(output).all():
                raise ValueError(f"worker {tile.index} returned invalid values or dtype")
            outputs[tile.index] = output
    total = torch.zeros_like(latent)
    weights = torch.zeros((height, width), dtype=latent.dtype)
    span = overlap * 2
    for tile in tiles:
        h, w = tile.bottom - tile.top, tile.right - tile.left
        wy = torch.ones(h, dtype=latent.dtype)
        wx = torch.ones(w, dtype=latent.dtype)
        if span:
            ramp = torch.linspace(0, 1, span, dtype=latent.dtype)
            if tile.top:
                wy[:span] = ramp
            if tile.bottom < height:
                wy[-span:] = 1 - ramp
            if tile.left:
                wx[:span] = ramp
            if tile.right < width:
                wx[-span:] = 1 - ramp
        weight = wy[:, None] * wx[None, :]
        total[..., tile.top:tile.bottom, tile.left:tile.right] += outputs[tile.index] * weight
        weights[tile.top:tile.bottom, tile.left:tile.right] += weight
    if torch.any(weights <= 0):
        raise RuntimeError("tile blend left uncovered pixels")
    return total / weights
