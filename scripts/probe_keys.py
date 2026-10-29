"""Ask every configured AI key one blunt question and record what came back.

Two providers, four credentials, several possible auth styles each: the only way
to know which of them work is to try them and write down the answers. This
script does exactly that and never prints a key.

Providers
---------
``openrouter``  ``GET /models`` and ``POST /chat/completions`` with a Bearer key.
``google``      the Gemini API, tried three ways because new-format AI Studio
                keys (``AQ.…``) are not documented to work with the OpenAI-shaped
                endpoint:
                  1. ``GET /v1beta/models`` with ``x-goog-api-key``
                  2. ``POST /v1beta/models/<id>:generateContent`` (native)
                  3. ``POST /v1beta/openai/chat/completions`` (OpenAI-compatible)

The output is a small JSON record: per key, per check, the status and the first
bytes of the body. It is committed by the workflow so the answer is readable
without the Actions log, which is not reachable from everywhere.
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

USER_AGENT = "Yura/2.0 (+https://github.com/asomethinggood-sys/SELF)"
TIMEOUT = 25
#: Newest-looking free Gemini chat models, best daily budget first.
GEMINI_CANDIDATES = (
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.8-flash",
    "gemini-2.5-flash",
    "gemma-4-31b-it",
    "gemma-4-26b-it",
)


def _request(method: str, url: str, *, body: dict | None = None, headers: dict[str, str] | None = None) -> tuple[int, str]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("User-Agent", USER_AGENT)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            return response.status, response.read(4000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(2000).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - a probe records, never raises
        return 0, f"{type(exc).__name__}: {exc}"


def _summary(text: str, limit: int = 220) -> str:
    return " ".join(text.split())[:limit]


def _keys() -> list[dict[str, str]]:
    path = pathlib.Path(os.environ.get("AI_KEYS_FILE") or ".auth/ai-keys.json")
    entries: list[dict[str, str]] = []
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        for item in raw.get("keys", []):
            key = item.get("key") or ""
            if not key and item.get("key_b64"):
                key = base64.b64decode(item["key_b64"]).decode().strip()
            if key:
                entries.append({"provider": item.get("provider", "openrouter"), "label": item.get("label", "key"), "key": key})
    for index, key in enumerate(filter(None, (os.environ.get("AI_API_KEY", "").split(",")))):
        entries.append({"provider": os.environ.get("AI_PROVIDER_HINT", "openrouter"), "label": f"env-{index + 1}", "key": key.strip()})
    return entries


def _probe_openrouter(key: str) -> dict[str, object]:
    auth = {"Authorization": f"Bearer {key}"}
    status, body = _request("GET", "https://openrouter.ai/api/v1/models", headers=auth)
    models = []
    if status == 200:
        try:
            models = [str(item["id"]) for item in json.loads(body).get("data", []) if str(item.get("id", "")).endswith(":free")]
        except json.JSONDecodeError:
            models = []
    checks = [{"check": "GET /models", "status": status, "body": _summary(body)}]
    status, body = _request(
        "POST",
        "https://openrouter.ai/api/v1/chat/completions",
        body={"model": "auto", "messages": [{"role": "user", "content": "2+2"}], "max_tokens": 300},
        headers=auth,
    )
    checks.append({"check": "POST /chat/completions (model auto)", "status": status, "body": _summary(body)})
    return {"provider": "openrouter", "checks": checks, "free_models": models[:40]}


def _probe_google(key: str) -> dict[str, object]:
    checks: list[dict[str, object]] = []
    listed: list[str] = []
    status, body = _request(
        "GET",
        "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
        headers={"x-goog-api-key": key},
    )
    if status == 200:
        try:
            listed = [str(item["name"]).split("/")[-1] for item in json.loads(body).get("models", [])]
        except json.JSONDecodeError:
            listed = []
    checks.append(
        {
            "check": "GET /v1beta/models",
            "status": status,
            "body": _summary(body),
            "count": len(listed),
        }
    )
    native = "gemini-2.5-flash"
    for candidate in GEMINI_CANDIDATES:
        if candidate in listed or not listed:
            native = candidate
            break
    status, body = _request(
        "POST",
        f"https://generativelanguage.googleapis.com/v1beta/models/{native}:generateContent",
        body={"contents": [{"parts": [{"text": "2+2"}]}]},
        headers={"x-goog-api-key": key},
    )
    checks.append({"check": f"POST :generateContent ({native})", "status": status, "body": _summary(body)})
    status, body = _request(
        "POST",
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        body={"model": native, "messages": [{"role": "user", "content": "2+2"}], "max_tokens": 300},
        headers={"Authorization": f"Bearer {key}"},
    )
    checks.append({"check": f"POST /v1beta/openai/chat/completions ({native})", "status": status, "body": _summary(body)})
    return {"provider": "google", "checks": checks, "models": listed[:120]}


def main() -> int:
    report: dict[str, object] = {"keys": []}
    entries = _keys()
    if not entries:
        print(json.dumps({"error": "no keys found"}, ensure_ascii=False))
        return 1
    results: list[dict[str, object]] = report["keys"]  # type: ignore[assignment]
    for entry in entries:
        record: dict[str, object] = {
            "provider": entry["provider"],
            "label": entry["label"],
            "key_hint": f"{entry['key'][:10]}…{entry['key'][-4:]}",
            "length": len(entry["key"]),
        }
        try:
            if entry["provider"] == "google":
                record.update(_probe_google(entry["key"]))
            else:
                record.update(_probe_openrouter(entry["key"]))
        except Exception as exc:  # noqa: BLE001 - one bad key must not stop the rest
            record["error"] = f"{type(exc).__name__}: {exc}"
        results.append(record)
        print(json.dumps(record, ensure_ascii=False)[:400], file=sys.stderr)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
