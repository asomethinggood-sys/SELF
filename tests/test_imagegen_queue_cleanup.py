"""A failed client removes only its own pending ComfyUI job."""
from __future__ import annotations

import pytest

from havoc.services import imagegen


async def test_timeout_deletes_only_its_pending_prompt(monkeypatch, tmp_path):
    calls = []

    async def post(url, payload, **kwargs):
        calls.append((url, payload))
        return {"prompt_id": "my-job"} if url.endswith("/prompt") else {}

    async def fail(*args, **kwargs):
        raise imagegen.ImageError("timed out", kind="timeout")

    async def checkpoint(*args, **kwargs):
        return "test.safetensors"

    monkeypatch.setattr(imagegen, "post_json", post)
    monkeypatch.setattr(imagegen.ImageGenerator, "_wait", fail)
    monkeypatch.setattr(imagegen.ImageGenerator, "pick_checkpoint", checkpoint)
    generator = imagegen.ImageGenerator(url="http://localhost:8188")
    with pytest.raises(imagegen.ImageError, match="timed out"):
        await generator.generate("test", outdir=tmp_path)
    assert calls[-1] == ("http://localhost:8188/queue", {"delete": ["my-job"]})
