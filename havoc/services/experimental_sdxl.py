"""Temporary, opt-in SDXL service client; never changes the normal generator."""
from __future__ import annotations

import asyncio
import json
import re
import secrets
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.sdxl_oidc import _OPENER, get_token

from .imagegen import ImageGenerator


class ExperimentalError(Exception):
    pass


def _discover(directory: Any) -> dict[str, Any]:
    override = getattr(directory, "override", None)
    if isinstance(override, dict) and isinstance(override.get("sdxl-experimental"), dict):
        item = override["sdxl-experimental"]
    else:
        branch = str(getattr(directory, "branch", ""))
        if not re.fullmatch(r"[a-zA-Z0-9_./-]{1,150}", branch) or ".." in branch:
            raise ExperimentalError("Experimental image service is unavailable here.")
        directory.pull_now(force=True)
        result = subprocess.run(  # noqa: S603 - fixed binary; validated branch
            ["git", "-C", str(directory.root), "show", f"origin/{branch}:.services/sdxl-experimental.json"],
            capture_output=True, timeout=20, check=False,
        )
        if result.returncode or len(result.stdout) > 4096:
            raise ExperimentalError("Experimental image service is offline or starting. Use .draw for the normal generator.")
        try:
            item = json.loads(result.stdout)
        except ValueError:
            raise ExperimentalError("Experimental image service is offline. Use .draw for the normal generator.") from None
    if not isinstance(item, dict):
        raise ExperimentalError("Experimental image service is offline. Use .draw for the normal generator.")
    return item


def _endpoint(directory: Any) -> str:
    item = _discover(directory)
    try:
        url = item["url"]
        if not isinstance(url, str) or not re.fullmatch(r"https://[a-z0-9-]{1,100}\.trycloudflare\.com", url):
            raise ValueError("invalid endpoint")
        started = datetime.fromisoformat(item["started_at"])
        expires = datetime.fromisoformat(item["expires_at"])
        now = datetime.now(UTC)
        if not (started <= now < expires and (expires - started).total_seconds() <= 4000):
            raise ValueError("expired endpoint")
    except (ValueError, TypeError, KeyError):
        raise ExperimentalError("Experimental image service is offline or expired. Use .draw for the normal generator.") from None
    return url


class Client:
    def __init__(self, base: str):
        self.base = base
        self._token = ""
        self._token_time = 0.0

    def request(self, path: str, *, data: dict | None = None, timeout: int = 20) -> bytes:
        if time.monotonic() - self._token_time >= 120 or not self._token:
            self._token = get_token()
            self._token_time = time.monotonic()
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(self.base + path, data=body, headers={
            "Authorization": f"Bearer {self._token}", "Content-Type": "application/json",
        })
        try:
            with _OPENER.open(req, timeout=timeout) as response:  # noqa: S310 - discovery validates the HTTPS tunnel, redirects disabled
                result = response.read(16 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise ExperimentalError("The experimental image queue is full; try again shortly.") from None
            raise ExperimentalError(f"The experimental image service refused the request (HTTP {exc.code}).") from None
        except (OSError, urllib.error.URLError):
            raise ExperimentalError("The experimental image service stopped responding.") from None
        if len(result) > 16 * 1024 * 1024:
            raise ExperimentalError("The experimental image response was too large.")
        return result


def _render(directory: Any, prompt: str, seed: int, output: Path) -> Path:
    if not isinstance(prompt, str) or not 1 <= len(prompt) <= 1500:
        raise ExperimentalError("Please provide a prompt of 1–1500 characters.")
    if type(seed) is not int or not 0 <= seed < 2**64:
        raise ExperimentalError("Seed must be between 0 and 2⁶⁴−1.")
    client = Client(_endpoint(directory))
    generator = ImageGenerator(checkpoint="DreamShaperXL_Lightning.safetensors", width=768, height=768)
    graph, _, _ = generator._builtin(prompt, seed, steps=4)
    graph["3"]["inputs"].update({"cfg": 1.0, "scheduler": "sgm_uniform"})
    try:
        response = json.loads(client.request("/prompt", data={"prompt": graph}, timeout=30))
        ident = response.get("prompt_id", "")
        if not isinstance(ident, str) or not re.fullmatch(r"[a-zA-Z0-9-]{1,80}", ident):
            raise ExperimentalError("The experimental service returned an invalid job ID.")
        deadline = time.monotonic() + 300
        try:
            while time.monotonic() < deadline:
                time.sleep(3)
                entry = json.loads(client.request("/history/" + ident)).get(ident, {})
                if entry.get("status", {}).get("status_str") == "error":
                    raise ExperimentalError("The experimental render failed. Normal .draw remains available.")
                for node in entry.get("outputs", {}).values():
                    for image in node.get("images", []):
                        filename = image.get("filename", "")
                        if not isinstance(filename, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,200}\.png", filename):
                            raise ExperimentalError("The experimental output name was invalid.")
                        query = urllib.parse.urlencode({"filename": filename, "subfolder": "", "type": "output"})
                        raw = client.request("/view?" + query, timeout=60)
                        if not raw.startswith(b"\x89PNG\r\n\x1a\n") or len(raw) < 1024:
                            raise ExperimentalError("The experimental output was not a valid PNG.")
                        output.mkdir(parents=True, exist_ok=True)
                        path = output / f"experimental-{secrets.token_hex(12)}.png"
                        path.write_bytes(raw)
                        return path
            raise ExperimentalError("The experimental render timed out. Normal .draw remains available.")
        finally:
            # Only remove this request if still queued; do not interrupt another owner's running image.
            try:
                client.request("/queue", data={"delete": [ident]}, timeout=10)
            except Exception:  # noqa: BLE001 - cleanup cannot hide the real result
                pass
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ExperimentalError("The experimental image service returned an invalid response.") from None


async def generate(directory: Any, prompt: str, *, seed: int | None, outdir: Path) -> Path:
    return await asyncio.to_thread(_render, directory, prompt, secrets.randbits(32) if seed is None else seed, outdir)
