#!/usr/bin/env python3
"""Same-runner SDXL FP32 vs BF16 CPU benchmark with synthetic prompts only."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("GITHUB_TOKEN", "local-test-no-github-token")
os.environ.setdefault("WORKER_INDEX", "0")
from havoc.services.imagegen import ImageGenerator  # noqa: E402
from scripts.sdxl_cluster_test import http, stop  # noqa: E402


def main() -> None:
    cpuinfo = Path("/proc/cpuinfo").read_text(errors="replace").lower()
    print("::notice::CPU BF16 capabilities: " + ", ".join(f"{flag}={flag in cpuinfo}" for flag in ("avx512_bf16", "amx_bf16")), flush=True)
    if "avx512_bf16" not in cpuinfo and "amx_bf16" not in cpuinfo:
        raise RuntimeError("runner lacks native CPU BF16; no precision comparison performed")
    generator = ImageGenerator(checkpoint="DreamShaperXL_Lightning.safetensors", width=768, height=768)
    times, images, peaks = {}, {}, {}
    with tempfile.TemporaryDirectory(prefix="sdxl-bf16-") as directory:
        for mode, extra in (("fp32", ["--fp32-unet"]), ("bf16", ["--bf16-unet"])):
            logpath = Path(directory) / f"{mode}.log"
            with logpath.open("w+") as output:
                env = os.environ.copy()
                if mode == "bf16":
                    env["SDXL_ALLOW_CPU_BF16"] = "1"
                else:
                    env.pop("SDXL_ALLOW_CPU_BF16", None)
                child = subprocess.Popen(  # noqa: S603 - fixed executable and arguments
                    [sys.executable, "main.py", "--cpu", "--listen", "127.0.0.1", "--port", "8188",
                     "--disable-auto-launch", "--dont-print-server", *extra],
                    cwd=os.environ["COMFY_DIR"], env=env, stdout=output,
                    stderr=subprocess.STDOUT, start_new_session=True,
                )
                try:
                    ready = time.monotonic() + 180
                    while time.monotonic() < ready:
                        if child.poll() is not None:
                            raise RuntimeError(f"{mode} server exited")
                        try:
                            http("/system_stats")
                            break
                        except (OSError, urllib.error.URLError):
                            time.sleep(2)
                    else:
                        raise TimeoutError(f"{mode} server did not start")

                    def render(sampler_id: str, *, mode: str = mode, child: subprocess.Popen = child) -> tuple[float, bytes]:
                        graph, _, _ = generator._builtin("a lighthouse on a rocky coast at sunrise", 1234, steps=4)
                        graph["3"]["inputs"].update({"cfg": 1.0, "scheduler": "sgm_uniform", "model": ["20", 0]})
                        graph["20"] = {"class_type": "ForceFreshSampler", "inputs": {"model": ["4", 0], "nonce": int(sampler_id)}}
                        if sampler_id != "3":
                            graph[sampler_id] = graph.pop("3")
                            graph["8"]["inputs"]["samples"] = [sampler_id, 0]
                        start = time.monotonic()
                        result = json.loads(http("/prompt", data={"prompt": graph}, timeout=60))
                        prompt_id = result.get("prompt_id")
                        if not prompt_id:
                            raise RuntimeError(f"{mode} graph refused")
                        deadline = time.monotonic() + 1200
                        while time.monotonic() < deadline:
                            if child.poll() is not None:
                                raise RuntimeError(f"{mode} server exited during render")
                            entry = json.loads(http(f"/history/{prompt_id}")).get(prompt_id, {})
                            if entry.get("status", {}).get("status_str") == "error":
                                raise RuntimeError(f"{mode} graph failed")
                            for kind, detail in entry.get("status", {}).get("messages", []):
                                if kind == "execution_cached" and sampler_id in detail.get("nodes", []):
                                    raise RuntimeError(f"{mode} sampler was cached; timing is invalid")
                            for node in entry.get("outputs", {}).values():
                                if node.get("images"):
                                    blob = http("/view?" + urllib.parse.urlencode(node["images"][0]), timeout=60)
                                    if len(blob) < 1024 or not blob.startswith(b"\x89PNG\r\n\x1a\n"):
                                        raise RuntimeError("invalid PNG")
                                    return time.monotonic() - start, blob
                            time.sleep(2)
                        raise TimeoutError(f"{mode} render timed out")

                    cold, _ = render("3")
                    times[mode], images[mode] = render("11")
                    status = Path(f"/proc/{child.pid}/status").read_text()
                    peak = re.search(r"^VmHWM:\s+(\d+)\s+kB", status, re.MULTILINE)
                    peaks[mode] = int(peak.group(1)) / 1048576 if peak else 0
                    output.flush()
                    text = logpath.read_text(errors="replace")
                    expected = f"model weight dtype torch.{mode if mode == 'bf16' else 'float32'}"
                    if mode == "bf16":
                        expected = "model weight dtype torch.bfloat16, manual cast: None"
                    if expected not in text:
                        raise RuntimeError(f"{mode} UNet dtype was not confirmed")
                    print(f"::notice::{mode}: cold {cold:.1f}s; warm {times[mode]:.1f}s; peak {peaks[mode]:.2f} GiB; PNG hash {hashlib.sha256(images[mode]).hexdigest()[:12]}", flush=True)
                finally:
                    stop(child)
    reference, candidate = (Image.open(BytesIO(images[mode])).convert("RGB") for mode in ("fp32", "bf16"))
    if reference.size != candidate.size:
        raise ValueError("different image sizes")
    mae = sum(ImageStat.Stat(ImageChops.difference(reference, candidate)).mean) / 3
    print(f"::notice::Warm FP32 {times['fp32']:.1f}s vs BF16 {times['bf16']:.1f}s; mean RGB difference {mae:.2f}/255 (not perceptual quality)", flush=True)
    folder = ROOT / ".reports" / "sdxl-bf16"
    folder.mkdir(parents=True, exist_ok=True)
    for mode, data in images.items():
        (folder / f"{mode}.png").write_bytes(data)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"::error::BF16 benchmark failed: {type(exc).__name__}: {str(exc)[:200]}", flush=True)
        raise SystemExit(1) from None
