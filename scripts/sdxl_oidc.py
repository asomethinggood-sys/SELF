"""GitHub Actions OIDC authentication for experimental picture services.

No repository secret is needed: short-lived tokens are minted by the runner.
Verification pins GitHub's issuer/JWKS URL and the repo, branch, audience and
workflow paths. Never log tokens or their claims.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.parse
import urllib.request
from typing import Any

AUDIENCE = "urn:yura:sdxl-cluster:v1"
ISSUER = "https://token.actions.githubusercontent.com"
JWKS_URL = ISSUER + "/.well-known/jwks"


def _un64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def get_token(audience: str = AUDIENCE) -> str:
    request_url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    request_token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not request_url or not request_token:
        raise ValueError("GitHub Actions OIDC is not enabled for this job")
    parts = urllib.parse.urlsplit(request_url)
    # GitHub supplies this runtime URL. Never send the request token to a value
    # set by an untrusted caller or an insecure endpoint.
    if (parts.scheme != "https" or not parts.hostname or
            not parts.hostname.lower().endswith(".actions.githubusercontent.com") or
            parts.username or parts.password):
        raise ValueError("invalid GitHub Actions token endpoint")
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query.append(("audience", audience))
    url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), ""))
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {request_token}"})
    with urllib.request.urlopen(req, timeout=20) as response:  # noqa: S310 - GitHub-provided runtime endpoint
        data = json.load(response)
    token = data.get("value")
    if not isinstance(token, str) or len(token) > 16000:
        raise ValueError("invalid OIDC response")
    return token


def github_keys() -> list[dict[str, Any]]:
    req = urllib.request.Request(JWKS_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as response:  # noqa: S310 - fixed GitHub endpoint
        data = json.load(response)
    keys = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(keys, list):
        raise ValueError("invalid GitHub signing keys")
    return keys


def verify(token: str, keys: list[dict[str, Any]], *, repository: str, branch: str,
           workflows: set[str], audience: str = AUDIENCE, now: float | None = None) -> bool:
    """Verify signature and scope; any malformed or untrusted token is rejected."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    try:
        if not isinstance(token, str) or len(token) > 16000:
            return False
        header_part, payload_part, signature_part = token.split(".")
        header = json.loads(_un64(header_part))
        claims = json.loads(_un64(payload_part))
        if not isinstance(header, dict) or not isinstance(claims, dict) or header.get("alg") != "RS256":
            return False
        matching = [k for k in keys if k.get("kid") == header.get("kid") and k.get("kty") == "RSA"]
        if len(matching) != 1:
            return False
        item = matching[0]
        numbers = rsa.RSAPublicNumbers(int.from_bytes(_un64(item["e"]), "big"), int.from_bytes(_un64(item["n"]), "big"))
        numbers.public_key().verify(_un64(signature_part), f"{header_part}.{payload_part}".encode(), padding.PKCS1v15(), hashes.SHA256())
        moment = time.time() if now is None else now
        if claims.get("iss") != ISSUER or claims.get("aud") != audience:
            return False
        if claims.get("repository", "").lower() != repository.lower() or claims.get("ref") != f"refs/heads/{branch}":
            return False
        expected = {f"{repository}/.github/workflows/{name}@refs/heads/{branch}".lower() for name in workflows}
        if str(claims.get("workflow_ref", "")).lower() not in expected:
            return False
        exp, nbf, issued = int(claims["exp"]), int(claims.get("nbf", 0)), int(claims["iat"])
        if not (nbf - 30 <= moment < exp and issued - 30 <= moment and exp - issued <= 900):
            return False
        return True
    except (ValueError, TypeError, KeyError, AttributeError, InvalidSignature):
        return False
