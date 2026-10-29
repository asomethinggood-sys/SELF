#!/usr/bin/env python3
"""Build the Hafez divination table that ships with Yura.

Source: ``Reza-Babakhani/fal_hafez`` — ``assets/db/HafezDb.db``, a SQLite file
whose ``Fals`` table holds 495 readings: the verses of a ghazal and the
traditional interpretation attached to it by the app.

The repository keeps the SQLite file out of the wheel and ships the same rows as
gzipped JSON: one small file, no SQLite driver needed at runtime, no binary blob
in Git that nobody can diff.

Usage::

    python scripts/build_fal_data.py            # fetch + convert
    python scripts/build_fal_data.py --check    # verify the shipped file
    python scripts/build_fal_data.py --db path  # convert an existing copy

Feature areas: 27, 63.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "havoc" / "data" / "fals.json.gz"
CACHE = ROOT / ".cache" / "fal"
REPO = "Reza-Babakhani/fal_hafez"
DB_PATH = "assets/db/HafezDb.db"
API = "https://api.github.com"


def _headers() -> dict[str, str]:
    token = os.environ.get("GITHUB_TOKEN") or ""
    if not token:
        try:
            token = subprocess.run(
                ["gh", "auth", "token"], capture_output=True, text=True, timeout=20, check=False
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            token = ""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "yura-fal-builder"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_db() -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    target = CACHE / "HafezDb.db"
    if target.exists() and target.stat().st_size > 100_000:
        return target
    tree_url = f"{API}/repos/{REPO}/git/trees/HEAD?recursive=1"
    with urllib.request.urlopen(urllib.request.Request(tree_url, headers=_headers()), timeout=60) as response:
        tree = json.loads(response.read())
    sha = next(
        entry["sha"] for entry in tree["tree"] if entry["path"] == DB_PATH and entry["type"] == "blob"
    )
    blob_url = f"{API}/repos/{REPO}/git/blobs/{sha}"
    with urllib.request.urlopen(urllib.request.Request(blob_url, headers=_headers()), timeout=120) as response:
        payload = json.loads(response.read())
    target.write_bytes(base64.b64decode(payload["content"]))
    return target


def read_rows(db_path: Path) -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        # sqlite3 needs a writable copy: the cache file is read-only in CI.
        local = Path(tmp) / "fal.db"
        local.write_bytes(db_path.read_bytes())
        connection = sqlite3.connect(local)
        try:
            rows = connection.execute("SELECT Id, Poem, Interpretation FROM Fals ORDER BY Id").fetchall()
        finally:
            connection.close()

    readings: list[dict] = []
    for identifier, poem, interpretation in rows:
        # The dump stores literal "\\r\\n" sequences, not real newlines.
        raw_text = str(poem or "").replace("\\r\\n", "\n").replace("\\n", "\n")
        verses = [line.strip() for line in raw_text.replace("\r", "\n").split("\n") if line.strip()]
        interpretation_text = " ".join(
            str(interpretation or "").replace("\\r\\n", " ").replace("\\n", " ").split()
        )
        if len(verses) < 2 or not interpretation_text:
            continue
        readings.append(
            {
                "id": int(identifier),
                "verses": verses,
                "interpretation": interpretation_text,
            }
        )
    return readings


def write(readings: list[dict]) -> Path:
    payload = {
        "source": f"{REPO} (HafezDb.db, {len(readings)} readings)",
        "poet": "حافظ",
        "readings": readings,
    }
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with gzip.open(TARGET, "wb", compresslevel=9) as handle:
        handle.write(raw)
    return TARGET


def load() -> dict:
    with gzip.open(TARGET, "rb") as handle:
        return json.loads(handle.read())


def check() -> int:
    if not TARGET.exists():
        print(f"missing {TARGET}", file=sys.stderr)
        return 1
    payload = load()
    readings = payload["readings"]
    lengths = [len(item["verses"]) for item in readings]
    print(
        f"{len(readings)} readings, verses {min(lengths)}–{max(lengths)}, "
        f"{TARGET.stat().st_size // 1024} KB"
    )
    sample = readings[0]
    print("  first:", sample["verses"][0])
    print("  note :", sample["interpretation"][:90], "…")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.check:
        return check()
    db_path = args.db or fetch_db()
    readings = read_rows(db_path)
    if not readings:
        print("no readings parsed", file=sys.stderr)
        return 1
    target = write(readings)
    print(f"wrote {len(readings)} readings to {target}", file=sys.stderr)
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
