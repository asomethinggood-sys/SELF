#!/usr/bin/env python3
"""Profile attention in a real SDXL render, isolated from production and private prompts."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("GITHUB_TOKEN", "local-profile-no-github-token")
os.environ.setdefault("WORKER_INDEX", "0")
from havoc.services.imagegen import ImageGenerator  # noqa: E402
from scripts.sdxl_cluster_test import http, server, stop  # noqa: E402


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="sdxl-attention-profile-") as directory:
        temp = Path(directory)
        child = None
        output = None
        try:
            child, output = server(dict(os.environ), temp / "comfy.log")
            generator = ImageGenerator(checkpoint="DreamShaperXL_Lightning.safetensors", width=768, height=768)

            def render(profile: bool, sampler_id: str) -> tuple[float, bytes]:
                graph, _, _ = generator._builtin("a lighthouse on a rocky coast at sunrise", 1234, steps=4)
                graph["3"]["inputs"].update({"cfg": 1.0, "scheduler": "sgm_uniform"})
                if profile:
                    graph["3"]["inputs"]["model"] = ["10", 0]
                    graph["10"] = {"class_type": "ProfileSDXLAttention", "inputs": {"model": ["4", 0]}}
                if sampler_id != "3":
                    graph[sampler_id] = graph.pop("3")
                    graph["8"]["inputs"]["samples"] = [sampler_id, 0]
                start = time.monotonic()
                result = json.loads(http("/prompt", data={"prompt": graph}, timeout=60))
                prompt_id = result.get("prompt_id")
                if not prompt_id:
                    raise RuntimeError("ComfyUI refused the test graph")
                deadline = time.monotonic() + 1200
                while time.monotonic() < deadline:
                    if child.poll() is not None:
                        raise RuntimeError("ComfyUI exited")
                    entry = json.loads(http(f"/history/{prompt_id}")).get(prompt_id, {})
                    if entry.get("status", {}).get("status_str") == "error":
                        raise RuntimeError("test graph failed")
                    for node in entry.get("outputs", {}).values():
                        if node.get("images"):
                            blob = http("/view?" + urllib.parse.urlencode(node["images"][0]), timeout=60)
                            if not blob.startswith(b"\x89PNG\r\n\x1a\n"):
                                raise RuntimeError("invalid image")
                            return time.monotonic() - start, blob
                    time.sleep(2)
                raise TimeoutError("SDXL profiling render timed out")

            cold_time, _ = render(False, "3")
            profile_time, profile_image = render(True, "11")
            single_time, single_image = render(False, "12")
            a, b = (Image.open(BytesIO(blob)).convert("RGB") for blob in (profile_image, single_image))
            if a.size != b.size:
                raise ValueError("generated images have different sizes")
            diff = ImageStat.Stat(ImageChops.difference(a, b))
            mae = sum(diff.mean) / 3
            max_diff = max(high for _, high in diff.extrema)
            output.flush()
            log = (temp / "comfy.log").read_text(errors="replace")
            rows = [json.loads(line.split("SDXL_ATTENTION_PROFILE ", 1)[1]) for line in log.splitlines()
                    if "SDXL_ATTENTION_PROFILE " in line]
            if len(rows) != 4:
                raise RuntimeError(f"expected four profile steps, got {len(rows)}")
            grouped: dict[str, dict[str, float]] = {}
            for row in rows:
                for item in row["shapes"]:
                    key = f"q={item['q']} k={item['k']} heads={item['heads']} dtype={item['dtype']}"
                    target = grouped.setdefault(key, {"calls": 0, "seconds": 0.0})
                    target["calls"] += item["calls"]
                    target["seconds"] += item["seconds"]
            if not grouped:
                raise RuntimeError("profile captured no attention operations")
            if mae > 0.1 or max_diff > 2:
                raise RuntimeError(f"profiling changed output pixels (mean {mae:.4f}, max {max_diff})")
            print(f"::notice::Profile completed: cold {cold_time:.1f}s; profiled {profile_time:.1f}s; warm baseline {single_time:.1f}s; pixel MAE {mae:.6f}, max {max_diff}; PNG hash {hashlib.sha256(profile_image).hexdigest()[:12]}", flush=True)
            print(f"::notice::UNet {sum(row['unet_seconds'] for row in rows):.2f}s; attention math {sum(x['seconds'] for x in grouped.values()):.2f}s over {int(sum(x['calls'] for x in grouped.values()))} calls; excludes Q/K/V projections", flush=True)
            for shape, values in sorted(grouped.items(), key=lambda pair: pair[1]["seconds"], reverse=True)[:6]:
                print(f"::notice::Attention {shape}: {int(values['calls'])} calls; {values['seconds']:.2f}s", flush=True)
            status = Path(f"/proc/{child.pid}/status").read_text()
            peak = re.search(r"^VmHWM:\s+(\d+)\s+kB", status, re.MULTILINE)
            if peak:
                print(f"::notice::Profile peak RSS: {int(peak.group(1)) / 1048576:.2f} GiB", flush=True)
        finally:
            stop(child)
            if output:
                output.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"::error::Attention profiling failed: {type(exc).__name__}: {str(exc)[:200]}", flush=True)
        raise SystemExit(1) from None
