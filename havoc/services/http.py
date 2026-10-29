"""Minimal async HTTP JSON client built on :mod:`urllib`.

HAVOC avoids heavyweight HTTP dependencies: optional integrations only need
``GET``/``POST`` JSON with a timeout. Requests run in a worker thread so the
event loop is never blocked, and every failure maps to a typed exception the
caller can localize.

Feature areas: 61, 70 (optional providers), 10/84 (timeouts, graceful
degradation).
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: The name this client calls itself. Not "HAVOC…": the CDN in front of
#: openrouter.ai answers 403 with an HTML error page for any User-Agent that
#: contains that word (measured from a GitHub runner: "HAVOC/2.0" and
#: "havoc/2.0" are both refused, "Yura/2.0" is accepted, so are curl and
#: python-urllib). A blocked request looks like a bad key when it is not.
USER_AGENT = "Yura/2.0 (+https://github.com/asomethinggood-sys/SELF)"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class HttpError(Exception):
    """Network/protocol failure with a machine-readable ``kind``."""

    def __init__(self, message: str, *, kind: str = "http_error", status: int | None = None) -> None:
        self.kind = kind
        self.status = status
        super().__init__(message)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpError(f"invalid JSON response: {exc}", kind="bad_json", status=self.status) from exc

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


def _request_sync(
    method: str,
    url: str,
    *,
    data: bytes | None,
    headers: Mapping[str, str],
    timeout: float,
) -> HttpResponse:
    request = urllib.request.Request(url, data=data, method=method.upper())
    request.add_header("User-Agent", USER_AGENT)
    for key, value in headers.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - explicit http(s) only
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise HttpError("response too large", kind="too_large", status=response.status)
            return HttpResponse(response.status, body, dict(response.headers))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(MAX_RESPONSE_BYTES).decode("utf-8", errors="replace")[:500]
        except Exception:  # pragma: no cover - best effort
            pass
        kind = "rate_limited" if exc.code == 429 else "unauthorized" if exc.code in (401, 403) else "http_error"
        raise HttpError(f"HTTP {exc.code}: {detail or exc.reason}", kind=kind, status=exc.code) from exc
    except urllib.error.URLError as exc:
        raise HttpError(f"network error: {exc.reason}", kind="network") from exc
    except TimeoutError as exc:
        raise HttpError("request timed out", kind="timeout") from exc


async def request(
    method: str,
    url: str,
    *,
    json_body: Any | None = None,
    form: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 15.0,
) -> HttpResponse:
    if not url.lower().startswith(("http://", "https://")):
        raise HttpError("only http(s) URLs are allowed", kind="bad_url")

    merged_headers = {"Accept": "application/json"}
    data: bytes | None = None
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        merged_headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        merged_headers["Content-Type"] = "application/x-www-form-urlencoded"
    merged_headers.update(headers or {})
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                _request_sync, method, url, data=data, headers=merged_headers, timeout=timeout
            ),
            timeout=timeout + 5,
        )
    except TimeoutError as exc:
        raise HttpError("request timed out", kind="timeout") from exc


async def get_json(url: str, *, params: Mapping[str, str] | None = None, timeout: float = 15.0, headers: Mapping[str, str] | None = None) -> Any:
    if params:
        query = urllib.parse.urlencode(params)
        url = f"{url}{'&' if '?' in url else '?'}{query}"
    response = await request("GET", url, timeout=timeout, headers=headers)
    return response.json()


async def post_json(url: str, body: Any, *, timeout: float = 30.0, headers: Mapping[str, str] | None = None) -> Any:
    response = await request("POST", url, json_body=body, timeout=timeout, headers=headers)
    return response.json()
