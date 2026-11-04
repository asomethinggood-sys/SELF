#!/usr/bin/env python3
"""Opt-in four-job distributed SDXL experiment with ephemeral authenticated peers.

Public GitHub status files contain a leader public key and encrypted worker
credentials only; all private keys and tokens live in runner memory. This is a
fixed public test prompt, not connected to user requests or the live service.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from havoc.services.imagegen import ImageGenerator  # noqa: E402

RUN = os.environ["GITHUB_RUN_ID"]
BRANCH = os.environ["GITHUB_REF_NAME"]
REPO = os.environ["GITHUB_REPOSITORY"]
TOKEN = os.environ["GITHUB_TOKEN"]
INDEX = int(os.environ["WORKER_INDEX"])
DIRECTORY = f".cluster-probes/{RUN}"
API = f"https://api.github.com/repos/{REPO}/contents/{DIRECTORY}"
TIMEOUT = 1500


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def un64(data: str) -> bytes:
    return base64.b64decode(data, validate=True)


def github(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    url = API + "/" + path + ("?ref=" + urllib.parse.quote(BRANCH, safe="") if method == "GET" else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json",
    })
    # A transient GitHub API error must not immediately kill a warm worker.
    # Bound retries and never print the request or response, which can contain
    # credentials or encrypted rendezvous material. PUT/DELETE callers still
    # handle conflicts by re-reading the current file SHA.
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as response:  # noqa: S310 - fixed GitHub API
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 4:
                return exc.code, {}
        except (OSError, urllib.error.URLError):
            if attempt == 4:
                raise RuntimeError("GitHub rendezvous network request failed") from None
        time.sleep(min(10, 2**attempt) + secrets.randbelow(1000) / 1000)
    raise RuntimeError("GitHub rendezvous retries exhausted")


def read(name: str) -> tuple[dict | None, str]:
    status, value = github("GET", name)
    if status == 404:
        return None, ""
    if status != 200:
        raise RuntimeError(f"GitHub status read failed ({status})")
    return json.loads(base64.b64decode(value["content"])), value["sha"]


def write(name: str, value: dict) -> None:
    for _ in range(20):
        _, sha = read(name)
        request = {"message": f"experimental SDXL rendezvous {RUN} [skip ci]", "branch": BRANCH,
                   "content": b64(json.dumps(value, separators=(",", ":")).encode())}
        if sha:
            request["sha"] = sha
        status, _ = github("PUT", name, request)
        if status in (200, 201):
            return
        if status not in (409, 422):
            raise RuntimeError(f"GitHub status write failed ({status})")
        time.sleep(2 + secrets.randbelow(3))
    raise RuntimeError("GitHub status write contention")


def delete(name: str) -> None:
    try:
        for _ in range(5):
            value, sha = read(name)
            if value is None:
                return
            status, _ = github("DELETE", name, {"message": f"remove experimental SDXL status {RUN} [skip ci]", "branch": BRANCH, "sha": sha})
            if status in (200, 404):
                return
            time.sleep(2)
    except Exception:
        print("::warning::Could not remove an experiment status file", flush=True)


def wait_for(name: str, seconds: int = TIMEOUT) -> dict:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value, _ = read(name)
        if value is not None:
            return value
        time.sleep(5)
    raise TimeoutError("rendezvous timed out")


def public(private: X25519PrivateKey) -> bytes:
    return private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def key(shared: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=RUN.encode(), info=b"yura-sdxl-experiment-v1").derive(shared)


def seal(leader_public: bytes, value: dict) -> dict:
    private = X25519PrivateKey.generate()
    nonce = os.urandom(12)
    secret = key(private.exchange(X25519PublicKey.from_public_bytes(leader_public)))
    ciphertext = AESGCM(secret).encrypt(nonce, json.dumps(value).encode(), RUN.encode())
    return {"public": b64(public(private)), "nonce": b64(nonce), "ciphertext": b64(ciphertext)}


def open_envelope(private: X25519PrivateKey, value: dict) -> dict:
    secret = key(private.exchange(X25519PublicKey.from_public_bytes(un64(value["public"]))))
    raw = AESGCM(secret).decrypt(un64(value["nonce"]), un64(value["ciphertext"]), RUN.encode())
    result = json.loads(raw)
    if not isinstance(result, dict) or set(result) != {"url", "token"}:
        raise ValueError("invalid worker envelope")
    return result


def http(path: str, *, data: dict | None = None, token: str = "", timeout: int = 20) -> bytes:
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request("http://127.0.0.1:8188" + path, data=body, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310 - loopback only
        return response.read(16 * 1024 * 1024)


def server(env: dict, logfile: Path) -> tuple[subprocess.Popen, object]:
    output = logfile.open("w+")
    comfy = Path(os.environ["COMFY_DIR"])
    child = subprocess.Popen(  # noqa: S603
        [sys.executable, "main.py", "--cpu", "--listen", "127.0.0.1", "--port", "8188", "--disable-auto-launch", "--dont-print-server"],
        cwd=comfy, env=env, stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
    )
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError("ComfyUI failed to start")
        try:
            http("/system_stats")
            return child, output
        except (OSError, urllib.error.URLError):
            time.sleep(2)
    raise TimeoutError("ComfyUI did not start")


def stop(child: subprocess.Popen | None) -> None:
    if child is None or child.poll() is not None:
        return
    os.killpg(child.pid, signal.SIGTERM)
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()


def tunnel(logpath: Path) -> tuple[subprocess.Popen, object, str]:
    output = logpath.open("w+")
    child = subprocess.Popen(  # noqa: S603
        [os.environ["CLOUDFLARED"], "tunnel", "--url", "http://127.0.0.1:9008", "--no-autoupdate"],
        stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
    )
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        output.flush()
        match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", logpath.read_text(errors="replace"))
        if match:
            return child, output, match.group(0)
        if child.poll() is not None:
            break
        time.sleep(1)
    stop(child)
    output.close()
    raise RuntimeError("tunnel failed to start")


def worker(temp: Path) -> int:
    child = proxy = gate = None
    output = tunnel_output = gate_output = None
    token = secrets.token_urlsafe(32)
    name = f"worker-{INDEX}.json"
    try:
        leader = wait_for("leader.json")
        env = {**os.environ, "SDXL_WORKER_TOKEN": token, "SDXL_WORKER_CHECKPOINT": "DreamShaperXL_Lightning.safetensors"}
        child, output = server(env, temp / "comfy.log")
        http("/sdxl-worker/warm", data={}, token=token, timeout=300)
        gate_output = (temp / "gate.log").open("w+")
        gate = subprocess.Popen(  # noqa: S603 - fixed repository script
            [sys.executable, str(ROOT / "scripts" / "sdxl_cluster_gate.py")],
            env=env, stdout=gate_output, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        import socket
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if gate.poll() is not None:
                raise RuntimeError("worker gate exited")
            try:
                with socket.create_connection(("127.0.0.1", 9008), timeout=1):
                    break
            except OSError:
                time.sleep(1)
        else:
            raise TimeoutError("worker gate did not start")
        proxy, tunnel_output, url = tunnel(temp / "tunnel.log")
        write(name, seal(un64(leader["public"]), {"url": url, "token": token}))
        print(f"::notice::Worker {INDEX}: loaded model and joined securely", flush=True)
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            stop_signal, _ = read(f"stop-{INDEX}.json")
            if stop_signal is not None:
                calls = (temp / "gate.log").read_text(errors="replace").count("SDXL_GATE_PREDICT ok")
                if calls < 8:
                    raise RuntimeError("cannot inject failure before both distributed renders completed")
                print(f"::notice::Worker {INDEX}: intentional shutdown to test coordinator fallback", flush=True)
                return 0
            done, _ = read("done.json")
            if done is not None:
                calls = (temp / "gate.log").read_text(errors="replace").count("SDXL_GATE_PREDICT ok")
                if done.get("success") and calls < 4:
                    print(f"::error::Worker {INDEX}: expected at least four real predictions, got {calls}", flush=True)
                    return 1
                return 0 if done.get("success") else 1
            if child.poll() is not None or proxy.poll() is not None:
                raise RuntimeError("worker process exited")
            time.sleep(5)
        raise TimeoutError("coordinator timed out")
    finally:
        if child and child.poll() is None:
            try:
                status = Path(f"/proc/{child.pid}/status").read_text()
                peak = re.search(r"^VmHWM:\s+(\d+)\s+kB", status, re.MULTILINE)
                if peak:
                    print(f"::notice::Worker {INDEX}: ComfyUI peak RSS {int(peak.group(1)) / 1048576:.2f} GiB", flush=True)
            except OSError:
                pass
        stop(proxy)
        stop(gate)
        stop(child)
        if gate_output:
            gate_output.close()
            calls = (temp / "gate.log").read_text(errors="replace").count("SDXL_GATE_PREDICT ok")
            print(f"::notice::Worker {INDEX}: {calls} authenticated tile predictions", flush=True)
        if output:
            output.close()
        if tunnel_output:
            tunnel_output.close()
        delete(name)


def leader(temp: Path) -> int:
    private = X25519PrivateKey.generate()
    child = None
    output = None
    success = False
    try:
        write("leader.json", {"public": b64(public(private))})
        workers = [open_envelope(private, wait_for(f"worker-{i}.json")) for i in (1, 2, 3)]
        # Quality comparisons must not silently use local fallback; the legacy
        # scenario enables it specifically to test actual worker loss.
        scenario = os.environ.get("SDXL_TEST_SCENARIO", "legacy")
        env = {**os.environ, "SDXL_COORDINATOR_WORKERS": json.dumps(workers),
               "SDXL_FALLBACK_LOCAL": "1" if scenario in {"", "legacy"} else "0"}
        child, output = server(env, temp / "comfy.log")
        resolution = int(os.environ.get("SDXL_TEST_RESOLUTION", "512"))
        if resolution not in {512, 768, 1024}:
            raise ValueError("unsupported test resolution")
        generator = ImageGenerator(checkpoint="DreamShaperXL_Lightning.safetensors", width=resolution, height=resolution)
        def render(*, distributed: bool, seed: int, overlap: int = 8, sampler_id: str = "3", label: str,
                   global_weight: float = 0.0, global_first_only: bool = False, global_full_res: bool = False,
                   spatial_crops: bool = False, prompt: str = "a lighthouse on a rocky coast at sunrise") -> tuple[float, bytes]:
            graph, _, _ = generator._builtin(prompt, seed, steps=4)
            graph["3"]["inputs"].update({"cfg": 1.0, "scheduler": "sgm_uniform"})
            if distributed:
                graph["3"]["inputs"]["model"] = ["10", 0]
                graph["10"] = {"class_type": "FourWorkerSDXL", "inputs": {"model": ["4", 0], "overlap": overlap}}
                if spatial_crops:
                    graph["10"]["inputs"]["spatial_crops"] = True
                if global_weight:
                    graph["10"]["inputs"]["global_weight"] = global_weight
                    if global_first_only:
                        graph["10"]["inputs"]["global_first_only"] = True
                    if global_full_res:
                        graph["10"]["inputs"]["global_full_res"] = True
            if sampler_id != "3":
                # A fresh sampler node prevents Comfy's graph cache from turning
                # the same-seed baseline into a near-instant cached result.
                graph[sampler_id] = graph.pop("3")
                for node in graph.values():
                    for key, value in node.get("inputs", {}).items():
                        if value == ["3", 0]:
                            node["inputs"][key] = [sampler_id, 0]
            started = time.monotonic()
            result = json.loads(http("/prompt", data={"prompt": graph}, timeout=60))
            prompt_id = result.get("prompt_id")
            if not prompt_id:
                raise RuntimeError("ComfyUI refused the graph")
            deadline = time.monotonic() + TIMEOUT
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise RuntimeError("coordinator ComfyUI exited")
                entry = json.loads(http(f"/history/{prompt_id}")).get(prompt_id, {})
                if entry.get("status", {}).get("status_str") == "error":
                    raise RuntimeError("render graph failed")
                for node in entry.get("outputs", {}).values():
                    images = node.get("images", [])
                    if images:
                        blob = http("/view?" + urllib.parse.urlencode(images[0]), timeout=60)
                        if len(blob) < 1024 or not blob.startswith(b"\x89PNG\r\n\x1a\n"):
                            raise RuntimeError("invalid image")
                        elapsed = time.monotonic() - started
                        print(f"::notice::{label}: {resolution}x{resolution}; PNG {len(blob)} bytes; render {elapsed:.1f}s; sha256 {hashlib.sha256(blob).hexdigest()[:16]}", flush=True)
                        folder = ROOT / ".reports" / "sdxl-quality"
                        folder.mkdir(parents=True, exist_ok=True)
                        (folder / f"{label}.png").write_bytes(blob)
                        return elapsed, blob
                time.sleep(3)
            raise TimeoutError("render timed out")

        if os.environ.get("SDXL_TEST_SCENARIO") in {"crop-stress", "wide-stress"}:
            from io import BytesIO

            from PIL import Image, ImageChops, ImageStat

            # Multiple prompts and seeds; each comparison uses a fresh sampler
            # node, fixed settings and the same seed as its full-frame reference.
            cases = (
                ("group", "three friends sitting together at a cafe table, cinematic photo", 2927),
                ("hand", "a close-up of a hand holding a red apple, studio photography", 7041),
                ("cat", "an orange cat sitting on a wooden chair by a window, natural light", 3582),
            )
            render(distributed=False, seed=1234, label="warmup")
            results = []
            wide = os.environ.get("SDXL_TEST_SCENARIO") == "wide-stress"
            for index, (name, prompt, seed) in enumerate(cases):
                cluster_time, actual = render(distributed=True, seed=seed, overlap=24 if wide else 16,
                    sampler_id=str(20 + index * 2), label=f"{name}-cluster", spatial_crops=not wide,
                    global_weight=1.0, global_first_only=True, global_full_res=True, prompt=prompt)
                single_time, baseline = render(distributed=False, seed=seed, sampler_id=str(21 + index * 2),
                    label=f"{name}-baseline", prompt=prompt)
                a = Image.open(BytesIO(actual)).convert("RGB")
                b = Image.open(BytesIO(baseline)).convert("RGB")
                if a.size != b.size:
                    raise ValueError("quality comparison size mismatch")
                mae = sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3
                results.append(f"{name}: cluster {cluster_time:.1f}s vs single {single_time:.1f}s, RGB MAE {mae:.2f}")
            print("::notice::" + "; ".join(results) + "; RGB MAE is not a perceptual score", flush=True)
            output.flush()
            log = (temp / "comfy.log").read_text(errors="replace")
            if "SDXL_DEGRADED" in log or log.count("SDXL_GLOBAL_FULL") != len(cases):
                raise RuntimeError("stress experiment did not run as requested")
            status = Path(f"/proc/{child.pid}/status").read_text()
            match = re.search(r"^VmHWM:\s+(\d+)\s+kB", status, re.MULTILINE)
            if match:
                print(f"::notice::Coordinator ComfyUI peak RSS: {int(match.group(1)) / 1048576:.2f} GiB", flush=True)
            success = True
            return 0

        if os.environ.get("SDXL_TEST_SCENARIO") == "wide-overlap":
            from io import BytesIO

            from PIL import Image, ImageChops, ImageStat

            render(distributed=False, seed=1234, label="warmup")
            lighthouse_time, lighthouse = render(distributed=True, seed=1234, overlap=32, sampler_id="11", label="lighthouse-wide32", global_weight=1.0, global_first_only=True, global_full_res=True)
            baseline_time, baseline = render(distributed=False, seed=1234, sampler_id="12", label="single-baseline")
            portrait = "a portrait of a woman with brown hair in a forest, cinematic photography"
            portrait24_time, portrait24 = render(distributed=True, seed=4321, overlap=24, sampler_id="13", label="portrait-wide24", global_weight=1.0, global_first_only=True, global_full_res=True, prompt=portrait)
            portrait32_time, portrait32 = render(distributed=True, seed=4321, overlap=32, sampler_id="14", label="portrait-wide32", global_weight=1.0, global_first_only=True, global_full_res=True, prompt=portrait)
            portrait_baseline_time, portrait_baseline = render(distributed=False, seed=4321, sampler_id="15", label="portrait-baseline", prompt=portrait)
            def difference(first: bytes, second: bytes) -> float:
                a = Image.open(BytesIO(first)).convert("RGB")
                b = Image.open(BytesIO(second)).convert("RGB")
                if a.size != b.size:
                    raise ValueError("quality comparison size mismatch")
                return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3
            print("::notice::Same-seed wide-overlap experiment: "
                  f"lighthouse baseline {baseline_time:.1f}s, 32 overlap {lighthouse_time:.1f}s (RGB MAE {difference(lighthouse, baseline):.2f}); "
                  f"portrait baseline {portrait_baseline_time:.1f}s, 24 overlap {portrait24_time:.1f}s (RGB MAE {difference(portrait24, portrait_baseline):.2f}), "
                  f"32 overlap {portrait32_time:.1f}s (RGB MAE {difference(portrait32, portrait_baseline):.2f}). "
                  "RGB difference is not a perceptual quality score.", flush=True)
            status = Path(f"/proc/{child.pid}/status").read_text()
            match = re.search(r"^VmHWM:\s+(\d+)\s+kB", status, re.MULTILINE)
            if match:
                print(f"::notice::Coordinator ComfyUI peak RSS: {int(match.group(1)) / 1048576:.2f} GiB", flush=True)
            output.flush()
            log = (temp / "comfy.log").read_text(errors="replace")
            if "SDXL_DEGRADED" in log or log.count("SDXL_GLOBAL_FULL") != 3:
                raise RuntimeError("wide-overlap experiment did not run as requested")
            success = True
            return 0

        if os.environ.get("SDXL_TEST_SCENARIO") == "crop-conditioning":
            from io import BytesIO

            from PIL import Image, ImageChops, ImageStat

            render(distributed=False, seed=1234, label="warmup")
            crop_time, cropped = render(distributed=True, seed=1234, overlap=16, sampler_id="11", label="crop-only", spatial_crops=True)
            anchor_time, anchored = render(distributed=True, seed=1234, overlap=16, sampler_id="12", label="crop-anchor", spatial_crops=True, global_weight=1.0, global_first_only=True, global_full_res=True)
            baseline_time, baseline = render(distributed=False, seed=1234, sampler_id="13", label="single-baseline")
            portrait = "a portrait of a woman with brown hair in a forest, cinematic photography"
            portrait_time, portrait_anchored = render(distributed=True, seed=4321, overlap=16, sampler_id="14", label="portrait-crop-anchor", spatial_crops=True, global_weight=1.0, global_first_only=True, global_full_res=True, prompt=portrait)
            portrait_baseline_time, portrait_baseline = render(distributed=False, seed=4321, sampler_id="15", label="portrait-baseline", prompt=portrait)
            def difference(first: bytes, second: bytes) -> float:
                a = Image.open(BytesIO(first)).convert("RGB")
                b = Image.open(BytesIO(second)).convert("RGB")
                if a.size != b.size:
                    raise ValueError("quality comparison size mismatch")
                return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3
            print("::notice::Same-seed crop experiment: "
                  f"lighthouse baseline {baseline_time:.1f}s, crop-only {crop_time:.1f}s (RGB MAE {difference(cropped, baseline):.2f}), "
                  f"crop plus full anchor {anchor_time:.1f}s (RGB MAE {difference(anchored, baseline):.2f}); "
                  f"portrait baseline {portrait_baseline_time:.1f}s, crop plus full anchor {portrait_time:.1f}s (RGB MAE {difference(portrait_anchored, portrait_baseline):.2f}). "
                  "RGB difference is not a perceptual quality score.", flush=True)
            status = Path(f"/proc/{child.pid}/status").read_text()
            match = re.search(r"^VmHWM:\s+(\d+)\s+kB", status, re.MULTILINE)
            if match:
                print(f"::notice::Coordinator ComfyUI peak RSS: {int(match.group(1)) / 1048576:.2f} GiB", flush=True)
            output.flush()
            log = (temp / "comfy.log").read_text(errors="replace")
            if "SDXL_DEGRADED" in log or log.count("SDXL_GLOBAL_FULL") != 2:
                raise RuntimeError("crop experiment did not run as requested")
            success = True
            return 0

        if os.environ.get("SDXL_TEST_SCENARIO") == "full-context-first":
            from io import BytesIO

            from PIL import Image, ImageChops, ImageStat

            render(distributed=False, seed=1234, label="warmup")
            blended_time, blended = render(distributed=True, seed=1234, overlap=16, sampler_id="11", label="full-first-blend", global_weight=0.75, global_first_only=True, global_full_res=True)
            anchored_time, anchored = render(distributed=True, seed=1234, overlap=16, sampler_id="12", label="full-first-anchor", global_weight=1.0, global_first_only=True, global_full_res=True)
            baseline_time, baseline = render(distributed=False, seed=1234, sampler_id="13", label="single-baseline")
            portrait = "a portrait of a woman with brown hair in a forest, cinematic photography"
            portrait_time, portrait_anchored = render(distributed=True, seed=4321, overlap=16, sampler_id="14", label="portrait-anchor", global_weight=1.0, global_first_only=True, global_full_res=True, prompt=portrait)
            portrait_baseline_time, portrait_baseline = render(distributed=False, seed=4321, sampler_id="15", label="portrait-baseline", prompt=portrait)
            def difference(first: bytes, second: bytes) -> float:
                a = Image.open(BytesIO(first)).convert("RGB")
                b = Image.open(BytesIO(second)).convert("RGB")
                if a.size != b.size:
                    raise ValueError("quality comparison size mismatch")
                return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3
            print("::notice::Same-seed comparison: "
                  f"lighthouse baseline {baseline_time:.1f}s, first-full-75% {blended_time:.1f}s (RGB MAE {difference(blended, baseline):.2f}), "
                  f"first-full-100% {anchored_time:.1f}s (RGB MAE {difference(anchored, baseline):.2f}); "
                  f"portrait baseline {portrait_baseline_time:.1f}s, first-full-100% {portrait_time:.1f}s (RGB MAE {difference(portrait_anchored, portrait_baseline):.2f}). "
                  "RGB difference is not a perceptual quality score.", flush=True)
            status = Path(f"/proc/{child.pid}/status").read_text()
            match = re.search(r"^VmHWM:\s+(\d+)\s+kB", status, re.MULTILINE)
            if match:
                print(f"::notice::Coordinator ComfyUI peak RSS: {int(match.group(1)) / 1048576:.2f} GiB", flush=True)
            output.flush()
            log = (temp / "comfy.log").read_text(errors="replace")
            if "SDXL_DEGRADED" in log:
                raise RuntimeError("comparison silently fell back instead of distributing")
            if log.count("SDXL_GLOBAL_FULL") != 2:
                raise RuntimeError("expected exactly one full-frame anchor for each anchored render")
            success = True
            return 0

        render(distributed=False, seed=1234, label="warmup")
        cluster_time, image8 = render(distributed=True, seed=1234, overlap=8, label="tiled-overlap-8")
        overlap_time, image16 = render(distributed=True, seed=1234, overlap=16, sampler_id="11", label="tiled-overlap-16")
        guided_time, guided = render(distributed=True, seed=1234, overlap=16, sampler_id="14", label="tiled-global-035", global_weight=0.35)
        first_time, first_guided = render(distributed=True, seed=1234, overlap=16, sampler_id="15", label="tiled-global-first", global_weight=0.35, global_first_only=True)
        single_time, baseline = render(distributed=False, seed=1234, sampler_id="12", label="single-baseline")
        print(f"::notice::Same-seed warm comparison: overlap 8 {cluster_time:.1f}s, overlap 16 {overlap_time:.1f}s, guided {guided_time:.1f}s, first-only {first_time:.1f}s, single {single_time:.1f}s", flush=True)
        from io import BytesIO

        from PIL import Image, ImageChops, ImageStat
        reference = Image.open(BytesIO(baseline)).convert("RGB")
        for name, blob in (("overlap 8", image8), ("overlap 16", image16), ("global 0.35", guided), ("global first", first_guided)):
            candidate = Image.open(BytesIO(blob)).convert("RGB")
            if candidate.size != reference.size:
                raise RuntimeError("quality comparison size mismatch")
            mae = sum(ImageStat.Stat(ImageChops.difference(reference, candidate)).mean) / 3
            print(f"::notice::{name}: mean absolute RGB difference vs same-seed full-frame {mae:.2f}/255 (not a perceptual quality score)", flush=True)
        # A real remote worker is stopped, not mocked. The next distributed
        # graph must finish locally without advancing on partial predictions.
        write("stop-1.json", {"reason": "exercise local fallback"})
        end = time.monotonic() + 60
        while time.monotonic() < end and read("worker-1.json")[0] is not None:
            time.sleep(2)
        if read("worker-1.json")[0] is not None:
            raise TimeoutError("failure injection worker did not stop")
        fallback_time, _ = render(distributed=True, seed=1236, sampler_id="13", label="local-fallback")
        output.flush()
        if "SDXL_DEGRADED switching to full-frame local inference" not in (temp / "comfy.log").read_text(errors="replace"):
            raise RuntimeError("worker failure did not trigger local fallback")
        print(f"::notice::Actual worker loss recovered via local inference in {fallback_time:.1f}s", flush=True)
        for match in re.finditer(r"SDXL_TILE_STATS step_seconds=[0-9.]+ tile_seconds=[0-9.,]+", (temp / "comfy.log").read_text(errors="replace")):
            print(f"::notice::{match.group(0)}", flush=True)
        success = True
        return 0
    finally:
        try:
            write("done.json", {"success": success})
        except Exception:
            print("::warning::Could not notify workers of shutdown", flush=True)
        stop(child)
        if output:
            output.close()
        # Workers poll done before removing their records. Do not remove done
        # until each has had a chance to observe it.
        end = time.monotonic() + 45
        while time.monotonic() < end:
            try:
                if all(read(f"worker-{i}.json")[0] is None for i in (1, 2, 3)):
                    break
            except Exception:
                break
            time.sleep(3)
        delete("leader.json")
        delete("done.json")
        delete("stop-1.json")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="yura-sdxl-cluster-") as temp:
        try:
            return leader(Path(temp)) if INDEX == 0 else worker(Path(temp))
        except Exception as exc:
            print(f"::error::Cluster experiment failed: {type(exc).__name__}: {str(exc)[:200]}", flush=True)
            return 1


if __name__ == "__main__":
    sys.exit(main())
