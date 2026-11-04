"""Experimental authenticated SDXL tile calls; not wired to production."""
from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import wire


class WorkerError(RuntimeError):
    """A worker failed, refused a call, or returned invalid data."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A tunnel or intermediary must never redirect the worker credential or
        # conditioning tensors to a different host, even another HTTPS host.
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def validate_worker(url: str, token: str, *, local_test: bool = False) -> str:
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    allowed_local = local_test and parts.scheme == "http" and host in {"127.0.0.1", "localhost", "::1"}
    if not ((parts.scheme == "https" and host.endswith(".trycloudflare.com")) or allowed_local):
        raise ValueError("worker must use a Cloudflare HTTPS tunnel")
    if (parts.username or parts.password or parts.query or parts.fragment or parts.path not in {"", "/"}
            or (not allowed_local and parts.port not in {None, 443})):
        raise ValueError("invalid worker URL")
    if len(token) < 32 or len(token) > 256 or any(not (c.isalnum() or c in "-_") for c in token):
        raise ValueError("invalid worker token")
    return url.rstrip("/")


def predict(url: str, token: str, latent: Any, sigma: Any, conditioning: dict, *, timeout: float = 180, local_test: bool = False) -> Any:
    base = validate_worker(url, token, local_test=local_test)
    body = wire.encode({"latent": latent, "sigma": sigma, "conditioning": conditioning})
    req = urllib.request.Request(base + "/sdxl-worker/predict", data=body, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
    })
    try:
        with _OPENER.open(req, timeout=max(1, min(timeout, 300))) as response:  # noqa: S310 - strict URL validation above
            data = response.read(wire.MAX_BODY + 1)
    except (OSError, urllib.error.URLError) as exc:
        # Do not log the URL, token, or any response body: the worker is private.
        raise WorkerError(f"worker request failed: {type(exc).__name__}") from exc
    try:
        result = wire.decode(data)
    except (ValueError, TypeError) as exc:
        raise WorkerError("worker returned an invalid tensor message") from exc
    if not isinstance(result, dict) or "prediction" not in result:
        raise WorkerError("worker returned no prediction")
    return result["prediction"]
