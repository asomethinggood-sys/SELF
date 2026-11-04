"""The one token both ends of a hosted service compute.

A service runs on a machine we do not own, behind a *public* URL, with no
login of its own: ComfyUI will draw a picture for anyone who asks, SearXNG will
search for anyone, Ollama will complete anything. The gate in front of them is
what keeps that from being a public API with our name on it.

The token cannot be read from ``.services/<name>.json`` — that file is committed.
Instead both sides derive the same value from one secret:

    token = HMAC-SHA256(secret, "<service>:<run_id>:<started_at>")   (hex)

Because ``run_id`` and ``started_at`` change on every restart, the token rotates
with the service: a token from last night's run is refused by this one.

**There is a default, and it is committed.** The owner said, in as many words,
that he does not want to manage a secret and does not mind the endpoints being
public. So :data:`DEFAULT_SECRET` is used when ``SERVICE_SHARED_SECRET`` is unset,
and a service publishes and serves with nothing configured at all.

Be clear about what that buys, because it is not security:

* it stops drive-by scanners, which knock on ``/prompt`` and ``/api/pull``
  without reading anybody's repository — they get a 401 and move on;
* it does **not** stop a person who reads this file, because the default is in it.

A real secret (set ``SERVICE_SHARED_SECRET``) is still the better default for
anyone who does not want strangers drawing pictures on their runner. And
``SERVICE_PUBLIC=1`` turns the token off entirely for a genuinely open service.

Kept in its own module, with no imports, so the gate (a stdlib script in a
workflow) and the bot (the `havoc` package) cannot drift apart.
"""

from __future__ import annotations

import hashlib
import hmac
import os

#: What a caller may be authorised to use. One name per hosted service.
SERVICES = ("search", "comfy", "ollama", "sdxl-experimental")

#: The long-lived services the supervisor keeps alive. The experimental SDXL
#: service is deliberately not one of them: it is short-lived, opt-in and
#: started by an explicit marker push, not by a five-minute heartbeat.
SUPERVISED = ("search", "comfy", "ollama")

#: Used when ``SERVICE_SHARED_SECRET`` is not set, so a runner publishes and
#: serves with nothing configured. Public by construction — see the module
#: docstring; it keeps scanner traffic out, not people.
DEFAULT_SECRET = "yura-hosted-services-2026"

#: ``SERVICE_PUBLIC=1`` means "no token at all". The gate then accepts any
#: request, the published file says ``gated: false``, and the bot sends no header.
PUBLIC_ENV = "SERVICE_PUBLIC"


def is_public(value: str | None = None) -> bool:
    """Has the owner asked for a genuinely open service?"""
    raw = (value if value is not None else os.environ.get(PUBLIC_ENV, "")) or ""
    return raw.strip().lower() in {"1", "true", "yes", "on", "public"}


def secret_for(value: str | None = None) -> str:
    """The secret in force: the environment, else the committed default.

    Empty string only when the service is explicitly public, which is the one
    case where no token is computed at all.
    """
    # `SERVICE_SECRET` first: that is what the gate is handed by the host and by
    # the workflow, and it is resolved from `SERVICE_SHARED_SECRET` before it gets
    # there. Reading only the long name here once made the gate sign with the
    # default while the bot signed with the chosen secret — a 401 on every call.
    if value is not None:
        raw = value
    else:
        raw = os.environ.get("SERVICE_SECRET") or os.environ.get("SERVICE_SHARED_SECRET") or ""
    raw = raw.strip()
    if raw:
        return raw
    if is_public():
        return ""
    return DEFAULT_SECRET


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


__all__ = [
    "DEFAULT_SECRET",
    "PUBLIC_ENV",
    "SERVICES",
    "is_public",
    "matches",
    "materials",
    "parse_authorization",
    "secret_for",
    "token_for",
]
