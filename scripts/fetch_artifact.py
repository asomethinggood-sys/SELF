#!/usr/bin/env python3
"""Get a model file onto this runner without committing it.

Three sources, tried in order of how cheap they are:

1. **Already here** — Actions cache restored it, or a previous step fetched it.
   Verified by size (and sha256 when the manifest knows one).
2. **Drive** — ``gdown --id`` from the owner's own 5 TB drive. This is the
   archive: links we control, no rate limit from a model hub, and a file can be
   swapped by editing the manifest instead of by re-uploading somewhere.
3. **Hugging Face** — the public URL, for anything not archived yet.

Every successful download is recorded with its sha256 and byte count in
``.services/assets.lock.json`` (committed by the workflow), so the *next* run
verifies rather than trusts. A manifest entry may also pin ``sha256`` up front;
when it does, a mismatch deletes the file and moves to the next source. A
half-written or truncated checkpoint is the sort of bug that looks like "the
model is stupid" for a week.

    python scripts/fetch_artifact.py --name sd-turbo --into /tmp/ckpt
    python scripts/fetch_artifact.py --name qwen3b --verify-only
    python scripts/fetch_artifact.py --list

Nothing above 2 GB is ever committed to the repository; the manifest holds
links, not weights.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / ".services" / "assets.json"
LOCK = ROOT / ".services" / "assets.lock.json"
CHUNK = 1024 * 1024


def log(message: str) -> None:
    print(f"[artifact] {message}", flush=True)


def load(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def record_lock(name: str, path: Path, digest: str) -> None:
    """Remember what we actually got, so the next run can verify it."""
    lock = load(LOCK, {})
    entry = {
        "file": path.name,
        "bytes": path.stat().st_size,
        "sha256": digest,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if lock.get(name) == entry:
        return
    lock[name] = entry
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    LOCK.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    log(f"recorded {name} → {path.name} ({entry['bytes']:,} bytes, sha256 {digest[:12]}…)")


def good(path: Path, *, sha256: str = "", size: int = 0) -> bool:
    """Is this file the one we wanted?"""
    if not path.exists() or path.stat().st_size == 0:
        return False
    if size and path.stat().st_size != int(size):
        log(f"{path.name} is {path.stat().st_size:,} bytes, expected {int(size):,}")
        return False
    if sha256:
        found = sha256_of(path)
        if found != sha256:
            log(f"{path.name} sha256 {found[:12]}… does not match {sha256[:12]}…")
            return False
    return True


def from_drive(drive_id: str, target: Path) -> bool:
    if not drive_id:
        return False
    if shutil.which("gdown") is None:
        if subprocess.call([sys.executable, "-m", "pip", "install", "-q", "gdown"]) != 0:
            log("gdown could not be installed")
            return False
    part = target.with_suffix(target.suffix + ".part")
    command = ["gdown", "--id", drive_id, "-O", str(part), "--fuzzy"]
    log(f"drive: gdown {drive_id}")
    if subprocess.call(command) != 0 or not part.exists():
        log("drive download failed")
        part.unlink(missing_ok=True)
        return False
    part.replace(target)
    return True


def from_hf(url: str, target: Path) -> bool:
    if not url:
        return False
    part = target.with_suffix(target.suffix + ".part")
    log(f"huggingface: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "Yura/2.0"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - an explicit https URL from our own manifest
            total = int(response.headers.get("Content-Length") or 0)
            written = 0
            with part.open("wb") as handle:
                while True:
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)
                    written += len(chunk)
                    if total and written % (64 * CHUNK) < CHUNK:
                        log(f"  {written / 1e6:.0f} / {total / 1e6:.0f} MB")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        log(f"huggingface download failed: {exc}")
        part.unlink(missing_ok=True)
        return False
    part.replace(target)
    return True


def fetch(name: str, *, into: Path, as_name: str = "") -> int:
    manifest = load(MANIFEST, {})
    assets = manifest.get("assets") or {}
    entry = assets.get(name)
    if not entry:
        log(f"{name} is not in {MANIFEST.relative_to(ROOT)} — known: {', '.join(sorted(assets)) or 'none'}")
        return 2
    lock = load(LOCK, {}).get(name, {})
    filename = as_name or entry.get("file") or Path(entry.get("hf_url", "")).name or f"{name}.bin"
    target = into / filename
    into.mkdir(parents=True, exist_ok=True)

    # The lock (what a previous run measured) beats the manifest (what a human
    # typed once), because the lock came from an actual byte count.
    expected_sha = str(entry.get("sha256") or lock.get("sha256") or "")
    expected_size = int(entry.get("bytes") or lock.get("bytes") or 0)

    if target.exists():
        if good(target, sha256=expected_sha, size=0):
            digest = sha256_of(target)
            if expected_sha and digest != expected_sha:
                log("cached file does not match the pinned sha256; discarding")
                target.unlink()
            else:
                log(f"{filename} is already here ({target.stat().st_size:,} bytes)")
                if expected_size and target.stat().st_size != expected_size:
                    log(f"  size note: {target.stat().st_size:,} vs manifest {expected_size:,}")
                record_lock(name, target, digest)
                return 0
        else:
            log(f"{filename} exists but is not valid; discarding")
            target.unlink(missing_ok=True)

    # Mirror candidates: a model page is deleted, gated, or rate-limited often
    # enough that one URL is a single point of failure for a whole build.
    hf_urls = [str(entry.get("hf_url") or "")] + [str(url) for url in entry.get("hf_urls") or []]
    sources: list[tuple[str, Any]] = [("drive", lambda: from_drive(str(entry.get("drive_id") or ""), target))]
    for url in [item for item in hf_urls if item]:
        sources.append((f"hf:{url.rsplit('/', 1)[-1]}", lambda url=url: from_hf(url, target)))

    attempts: list[str] = []
    for label, fetcher in sources:
        if not fetcher():
            attempts.append(f"{label}: no")
            continue
        digest = sha256_of(target)
        if expected_sha and digest != expected_sha:
            log(f"{label} gave the wrong file (sha256 {digest[:12]}… ≠ {expected_sha[:12]}…)")
            target.unlink(missing_ok=True)
            attempts.append(f"{label}: wrong sha256")
            continue
        record_lock(name, target, digest)
        log(f"{filename} ready from {label}: {target.stat().st_size:,} bytes")
        return 0

    log(f"could not fetch {name} ({'; '.join(attempts)})")
    log("fix: upload it to Drive and set drive_id in .services/assets.json, "
        "or add an hf_url, or pin sha256: null to accept whatever arrives once")
    return 1


def verify(name: str) -> int:
    manifest = load(MANIFEST, {})
    entry = (manifest.get("assets") or {}).get(name) or {}
    lock = load(LOCK, {}).get(name) or {}
    if not lock:
        log(f"no lock entry for {name}: it has never been fetched here")
        return 2
    path = ROOT / lock.get("path", "") if lock.get("path") else None
    expected = str(entry.get("sha256") or lock.get("sha256"))
    log(f"{name}: locked sha256 {expected[:12]}… ({lock.get('bytes'):,} bytes)")
    if path is None or not path.exists():
        log("(no local copy to verify — the lock is the record)")
        return 0
    found = sha256_of(path)
    log(f"{name}: local sha256 {found[:12]}…")
    return 0 if found == expected else 1


def listing() -> int:
    manifest = load(MANIFEST, {})
    lock = load(LOCK, {})
    assets = manifest.get("assets") or {}
    if not assets:
        log(f"{MANIFEST.relative_to(ROOT)} has no assets")
        return 0
    for name in sorted(assets):
        entry = assets[name]
        state = lock.get(name, {})
        size = entry.get("bytes") or state.get("bytes") or 0
        where = "drive" if entry.get("drive_id") else ("hf" if entry.get("hf_url") else "—")
        pinned = "pinned" if entry.get("sha256") else ("locked" if state else "unpinned")
        print(f"{name:<16} {str(entry.get('file') or name):<34} {where:<6} {size/1e9:6.2f} GB  {pinned}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="fetch a model file without committing it")
    parser.add_argument("--name", default="")
    parser.add_argument("--into", default="")
    parser.add_argument("--as", dest="as_name", default="")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)

    if args.list:
        return listing()
    if not args.name:
        parser.error("--name is required unless --list")
    if args.verify_only:
        return verify(args.name)
    into = Path(args.into or (ROOT / ".runtime" / "assets"))
    return fetch(args.name, into=into, as_name=args.as_name)


if __name__ == "__main__":
    raise SystemExit(main())
