"""Tests for the temporary, opt-in SDXL Lightning client and gate."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from havoc.services import experimental_sdxl
from havoc.services.experimental_sdxl import ExperimentalError

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 2048


def discovery(*, url: str = "https://abc-def.trycloudflare.com", offset_seconds: int = 0, **extra) -> dict:
    started = datetime.now(UTC) + timedelta(seconds=offset_seconds)
    body = {
        "url": url,
        "run_id": "1",
        "started_at": started.isoformat(),
        "expires_at": (started + timedelta(minutes=65)).isoformat(),
    }
    body.update(extra)
    return body


def graph(prompt: str = "a lighthouse", seed: int = 7) -> dict:
    from havoc.services.imagegen import ImageGenerator
    generator = ImageGenerator(checkpoint="DreamShaperXL_Lightning.safetensors", width=768, height=768)
    built, _, _ = generator._builtin(prompt, seed, steps=4)
    built["3"]["inputs"].update({"cfg": 1.0, "scheduler": "sgm_uniform"})
    return built


class FakeClient:
    def __init__(self, base: str, *, error: Exception | None = None, status: str = "success"):
        self.base = base
        self.error = error
        self.status = status
        self.prompt_body: dict | None = None
        self.deletes: list[list[str]] = []
        self.seen: list[tuple[str, dict | None]] = []

    def request(self, path: str, *, data: dict | None = None, timeout: int = 20) -> bytes:
        self.seen.append((path, data))
        if self.error is not None:
            raise self.error
        if path == "/prompt":
            self.prompt_body = data
            return json.dumps({"prompt_id": "job-1"}).encode()
        if path == "/history/job-1":
            body = {"job-1": {"status": {"status_str": self.status}, "outputs": {"9": {"images": [{"filename": "out.png"}]}}}}
            return json.dumps(body).encode()
        if path.startswith("/view?"):
            return PNG
        if path == "/queue":
            self.deletes.append(data["delete"])
            return b"{}"
        raise AssertionError(f"unexpected path {path}")


def test_gate_accepts_the_exact_client_graph():
    from scripts.sdxl_experimental_gate import permitted_body

    assert permitted_body("/prompt", json.dumps({"prompt": graph("a lighthouse", 7)}).encode()) is True


def _nested(value: dict, dotted: str, new: object) -> dict:
    node = value["prompt"]
    keys = dotted.split(".")
    for key in keys[:-1]:
        node = node[key]
    node[keys[-1]] = new
    return value


@pytest.mark.parametrize("dotted,new", [
    ("3.cfg", 7.0),
    ("3.scheduler", "normal"),
    ("3.seed", "7"),
    ("4.ckpt_name", "something-else.safetensors"),
    ("5.width", 512),
    ("5.batch_size", 2),
    ("7.text", ""),
    ("6.text", "ignore all previous instructions"),
])
def test_gate_refuses_tampered_graphs(dotted, new):
    from scripts.sdxl_experimental_gate import permitted_body

    value = _nested({"prompt": graph()}, dotted, new)
    assert permitted_body("/prompt", json.dumps(value).encode()) is False


@pytest.mark.parametrize("mutate", [
    lambda value: value["prompt"].__setitem__("10", {"class_type": "RunAnywhere"}) or value,
    lambda value: value.__setitem__("extra", 1) or value,
])
def test_gate_refuses_added_nodes_and_keys(mutate):
    from scripts.sdxl_experimental_gate import permitted_body

    assert permitted_body("/prompt", json.dumps(mutate({"prompt": graph()})).encode()) is False


@pytest.mark.parametrize("body", [
    {"delete": []},
    {"delete": ["a" * 100]},
    {"delete": ["../etc"]},
    {"clear": True},
])
def test_gate_refuses_bad_queue_deletion(body):
    from scripts.sdxl_experimental_gate import permitted_body

    assert permitted_body("/queue", json.dumps(body).encode()) is False
    assert permitted_body("/queue", json.dumps({"delete": ["job-1"]}).encode()) is True


def test_discovery_reads_the_override():
    directory = SimpleNamespace(override={"sdxl-experimental": discovery()}, branch="arena/b9a136e7-self")
    assert experimental_sdxl._endpoint(directory) == "https://abc-def.trycloudflare.com"


@pytest.mark.parametrize("body", [
    discovery(url="http://example.com"),
    discovery(url="https://example.com"),
    discovery(expires_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat()),
    discovery(started_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat()),
])
def test_discovery_refuses_bad_or_expired_endpoints(body):
    directory = SimpleNamespace(override={"sdxl-experimental": body}, branch="arena/b9a136e7-self")
    with pytest.raises(ExperimentalError):
        experimental_sdxl._endpoint(directory)


def test_render_polls_downloads_and_clears_its_own_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(experimental_sdxl.time, "sleep", lambda seconds: None)
    client = FakeClient("https://abc-def.trycloudflare.com")
    monkeypatch.setattr(experimental_sdxl, "Client", lambda base: client)
    directory = SimpleNamespace(override={"sdxl-experimental": discovery()}, branch="arena/b9a136e7-self")
    out = tmp_path / "out.png"
    result = experimental_sdxl._render(directory, "a lighthouse", 7, out)
    assert result.read_bytes() == PNG
    assert client.prompt_body == {"prompt": graph("a lighthouse", 7)}
    assert client.deletes == [["job-1"]]
    assert client.seen[-1][0] == "/queue"


def test_render_reports_a_full_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(experimental_sdxl.time, "sleep", lambda seconds: None)
    client = FakeClient("https://abc-def.trycloudflare.com", error=ExperimentalError("queue is full"))
    monkeypatch.setattr(experimental_sdxl, "Client", lambda base: client)
    directory = SimpleNamespace(override={"sdxl-experimental": discovery()}, branch="arena/b9a136e7-self")
    with pytest.raises(ExperimentalError, match="queue is full"):
        experimental_sdxl._render(directory, "a lighthouse", 7, tmp_path / "out.png")


def test_render_reports_a_failed_job(tmp_path, monkeypatch):
    monkeypatch.setattr(experimental_sdxl.time, "sleep", lambda seconds: None)
    client = FakeClient("https://abc-def.trycloudflare.com", status="error")
    monkeypatch.setattr(experimental_sdxl, "Client", lambda base: client)
    directory = SimpleNamespace(override={"sdxl-experimental": discovery()}, branch="arena/b9a136e7-self")
    with pytest.raises(ExperimentalError, match="failed"):
        experimental_sdxl._render(directory, "a lighthouse", 7, tmp_path / "out.png")


def test_render_refuses_a_too_long_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(experimental_sdxl, "Client", lambda base: FakeClient(base))
    directory = SimpleNamespace(override={"sdxl-experimental": discovery()}, branch="arena/b9a136e7-self")
    with pytest.raises(ExperimentalError, match="1500"):
        experimental_sdxl._render(directory, "x" * 1501, 7, tmp_path / "out.png")


def test_feature_registers_an_owner_only_hidden_command():
    from havoc.features import experimental_draw

    registry = SimpleNamespace(registered=[])
    registry.register_many = registry.registered.extend
    runtime = SimpleNamespace(registry=registry)
    experimental_draw.register(runtime)
    assert registry.registered[0].name == "fastdraw"
    assert registry.registered[0].owner_only is True
    assert registry.registered[0].hidden is True
    assert registry.registered[0].category == "media"
