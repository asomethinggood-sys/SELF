#!/usr/bin/env python3
"""Show tiny synthetic test-image previews in check annotations, not user images."""
from __future__ import annotations

import base64
import os
from io import BytesIO
from pathlib import Path

from PIL import Image

names = ("single-baseline", "tiled-overlap-8", "tiled-overlap-16", "tiled-global-035", "tiled-global-first")
selected = os.environ.get("SDXL_PREVIEW", "")
if selected not in names:
    raise SystemExit("invalid preview selection")
for name in (selected,):
    path = Path(".reports/sdxl-quality") / f"{name}.png"
    image = Image.open(path).convert("RGB")
    image.thumbnail((256, 256))
    buffer = BytesIO()
    image.save(buffer, "JPEG", quality=70, optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    for number, start in enumerate(range(0, len(encoded), 3900)):
        print(f"::notice title=synthetic-sdxl-{name}-part-{number:02d}::{encoded[start:start + 3900]}", flush=True)
