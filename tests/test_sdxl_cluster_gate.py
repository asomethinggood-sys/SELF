"""The experimental tunnel must not expose the rest of ComfyUI."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_gate_rejects_unrelated_paths_and_missing_credentials():
    token = "a" * 43
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 9008)) == 0:
            pytest.skip("experimental gate port already in use")
    process = subprocess.Popen(  # noqa: S603 - fixed repository test target
        [sys.executable, str(ROOT / "scripts" / "sdxl_cluster_gate.py")],
        env={**os.environ, "SDXL_WORKER_TOKEN": token},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", 9008), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("gate did not start")
        for path, headers, expected in (
            ("/prompt", {"Authorization": f"Bearer {token}"}, 404),
            ("/sdxl-worker/predict", {}, 401),
            ("/sdxl-worker/warm", {"Authorization": f"Bearer {token}"}, 404),
        ):
            request = urllib.request.Request("http://127.0.0.1:9008" + path, data=b"{}", headers=headers)
            with pytest.raises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=2)  # noqa: S310 - test server
            assert caught.value.code == expected
    finally:
        process.terminate()
        process.wait(timeout=5)
