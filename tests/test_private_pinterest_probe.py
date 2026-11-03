"""The hosted link handoff must not put plaintext URLs in the repository."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("cryptography")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import private_pinterest_probe as probe  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402


def test_envelope_hides_urls_and_round_trips(tmp_path: Path) -> None:
    private = X25519PrivateKey.generate()
    key = tmp_path / "key.json"
    key.write_text(json.dumps({"current": {"run_id": "123", "extra": {"public_key": probe.b64(probe.public_bytes(private.public_key()))}}}))
    links = ["https://pin.it/secretA", "https://pin.it/secretB"]
    plain = tmp_path / "links.json"
    plain.write_text(json.dumps(links))
    envelope = tmp_path / "envelope.json"
    probe.seal(key, plain, envelope)
    assert "secretA" not in envelope.read_text()
    encoded = json.loads(envelope.read_text())
    public = X25519PublicKey.from_public_bytes(probe.unb64(encoded["public_key"]))
    data = AESGCM(probe.derive(private.exchange(public))).decrypt(probe.unb64(encoded["nonce"]), probe.unb64(encoded["ciphertext"]), b"123")
    assert json.loads(data) == links
