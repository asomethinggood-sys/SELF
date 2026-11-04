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
TIMEOUT = 900


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
    try:
        with urllib.request.urlopen(req, timeout=30) as response:  # noqa: S310 - fixed GitHub API
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, {}


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
    output = tunnel_output = None
    token = secrets.token_urlsafe(32)
    name = f"worker-{INDEX}.json"
    try:
        leader = wait_for("leader.json")
        env = {**os.environ, "SDXL_WORKER_TOKEN": token, "SDXL_WORKER_CHECKPOINT": "DreamShaperXL_Lightning.safetensors"}
        child, output = server(env, temp / "comfy.log")
        http("/sdxl-worker/warm", data={}, token=token, timeout=300)
        gate = subprocess.Popen(  # noqa: S603 - fixed repository script
            [sys.executable, str(ROOT / "scripts" / "sdxl_cluster_gate.py")],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
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
            done, _ = read("done.json")
            if done is not None:
                return 0 if done.get("success") else 1
            if child.poll() is not None or proxy.poll() is not None:
                raise RuntimeError("worker process exited")
            time.sleep(5)
        raise TimeoutError("coordinator timed out")
    finally:
        stop(proxy)
        stop(gate)
        stop(child)
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
        env = {**os.environ, "SDXL_COORDINATOR_WORKERS": json.dumps(workers)}
        child, output = server(env, temp / "comfy.log")
        generator = ImageGenerator(checkpoint="DreamShaperXL_Lightning.safetensors", width=512, height=512)
        graph, _, _ = generator._builtin("a lighthouse on a rocky coast at sunrise", 1234, steps=4)
        graph["3"]["inputs"].update({"cfg": 1.0, "scheduler": "sgm_uniform", "model": ["10", 0]})
        graph["10"] = {"class_type": "FourWorkerSDXL", "inputs": {"model": ["4", 0], "overlap": 8}}
        started = time.monotonic()
        result = json.loads(http("/prompt", data={"prompt": graph}, timeout=60))
        prompt_id = result.get("prompt_id")
        if not prompt_id:
            raise RuntimeError("ComfyUI refused the distributed graph")
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError("coordinator ComfyUI exited")
            entry = json.loads(http(f"/history/{prompt_id}")).get(prompt_id, {})
            if entry.get("status", {}).get("status_str") == "error":
                # Print only error type, never prompts or model inputs.
                raise RuntimeError("distributed graph failed; see sanitized error report")
            for node in entry.get("outputs", {}).values():
                images = node.get("images", [])
                if images:
                    blob = http("/view?" + urllib.parse.urlencode(images[0]), timeout=60)
                    if len(blob) < 1024 or not blob.startswith(b"\x89PNG\r\n\x1a\n"):
                        raise RuntimeError("invalid image")
                    print(f"::notice::Four-worker SDXL: PNG {len(blob)} bytes; render {time.monotonic()-started:.1f}s; sha256 {hashlib.sha256(blob).hexdigest()[:16]}", flush=True)
                    success = True
                    return 0
            time.sleep(3)
        raise TimeoutError("distributed render timed out")
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


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="yura-sdxl-cluster-") as temp:
        try:
            return leader(Path(temp)) if INDEX == 0 else worker(Path(temp))
        except Exception as exc:
            print(f"::error::Cluster experiment failed: {type(exc).__name__}: {str(exc)[:200]}", flush=True)
            return 1


if __name__ == "__main__":
    sys.exit(main())
