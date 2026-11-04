"""Coordinator failover never blends a partial denoising step."""
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

pytest.importorskip("aiohttp")

from scripts.sdxl_cluster import coordinator


class Model:
    def clone(self):
        return self

    def set_model_unet_function_wrapper(self, callback):
        self.wrapper = callback


def test_worker_failure_latches_full_frame_fallback(monkeypatch):
    monkeypatch.setenv("SDXL_FALLBACK_LOCAL", "1")
    monkeypatch.setattr(coordinator, "_workers", lambda: [{"url": "x", "token": "x"}] * 3)
    monkeypatch.setitem(__import__("sys").modules, "torch", SimpleNamespace(inference_mode=nullcontext))
    calls = []

    def fail(*args, **kwargs):
        calls.append("distributed")
        raise coordinator.WorkerError("offline")

    monkeypatch.setattr(coordinator, "predict_four", fail)
    model, = coordinator.FourWorkerSDXL().patch(Model(), 8)
    local = []

    def apply(data, sigma, **conditions):
        local.append((data, sigma, conditions))
        return "full prediction"

    args = {"input": "entire latent", "timestep": "sigma", "c": {}}
    assert model.wrapper(apply, args) == "full prediction"
    assert model.wrapper(apply, args) == "full prediction"
    assert calls == ["distributed"]
    assert len(local) == 2 and local[0][0] == "entire latent"


def test_fallback_is_opt_in(monkeypatch):
    monkeypatch.delenv("SDXL_FALLBACK_LOCAL", raising=False)
    monkeypatch.setattr(coordinator, "_workers", lambda: [{"url": "x", "token": "x"}] * 3)
    def fail(*args, **kwargs):
        raise coordinator.WorkerError("offline")
    monkeypatch.setattr(coordinator, "predict_four", fail)
    model, = coordinator.FourWorkerSDXL().patch(Model(), 8)
    with pytest.raises(coordinator.WorkerError):
        model.wrapper(lambda *_a, **_kw: None, {"input": None, "timestep": None, "c": {}})
