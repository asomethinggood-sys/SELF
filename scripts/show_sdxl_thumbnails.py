#!/usr/bin/env python3
"""Show tiny synthetic test-image previews in check annotations, not user images."""
from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path

from PIL import Image

for name in ("single-baseline", "tiled-overlap-8", "tiled-overlap-16"):
    path = Path(".reports/sdxl-quality") / f"{name}.png"
    image = Image.open(path).convert("RGB")
    image.thumbnail((128, 128))
    for quality in (55, 45, 35, 25, 15):
        buffer = BytesIO()
        image.save(buffer, "JPEG", quality=quality, optimize=True)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        if len(encoded) <= 4000:
            break
    else:
        raise RuntimeError("thumbnail is too large for an annotation")
    print(f"::notice title=synthetic-sdxl-{name}::{encoded}", flush=True)
