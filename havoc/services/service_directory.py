"""Where the services are right now, and how to talk to them.

Three services run on their own GitHub runners, in their own workflows, and
publish a small file when they come up:

    .services/search.json   {url, run_id, started_at, expires_at, gate_port, model}

The address changes on every restart, so the bot cannot be configured with it.
It reads it. That is the whole idea of this module: one place that answers
"where is the picture server", with three properties that matter more than the
lookup itself.

**It never throws at a caller.** A service between runs is a *state*
(``between_runs``), not an exception. ``.draw`` prints "the picture server is
between runs, last seen 40 minutes ago" and the chat stays usable.

**It does not trust the clock or the file.** A published file can be stale (the
service died without cleaning up) or missing (never started). Both are detected,
both are described, and neither is guessed at.

**It computes the token, it never reads one.** The bearer token is
HMAC-SHA256(SERVICE_SHARED_SECRET, "service:run_id:started_at") — the same
derivation the gate uses, so nothing secret is ever committed to a public
repository, and a leaked token dies with the run.

Caching is 60 seconds: enough that a burst of picture polls or a search plus a
follow-up costs one git fetch, short enough that a restart is noticed within a
minute. ``refresh()`` forces a re-read for ``.services``, because a card that
says "between runs" when it has just come back is a card that lies.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .service_token import SERVICES, token_for

#: How long a resolved answer is reused. See the module docstring for why 60.
CACHE_SECONDS = 60.0
#: A published file older than this is treated as expired even if its own
#: ``expires_at`` says otherwise: a clock put forward by six hours is a bug, a
#: six-hour-old publish is a corpse.
MAX_AGE_SECONDS = 6 * 3600 + 900
#: How often a long-running process re-reads `.services/*.json` from the branch.
#: A run of the bot lasts six hours; a service that is restarted in the middle of
#: it publishes a new address, and the checkout on disk will never notice. Five
#: minutes is a compromise: one cheap `git fetch` (or two `git show`s) against a
#: repo whose only changes are those small files.
PULL_SECONDS = 300.0
#: Three consecutive failures and the resolver stops trying for a minute, so a
#: dead service costs one timeout per minute instead of one per request.
BREAKER_THRESHOLD = 3
BREAKER_SECONDS = 60.0

#: connect / read. A drawing polls ``/history`` for a minute or two, and each
#: poll is a separate fast request, so 8 seconds of read is generous without
#: being a hang.
CONNECT_TIMEOUT = 2.0
READ_TIMEOUT = 8.0

STATE_OK = "ok"
STATE_BETWEEN_RUNS = "between_runs"
STATE_NOT_FOUND = "not_found"
STATE_EXPIRED = "expired"
STATE_UNCONFIGURED = "unconfigured"
STATE_BROKEN = "broken"

STATE_KEYS = {
    STATE_OK: "services.state_ok",
    STATE_BETWEEN_RUNS: "services.state_between",
    STATE_NOT_FOUND: "services.state_never",
    STATE_EXPIRED: "services.state_expired",
    STATE_UNCONFIGURED: "services.state_unconfigured",
    STATE_BROKEN: "services.state_broken",
}


@dataclass
class Endpoint:
    """One service, as far as the bot can tell."""

    name: str
    state: str = STATE_UNCONFIGURED
    url: str = ""
    run_id: str = ""
    started_at: str = ""
    expires_at: str = ""
    model: str = ""
    version: str = ""
    published_at: str = ""
    #: Seconds since the published file was written, or ``None`` when unknown.
    age: float | None = None
    #: A sentence, not a stack trace, when the state is not ``ok``.
    note: str = ""
    #: ``False`` for the local/env-configured case, where the token gate is not
    #: in the path at all.
    gated: bool = False
    token: str = ""

    @property
    def ok(self) -> bool:
        return self.state == STATE_OK and bool(self.url)

    @property
    def base_url(self) -> str:
        """The URL without a trailing slash, ready to append a path to."""
        return self.url.rstrip("/")

    def headers(self) -> dict[str, str]:
        if self.gated and self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return {}

    def snapshot(self) -> dict[str, Any]:
        return {
            "service": self.name,
            "state": self.state,
            "url": self.url,
            "age_seconds": round(self.age, 1) if self.age is not None else None,
            "model": self.model,
            "version": self.version,
            "run_id": self.run_id,
            "expires_at": self.expires_at,
            "gated": self.gated,
            "note": self.note,
        }


@dataclass
class ServiceDirectory:
    """Reads ``.services/<name>.json`` and turns it into an endpoint or a reason."""

    root: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent.parent)
    branch: str = ""
    secret: str = ""
    logger: Any = None
    now: Any = time.time
    #: ``None`` means "look at the committed files"; a dict replaces the files
    #: entirely, which is how tests drive every state without a repository.
    override: dict[str, dict[str, Any]] | None = None
    _cache: dict[str, tuple[float, Endpoint]] = field(default_factory=dict, repr=False)
    _failures: dict[str, int] = field(default_factory=dict, repr=False)
    _tripped_at: dict[str, float] = field(default_factory=dict, repr=False)
    _raw: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    _last_pull: float = field(default=0.0, repr=False)
    _pull_lock: Any = field(default_factory=threading.Lock, repr=False)
    _pull_note: str = field(default="", repr=False)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls, *, root: Path | None = None, logger: Any = None) -> ServiceDirectory:
        return cls(
            root=root or Path(__file__).resolve().parent.parent.parent,
            branch=(os.environ.get("SERVICE_BRANCH") or os.environ.get("GITHUB_REF_NAME") or "").strip(),
            secret=(os.environ.get("SERVICE_SHARED_SECRET") or "").strip(),
            logger=logger,
        )

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------
    def _path_for(self, name: str) -> Path:
        return Path(self.root) / ".services" / f"{name}.json"

    def raw(self, name: str) -> dict[str, Any]:
        """The published file's contents, or ``{}``. Never raises."""
        if self.override is not None:
            return dict(self.override.get(name) or {})
        if name in self._raw:
            return self._raw[name]
        body: dict[str, Any] = {}
        path = self._path_for(name)
        try:
            if path.exists():
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    body = loaded
        except (OSError, ValueError) as exc:
            if self.logger is not None:
                self.logger.debug("service file %s unreadable: %s", name, exc)
        self._raw[name] = body
        return body

    def pull(self, *, force: bool = False) -> bool:
        """Start a throttled background refresh of `.services/*.json`.

        The bot runs for six hours from one checkout. If a service dies at minute
        ninety and its successor publishes a new tunnel address, nothing on disk
        changes — so without this the bot would keep knocking on a dead hostname
        until its own run ended.

        It is a *thread*, never an inline `git fetch`: `resolve()` is called from
        the event loop while a person is waiting for an answer, and a fetch can
        take seconds. The result lands in `_raw` a moment later and the next
        resolve sees it.
        """
        if self.override is not None or not self.branch:
            return False
        if not force and self.now() - self._last_pull < PULL_SECONDS:
            return False
        if not self._pull_lock.acquire(blocking=False):
            return False
        self._last_pull = self.now()
        thread = threading.Thread(target=self._pull_blocking, name="services-pull", daemon=True)
        try:
            thread.start()
        finally:
            self._pull_lock.release()
        return True

    def pull_now(self, *, force: bool = True) -> bool:
        """Fetch and read the branch's service files synchronously, and say if anything changed."""
        if self.override is not None or not self.branch:
            return False
        if not force and self.now() - self._last_pull < PULL_SECONDS:
            return False
        self._last_pull = self.now()
        return self._pull_blocking()

    def _pull_blocking(self) -> bool:
        """The fetch itself. Never raises; a broken `git` is not a broken bot."""
        try:
            self._pull_note = ""
            remote = f"origin/{self.branch}"
            fetch = subprocess.run(
                ["git", "-C", str(self.root), "fetch", "--quiet", "--depth", "1", "origin", self.branch],
                capture_output=True,
                text=True,
                timeout=45,
            )
            if fetch.returncode != 0:
                self._pull_note = (fetch.stderr or "git fetch failed").strip()[:200]
                return False
            updated = False
            for name in SERVICES:
                shown = subprocess.run(
                    ["git", "-C", str(self.root), "show", f"{remote}:.services/{name}.json"],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                if shown.returncode != 0:
                    continue  # never published on this branch: keep what we have
                try:
                    body = json.loads(shown.stdout)
                except ValueError:
                    continue
                if not isinstance(body, dict):
                    continue
                if body != self._raw.get(name):
                    updated = True
                self._raw[name] = body
                self._cache.pop(name, None)
            return updated
        except (OSError, subprocess.SubprocessError) as exc:
            self._pull_note = str(exc)[:200]
            return False

    def refresh(self) -> None:
        """Forget everything cached — the next resolve re-reads the files."""
        self._last_pull = 0.0
        self._cache.clear()
        self._raw.clear()
        self._failures.clear()
        self._tripped_at.clear()

    # ------------------------------------------------------------------
    # Resolving
    # ------------------------------------------------------------------
    def resolve(self, name: str, *, force: bool = False) -> Endpoint:
        """Where ``name`` is right now, or a state that explains why not."""
        now = self.now()
        cached = self._cache.get(name)
        if cached is not None and not force and now - cached[0] < CACHE_SECONDS:
            # Throttled to PULL_SECONDS and off the event loop, so a bot that
            # asks for the same service twice in a row pays nothing.
            if not cached[1].ok:
                self.pull()
            return self._cache.get(name, cached)[1]

        if not force:
            # Missing or expired: the answer may simply be newer than our
            # checkout. Ask for it in the background and answer with what we know.
            self.pull()

        tripped = self._tripped_at.get(name)
        if tripped is not None and not force:
            if now - tripped < BREAKER_SECONDS:
                previous = self._cache.get(name)
                endpoint = previous[1] if previous else Endpoint(name=name, state=STATE_BROKEN)
                endpoint.state = STATE_BROKEN
                endpoint.note = f"skipped after {BREAKER_THRESHOLD} failures in a row"
                return endpoint
            # The breaker's minute is over: give the service one fresh chance.
            self._failures.pop(name, None)
            self._tripped_at.pop(name, None)

        endpoint = self._build(name, now)
        self._cache[name] = (now, endpoint)
        return endpoint

    def _build(self, name: str, now: float) -> Endpoint:
        body = self.raw(name)
        if not body:
            return Endpoint(
                name=name,
                state=STATE_NOT_FOUND,
                note="it has never published an address — the workflow has not run yet",
            )
        url = str(body.get("url") or "").rstrip("/")
        run_id = str(body.get("run_id") or "")
        started_at = str(body.get("started_at") or "")
        expires_at = str(body.get("expires_at") or "")
        published_at = str(body.get("published_at") or "")
        age = _age_seconds(published_at or started_at, now)

        if not url:
            return Endpoint(
                name=name,
                state=STATE_BETWEEN_RUNS,
                run_id=run_id,
                started_at=started_at,
                age=age,
                note="it published no address — between runs, or the tunnel did not open",
            )
        if _is_expired(expires_at, now) or (age is not None and age > MAX_AGE_SECONDS):
            return Endpoint(
                name=name,
                state=STATE_EXPIRED,
                url=url,
                run_id=run_id,
                started_at=started_at,
                expires_at=expires_at,
                age=age,
                note="its published address is past its six hours; the next run has not started",
            )

        gated = bool(run_id and started_at)
        token = token_for(name, run_id, started_at, self.secret) if gated else ""
        if gated and not token:
            # A gate exists but we have no secret to open it with. Saying so here
            # is far better than a 401 from inside a feature that cannot explain
            # it.
            return Endpoint(
                name=name,
                state=STATE_BROKEN,
                url=url,
                run_id=run_id,
                started_at=started_at,
                age=age,
                note="the route is published but SERVICE_SHARED_SECRET is not set, so the gate will refuse me",
            )
        return Endpoint(
            name=name,
            state=STATE_OK,
            url=url,
            run_id=run_id,
            started_at=started_at,
            expires_at=expires_at,
            model=str(body.get("model") or ""),
            version=str(body.get("version") or ""),
            published_at=published_at,
            age=age,
            gated=gated,
            token=token,
        )

    # ------------------------------------------------------------------
    # Health, for the breaker
    # ------------------------------------------------------------------
    def note_success(self, name: str) -> None:
        self._failures.pop(name, None)
        self._tripped_at.pop(name, None)

    def note_failure(self, name: str) -> None:
        count = self._failures.get(name, 0) + 1
        self._failures[name] = count
        if count >= BREAKER_THRESHOLD:
            self._tripped_at[name] = self.now()
            if self.logger is not None:
                self.logger.warning("service %s tripped the breaker after %d failures", name, count)

    def failures(self, name: str) -> int:
        return self._failures.get(name, 0)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def card(self) -> list[Endpoint]:
        """Every known service, freshly read, in a stable order."""
        self.refresh()
        return [self.resolve(name, force=True) for name in SERVICES]

    async def probe(self, name: str, *, path: str = "/healthz", timeout: float = 5.0) -> tuple[bool, str]:
        """One real request to ``/healthz``. ``(ok, detail-for-the-owner)``."""
        endpoint = self.resolve(name)
        if not endpoint.ok:
            return False, endpoint.note or "no address"
        from .http import HttpError, request

        try:
            response = await asyncio.wait_for(
                request("GET", f"{endpoint.base_url}{path}", headers=endpoint.headers(), timeout=timeout),
                timeout=timeout + 1,
            )
        except (TimeoutError, HttpError, Exception) as exc:  # noqa: BLE001 - a probe never throws
            self.note_failure(name)
            detail = str(exc)[:160]
            if "401" in detail:
                return False, "the gate refused me — the shared secret does not match this run"
            return False, detail
        if response.status == 200:
            self.note_success(name)
            return True, "reachable"
        if response.status == 401:
            self.note_failure(name)
            return False, "the gate refused me — the shared secret does not match this run"
        if response.status == 502:
            self.note_failure(name)
            return False, "the gate is up but the service behind it is not answering"
        return False, f"answered HTTP {response.status}"


def _parse_stamp(value: str) -> float | None:
    if not value:
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _age_seconds(stamp: str, now: float) -> float | None:
    parsed = _parse_stamp(stamp)
    if parsed is None:
        return None
    return max(0.0, now - parsed)


def _is_expired(expires_at: str, now: float) -> bool:
    parsed = _parse_stamp(expires_at)
    if parsed is None:
        return False
    return now > parsed


def describe_age(seconds: float | None, language: str = "en") -> str:
    """``40 minutes ago`` / ``2 hours ago`` — a sentence, not a timestamp."""
    if seconds is None:
        return "at an unknown time" if language == "en" else "زمان نامعلوم"
    minutes = int(seconds // 60)
    if minutes < 1:
        return "just now" if language == "en" else "همین حالا"
    if minutes < 60:
        return f"{minutes} minutes ago" if language == "en" else f"{minutes} دقیقه پیش"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} hours ago" if language == "en" else f"{hours} ساعت پیش"
    return f"{hours // 24} days ago" if language == "en" else f"{hours // 24} روز پیش"


__all__ = [
    "BREAKER_THRESHOLD",
    "CACHE_SECONDS",
    "PULL_SECONDS",
    "CONNECT_TIMEOUT",
    "READ_TIMEOUT",
    "Endpoint",
    "ServiceDirectory",
    "STATE_BETWEEN_RUNS",
    "STATE_BROKEN",
    "STATE_EXPIRED",
    "STATE_KEYS",
    "STATE_NOT_FOUND",
    "STATE_OK",
    "STATE_UNCONFIGURED",
    "describe_age",
]
