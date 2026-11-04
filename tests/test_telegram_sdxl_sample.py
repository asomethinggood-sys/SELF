"""Safety checks for the opt-in synthetic Telegram sample."""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from scripts import probe_telegram_sdxl_sample as probe


@pytest.mark.asyncio
@pytest.mark.parametrize("account_id", [probe.OWNER_ID, 8384455313])
async def test_sample_targets_only_verified_owner(tmp_path, monkeypatch, account_id):
    image = b"\x89PNG\r\n\x1a\n" + b"synthetic"
    sample = tmp_path / "bf16.png"
    sample.write_bytes(image)
    monkeypatch.setattr(probe, "SAMPLE", sample)
    monkeypatch.setattr(probe, "SHA256_PREFIX", hashlib.sha256(image).hexdigest()[:12])
    monkeypatch.setattr(probe, "load_config", lambda: (object(), None))
    sent = []

    class Client:
        async def get_entity(self, username):
            assert username == "edited_by_nyx"
            return SimpleNamespace(id=probe.OWNER_ID)

        async def send_file(self, recipient, filename, **kwargs):
            sent.append((recipient, filename, kwargs))
            return SimpleNamespace(id=9)

        async def get_messages(self, recipient, ids):
            assert ids == 9
            return SimpleNamespace(photo=object())

    class Service:
        def __init__(self, *_):
            self.client = Client()

        async def authorize(self, **_):
            return SimpleNamespace(user_id=account_id)

        async def disconnect(self):
            pass

    monkeypatch.setattr(probe, "TelegramService", Service)
    await probe.main()
    recipient = sent[0][0]
    assert recipient == "me" if account_id == probe.OWNER_ID else recipient.id == probe.OWNER_ID
    assert "Experimental" in sent[0][2]["caption"]


@pytest.mark.asyncio
async def test_sample_refuses_unverified_file(tmp_path, monkeypatch):
    sample = tmp_path / "bf16.png"
    sample.write_bytes(b"not the verified png")
    monkeypatch.setattr(probe, "SAMPLE", sample)
    with pytest.raises(ValueError, match="does not match"):
        await probe.main()
