#!/usr/bin/env python3
"""Real CPU baseline for DreamShaper XL Lightning, isolated from live .draw.

Expects ComfyUI and the checkpoint to have been installed by the opt-in
benchmark workflow. This is a single-runner baseline, not distributed inference.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from havoc.services.imagegen import ImageGenerator  # noqa: E402


def request(path: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:8188" + path, data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as response:  # noqa: S310 - local server only
        return json.load(response)


def main() -> int:
    checkout = Path(os.environ["COMFY_DIR"]).resolve()
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="sdxl-benchmark-") as temp:
        logpath = Path(temp) / "comfy.log"
        with logpath.open("w+") as logfile:
            process = subprocess.Popen(  # noqa: S603 - fixed local executable and arguments
                [sys.executable, "main.py", "--cpu", "--listen", "127.0.0.1", "--port", "8188", "--disable-auto-launch", "--dont-print-server"],
                cwd=checkout, stdout=logfile, stderr=subprocess.STDOUT, start_new_session=True,
            )
            try:
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("ComfyUI exited during startup")
                    try:
                        request("/system_stats")
                        break
                    except (OSError, urllib.error.URLError):
                        time.sleep(2)
                else:
                    raise TimeoutError("ComfyUI did not start")
                generator = ImageGenerator(checkpoint="DreamShaperXL_Lightning.safetensors", width=512, height=512)
                graph, _, _ = generator._builtin("a lighthouse on a rocky coast at sunrise", 1234, steps=4)
                graph["3"]["inputs"]["cfg"] = 1.0
                # A fast Euler / SGM-uniform CPU baseline; the model card also
                # recommends a DPM solver and CFG 2, which needs its own comparison.
                graph["3"]["inputs"]["scheduler"] = "sgm_uniform"
                result = request("/prompt", {"prompt": graph})
                prompt_id = result.get("prompt_id")
                if not prompt_id:
                    raise RuntimeError("server did not return a prompt ID")
                render_start = time.monotonic()
                peak_rss = 0
                deadline = time.monotonic() + 1200
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("ComfyUI exited during rendering")
                    try:
                        for line in Path(f"/proc/{process.pid}/status").read_text().splitlines():
                            if line.startswith("VmHWM:"):
                                peak_rss = max(peak_rss, int(line.split()[1]))
                    except OSError:
                        pass
                    entry = request(f"/history/{prompt_id}").get(prompt_id, {})
                    if entry.get("status", {}).get("status_str") == "error":
                        raise RuntimeError("ComfyUI reported a workflow error")
                    for node in entry.get("outputs", {}).values():
                        images = node.get("images", [])
                        if images:
                            meta = images[0]
                            from urllib.parse import urlencode
                            url = "http://127.0.0.1:8188/view?" + urlencode(meta)
                            with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
                                picture = response.read()
                            if len(picture) < 1024 or not picture.startswith(b"\x89PNG\r\n\x1a\n"):
                                raise RuntimeError("invalid image returned")
                            print(f"::notice::SDXL baseline: PNG {len(picture)} bytes; render {time.monotonic()-render_start:.1f}s; peak RSS {peak_rss / 1024 / 1024:.2f} GiB; total {time.monotonic()-started:.1f}s", flush=True)
                            return 0
                    time.sleep(3)
                raise TimeoutError("SDXL generation did not finish in 20 minutes")
            except Exception as exc:
                print(f"::error::SDXL baseline failed: {type(exc).__name__}: {exc}", flush=True)
                logfile.flush()
                print("--- ComfyUI log tail ---", flush=True)
                print("\n".join(logpath.read_text(errors="replace").splitlines()[-40:]), flush=True)
                return 1
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()


if __name__ == "__main__":
    sys.exit(main())
