#!/usr/bin/env python3
"""Expose a small synthetic precision-test image via check annotations."""
from __future__ import annotations

import base64
import os
from io import BytesIO
from pathlib import Path

from PIL import Image

name = os.environ.get("SDXL_PREVIEW", "")
if name not in {"fp32", "bf16"}:
    raise SystemExit("invalid precision preview")
image = Image.open(Path(".reports/sdxl-bf16") / f"{name}.png").convert("RGB")
image.thumbnail((256, 256))
buffer = BytesIO()
image.save(buffer, "JPEG", quality=70, optimize=True)
encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
for index, start in enumerate(range(0, len(encoded), 3900)):
    print(f"::notice title=precision-{name}-{index:02d}::{encoded[start:start + 3900]}", flush=True)
