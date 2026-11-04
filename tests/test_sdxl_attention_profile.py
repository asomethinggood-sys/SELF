"""Attention instrumentation must delegate without changing the computation."""
from types import SimpleNamespace

from scripts.sdxl_attention_profile import ProfileSDXLAttention


class Model:
    def __init__(self):
        self.model_options = {"transformer_options": {}}
    def clone(self):
        return Model()
    def set_model_unet_function_wrapper(self, callback):
        self.wrapper = callback


def test_profile_delegates_attention_and_records_metadata(capsys):
    model, = ProfileSDXLAttention().patch(Model())
    tensor = SimpleNamespace(shape=(1, 2304, 640), dtype="float32")
    calls = []

    def original(q, k, v, heads, **kwargs):
        calls.append((q, k, v, heads, kwargs))
        return "unchanged"

    override = model.model_options["transformer_options"]["optimized_attention_override"]
    def apply(_x, _sigma, **_conditions):
        return override(original, tensor, tensor, tensor, 10, mask=None)

    result = model.wrapper(apply, {"input": 1, "timestep": 2, "c": {}})
    assert result == "unchanged" and len(calls) == 1
    output = capsys.readouterr().out
    assert "SDXL_ATTENTION_PROFILE" in output
    assert '"calls":1' in output
    assert '"heads":10' in output
