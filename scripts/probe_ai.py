"""Ask the configured AI provider three ways and report exactly what came back.

A single "it failed" line is not enough to fix anything: the gateway can answer
``/models`` with 200 and still refuse ``/chat/completions``, and the refusal page
is an HTML error from the CDN in front of it, not JSON from the provider. This
script separates those cases so the answer is a fact instead of a guess:

1. ``GET /models`` **with** the key, the way the provider should ask;
2. ``POST /chat/completions`` with a minimal payload, through the same HTTP
   helper the bot uses;
3. the same POST with the persona system prompt attached, because content
   filters inspect the body.

Each result is ``status`` plus the first bytes of the body. Nothing here prints
the key.
"""

from __future__ import annotations

import asyncio
import json
import sys

from havoc.config import load_config
from havoc.persona import system_prompt
from havoc.services import http as http_module
from havoc.services.ai_provider import AIMessage, AIProvider


def _head(text: str, limit: int = 200) -> str:
    """One line, no newlines, short enough for a log line."""
    return " ".join(str(text).split())[:limit]


async def _attempt(name: str, coroutine) -> dict[str, object]:
    try:
        result = await coroutine
    except http_module.HttpError as exc:
        return {"check": name, "status": exc.status, "ok": False, "detail": _head(str(exc))}
    except Exception as exc:  # noqa: BLE001 - a probe reports, never raises
        return {"check": name, "status": None, "ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    return {"check": name, "status": 200, "ok": True, "detail": _head(json.dumps(result)[:200])}


async def main() -> int:
    config, _issues = load_config()
    provider = AIProvider.from_config(config)
    report: dict[str, object] = {
        "base_url": provider.base_url,
        "model": provider.model,
        "available": provider.available,
        "checks": [],
    }
    checks: list[dict[str, object]] = report["checks"]  # type: ignore[assignment]
    if not provider.available:
        report["error"] = "provider is not configured (AI_PROVIDER / AI_API_KEY)"
        report["discovered_models"] = provider.models[:5]
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    auth = {"Authorization": f"Bearer {provider._api_key}"}
    listing = await _attempt(
        "models with key", http_module.get_json(f"{provider.base_url}/models", headers=auth)
    )
    checks.append(listing)

    model = ""
    if listing["ok"]:
        try:
            entries = await http_module.get_json(f"{provider.base_url}/models", headers=auth)
            free = [entry["id"] for entry in entries.get("data", []) if str(entry.get("id", "")).endswith(":free")]
            model = sorted(free)[0] if free else ""
        except Exception:  # noqa: BLE001 - the listing already succeeded once
            model = ""
    model = model or provider.models[0]

    short = {
        "model": model,
        "messages": [{"role": "user", "content": "2+2"}],
        "max_tokens": 16,
    }
    checks.append(
        await _attempt(
            "chat, minimal payload",
            http_module.post_json(
                f"{provider.base_url}/chat/completions", short, timeout=min(provider.timeout, 40), headers=auth
            ),
        )
    )
    long_payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt("en")},
            {"role": "user", "content": "what is 2+2"},
        ],
        "max_tokens": 40,
    }
    checks.append(
        await _attempt(
            "chat, with the persona prompt",
            http_module.post_json(
                f"{provider.base_url}/chat/completions", long_payload, timeout=min(provider.timeout, 40), headers=auth
            ),
        )
    )
    checks.append(
        await _attempt(
            "chat, with referer and title",
            http_module.post_json(
                f"{provider.base_url}/chat/completions",
                short,
                timeout=min(provider.timeout, 40),
                headers={**auth, "HTTP-Referer": "https://github.com/asomethinggood-sys/SELF", "X-Title": "HAVOC Yura"},
            ),
        )
    )

    report["discovered_models"] = provider.models[:5]
    try:
        reply = await provider.converse([AIMessage("user", "2+2")], max_tokens=16)
        checks.append({"check": "provider.converse", "status": 200, "ok": True, "detail": _head(reply)})
        report["active_model"] = provider.active_model
    except Exception as exc:  # noqa: BLE001 - a probe reports, never raises
        checks.append(
            {
                "check": "provider.converse",
                "status": None,
                "ok": False,
                "detail": _head(f"{type(exc).__name__}: {exc}"),
            }
        )
        report["active_model"] = provider.active_model

    report["discovered_models"] = provider.models[:5]
    try:
        reply = await provider.converse([AIMessage("user", "2+2")], max_tokens=16)
        checks.append({"check": "provider.converse", "status": 200, "ok": True, "detail": _head(reply)})
    except Exception as exc:  # noqa: BLE001 - a probe reports, never raises
        checks.append(
            {
                "check": "provider.converse",
                "status": None,
                "ok": False,
                "detail": _head(f"{type(exc).__name__}: {exc}"),
            }
        )
    report["active_model"] = provider.active_model
    report["user_agent"] = http_module.USER_AGENT

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all(check["ok"] for check in checks) else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
