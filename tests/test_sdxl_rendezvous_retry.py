"""Transient coordination errors must not immediately kill SDXL workers."""
from __future__ import annotations

import importlib
import io
import sys
import urllib.error
from unittest.mock import patch

import pytest

pytest.importorskip("cryptography")


def test_github_rendezvous_retries_transient_http_error(monkeypatch):
    for key, value in {"GITHUB_RUN_ID": "123", "GITHUB_REF_NAME": "branch", "GITHUB_REPOSITORY": "owner/repo",
                       "GITHUB_TOKEN": "synthetic-token", "WORKER_INDEX": "1"}.items():
        monkeypatch.setenv(key, value)
    sys.modules.pop("scripts.sdxl_cluster_test", None)
    harness = importlib.import_module("scripts.sdxl_cluster_test")
    errors = [urllib.error.HTTPError("https://api.github.com", 504, "timeout", {}, None),
              urllib.error.HTTPError("https://api.github.com", 503, "unavailable", {}, None)]

    class Response:
        status = 200
        def __enter__(self):
            return io.BytesIO(b'{"ok": true}')
        def __exit__(self, *_):
            return False

    calls = []
    def open_url(*_args, **_kwargs):
        calls.append(1)
        if errors:
            raise errors.pop(0)
        return Response()

    # urllib's response context manager normally returns itself and status is
    # read from that object; the fake needs the same interface.
    class ResponseBody(io.BytesIO):
        status = 200

    def open_response(*args, **kwargs):
        response = open_url(*args, **kwargs)
        return ResponseBody(b'{"ok": true}') if isinstance(response, Response) else response

    with patch.object(harness.urllib.request, "urlopen", open_response), patch.object(harness.time, "sleep"):
        status, payload = harness.github("GET", "leader.json")
    assert status == 200 and payload == {"ok": True} and len(calls) == 3
    sys.modules.pop("scripts.sdxl_cluster_test", None)


def test_github_rendezvous_does_not_retry_auth_failure(monkeypatch):
    for key, value in {"GITHUB_RUN_ID": "123", "GITHUB_REF_NAME": "branch", "GITHUB_REPOSITORY": "owner/repo",
                       "GITHUB_TOKEN": "synthetic-token", "WORKER_INDEX": "1"}.items():
        monkeypatch.setenv(key, value)
    sys.modules.pop("scripts.sdxl_cluster_test", None)
    harness = importlib.import_module("scripts.sdxl_cluster_test")
    with patch.object(harness.urllib.request, "urlopen", side_effect=urllib.error.HTTPError("https://api.github.com", 403, "no", {}, None)) as opened, patch.object(harness.time, "sleep") as sleep:
        assert harness.github("GET", "leader.json") == (403, {})
        assert opened.call_count == 1 and sleep.call_count == 0
    sys.modules.pop("scripts.sdxl_cluster_test", None)
