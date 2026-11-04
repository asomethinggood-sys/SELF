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


def test_exact_first_step_then_distributed_steps(monkeypatch):
    class Scalar:
        def __init__(self, value):
            self.value = value
        def flatten(self):
            return [self]
        def numel(self):
            return 1
        def to(self, _other):
            return self

    fake_torch = SimpleNamespace(Tensor=Scalar, inference_mode=nullcontext,
                                 isclose=lambda a, b, **_kw: a.value == b.value)
    monkeypatch.setitem(__import__("sys").modules, "torch", fake_torch)
    monkeypatch.setattr(coordinator, "_workers", lambda: [{"url": "x", "token": "x"}] * 3)
    remote_calls = []
    monkeypatch.setattr(coordinator, "predict", lambda *_a: remote_calls.append(1) or "remote")

    def dispatch(_x, run, *, overlap):
        for index in range(4):
            run(SimpleNamespace(index=index, top=0, left=0), "tile")
        return "combined"

    monkeypatch.setattr(coordinator, "predict_four", dispatch)
    model, = coordinator.FourWorkerSDXL().patch(Model(), 16, global_weight=1.0,
                                                global_first_only=True, global_full_res=True)
    local = []
    def apply(data, sigma, **conditions):
        local.append(data)
        return "local"

    args = {"input": "full", "timestep": Scalar(10), "c": {"transformer_options": {"sample_sigmas": Scalar(10)}}}
    assert model.wrapper(apply, args) == "local"
    assert local == ["full"] and not remote_calls
    args["timestep"] = Scalar(5)
    assert model.wrapper(apply, args) == "combined"
    assert local == ["full", "tile"] and len(remote_calls) == 3


def test_fallback_is_opt_in(monkeypatch):
    monkeypatch.delenv("SDXL_FALLBACK_LOCAL", raising=False)
    monkeypatch.setattr(coordinator, "_workers", lambda: [{"url": "x", "token": "x"}] * 3)
    def fail(*args, **kwargs):
        raise coordinator.WorkerError("offline")
    monkeypatch.setattr(coordinator, "predict_four", fail)
    model, = coordinator.FourWorkerSDXL().patch(Model(), 8)
    with pytest.raises(coordinator.WorkerError):
        model.wrapper(lambda *_a, **_kw: None, {"input": None, "timestep": None, "c": {}})
