#!/usr/bin/env python3
"""Eight-runner exact-attention microbenchmark, synthetic tensors only.

Queries are partitioned; every worker sees all keys and values, so this is the
same mathematical attention as the reference, not latent tiling. It is NOT a
full-model or image-generation benchmark. One job coordinates and computes one
slice; the remaining jobs expose only this bounded, token-authenticated route.
"""
from __future__ import annotations

import concurrent.futures
import hmac
import secrets
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import torch
import torch.nn.functional as functional
from safetensors.torch import load, save

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import sdxl_cluster_test as cluster  # noqa: E402

MAX_BODY = 24 * 1024 * 1024
COUNT = 8
ITERATIONS = 3


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect())


def attention(q: torch.Tensor, k: torch.Tensor, v: torch.Tensor) -> torch.Tensor:
    """Full-context, non-causal SDPA, partitionable along query tokens."""
    if any(not isinstance(t, torch.Tensor) or t.device.type != "cpu" or t.dtype != torch.float32 or t.ndim != 3 for t in (q, k, v)):
        raise ValueError("invalid attention tensor type")
    b, queries, channels = q.shape
    if b != 1 or not 1 <= queries <= 2304 or channels not in {640, 1280}:
        raise ValueError("unsupported query shape")
    if tuple(k.shape) != tuple(v.shape) or k.shape[0] != b or k.shape[2] != channels or not 1 <= k.shape[1] <= 2304:
        raise ValueError("unsupported key/value shape")
    if not all(torch.isfinite(t).all() for t in (q, k, v)):
        raise ValueError("non-finite tensor")
    heads = channels // 64
    qh = q.reshape(b, queries, heads, 64).transpose(1, 2)
    kh = k.reshape(b, k.shape[1], heads, 64).transpose(1, 2)
    vh = v.reshape(b, v.shape[1], heads, 64).transpose(1, 2)
    with torch.inference_mode():
        out = functional.scaled_dot_product_attention(qh, kh, vh, dropout_p=0.0, is_causal=False)
    return out.transpose(1, 2).reshape(b, queries, channels).contiguous()


def valid_url(url: str, token: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme != "https" or not (parts.hostname or "").lower().endswith(".trycloudflare.com") or
            parts.port not in {None, 443} or parts.username or parts.password or parts.query or parts.fragment or
            parts.path not in {"", "/"} or len(token) < 32 or len(token) > 256 or
            any(not (c.isalnum() or c in "-_") for c in token)):
        raise ValueError("invalid worker endpoint")
    return url.rstrip("/")


class AttentionServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, token: str):
        super().__init__(("127.0.0.1", 9008), Handler)
        self.token = token
        self.calls = 0
        self.lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        pass

    def do_POST(self):  # noqa: N802
        if self.path != "/attention":
            self.send_error(404)
            return
        if not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {self.server.token}"):
            self.send_error(401)
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.send_error(411)
            return
        if not 1 <= length <= MAX_BODY:
            self.send_error(413)
            return
        body = self.rfile.read(length)
        if len(body) != length:
            self.send_error(400)
            return
        try:
            tensors = load(body)
            if set(tensors) != {"q", "k", "v"}:
                raise ValueError("invalid tensor bundle")
            with self.server.lock:
                result = attention(tensors["q"], tensors["k"], tensors["v"])
                response = save({"output": result})
                self.server.calls += 1
        except Exception:  # noqa: BLE001 - no payload or internal details in responses
            self.send_error(400)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


def remote(worker: dict, q: torch.Tensor, k: torch.Tensor, v: torch.Tensor) -> torch.Tensor:
    url = valid_url(worker["url"], worker["token"])
    body = save({"q": q.contiguous(), "k": k.contiguous(), "v": v.contiguous()})
    if len(body) > MAX_BODY:
        raise ValueError("request too large")
    req = urllib.request.Request(url + "/attention", data=body, headers={
        "Authorization": f"Bearer {worker['token']}", "Content-Type": "application/octet-stream"})
    try:
        with OPENER.open(req, timeout=120) as response:  # noqa: S310 - validated HTTPS, redirects disabled
            raw = response.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            raise ValueError("response too large")
        value = load(raw)
        result = value.get("output")
        if set(value) != {"output"} or result is None or result.shape != q.shape or result.dtype != q.dtype or not torch.isfinite(result).all():
            raise ValueError("invalid worker response")
        return result
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise RuntimeError(f"attention worker failed: {type(exc).__name__}") from None


def distributed(workers: list[dict], q: torch.Tensor, k: torch.Tensor, v: torch.Tensor, count: int) -> torch.Tensor:
    chunks = torch.tensor_split(q, count, dim=1)
    with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(attention, chunks[0].contiguous(), k, v)]
        futures += [pool.submit(remote, workers[i - 1], chunks[i], k, v) for i in range(1, count)]
        parts = [future.result() for future in futures]
    return torch.cat(parts, dim=1)


def worker(temp: Path) -> int:
    proxy = None
    tunnel_output = None
    service = None
    thread = None
    name = f"attention-worker-{cluster.INDEX}.json"
    token = secrets.token_urlsafe(32)
    try:
        leader = cluster.wait_for("attention-leader.json")
        service = AttentionServer(token)
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        proxy, tunnel_output, url = cluster.tunnel(temp / "tunnel.log")
        cluster.write(name, cluster.seal(cluster.un64(leader["public"]), {"url": url, "token": token}))
        print(f"::notice::Attention worker {cluster.INDEX} ready", flush=True)
        deadline = time.monotonic() + 1200
        while time.monotonic() < deadline:
            done, _ = cluster.read("attention-done.json")
            if done is not None:
                if done.get("success") and service.calls < ITERATIONS:
                    raise RuntimeError("worker did not handle enough real requests")
                return 0 if done.get("success") else 1
            if proxy.poll() is not None:
                raise RuntimeError("worker tunnel exited")
            time.sleep(5)
        raise TimeoutError("attention coordinator timed out")
    finally:
        cluster.stop(proxy)
        if tunnel_output:
            tunnel_output.close()
        if service:
            service.shutdown()
            service.server_close()
            if thread:
                thread.join(timeout=5)
            print(f"::notice::Attention worker {cluster.INDEX} processed {service.calls} authenticated requests", flush=True)
        cluster.delete(name)


def leader(temp: Path) -> int:
    del temp
    private = cluster.X25519PrivateKey.generate()
    success = False
    try:
        cluster.write("attention-leader.json", {"public": cluster.b64(cluster.public(private))})
        workers = [cluster.open_envelope(private, cluster.wait_for(f"attention-worker-{i}.json", seconds=900))
                   for i in range(1, COUNT)]
        for row in workers:
            valid_url(row["url"], row["token"])
        if len({worker["url"] for worker in workers}) != COUNT - 1:
            raise ValueError("worker URLs must be distinct")
        torch.manual_seed(1234)
        for label, tokens, channels in (("48x48-self", 2304, 640), ("24x24-self", 576, 1280)):
            q, k, v = (torch.randn((1, tokens, channels), dtype=torch.float32) for _ in range(3))
            reference = attention(q, k, v)
            baseline, four, eight = [], [], []
            for _ in range(ITERATIONS):
                start = time.perf_counter()
                reference = attention(q, k, v)
                baseline.append(time.perf_counter() - start)
                for count, series in ((4, four), (8, eight)):
                    start = time.perf_counter()
                    result = distributed(workers, q, k, v, count)
                    series.append(time.perf_counter() - start)
                    if not torch.allclose(reference, result, atol=2e-5, rtol=2e-5):
                        raise ValueError("split attention differed from full attention")
            median = lambda values: sorted(values)[len(values) // 2]  # noqa: E731
            print(f"::notice::{label}: median local {median(baseline):.3f}s, 4-runner {median(four):.3f}s, 8-runner {median(eight):.3f}s; exactness tolerance 2e-5; {ITERATIONS} iterations", flush=True)
        success = True
        return 0
    finally:
        try:
            cluster.write("attention-done.json", {"success": success})
        except Exception:
            print("::warning::Could not notify attention workers", flush=True)
        end = time.monotonic() + 60
        while time.monotonic() < end:
            try:
                if all(cluster.read(f"attention-worker-{i}.json")[0] is None for i in range(1, COUNT)):
                    break
            except Exception:
                break
            time.sleep(3)
        cluster.delete("attention-leader.json")
        cluster.delete("attention-done.json")


def main() -> int:
    torch.set_num_threads(4)
    with tempfile.TemporaryDirectory(prefix="sdxl-attention-bench-") as temp:
        try:
            return leader(Path(temp)) if cluster.INDEX == 0 else worker(Path(temp))
        except Exception as exc:
            print(f"::error::Attention benchmark failed: {type(exc).__name__}: {str(exc)[:160]}", flush=True)
            return 1


if __name__ == "__main__":
    sys.exit(main())
