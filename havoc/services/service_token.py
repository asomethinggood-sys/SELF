"""The one token both ends of a hosted service compute.

A service runs on a machine we do not own, behind a *public* URL, with no
login of its own: ComfyUI will draw a picture for anyone who asks, SearXNG will
search for anyone, Ollama will complete anything. The gate in front of them is
what keeps that from being a public API with our name on it.

The token cannot be read from the repository — ``.services/<name>.json`` is
committed, so anything in it is public. Instead both sides derive the same value
from a secret only the workflows and the bot have:

    token = HMAC-SHA256(secret, "<service>:<run_id>:<started_at>")   (hex)

Because ``run_id`` and ``started_at`` change on every restart, the token rotates
with the service: a leaked token dies when the run does. Nothing secret is ever
written to git, and nothing in the published file is worth stealing.

Kept in its own module, with no imports, so the gate (a stdlib script in a
workflow) and the bot (the `havoc` package) cannot drift apart.
"""

from __future__ import annotations

import hashlib
import hmac

#: What a caller may be authorised to use. One name per hosted service.
SERVICES = ("search", "comfy", "ollama")


def materials(service: str, run_id: str | int, started_at: str) -> bytes:
    """The message that gets signed. Public: it is in the published file."""
    return f"{service}:{run_id}:{started_at}".encode()


def token_for(service: str, run_id: str | int, started_at: str, secret: str) -> str:
    """The bearer token for one run of one service."""
    if not secret:
        return ""
    return hmac.new(secret.encode("utf-8"), materials(service, run_id, started_at), hashlib.sha256).hexdigest()


def matches(expected: str, offered: str) -> bool:
    """Constant-time comparison that never raises on junk input."""
    if not expected or not offered:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), offered.encode("utf-8"))


def parse_authorization(header: str | None) -> str:
    """``Bearer abc`` -> ``abc``. Anything else is an empty string."""
    text = (header or "").strip()
    if not text:
        return ""
    parts = text.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    return parts[1].strip()


__all__ = ["SERVICES", "materials", "token_for", "matches", "parse_authorization"]
