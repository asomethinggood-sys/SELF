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
    image.thumbnail((256, 256))
    buffer = BytesIO()
    image.save(buffer, "JPEG", quality=70, optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    print(f"::notice title=synthetic-sdxl-{name}::{encoded}", flush=True)
