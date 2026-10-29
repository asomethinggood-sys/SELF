"""Key rotation: several keys, one voice, and an honest budget.

Why this exists
---------------
One key is one point of failure. Two OpenRouter keys and two Gemini keys, rotated
properly, mean a spent daily allowance stops being a silent mute: the next
credential answers, and the card says which one did.

What "properly" means here
--------------------------
* **Budgets are tracked, not assumed.** Each provider publishes a request limit
  per model per day (Google) or per account (OpenRouter). Every successful call
  increments a counter that is persisted in the database, so a deploy at 14:00
  does not hand out a fresh 500 requests.
* **A 429 is remembered.** A rate-limited key is not retried on every message: it
  goes into cooldown (or is marked spent for the rest of the day when a daily
  limit is what answered), and the next key takes over.
* **A dead key is retired, not reprieved.** ``401``/``403`` disables that
  credential until it is replaced, because retrying a wrong key is just noise.
* **The order is the owner's.** ``.ai model`` pins a model; otherwise the plan
  walks the catalogue from the largest daily budget down.

The class duck-types :class:`havoc.services.ai_provider.AIProvider` — same
``available`` / ``complete`` / ``converse`` / ``describe`` — so every feature that
already asked ``runtime.ai`` for a sentence keeps working unchanged.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..ai_models import (
    OPENROUTER_MODEL,
    PROVIDER_ENDPOINTS,
    ModelSpec,
    auto_plan,
    catalogue,
    find,
)
from ..utils.time import utc_now
from .ai_provider import AIError, AIMessage, AIProvider

#: How many credentials one message may burn before she answers offline.
MAX_ATTEMPTS = 6
#: Cooldowns, in seconds, for failures that are not the key's fault.
NETWORK_COOLDOWN = 45
RATE_COOLDOWN = 90
#: Settings keys. `ai.*` lives in the same table as every other preference.
USAGE_SETTING = "ai.usage"
HEALTH_SETTING = "ai.health"
KEYS_SETTING = "ai.keys"
MODEL_SETTING = "ai.model"


@dataclass(frozen=True)
class Credential:
    """One key, one provider, one label the owner can recognise."""

    provider: str
    label: str
    key: str
    source: str = "file"

    @property
    def id(self) -> str:
        """Short stable id: enough to persist state, not enough to be the key."""
        return hashlib.sha256(self.key.encode("utf-8")).hexdigest()[:12]

    @property
    def hint(self) -> str:
        return f"{self.key[:8]}…{self.key[-4:]}" if len(self.key) > 14 else "…"

    @property
    def base_url(self) -> str:
        return PROVIDER_ENDPOINTS.get(self.provider, PROVIDER_ENDPOINTS["openrouter"])


@dataclass
class Attempt:
    """What one credential+model pair did. Shown in `.ai test` and in logs."""

    label: str
    model: str
    ok: bool
    detail: str = ""


@dataclass
class KeyHealth:
    """Persisted state for one credential."""

    disabled: bool = False
    cooldown_until: str = ""
    error: str = ""
    successes: int = 0
    failures: int = 0

    def cooling(self, now: datetime) -> bool:
        if not self.cooldown_until:
            return False
        try:
            return datetime.fromisoformat(self.cooldown_until) > now
        except ValueError:
            return False

    def to_json(self) -> dict[str, Any]:
        return {
            "disabled": self.disabled,
            "cooldown_until": self.cooldown_until,
            "error": self.error,
            "successes": self.successes,
            "failures": self.failures,
        }

    @classmethod
    def from_json(cls, data: Any) -> KeyHealth:
        if not isinstance(data, dict):
            return cls()
        return cls(
            disabled=bool(data.get("disabled")),
            cooldown_until=str(data.get("cooldown_until") or ""),
            error=str(data.get("error") or ""),
            successes=int(data.get("successes") or 0),
            failures=int(data.get("failures") or 0),
        )


def read_key_file(path: Path) -> list[Credential]:
    """Credentials from ``.auth/ai-keys.json`` — base64, one row per key."""
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries: list[Credential] = []
    for index, item in enumerate(raw.get("keys", []), 1):
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if not key and item.get("key_b64"):
            try:
                key = base64.b64decode(str(item["key_b64"])).decode("utf-8").strip()
            except (ValueError, UnicodeDecodeError):
                key = ""
        if not key:
            continue
        entries.append(
            Credential(
                provider=str(item.get("provider") or "openrouter").lower(),
                label=str(item.get("label") or f"key-{index}"),
                key=key,
                source="file",
            )
        )
    return entries


class AIRouter:
    """Rotation over several credentials, with persisted budgets."""

    name = "rotating"

    def __init__(
        self,
        credentials: list[Credential],
        *,
        store: Any | None = None,
        timeout: float = 30.0,
        selection: str = "auto",
        logger: logging.Logger | None = None,
    ) -> None:
        self.credentials = credentials
        self.store = store
        self.timeout = timeout
        self.selection = selection or "auto"
        self.logger = logger or logging.getLogger("havoc.ai")
        self.health: dict[str, KeyHealth] = {}
        self.usage: dict[str, dict[str, Any]] = {}
        self.requests = 0
        self.failures = 0
        self.active_model = ""
        self.active_label = ""
        self.last_error = ""
        self.last_attempts: list[Attempt] = []
        self._loaded = False

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @classmethod
    async def load(
        cls,
        config: Any,
        store: Any | None = None,
        logger: logging.Logger | None = None,
    ) -> AIRouter:
        """Build the router from the key file, the environment and the database."""
        credentials: list[Credential] = []
        path = Path(getattr(config, "ai_keys_path", "") or ".auth/ai-keys.json")
        credentials.extend(read_key_file(path))

        env_keys = [part.strip() for part in str(getattr(config, "ai_api_key", "") or "").split(",") if part.strip()]
        for index, key in enumerate(env_keys, 1):
            provider = "google" if key.startswith("AQ.") else "openrouter"
            credentials.append(Credential(provider, f"env-{index}", key, "env"))

        if store is not None:
            # Added at runtime with `.ai key add …`: they live in the database so
            # they survive a restart without ever entering the repository.
            stored = await store.get_setting(KEYS_SETTING, None)
            if isinstance(stored, list):
                for index, item in enumerate(stored, 1):
                    if isinstance(item, dict) and item.get("key"):
                        credentials.append(
                            Credential(
                                str(item.get("provider") or "openrouter").lower(),
                                str(item.get("label") or f"chat-{index}"),
                                str(item["key"]),
                                "database",
                            )
                        )
            legacy = await store.get_setting("ai.api_key", None)
            if legacy and str(legacy).strip() and all(entry.key != str(legacy).strip() for entry in credentials):
                key = str(legacy).strip()
                provider = "google" if key.startswith("AQ.") else "openrouter"
                credentials.append(Credential(provider, "chat-key", key, "database"))
            selection = str(await store.get_setting(MODEL_SETTING, "auto") or "auto")
        else:
            selection = "auto"

        router = cls(credentials, store=store, timeout=float(getattr(config, "ai_timeout", 30.0)), selection=selection, logger=logger)
        await router.refresh()
        return router

    async def refresh(self) -> None:
        """Reload health and usage from the database."""
        if self.store is not None:
            health = await self.store.get_setting(HEALTH_SETTING, {})
            self.health = {
                str(key): KeyHealth.from_json(value) for key, value in (health or {}).items() if isinstance(value, dict)
            }
            usage = await self.store.get_setting(USAGE_SETTING, {})
            self.usage = usage if isinstance(usage, dict) else {}
        self._prune_usage()
        self._loaded = True

    # ------------------------------------------------------------------
    # Budgets
    # ------------------------------------------------------------------
    @staticmethod
    def _today() -> str:
        return utc_now().strftime("%Y-%m-%d")

    def _prune_usage(self) -> None:
        """Keep today's counters only: yesterday's budget is not this budget."""
        today = self._today()
        for name in list(self.usage):
            record = self.usage[name]
            if not isinstance(record, dict) or record.get("day") != today:
                self.usage.pop(name, None)

    def used(self, credential: Credential, spec: ModelSpec) -> int:
        record = self.usage.get(f"{credential.id}|{spec.key}")
        if not isinstance(record, dict) or record.get("day") != self._today():
            return 0
        return int(record.get("count") or 0)

    async def _count(self, credential: Credential, spec: ModelSpec) -> None:
        token = f"{credential.id}|{spec.key}"
        record = self.usage.get(token)
        if not isinstance(record, dict) or record.get("day") != self._today():
            record = {"day": self._today(), "count": 0}
        record["count"] = int(record.get("count") or 0) + 1
        self.usage[token] = record
        await self._save_usage()

    def _burn(self, credential: Credential, specs: Any) -> None:
        """Mark every (key, model) pair as spent for the rest of today."""
        for spec in specs:
            if not spec.rpd or spec.provider != credential.provider:
                continue
            self.usage[f"{credential.id}|{spec.key}"] = {"day": self._today(), "count": spec.rpd}

    async def _save_usage(self) -> None:
        if self.store is not None:
            await self.store.set_setting(USAGE_SETTING, self.usage)

    async def _save_health(self) -> None:
        if self.store is not None:
            await self.store.set_setting(
                HEALTH_SETTING, {key: value.to_json() for key, value in self.health.items()}
            )

    def health_of(self, credential: Credential) -> KeyHealth:
        return self.health.get(credential.id) or KeyHealth()

    def remaining(self, credential: Credential, spec: ModelSpec) -> int:
        return spec.budget(self.used(credential, spec))

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------
    @property
    def available(self) -> bool:
        """True when at least one credential could answer right now."""
        if not self.credentials:
            return False
        now = utc_now()
        return any(
            not self.health_of(credential).disabled and not self.health_of(credential).cooling(now)
            for credential in self.credentials
        )

    @property
    def model(self) -> str:
        return self.active_model or self.selection

    def selected_spec(self) -> ModelSpec | None:
        if self.selection and self.selection != "auto":
            return find(self.selection)
        return None

    def plan(self) -> list[tuple[Credential, ModelSpec]]:
        """The ordered (credential, model) pairs one message may try."""
        now = utc_now()
        chosen = self.selected_spec()
        if chosen is not None:
            specs = [chosen]
        else:
            specs = list(auto_plan())

        pairs: list[tuple[Credential, ModelSpec]] = []
        for spec in specs:
            candidates = [
                credential
                for credential in self.credentials
                if credential.provider == spec.provider
                and not self.health_of(credential).disabled
                and not self.health_of(credential).cooling(now)
                and self.remaining(credential, spec) > 0
            ]
            # Most budget left first: the 14,400-a-day backup is a backup, not
            # the voice, and the 20-a-day model should be spent late.
            candidates.sort(key=lambda credential: -self.remaining(credential, spec))
            pairs.extend((credential, spec) for credential in candidates)
        return pairs

    # ------------------------------------------------------------------
    # Calls
    # ------------------------------------------------------------------
    def _client(self, credential: Credential, spec: ModelSpec) -> AIProvider:
        return AIProvider(
            base_url=credential.base_url,
            api_key=credential.key,
            model=spec.api_id,
            timeout=self.timeout,
            enabled=True,
        )

    async def converse(
        self,
        messages: Any,
        *,
        max_tokens: int = 400,
        temperature: float = 0.85,
    ) -> str:
        """Try credentials in order until one answers. Raises the last AIError."""
        if not self.credentials:
            raise AIError("AI provider is not configured", kind="disabled")
        self.requests += 1
        attempts: list[Attempt] = []
        last: AIError | None = None
        for credential, spec in self.plan():
            if len(attempts) >= MAX_ATTEMPTS:
                break
            # The plan was drawn before this message touched anything; a key that
            # has just been refused must not be asked again two models later.
            health = self.health_of(credential)
            if health.disabled or health.cooling(utc_now()) or self.remaining(credential, spec) <= 0:
                continue
            client = self._client(credential, spec)
            try:
                reply = await client.converse(messages, max_tokens=max_tokens, temperature=temperature)
            except AIError as exc:
                last = exc
                attempts.append(Attempt(credential.label, spec.api_id, False, f"{exc.kind}: {exc}"))
                await self._record_failure(credential, spec, exc)
                continue
            except Exception as exc:  # noqa: BLE001 - rotation must survive anything
                last = AIError(f"{type(exc).__name__}: {exc}", kind="network")
                attempts.append(Attempt(credential.label, spec.api_id, False, str(last)))
                await self._record_failure(credential, spec, last)
                continue
            self.active_model = client.active_model or spec.api_id
            self.active_label = credential.label
            self.last_error = ""
            attempts.append(Attempt(credential.label, self.active_model, True))
            self.last_attempts = attempts
            health = self.health.setdefault(credential.id, KeyHealth())
            health.successes += 1
            health.error = ""
            await self._count(credential, spec)
            await self._save_health()
            if len(attempts) > 1:
                self.logger.info("ai: answered by %s after %d attempt(s)", credential.label, len(attempts))
            return reply
        self.failures += 1
        self.last_attempts = attempts
        self.last_error = f"{last.kind}: {last}" if last else "no usable credential"
        raise last or AIError("every credential is spent or disabled", kind="rate_limited")

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 300,
        temperature: float = 0.8,
    ) -> str:
        thread: list[AIMessage] = []
        if system:
            thread.append(AIMessage("system", system))
        thread.append(AIMessage("user", prompt))
        return await self.converse(thread, max_tokens=max_tokens, temperature=temperature)

    async def chat(self, messages: Any, *, max_tokens: int = 400) -> str:
        return await self.converse(messages, max_tokens=max_tokens)

    async def _record_failure(self, credential: Credential, spec: ModelSpec, error: AIError) -> None:
        now = utc_now()
        health = self.health.setdefault(credential.id, KeyHealth())
        health.failures += 1
        health.error = f"{error.kind}: {error}"[:300]
        self.last_error = health.error
        if error.kind == "unauthorized":
            health.disabled = True
            health.cooldown_until = ""
        elif error.kind == "rate_limited":
            # A daily limit is not a 90-second problem. Spend the counter for this
            # model on this key instead of retrying into the same wall.
            message = str(error).lower()
            if "free-models-per-day" in message or "daily" in message:
                if credential.provider == "openrouter":
                    # OpenRouter's allowance is per account, not per model: a
                    # daily refusal there means every model behind that key is
                    # gone until 00:00 UTC.
                    self._burn(credential, catalogue())
                else:
                    # Google counts per model, so the other models on this key
                    # are still worth asking.
                    self._burn(credential, (spec,))
                await self._save_usage()
            else:
                health.cooldown_until = (now + timedelta(seconds=RATE_COOLDOWN)).isoformat()
        elif error.kind in {"network", "timeout"}:
            health.cooldown_until = (now + timedelta(seconds=NETWORK_COOLDOWN)).isoformat()
        await self._save_health()

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    async def probe(self) -> tuple[bool, str]:
        """One real question through the rotation, for `.ai test`."""
        try:
            reply = await self.complete("Reply with the single word: ok", max_tokens=12, temperature=0.0)
        except AIError as exc:
            return False, f"{exc.kind}: {exc}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        reply = " ".join(str(reply).split())
        if not reply:
            return False, "empty reply"
        return True, reply[:80]

    def describe(self) -> dict[str, object]:
        now = utc_now()
        return {
            "provider": "rotating" if self.available else "none",
            "model": self.active_model or self.selection,
            "requests": self.requests,
            "failures": self.failures,
            "keys": len(self.credentials),
            "usable_keys": sum(
                1
                for credential in self.credentials
                if not self.health_of(credential).disabled and not self.health_of(credential).cooling(now)
            ),
        }

    def status(self) -> list[dict[str, Any]]:
        """Everything the UI needs, in one pass: keys, budgets, health."""
        now = utc_now()
        rows: list[dict[str, Any]] = []
        for credential in self.credentials:
            health = self.health_of(credential)
            provider_models = [spec for spec in catalogue() if spec.provider == credential.provider]
            used = {spec.api_id: self.used(credential, spec) for spec in provider_models}
            rows.append(
                {
                    "id": credential.id,
                    "label": credential.label,
                    "provider": credential.provider,
                    "hint": credential.hint,
                    "source": credential.source,
                    "disabled": health.disabled,
                    "cooling": health.cooling(now),
                    "cooldown_until": health.cooldown_until,
                    "error": health.error,
                    "successes": health.successes,
                    "failures": health.failures,
                    "used": used,
                    "budget": sum(self.remaining(credential, spec) for spec in provider_models if spec.available),
                }
            )
        return rows

    # ------------------------------------------------------------------
    # Owner actions
    # ------------------------------------------------------------------
    async def select(self, reference: str) -> ModelSpec | None:
        """Pin a model, or ``auto`` to hand the choice back to the plan."""
        if reference.strip().lower() == "auto":
            self.selection = "auto"
            if self.store is not None:
                await self.store.set_setting(MODEL_SETTING, "auto")
            return None
        spec = find(reference)
        if spec is None or not spec.available:
            return None
        self.selection = spec.key
        if self.store is not None:
            await self.store.set_setting(MODEL_SETTING, spec.key)
        return spec

    async def add_key(self, provider: str, key: str) -> Credential:
        """Add a key at runtime; it is stored in the database, not the repo."""
        provider = (provider or "auto").lower()
        if provider not in PROVIDER_ENDPOINTS:
            # The shape of the key says which provider it belongs to; nobody
            # should have to remember that `AQ.` means Google.
            provider = "google" if key.strip().startswith("AQ.") else "openrouter"
        label = f"{provider}-{sum(1 for c in self.credentials if c.provider == provider) + 1}"
        credential = Credential(provider, label, key.strip(), "database")
        self.credentials.append(credential)
        if self.store is not None:
            stored = list(await self.store.get_setting(KEYS_SETTING, []) or [])
            stored.append({"provider": provider, "label": label, "key": key.strip()})
            await self.store.set_setting(KEYS_SETTING, stored)
        await self._save_health()
        return credential

    async def retire(self, reference: str) -> bool:
        """Forget a key by label or id (the file-owned ones come back on reload)."""
        target = next(
            (
                credential
                for credential in self.credentials
                if reference in {credential.label, credential.id}
            ),
            None,
        )
        if target is None:
            return False
        self.credentials = [c for c in self.credentials if c is not target]
        self.health.pop(target.id, None)
        if self.store is not None:
            stored = [
                item
                for item in (await self.store.get_setting(KEYS_SETTING, []) or [])
                if item.get("key") != target.key
            ]
            await self.store.set_setting(KEYS_SETTING, stored)
            await self._save_health()
        return True

    async def reset_health(self) -> None:
        """Clear cooldowns and re-enable every key — after a quota reset."""
        self.health = {}
        self.usage = {}
        await self._save_health()
        await self._save_usage()


def openrouter_placeholder() -> ModelSpec:
    """The OpenRouter pseudo-model, exported for the UI's provider header."""
    return next(spec for spec in catalogue() if spec.key == f"openrouter:{OPENROUTER_MODEL}")


__all__ = [
    "AIRouter",
    "Attempt",
    "Credential",
    "KeyHealth",
    "read_key_file",
]
