"""Carrying a file from where it was made to the message that shows it.

A command often writes its picture to a temporary file and deletes it in the
same breath. Her answer arrives a moment later, so the file has to travel as
bytes and be written again here — one message, with the words as the caption.
"""

from __future__ import annotations

import tempfile
from pathlib import Path, PurePath


def materialize(blob: bytes, name: str = "", suffix: str = "") -> Path:
    """Write ``blob`` to a temporary file that survives the send."""
    ending = suffix or PurePath(str(name or "")).suffix or ".bin"
    handle = tempfile.NamedTemporaryFile(prefix="yura-out-", suffix=ending, delete=False)
    try:
        handle.write(blob)
    finally:
        handle.close()
    return Path(handle.name)
