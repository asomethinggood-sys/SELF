"""Continuous off-machine snapshots of everything the bot stores.

The Actions cache is the fast path for a restart, but it is a cache: it can
miss, be evicted, or be scoped away, and a miss looks exactly like a first
boot. This module gives the data a second, independent home — a JSON snapshot
committed through the GitHub contents API on the same branch.

What it contains: exactly the tables listed in ``Store.BACKUP_TABLES``, read
through the same exporter the manual backup command uses. Sessions, the phone
number and the API credentials are not in those tables, so a snapshot cannot
leak them.

The design rule is that a snapshot must never be able to hurt: every call here
is best effort, errors are logged and swallowed, and the bot keeps running with
whatever it has.

A restore is additive: rows are inserted or updated by primary key and nothing
is deleted, so importing a snapshot can fill gaps but cannot wipe a table.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .services.http import HttpError, request
from .utils.time import iso, utc_now

SNAPSHOT_VERSION = 1
DEFAULT_PATH = ".data/snapshot.json"
DEFAULT_MINUTES = 5
API_ROOT = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")


@dataclass(frozen=True)
class SnapshotSettings:
    """Where a snapshot lives. ``enabled`` is false unless a token is present."""

    token: str
    repo: str
    branch: str
    path: str = DEFAULT_PATH
    minutes: int = DEFAULT_MINUTES

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.repo and self.branch)

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> SnapshotSettings:
        env = os.environ if environ is None else environ
        minutes = DEFAULT_MINUTES
        raw = env.get("HAVOC_SNAPSHOT_MINUTES", "")
        if raw.isdigit() and int(raw) > 0:
            minutes = int(raw)
        return cls(
            token=env.get("HAVOC_SNAPSHOT_TOKEN", "") or env.get("GITHUB_TOKEN", ""),
            repo=env.get("GITHUB_REPOSITORY", ""),
            branch=env.get("HAVOC_SNAPSHOT_BRANCH", "")
            or env.get("GITHUB_REF_NAME", "")
            or env.get("GITHUB_HEAD_REF", ""),
            path=env.get("HAVOC_SNAPSHOT_PATH", DEFAULT_PATH),
            minutes=minutes,
        )

    def url(self) -> str:
        return f"{API_ROOT}/repos/{self.repo}/contents/{self.path.lstrip('/')}"

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }


def strip_diagnostics(tables: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    """Drop bookkeeping rows so a restore cannot resurrect stale counters.

    ``diagnostics.*`` settings describe the machine that wrote them, not the
    user's data, and copying them back would make a fresh boot look like the
    previous one.
    """
    cleaned = dict(tables)
    rows = cleaned.get("settings")
    if rows:
        cleaned["settings"] = [
            row for row in rows if not str(row.get("key", "")).startswith("diagnostics.")
        ]
    return cleaned


def build_payload(tables: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """The document that gets committed."""
    return {
        "app": "havoc",
        "version": SNAPSHOT_VERSION,
        "created_at": iso(utc_now()),
        "tables": tables,
    }


def digest(payload: dict[str, Any]) -> str:
    """A stable fingerprint, ignoring the timestamp, to skip pointless commits."""
    body = json.dumps(payload.get("tables", {}), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def is_empty(tables: dict[str, list[dict[str, Any]]], *, ignore: tuple[str, ...] = ("features", "settings")) -> bool:
    """True when nothing worth restoring is stored.

    Feature switches and settings are seeded on first boot, so their presence
    says nothing about whether real data survived.
    """
    for table, rows in tables.items():
        if table in ignore:
            continue
        if rows:
            return False
    return True


#: Tables that the runtime fills in on a first boot. Their presence says
#: nothing about whether real data survived a restart.
SEEDED_TABLES: tuple[str, ...] = ("features", "settings", "language_prefs", "chat_settings")


def adds_data(
    local: dict[str, list[dict[str, Any]]],
    remote: dict[str, list[dict[str, Any]]],
    *,
    ignore: tuple[str, ...] = SEEDED_TABLES,
) -> bool:
    """True when the snapshot holds a table the local database has nothing in.

    This is the whole decision: if every table that matters is already
    populated locally there is nothing to restore. Seeded tables are ignored
    because the runtime fills them on a first boot, which is exactly why a boot
    that lost everything used to look complete.
    """
    for table, rows in remote.items():
        if not rows or table in ignore:
            continue
        if not local.get(table):
            return True
    return False


def _decode(content: str) -> dict[str, Any]:
    raw = base64.b64decode(content.encode("ascii"))
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("tables"), dict):
        raise ValueError("snapshot is not a havoc snapshot")
    return data


async def fetch(settings: SnapshotSettings, *, timeout: float = 20.0) -> tuple[dict[str, Any], str] | None:
    """Read the committed snapshot, or None when there is not one yet."""
    if not settings.enabled:
        return None
    url = f"{settings.url()}?ref={settings.branch}"
    try:
        response = await request("GET", url, headers=settings.headers(), timeout=timeout)
    except HttpError as exc:
        if exc.status == 404:
            return None
        raise
    body = response.json()
    if not isinstance(body, dict) or "content" not in body:
        return None
    return _decode(body["content"]), str(body.get("sha", ""))


async def push(settings: SnapshotSettings, payload: dict[str, Any], *, timeout: float = 30.0) -> str:
    """Commit the snapshot, creating the file or replacing it.

    Returns the new blob sha. Raises HttpError on failure so the caller can
    decide how loudly to complain.
    """
    remote = await fetch(settings, timeout=timeout)
    sha = remote[1] if remote else ""
    document = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    content = base64.b64encode(document.encode("utf-8")).decode("ascii")
    stamp = payload.get("created_at") or iso(utc_now())
    body: dict[str, Any] = {
        "message": f"data snapshot {stamp}",
        "content": content,
        "branch": settings.branch,
    }
    for attempt in range(3):
        if sha:
            body["sha"] = sha
        try:
            response = await request(
                "PUT", settings.url(), json_body=body, headers=settings.headers(), timeout=timeout
            )
        except HttpError as exc:
            # Another writer touched the file between our read and our write.
            # Re-read the sha and try again rather than losing this snapshot.
            if exc.status not in (409, 422) or attempt == 2:
                raise
            remote = await fetch(settings, timeout=timeout)
            sha = remote[1] if remote else ""
            continue
        result = response.json()
        return str((result.get("content") or {}).get("sha", ""))
    raise HttpError("could not commit the snapshot", kind="conflict")


async def store_tables(store: Any, *, timeout: float = 20.0) -> dict[str, list[dict[str, Any]]]:
    return await store.export_tables()


def parse_timestamp(payload: dict[str, Any]) -> datetime | None:
    raw = payload.get("created_at")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None
