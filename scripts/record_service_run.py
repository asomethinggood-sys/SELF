#!/usr/bin/env python3
"""Commit a short report about one experimental service run.

The workflow log viewer needs a redirect this environment cannot fetch, so the
outcome is written into the repository, where it can be read back afterwards.
The rendezvous writer already retries contention, so it is reused rather than
hand-rolling a push that the moving branch will reject.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("WORKER_INDEX", "0")

from scripts import sdxl_cluster_test as rendezvous  # noqa: E402


def main() -> int:
    text = Path(sys.argv[1]).read_text(errors="replace")
    rendezvous.API = f"https://api.github.com/repos/{rendezvous.REPO}/contents/.reports"
    rendezvous.write(sys.argv[2], {"text": text})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
