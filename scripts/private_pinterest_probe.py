#!/usr/bin/env python3
"""One-shot encrypted handoff of user-supplied Pinterest links to hosted CI.

The CI runner publishes an ephemeral X25519 public key, never its private key.
The sender encrypts links to that key; only ciphertext is committed. Logs contain
only diagnostic shapes and verdicts, never link URLs or metadata.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from publish_status import build, publish  # noqa: E402

from havoc.services.downloader import _gallery_run, discard, fetch, inspect, validate_file  # noqa: E402

PUB_PATH = ".reports/pinterest-probe-key.json"
ENVELOPE_PATH = ".reports/pinterest-probe-envelope.json"
INFO = b"yura-private-pinterest-probe-v1"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(data: str) -> bytes:
    return base64.b64decode(data, validate=True)


def public_bytes(key: X25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def derive(shared: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=INFO).derive(shared)


def seal(keyfile: Path, linkfile: Path, outfile: Path) -> None:
    report = json.loads(keyfile.read_text())["current"]
    run_id = report["run_id"]
    remote = X25519PublicKey.from_public_bytes(unb64(report["extra"]["public_key"]))
    ephemeral = X25519PrivateKey.generate()
    nonce = os.urandom(12)
    plaintext = linkfile.read_bytes()
    ciphertext = AESGCM(derive(ephemeral.exchange(remote))).encrypt(nonce, plaintext, run_id.encode())
    outfile.write_text(json.dumps({"run_id": run_id, "public_key": b64(public_bytes(ephemeral.public_key())), "nonce": b64(nonce), "ciphertext": b64(ciphertext)}) + "\n")


def wait_for_envelope(private: X25519PrivateKey, run_id: str) -> list[str]:
    branch = os.environ["GITHUB_REF_NAME"]
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        try:
            subprocess.run(["git", "fetch", "-q", "origin", branch], cwd=ROOT, timeout=25, check=True, capture_output=True)
            data = subprocess.run(["git", "show", f"FETCH_HEAD:{ENVELOPE_PATH}"], cwd=ROOT, timeout=10, check=True, capture_output=True).stdout
            env = json.loads(data)
            if env.get("run_id") == run_id:
                remote = X25519PublicKey.from_public_bytes(unb64(env["public_key"]))
                plain = AESGCM(derive(private.exchange(remote))).decrypt(unb64(env["nonce"]), unb64(env["ciphertext"]), run_id.encode())
                links = json.loads(plain)
                if not isinstance(links, list) or len(links) != 2 or not all(isinstance(x, str) and x.startswith("https://pin.it/") for x in links):
                    raise ValueError("invalid link list")
                return links
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            pass
        time.sleep(5)
    raise TimeoutError("encrypted link handoff did not arrive")


async def check_links(links: list[str]) -> int:
    failed = 0
    for index, url in enumerate(links, 1):
        path = None
        try:
            shapes = []
            for flag in ("-j", "-J"):
                code, output, _ = await _gallery_run(["--range", "1", flag, url], timeout=60)
                entries = json.loads(output) if code == 0 else []
                types = [entry[0] for entry in entries if isinstance(entry, list) and entry and isinstance(entry[0], int)]
                shapes.append(f"{flag} exit={code} types={types[:10]}")
            print(f"::notice::Link {index}: {'; '.join(shapes)}", flush=True)
            item = await inspect(url)
            quality = item.quality(1)
            if quality is None:
                raise ValueError("quality missing")
            path = await fetch(item.url, quality, max_mb=50, timeout=120)
            await validate_file(path, item, quality)
            print(f"::notice::Link {index}: Yura inspected, downloaded and validated {path.stat().st_size} bytes ({path.suffix.lower()})", flush=True)
        except Exception as exc:
            failed += 1
            print(f"::error::Link {index}: {type(exc).__name__}", flush=True)
        finally:
            discard(path)
    return int(bool(failed))


def main() -> int:
    if len(sys.argv) == 5 and sys.argv[1] == "seal":
        seal(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))
        return 0
    private = X25519PrivateKey.generate()
    key = b64(public_bytes(private.public_key()))
    run_id = os.environ["GITHUB_RUN_ID"]
    payload = build(exit_code=0, uptime_seconds=None, reason="ephemeral Pinterest probe key", extra={"public_key": key})
    result = publish(ROOT / PUB_PATH, {"current": payload})
    if result != "committed":
        print("::error::Could not publish probe public key", flush=True)
        return 1
    links = wait_for_envelope(private, run_id)
    return asyncio.run(check_links(links))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"::error::Private probe failed: {type(exc).__name__}", flush=True)
        sys.exit(1)
