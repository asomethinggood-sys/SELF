"""The committed snapshot is the safety net under the Actions cache.

Everything here runs against a fake contents API: no network, no token.
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from typing import Any

import pytest

from havoc import snapshot
from havoc.snapshot import SnapshotSettings


class FakeGithub:
    """A minimal contents API: a dict of path -> file, created then updated."""

    def __init__(self, *, existing: dict[str, Any] | None = None) -> None:
        self.files: dict[str, bytes] = {}
        self.shas: dict[str, str] = {}
        self.writes: list[dict[str, Any]] = []
        self.fail_with: Exception | None = None
        if existing is not None:
            self.files[snapshot.DEFAULT_PATH] = json.dumps(existing).encode("utf-8")
            self.shas[snapshot.DEFAULT_PATH] = "sha-1"

    @property
    def file(self) -> bytes | None:
        return self.files.get(snapshot.DEFAULT_PATH)

    def json_at(self, path: str) -> Any:
        raw = self.files.get(path)
        return None if raw is None else json.loads(raw)

    @staticmethod
    def _path_of(url: str) -> str:
        return url.split("/contents/", 1)[1].split("?", 1)[0]

    async def __call__(self, method: str, url: str, **kwargs: Any) -> Any:
        if self.fail_with is not None:
            raise self.fail_with
        path = self._path_of(url)
        if method == "GET":
            if path not in self.files:
                from havoc.services.http import HttpError

                raise HttpError("not found", kind="http_error", status=404)
            content = base64.b64encode(self.files[path]).decode("ascii")
            sha = self.shas[path]
            return SimpleNamespace(status=200, json=lambda: {"content": content, "sha": sha})
        body = kwargs["json_body"]
        self.writes.append({**body, "path": path})
        self.files[path] = base64.b64decode(body["content"])
        self.shas[path] = f"sha-{len(self.writes) + 1}"
        return SimpleNamespace(status=200, json=lambda: {"content": {"sha": self.shas[path]}})


@pytest.fixture()
def settings() -> SnapshotSettings:
    return SnapshotSettings(token="tok", repo="owner/repo", branch="work", path=".data/snapshot.json")


def test_it_stays_off_without_a_token() -> None:
    """No token means no network calls and no surprises in tests or locally."""
    disabled = SnapshotSettings.from_env({})
    assert disabled.enabled is False
    assert SnapshotSettings.from_env({"GITHUB_TOKEN": "x"}).enabled is False  # no repository
    enabled = SnapshotSettings.from_env(
        {"GITHUB_TOKEN": "x", "GITHUB_REPOSITORY": "o/r", "GITHUB_REF_NAME": "main"}
    )
    assert enabled.enabled is True
    assert enabled.branch == "main"
    assert enabled.path == snapshot.DEFAULT_PATH


def test_the_token_can_be_overridden() -> None:
    custom = SnapshotSettings.from_env(
        {
            "GITHUB_TOKEN": "builtin",
            "HAVOC_SNAPSHOT_TOKEN": "explicit",
            "GITHUB_REPOSITORY": "o/r",
            "GITHUB_REF_NAME": "main",
            "HAVOC_SNAPSHOT_MINUTES": "2",
            "HAVOC_SNAPSHOT_PATH": "state/backup.json",
        }
    )
    assert custom.token == "explicit"
    assert custom.minutes == 2
    assert custom.path == "state/backup.json"
    assert custom.url().endswith("/repos/o/r/contents/state/backup.json")


def test_nonsense_intervals_are_ignored() -> None:
    for raw in ("0", "-5", "soon", ""):
        built = SnapshotSettings.from_env(
            {"GITHUB_TOKEN": "x", "GITHUB_REPOSITORY": "o/r", "GITHUB_REF_NAME": "b", "HAVOC_SNAPSHOT_MINUTES": raw}
        )
        assert built.minutes == snapshot.DEFAULT_MINUTES


def test_seeded_tables_do_not_count_as_data() -> None:
    """Feature switches exist on a fresh boot; they must not block a restore."""
    seeded = {"features": [{"name": "notes"}], "settings": [{"key": "language"}], "reminders": []}
    assert snapshot.is_empty(seeded) is True
    assert snapshot.is_empty({**seeded, "reminders": [{"id": 1}]}) is False


def test_the_fingerprint_ignores_the_timestamp() -> None:
    tables = {"notes": [{"id": 1, "body": "x"}]}
    first = snapshot.build_payload(tables)
    second = snapshot.build_payload(tables)
    second["created_at"] = "2000-01-01T00:00:00+00:00"
    assert snapshot.digest(first) == snapshot.digest(second)
    assert snapshot.digest(first) != snapshot.digest(snapshot.build_payload({"notes": []}))


def test_a_snapshot_round_trips_through_the_api(monkeypatch, settings) -> None:
    import asyncio

    api = FakeGithub()
    monkeypatch.setattr(snapshot, "request", api)

    payload = snapshot.build_payload({"notes": [{"id": 1, "body": "remembered"}]})
    asyncio.run(snapshot.push(settings, payload))
    assert api.writes[0]["branch"] == "work"
    assert "sha" not in api.writes[0]  # the file did not exist yet

    asyncio.run(snapshot.push(settings, payload))
    assert api.writes[1]["sha"] == "sha-2"  # the update carries the previous sha

    fetched, sha = asyncio.run(snapshot.fetch(settings))
    assert fetched["tables"]["notes"][0]["body"] == "remembered"
    assert sha == "sha-3"


def test_a_missing_snapshot_is_not_an_error(monkeypatch, settings) -> None:
    import asyncio

    monkeypatch.setattr(snapshot, "request", FakeGithub())
    assert asyncio.run(snapshot.fetch(settings)) is None


def test_a_corrupt_snapshot_is_rejected(monkeypatch, settings) -> None:
    import asyncio

    api = FakeGithub(existing={"hello": "world"})  # not a havoc snapshot
    monkeypatch.setattr(snapshot, "request", api)
    with pytest.raises(ValueError):
        asyncio.run(snapshot.fetch(settings))


class FakeStore:
    def __init__(self, tables: dict[str, list[dict[str, Any]]]) -> None:
        self.tables = tables
        self.imported: list[dict[str, Any]] = []

    async def export_tables(self) -> dict[str, list[dict[str, Any]]]:
        return self.tables

    async def import_tables(self, payload: dict[str, list[dict[str, Any]]], *, replace: bool = False) -> dict[str, int]:
        self.imported.append(payload)
        return {name: len(rows) for name, rows in payload.items() if rows}


def _runtime(tables: dict[str, list[dict[str, Any]]]) -> Any:
    from havoc.health import Metrics

    runtime = SimpleNamespace(store=FakeStore(tables), metrics=Metrics())
    return runtime


def test_an_empty_database_is_restored_from_the_snapshot(monkeypatch, settings) -> None:
    import asyncio

    from havoc import main as havoc_main

    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_REF_NAME", "work")
    existing = snapshot.build_payload({"reminders": [{"id": 1, "text": "water the plants"}]})
    monkeypatch.setattr(snapshot, "request", FakeGithub(existing=existing))

    runtime = _runtime({"features": [{"name": "notes"}], "reminders": []})
    report = asyncio.run(havoc_main._restore_from_snapshot(runtime, _logger()))

    assert "reminders=1" in report
    assert runtime.store.imported == [{"reminders": [{"id": 1, "text": "water the plants"}]}]


def test_a_restore_never_overwrites_local_data(monkeypatch) -> None:
    import asyncio

    from havoc import main as havoc_main

    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_REF_NAME", "work")
    api = FakeGithub(existing=snapshot.build_payload({"reminders": [{"id": 99}]}))
    monkeypatch.setattr(snapshot, "request", api)

    runtime = _runtime({"reminders": [{"id": 1, "text": "local"}]})
    assert asyncio.run(havoc_main._restore_from_snapshot(runtime, _logger())) == ""
    assert runtime.store.imported == []
    assert api.writes == []  # and nothing was written either


def test_snapshots_skip_unchanged_data(monkeypatch) -> None:
    import asyncio

    from havoc import main as havoc_main

    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_REF_NAME", "work")
    api = FakeGithub()
    monkeypatch.setattr(snapshot, "request", api)

    runtime = _runtime({"notes": [{"id": 1}]})
    assert asyncio.run(havoc_main._snapshot_once(runtime, _logger())) is True
    assert asyncio.run(havoc_main._snapshot_once(runtime, _logger())) is False  # nothing changed
    assert len(api.writes) == 1

    runtime.store.tables = {"notes": [{"id": 1}, {"id": 2}]}
    assert asyncio.run(havoc_main._snapshot_once(runtime, _logger())) is True
    assert len(api.writes) == 2

    # On the way out it always writes, even without a change.
    assert asyncio.run(havoc_main._snapshot_once(runtime, _logger(), final=True)) is True
    assert len(api.writes) == 3


def test_a_failure_to_snapshot_is_not_fatal(monkeypatch) -> None:
    import asyncio

    from havoc import main as havoc_main
    from havoc.services.http import HttpError

    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_REF_NAME", "work")
    api = FakeGithub()
    api.fail_with = HttpError("boom", kind="network")
    monkeypatch.setattr(snapshot, "request", api)

    runtime = _runtime({"notes": [{"id": 1}]})
    with pytest.raises(HttpError):
        asyncio.run(havoc_main._snapshot_once(runtime, _logger()))
    # The loop itself swallows it, so the bot keeps running.
    assert runtime.snapshot_state["digest"] == ""


def test_disabled_snapshots_touch_nothing(monkeypatch) -> None:
    import asyncio

    from havoc import main as havoc_main

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("HAVOC_SNAPSHOT_TOKEN", raising=False)
    api = FakeGithub()
    monkeypatch.setattr(snapshot, "request", api)

    runtime = _runtime({"notes": [{"id": 1}]})
    assert asyncio.run(havoc_main._snapshot_once(runtime, _logger())) is False
    assert asyncio.run(havoc_main._restore_from_snapshot(runtime, _logger())) == ""
    assert api.writes == []


class _Logger:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def info(self, message: str, *args: Any) -> None:
        self.lines.append(message % args if args else message)

    def warning(self, message: str, *args: Any) -> None:
        self.lines.append("WARN " + (message % args if args else message))


def _logger() -> Any:
    return _Logger()


def test_bookkeeping_rows_are_not_snapshotted() -> None:
    """A restore must not resurrect the previous machine's boot counter."""
    tables = {
        "settings": [
            {"key": "diagnostics.boots", "value": "9"},
            {"key": "language", "value": "fa"},
        ]
    }
    cleaned = snapshot.strip_diagnostics(tables)
    assert [row["key"] for row in cleaned["settings"]] == ["language"]
    # The input is left alone, so callers cannot be surprised.
    assert len(tables["settings"]) == 2


def test_a_restore_leaves_evidence(monkeypatch) -> None:
    import asyncio

    from havoc import main as havoc_main

    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_REF_NAME", "work")
    monkeypatch.setenv("GITHUB_RUN_ID", "4242")
    api = FakeGithub(existing=snapshot.build_payload({"reminders": [{"id": 3, "text": "call back"}]}))
    monkeypatch.setattr(snapshot, "request", api)

    runtime = _runtime({"reminders": []})
    asyncio.run(havoc_main._restore_from_snapshot(runtime, _logger()))

    report = api.json_at(".data/restore-report.json")
    assert report["kind"] == "restore-report"
    assert report["counts"] == {"reminders": 1}
    assert report["restored_at"]
    assert report["run_id"] == "4242"
    # It is written to its own path; the snapshot it read is left untouched.
    assert api.json_at(".data/snapshot.json")["tables"]["reminders"] == [{"id": 3, "text": "call back"}]


def test_seeded_rows_do_not_stop_a_restore(monkeypatch) -> None:
    """The live failure: a boot that seeded features looked like real data."""
    import asyncio

    from havoc import main as havoc_main

    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_REF_NAME", "work")
    existing = snapshot.build_payload({"reminders": [{"id": 1, "text": "call back"}]})
    monkeypatch.setattr(snapshot, "request", FakeGithub(existing=existing))

    # Exactly what a fresh runtime has after load_features(): seeded switches.
    seeded = {
        "features": [{"name": "notes", "enabled": 0}],
        "settings": [{"key": "language", "value": "en"}],
        "language_prefs": [{"scope": "default", "language": "en"}],
        "reminders": [],
    }
    runtime = _runtime(seeded)
    assert asyncio.run(havoc_main._restore_from_snapshot(runtime, _logger())) == "reminders=1"
    assert runtime.store.imported == [{"reminders": [{"id": 1, "text": "call back"}]}]


def test_a_snapshot_only_fills_empty_tables() -> None:
    """It must never decide that local data should be replaced."""
    local = {"reminders": [{"id": 9, "text": "mine"}], "notes": []}
    remote = {"reminders": [{"id": 1}], "notes": [{"id": 2}]}
    assert snapshot.adds_data(local, remote) is True  # notes is empty locally
    assert snapshot.adds_data({"notes": [{"id": 5}]}, {"notes": [{"id": 2}]}) is False
    assert snapshot.adds_data({}, {"features": [{"name": "x"}]}) is False  # seeded


def test_a_conflicting_write_is_retried(monkeypatch, settings) -> None:
    """The bot and the workflow both commit; a lost race must not lose data."""
    import asyncio

    from havoc.services.http import HttpError

    api = FakeGithub(existing=snapshot.build_payload({"notes": [{"id": 1}]}))
    conflicts = {"left": 1}
    original = api.__call__

    async def flaky(method: str, url: str, **kwargs: Any) -> Any:
        if method == "PUT" and conflicts["left"]:
            conflicts["left"] -= 1
            raise HttpError("sha mismatch", kind="http_error", status=409)
        return await original(method, url, **kwargs)

    monkeypatch.setattr(snapshot, "request", flaky)
    payload = snapshot.build_payload({"notes": [{"id": 1}, {"id": 2}]})
    asyncio.run(snapshot.push(settings, payload))

    assert api.json_at(snapshot.DEFAULT_PATH)["tables"]["notes"] == [{"id": 1}, {"id": 2}]
    assert len(api.writes) == 1  # the failed attempt was not recorded


def test_a_persistent_conflict_gives_up_loudly(monkeypatch, settings) -> None:
    import asyncio

    from havoc.services.http import HttpError

    existing = json.dumps(snapshot.build_payload({"notes": []})).encode("utf-8")

    async def always_conflict(method: str, url: str, **kwargs: Any) -> Any:
        if method == "PUT":
            raise HttpError("sha mismatch", kind="http_error", status=409)
        return SimpleNamespace(
            status=200,
            json=lambda: {"content": base64.b64encode(existing).decode("ascii"), "sha": "x"},
        )

    monkeypatch.setattr(snapshot, "request", always_conflict)
    with pytest.raises(HttpError):
        asyncio.run(snapshot.push(settings, snapshot.build_payload({"notes": []})))


def test_the_snapshot_carries_the_restore_it_came_from(monkeypatch) -> None:
    """One write instead of two: the evidence travels with the data."""
    import asyncio

    from havoc import main as havoc_main

    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_REF_NAME", "work")
    monkeypatch.setenv("GITHUB_RUN_ID", "777")
    existing = snapshot.build_payload({"reminders": [{"id": 1, "text": "call back"}]})
    api = FakeGithub(existing=existing)
    monkeypatch.setattr(snapshot, "request", api)

    runtime = _runtime({"features": [{"name": "notes"}], "reminders": []})
    asyncio.run(havoc_main._restore_from_snapshot(runtime, _logger()))
    assert runtime.snapshot_state["last_restore"]["counts"] == {"reminders": 1}

    # The fake store does not apply the import itself; the real one would.
    runtime.store.tables = {"reminders": [{"id": 1, "text": "call back"}]}
    asyncio.run(havoc_main._snapshot_once(runtime, _logger()))
    stored = api.json_at(snapshot.DEFAULT_PATH)
    assert stored["last_restore"]["run_id"] == "777"
    assert stored["last_restore"]["counts"] == {"reminders": 1}
    assert stored["tables"]["reminders"] == [{"id": 1, "text": "call back"}]


def test_a_later_snapshot_keeps_reporting_where_it_came_from(monkeypatch) -> None:
    import asyncio

    from havoc import main as havoc_main

    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_REF_NAME", "work")
    api = FakeGithub(existing=snapshot.build_payload({"reminders": [{"id": 1}]}))
    monkeypatch.setattr(snapshot, "request", api)

    runtime = _runtime({"reminders": []})
    asyncio.run(havoc_main._restore_from_snapshot(runtime, _logger()))
    runtime.store.tables = {"reminders": [{"id": 1}, {"id": 2}]}  # the bot keeps working
    asyncio.run(havoc_main._snapshot_once(runtime, _logger()))
    stored = api.json_at(snapshot.DEFAULT_PATH)
    assert "last_restore" in stored
    assert len(stored["tables"]["reminders"]) == 2
