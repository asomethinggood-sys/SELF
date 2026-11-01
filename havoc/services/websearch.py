"""The open web, through a search engine the owner runs himself.

Telegram history is searchable in ``.search`` — that is *your* messages. This is
the other thing people mean by "search": the actual internet. Doing it through an
API key is a subscription; doing it through a scraped Google page is a captcha.
SearXNG is neither: it is a metasearch front end that runs on the same machine
(or the same container) and returns ordinary JSON.

There is exactly one option, and it is a URL:

* ``SEARXNG_URL`` — ``http://127.0.0.1:8080`` for a container next to the bot.
  Unset means the feature is off and says so, which is better than a key the
  owner has to go and find.

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

    @classmethod
    def from_env(cls) -> WebSearch | None:
        url = (os.environ.get("SEARXNG_URL") or "").strip()
        if not url:
            return None
        engines = tuple(
            item.strip()
            for item in (os.environ.get("SEARXNG_ENGINES") or "").split(",")
            if item.strip()
        )
        return cls(
            url=url.rstrip("/"),
            timeout=float(os.environ.get("SEARXNG_TIMEOUT") or DEFAULT_TIMEOUT),
            engines=engines,
        )

    @property
    def available(self) -> bool:
        return bool(self.url)

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
        "no results" is a normal answer and is *not* an exception.
        """
        text = " ".join(str(query or "").split())
        if not text:
            raise SearchError("there is nothing to search for", kind="empty")
        if not self.available:
            raise SearchError("no search engine is configured", kind="disabled")
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
            payload = await get_json(f"{self.url}/search", params=params, timeout=self.timeout)
        except HttpError as exc:
            self.failures += 1
            self.last_error = str(exc)[:200]
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
        return _clean(rows, count)


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


def from_env() -> WebSearch | None:
    return WebSearch.from_env()


__all__ = ["Result", "SearchError", "WebSearch", "from_env"]
