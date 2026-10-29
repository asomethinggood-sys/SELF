"""Ask the configured AI provider several ways and report exactly what came back.

A single "it failed" line is not enough to fix anything. The gateway can answer
``/models`` with 200 and still refuse ``/chat/completions``; the refusal can be an
HTML page from the CDN in front of it rather than JSON from the provider; and a
free model can answer 200 with no content at all. This script separates those
cases so the answer is a fact instead of a guess:

1. ``GET /models`` with the key;
2. ``POST /chat/completions`` with a minimal payload;
3. the same with the persona system prompt attached;
4. the same with OpenRouter's optional attribution headers;
5. the provider's own ``converse()``, which is what the bot actually calls;
6. a small matrix of the discovered models at two token budgets, because some
   free endpoints spend the whole budget on hidden reasoning and return nothing.

Nothing here prints the key.
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


async def _first_free_model(provider: AIProvider, auth: dict[str, str]) -> str:
    """One free slug the gateway currently lists, for the raw checks."""
    try:
        entries = await http_module.get_json(f"{provider.base_url}/models", headers=auth)
    except Exception:  # noqa: BLE001 - the listing check already reports failures
        return provider.models[0] if provider.models else provider.model
    free = [str(entry["id"]) for entry in entries.get("data", []) if str(entry.get("id", "")).endswith(":free")]
    return sorted(free)[0] if free else (provider.models[0] if provider.models else provider.model)


async def _matrix(provider: AIProvider, auth: dict[str, str]) -> list[dict[str, object]]:
    """Which discovered models answer a plain question, and at what budget."""
    rows: list[dict[str, object]] = []
    for candidate in (provider.models or [])[:6]:
        for budget in (16, 300):
            entry: dict[str, object] = {"model": candidate, "budget": budget}
            try:
                data = await http_module.post_json(
                    f"{provider.base_url}/chat/completions",
                    {
                        "model": candidate,
                        "messages": [{"role": "user", "content": "2+2"}],
                        "max_tokens": budget,
                    },
                    timeout=min(provider.timeout, 40),
                    headers=auth,
                )
                choices = (data or {}).get("choices") or [{}]
                message = choices[0].get("message") or {}
                entry["content"] = _head(str(message.get("content") or ""), 40)
                entry["has_reasoning"] = bool(message.get("reasoning") or message.get("reasoning_content"))
            except Exception as exc:  # noqa: BLE001 - a probe reports, never raises
                entry["error"] = _head(str(exc), 80)
            rows.append(entry)
    return rows


async def main() -> int:
    config, _issues = load_config()
    provider = AIProvider.from_config(config)
    report: dict[str, object] = {
        "base_url": provider.base_url,
        "model": provider.model,
        "available": provider.available,
        "user_agent": http_module.USER_AGENT,
        "checks": [],
    }
    checks: list[dict[str, object]] = report["checks"]  # type: ignore[assignment]
    if not provider.available:
        report["error"] = "provider is not configured (AI_PROVIDER / AI_API_KEY)"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    auth = {"Authorization": f"Bearer {provider._api_key}"}
    checks.append(
        await _attempt("models with key", http_module.get_json(f"{provider.base_url}/models", headers=auth))
    )

    model = await _first_free_model(provider, auth)
    report["probe_model"] = model
    short = {"model": model, "messages": [{"role": "user", "content": "2+2"}], "max_tokens": 16}
    checks.append(
        await _attempt(
            "chat, minimal payload",
            http_module.post_json(
                f"{provider.base_url}/chat/completions", short, timeout=min(provider.timeout, 40), headers=auth
            ),
        )
    )
    checks.append(
        await _attempt(
            "chat, with the persona prompt",
            http_module.post_json(
                f"{provider.base_url}/chat/completions",
                {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt("en")},
                        {"role": "user", "content": "what is 2+2"},
                    ],
                    "max_tokens": 300,
                },
                timeout=min(provider.timeout, 40),
                headers=auth,
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
                headers={
                    **auth,
                    "HTTP-Referer": "https://github.com/asomethinggood-sys/SELF",
                    "X-Title": "Yura",
                },
            ),
        )
    )

    report["discovered_models"] = provider.models[:6]
    try:
        reply = await provider.converse([AIMessage("user", "2+2")], max_tokens=300)
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
    report["model_matrix"] = await _matrix(provider, auth)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all(check["ok"] for check in checks) else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
