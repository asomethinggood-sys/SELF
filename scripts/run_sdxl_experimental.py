#!/usr/bin/env python3
"""Run one time-limited, authenticated experimental SDXL service on a runner."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("WORKER_INDEX", "0")
from scripts import sdxl_cluster_test as rendezvous  # noqa: E402

rendezvous.API = f"https://api.github.com/repos/{rendezvous.REPO}/contents/.services"
NAME = "sdxl-experimental.json"
FAILURE = "sdxl-experimental-failure.json"

# Logs go under the cached runtime rather than the scratch directory, so that a
# failed start can still be read back and published as a diagnostic.
LOG_DIR = Path(os.environ.get("SDXL_SERVICE_LOGS", str(ROOT / ".runtime" / "sdxl-experimental" / "service-logs")))

_STAGE = "start"


def _tails() -> dict[str, str]:
    out: dict[str, str] = {}
    for name in ("comfy.log", "gate.log", "tunnel.log"):
        try:
            out[name] = (LOG_DIR / name).read_text(errors="replace")[-4000:]
        except OSError:
            continue
    return out


def _report(error: str) -> None:
    """Publish what went wrong, so a failed start is readable after the runner is gone."""
    try:
        rendezvous.write(FAILURE, {"run_id": rendezvous.RUN, "stage": _STAGE,
                                   "error": error, "logs": _tails()})
    except Exception as exc:  # noqa: BLE001 - the report must never hide the original failure
        print(f"::warning::Could not publish failure diagnostics: {exc}", flush=True)


def main() -> None:
    child = gateway = tunnel = None
    handles = []
    published = False
    try:
        with tempfile.TemporaryDirectory(prefix="sdxl-service-"):
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            cpu = Path("/proc/cpuinfo").read_text(errors="replace").lower()
            bf16 = "avx512_bf16" in cpu or "amx_bf16" in cpu
            if bf16:
                env["SDXL_ALLOW_CPU_BF16"] = "1"
            # ComfyUI owns the precision flags; the custom node only opts into
            # BF16 CPU compute, so a missing node cannot stop it from starting.
            if not (Path(env["COMFY_DIR"]) / "custom_nodes" / "sdxl_cpu_bf16" / "__init__.py").is_file():
                print("::warning::SDXL CPU BF16 custom node is missing; BF16 weights will not be used",
                      flush=True)
            command = [sys.executable, "main.py", "--cpu", "--listen", "127.0.0.1", "--port", "8188",
                       "--disable-auto-launch", "--dont-print-server", "--bf16-unet" if bf16 else "--fp32-unet"]
            comfy_log = (LOG_DIR / "comfy.log").open("w+")
            global _STAGE
            _STAGE = "comfy-start"
            handles.append(comfy_log)
            child = subprocess.Popen(command, cwd=env["COMFY_DIR"], env=env, stdout=comfy_log,  # noqa: S603
                                     stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise RuntimeError("image server failed to start")
                try:
                    rendezvous.http("/system_stats")
                    break
                except (OSError, urllib.error.URLError):
                    time.sleep(2)
            else:
                raise TimeoutError("image server startup timed out")
            _STAGE = "gate-start"
            gateway_log = (LOG_DIR / "gate.log").open("w+")
            handles.append(gateway_log)
            gateway = subprocess.Popen([sys.executable, str(ROOT / "scripts/sdxl_experimental_gate.py")],  # noqa: S603
                                       env=env, stdout=gateway_log, stderr=subprocess.STDOUT, start_new_session=True)
            _STAGE = "tunnel-start"
            tunnel_log = (LOG_DIR / "tunnel.log").open("w+")
            handles.append(tunnel_log)
            tunnel = subprocess.Popen([env["CLOUDFLARED"], "tunnel", "--url", "http://127.0.0.1:9009", "--no-autoupdate"],  # noqa: S603
                                      stdout=tunnel_log, stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + 90
            url = ""
            while time.monotonic() < deadline:
                if gateway.poll() is not None or tunnel.poll() is not None:
                    raise RuntimeError("private gateway or tunnel failed")
                tunnel_log.flush()
                found = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com",
                               (LOG_DIR / "tunnel.log").read_text(errors="replace"))
                if found:
                    url = found.group()
                    break
                time.sleep(1)
            if not url:
                raise TimeoutError("tunnel startup timed out")
            from scripts.sdxl_oidc import _OPENER
            _STAGE = "health"
            healthy = False
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                try:
                    with _OPENER.open(url + "/healthz", timeout=10) as response:  # noqa: S310 - validated tunnel URL
                        healthy = response.status == 200
                    if healthy:
                        break
                except (OSError, urllib.error.URLError):
                    pass
                time.sleep(3)
            if not healthy:
                raise RuntimeError("experimental gateway is not reachable")
            try:
                _OPENER.open(url + "/system_stats", timeout=10).close()  # noqa: S310 - validated tunnel URL
            except urllib.error.HTTPError as exc:
                if exc.code != 401:
                    raise RuntimeError("experimental gateway authentication check failed") from None
            else:
                raise RuntimeError("experimental gateway accepted an unauthenticated request")
            _STAGE = "serving"
            start = datetime.now(UTC)
            expires = start + timedelta(minutes=65)
            rendezvous.write(NAME, {"url": url, "run_id": rendezvous.RUN, "started_at": start.isoformat(),
                                    "expires_at": expires.isoformat(), "precision": "bf16" if bf16 else "fp32"})
            published = True
            print(f"::notice::Private experimental image service is available for up to 65 minutes; precision {'BF16' if bf16 else 'FP32 fallback'}.", flush=True)
            deadline = time.monotonic() + 65 * 60
            while time.monotonic() < deadline:
                if any(process.poll() is not None for process in (child, gateway, tunnel)):
                    raise RuntimeError("experimental service stopped unexpectedly")
                time.sleep(5)
    finally:
        if published:
            try:
                current, _ = rendezvous.read(NAME)
                if current and current.get("run_id") == rendezvous.RUN:
                    rendezvous.delete(NAME)
            except Exception:  # noqa: BLE001 - expiration also makes a stale endpoint unusable
                print("::warning::Could not remove expired experimental service discovery.", flush=True)
        for process in (tunnel, gateway, child):
            rendezvous.stop(process)
        for handle in handles:
            handle.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"::error::Experimental image service failed at {_STAGE}: {type(exc).__name__}: {exc}",
              flush=True)
        _report(f"{type(exc).__name__}: {exc}")
        raise SystemExit(1) from None
