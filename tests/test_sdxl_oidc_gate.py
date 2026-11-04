"""Experimental cluster gate and token checks fail closed."""
from __future__ import annotations

import base64
import json
import time

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from scripts.sdxl_oidc import AUDIENCE, ISSUER, verify
from scripts.sdxl_oidc_gate import allowed, permitted_body


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _token(private, claims):
    header = _b64(json.dumps({"alg": "RS256", "kid": "one"}).encode())
    body = _b64(json.dumps(claims).encode())
    signature = private.sign(f"{header}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{body}.{_b64(signature)}"


def test_oidc_verifier_requires_signature_repository_branch_and_workflow():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = private.public_key().public_numbers()
    def bignum(num):
        return _b64(num.to_bytes((num.bit_length() + 7) // 8, "big"))
    keys = [{"kty": "RSA", "kid": "one", "e": bignum(pub.e), "n": bignum(pub.n)}]
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "repository": "owner/repo", "ref": "refs/heads/branch",
              "workflow_ref": "owner/repo/.github/workflows/auth-and-run.yml@refs/heads/branch",
              "iat": now, "nbf": now, "exp": now + 300}
    scope = {"repository": "owner/repo", "branch": "branch", "workflows": {"auth-and-run.yml"}}
    token = _token(private, claims)
    assert verify(token, keys, **scope)
    assert not verify(token, keys, **{**scope, "branch": "other"})
    assert not verify(token, keys, **{**scope, "workflows": {"other.yml"}})
    assert not verify(_token(private, {**claims, "exp": now - 1}), keys, **scope)
    assert not verify(token, [], **scope)


def test_gateway_only_forwards_expected_image_routes():
    assert allowed("POST", "/prompt")
    assert allowed("GET", "/history/123-abcd")
    assert allowed("GET", "/view?filename=example.png&type=output")
    assert not allowed("POST", "/sdxl-worker/predict")
    assert not allowed("GET", "/queue")
    assert not allowed("POST", "/prompt?x=y")
    assert not allowed("GET", "/history/../../prompt")
    assert not allowed("GET", "/view?filename=../../secret.png")
    assert not allowed("GET", "/view?filename=example.png&subfolder=secrets")


def test_gateway_restricts_prompt_to_fixed_distributed_graph():
    from havoc.services.imagegen import ImageGenerator

    generator = ImageGenerator(checkpoint="DreamShaperXL_Lightning.safetensors", width=768, height=768)
    graph, _, _ = generator._builtin("a lighthouse", 42, steps=4)
    graph["3"]["inputs"].update({"cfg": 1.0, "scheduler": "sgm_uniform", "model": ["10", 0]})
    graph["10"] = {"class_type": "FourWorkerSDXL", "inputs": {"model": ["4", 0], "overlap": 8}}
    encode = lambda: json.dumps({"prompt": graph}).encode()  # noqa: E731
    assert permitted_body("/prompt", encode())
    graph["10"]["class_type"] = "ArbitraryCustomNode"
    assert not permitted_body("/prompt", encode())
    assert permitted_body("/queue", b'{"delete":["123-abcd"]}')
    assert not permitted_body("/queue", b'{"clear":true}')
