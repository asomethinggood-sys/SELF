"""The open web, through a search engine the owner runs himself.

Telegram history is searchable in ``.search`` — that is *your* messages. This is
the other thing people mean by "search": the actual internet. Doing it through an
API key is a subscription; doing it through a scraped Google page is a captcha.
SearXNG is neither: it is a metasearch front end that runs on the same machine
(or the same container) and returns ordinary JSON.

Two ways to find the instance, in this order:

* ``SEARXNG_URL`` — ``http://127.0.0.1:8080`` for a container next to the bot,
  or a tunnel someone else keeps alive. Ungated: a loopback instance has no gate.
* **the service directory** — when the URL is unset, the address comes from
  ``.services/search.json``, which the ``service-search`` workflow publishes on
  every restart, along with the bearer token the gate in front of it wants. That
  is what makes the engine survive a runner dying: nobody has to re-configure the
  bot, because the bot reads where the engine *is*.

Unset *and* unpublished means the feature is off and says so.

There are two ways a search can come back empty, and they are not the same
thing: no results, and *every engine refused us*. A datacenter IP gets captcha'd
by DuckDuckGo within minutes, and SearXNG reports that per engine
(``unresponsive_engines``). The client says which engine refused, and falls back
to an in-process engine (``ddgs``) before it gives up, because "the engines say
no" is a different sentence from "there is nothing to find".

The client is defensive because SearXNG deployments vary: the JSON format has to
be enabled in its ``settings.yml``, some instances rate-limit, and a private one
often has no ``answers`` block at all. None of that is an error the person in the
chat should see as a crash — an unreachable engine is a sentence, not a
traceback.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .http import HttpError, get_json

#: A search is a query, not a crawl. Longer than this and the page is already a
#: different product.
DEFAULT_TIMEOUT = 20.0
MAX_RESULTS = 10


class SearchError(Exception):
    """A search that could not be made, with a sentence for the owner."""

    def __init__(self, message: str, *, kind: str = "failed") -> None:
        self.kind = kind
        super().__init__(message)


@dataclass(frozen=True)
class Result:
    title: str
    url: str
    snippet: str = ""
    engine: str = ""

    def line(self) -> str:
        return f"{self.title} — {self.url}"


@dataclass
class WebSearch:
    """Thin client over one SearXNG instance."""

    url: str = ""
    timeout: float = DEFAULT_TIMEOUT
    #: Which engines to ask. Empty means the instance's defaults, which is what a
    #: self-hosted setup configured for its own taste.
    engines: tuple[str, ...] = ()
    language: str = ""
    requests: int = 0
    failures: int = 0
    last_error: str = ""
    #: The bearer token for a hosted instance, read from the service directory.
    token: str = ""
    #: True when the URL came from ``.services/search.json`` and a gate guards it.
    gated: bool = False
    #: The directory to ask when ``url`` is empty. ``None`` means "no hosting".
    directory: Any = None
    #: Last engines the instance said it could not ask.
    blocked_engines: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, directory: Any = None) -> WebSearch | None:
        url = (os.environ.get("SEARXNG_URL") or "").strip()
        engines = tuple(
            item.strip()
            for item in (os.environ.get("SEARXNG_ENGINES") or "").split(",")
            if item.strip()
        )
        if not url and directory is None:
            return None
        if not url and directory is not None:
            # No env var: the address is whatever the last service run published.
            # The object exists either way, so `.web` can say "between runs"
            # instead of "not configured".
            return cls(
                url="",
                timeout=float(os.environ.get("SEARXNG_TIMEOUT") or DEFAULT_TIMEOUT),
                engines=engines,
                directory=directory,
            )
        return cls(
            url=url.rstrip("/"),
            timeout=float(os.environ.get("SEARXNG_TIMEOUT") or DEFAULT_TIMEOUT),
            engines=engines,
        )

    def _target(self) -> tuple[str, dict[str, str], str]:
        """``(base_url, headers, reason)`` — resolved lazily, so a restart is picked up.

        A hosted instance moves every six hours; re-reading the published file on
        each search (through a 60-second cache) is what makes that invisible
        instead of a stack trace.
        """
        if self.url:
            return self.url, ({"Authorization": f"Bearer {self.token}"} if self.gated and self.token else {}), ""
        if self.directory is None:
            return "", {}, "no search engine is configured"
        endpoint = self.directory.resolve("search")
        if not getattr(endpoint, "ok", False):
            return "", {}, getattr(endpoint, "note", "") or "the search service is between runs"
        return endpoint.base_url, endpoint.headers(), ""

    @property
    def available(self) -> bool:
        if self.url:
            return True
        if self.directory is not None:
            return bool(getattr(self.directory.resolve("search"), "ok", False))
        return False

    def describe(self) -> dict[str, Any]:
        return {
            "provider": "searxng" if self.available else "none",
            "url": self.url,
            "engines": list(self.engines),
            "requests": self.requests,
            "failures": self.failures,
            "last_error": self.last_error,
        }

    async def search(
        self,
        query: str,
        *,
        limit: int = 5,
        language: str = "",
        categories: tuple[str, ...] = ("general",),
        safe: int = 0,
    ) -> list[Result]:
        """Ask the instance and return at most ``limit`` usable results.

        Raises :class:`SearchError` when the engine cannot be reached at all —
        "no results" is a normal answer and is *not* an exception. When every
        engine refuses (a captcha on a datacenter IP) and nothing came back, the
        in-process fallback is tried before that becomes the answer.
        """
        text = " ".join(str(query or "").split())
        if not text:
            raise SearchError("there is nothing to search for", kind="empty")
        base, headers, reason = self._target()
        if not base:
            raise SearchError(reason or "no search engine is configured", kind="disabled")
        count = max(1, min(int(limit), MAX_RESULTS))
        params = {
            "q": text,
            "format": "json",
            "safesearch": str(max(0, min(2, int(safe)))),
        }
        if categories:
            params["categories"] = ",".join(categories)
        if self.engines:
            params["engines"] = ",".join(self.engines)
        chosen = language or self.language
        if chosen in {"en", "fa"}:
            params["language"] = f"{chosen}-{'US' if chosen == 'en' else 'IR'}"
        try:
            payload = await get_json(f"{base}/search", params=params, timeout=self.timeout, headers=headers)
        except HttpError as exc:
            self.failures += 1
            self.last_error = str(exc)[:200]
            if exc.status == 401:
                raise SearchError(
                    "the gate refused me — the shared secret does not match this run",
                    kind="unauthorized",
                ) from exc
            if exc.status in {403, 404}:
                raise SearchError(
                    "the search instance refused the JSON format — enable `json` in its settings.yml",
                    kind="misconfigured",
                ) from exc
            raise SearchError(f"the search engine did not answer: {exc}", kind="network") from exc
        self.requests += 1
        rows = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise SearchError("the search engine answered in a shape i do not know", kind="bad_response")
        self.blocked_engines = _refused(payload)
        results = _clean(rows, count)
        if results:
            return results
        # Empty and every engine refused is a captcha, not a quiet nothing.
        if self.blocked_engines:
            fallback = await self._search_direct(text, count)
            if fallback:
                return fallback
            names = ", ".join(self.blocked_engines[:3])
            raise SearchError(
                f"the engines refused this address — {names} asked for a captcha, "
                "which is what a datacenter IP gets. nothing to show from here",
                kind="blocked",
            )
        return results

    async def _search_direct(self, text: str, limit: int) -> list[Result]:
        """Last resort: query the web from inside this process.

        ``ddgs`` is optional. When it is installed it turns "every engine in my
        instance is captcha'd" from a dead end into a slower answer; when it is
        not, the caller gets the honest sentence above.
        """
        try:
            from ddgs import DDGS  # type: ignore[import-not-found]
        except Exception:  # noqa: BLE001 - optional, and ImportError is the normal case
            try:
                from duckduckgo_search import DDGS  # type: ignore[import-not-found,no-redef]
            except Exception:  # noqa: BLE001
                return []
        import asyncio

        def _run() -> list[dict[str, Any]]:
            with DDGS() as engine:
                return list(engine.text(text, max_results=limit) or [])

        try:
            rows = await asyncio.wait_for(asyncio.to_thread(_run), timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001 - a fallback that fails is not a crash
            self.last_error = f"fallback: {exc}"[:200]
            return []
        return _clean(rows, limit)


def _refused(payload: Any) -> tuple[str, ...]:
    """The engines SearXNG says it could not ask, as names.

    ``unresponsive_engines`` is a list of ``[name, reason]`` pairs; the shape has
    changed once already, so both are handled and anything unexpected is dropped
    rather than raised.
    """
    if not isinstance(payload, dict):
        return ()
    raw = payload.get("unresponsive_engines")
    if not isinstance(raw, list):
        return ()
    names: list[str] = []
    for item in raw:
        if isinstance(item, (list, tuple)) and item:
            names.append(str(item[0])[:30])
        elif isinstance(item, str):
            names.append(item[:30])
    return tuple(names)


def _clean(rows: list[Any], limit: int) -> list[Result]:
    """Drop the duplicates and the empties a metasearch engine always returns."""
    seen: set[str] = set()
    out: list[Result] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "").strip()
        title = " ".join(str(row.get("title") or "").split())
        if not url or not title or url in seen:
            continue
        if not url.lower().startswith(("http://", "https://")):
            continue
        seen.add(url)
        out.append(
            Result(
                title=title[:160],
                url=url[:300],
                snippet=" ".join(str(row.get("content") or "").split())[:300],
                engine=str(row.get("engine") or "")[:40],
            )
        )
        if len(out) >= limit:
            break
    return out


def from_env(directory: Any = None) -> WebSearch | None:
    return WebSearch.from_env(directory)


__all__ = ["Result", "SearchError", "WebSearch", "from_env"]
