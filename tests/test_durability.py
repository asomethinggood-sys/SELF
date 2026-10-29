"""Durability: does data survive crashes and restarts?

These tests are deliberately destructive — they kill processes with SIGKILL,
reopen databases, and compare what came back. A userbot that loses a reminder
because a runner was recycled is worse than one that never scheduled it.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from havoc.database import Database

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Transactions must not capture somebody else's writes
# ---------------------------------------------------------------------------
async def test_failed_transaction_does_not_discard_unrelated_writes(tmp_path: Path) -> None:
    """Regression: a shared depth counter let an unrelated task's INSERT join
    another task's transaction, and the rollback silently deleted it."""
    db = Database(tmp_path / "tx.db")
    await db.connect()
    await db.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")

    async def transactional() -> None:
        async with db.transaction():
            await db.execute("INSERT INTO t(v) VALUES ('in-tx')")
            await asyncio.sleep(0.05)  # yield, as real I/O would
            raise RuntimeError("boom")

    async def independent() -> None:
        await asyncio.sleep(0.01)  # arrives while the transaction is open
        await db.execute("INSERT INTO t(v) VALUES ('independent')")

    results = await asyncio.gather(transactional(), independent(), return_exceptions=True)
    assert isinstance(results[0], RuntimeError)

    rows = [row["v"] for row in await db.fetch_all("SELECT v FROM t ORDER BY id")]
    assert rows == ["independent"], f"lost or leaked writes: {rows}"
    await db.close()


async def test_nested_transaction_joins_the_outer_one(tmp_path: Path) -> None:
    db = Database(tmp_path / "nested.db")
    await db.connect()
    await db.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")

    async with db.transaction():
        await db.execute("INSERT INTO t(v) VALUES ('outer')")
        async with db.transaction():  # must not issue a second BEGIN
            await db.execute("INSERT INTO t(v) VALUES ('inner')")

    rows = await db.fetch_all("SELECT v FROM t ORDER BY id")
    assert [row["v"] for row in rows] == ["outer", "inner"]
    await db.close()


async def test_parallel_writers_all_land(tmp_path: Path) -> None:
    """Two hundred concurrent inserts must all be committed, none interleaved away."""
    db = Database(tmp_path / "parallel.db")
    await db.connect()
    await db.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")

    await asyncio.gather(
        *(db.execute("INSERT INTO t(v) VALUES (?)", (f"row-{index}",)) for index in range(200))
    )
    count = await db.scalar("SELECT COUNT(*) FROM t")
    assert count == 200
    assert await db.integrity_check() == "ok"
    await db.close()


# ---------------------------------------------------------------------------
# Crash durability: SIGKILL, not a graceful close
# ---------------------------------------------------------------------------
CRASH_SCRIPT = textwrap.dedent(
    """
    import asyncio, os, signal, sys
    sys.path.insert(0, {root!r})
    from havoc.database import Database

    async def main():
        db = Database({path!r})
        await db.connect()
        await db.execute("CREATE TABLE IF NOT EXISTS t(id INTEGER PRIMARY KEY, v TEXT)")
        for index in range(50):
            await db.execute("INSERT INTO t(v) VALUES (?)", (f"row-{{index}}",))
        # Nothing is closed, nothing is checkpointed: this is a hard kill.
        os.kill(os.getpid(), signal.SIGKILL)

    asyncio.run(main())
    """
)


def test_sigkill_leaves_committed_rows_readable(tmp_path: Path) -> None:
    path = tmp_path / "crash.db"
    script = CRASH_SCRIPT.format(root=str(ROOT), path=str(path))
    completed = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert completed.returncode == -signal.SIGKILL, completed.stderr

    # The WAL is still on disk because the process never got to checkpoint.
    assert path.exists()

    async def read() -> tuple[int, str]:
        db = Database(path)
        await db.connect()
        try:
            count = await db.scalar("SELECT COUNT(*) FROM t", default=0)
            integrity = await db.integrity_check()
            return int(count or 0), integrity
        finally:
            await db.close()

    count, integrity = asyncio.run(read())
    assert count == 50, f"lost committed rows in a crash: {count}/50"
    assert integrity == "ok"


def test_wal_files_are_part_of_the_data(tmp_path: Path) -> None:
    """Copying only ``.db`` after a crash loses rows — the whole directory must travel.

    This is why the workflow caches ``.runtime/data`` rather than the database file.
    """
    import shutil

    path = tmp_path / "wal.db"
    script = textwrap.dedent(
        f"""
        import asyncio, os, signal, sys
        sys.path.insert(0, {str(ROOT)!r})
        from havoc.database import Database

        async def main():
            db = Database({str(path)!r})
            await db.connect()
            await db.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
            await db.execute_many(
                "INSERT INTO t(v) VALUES (?)", [(f"row-{{i}}",) for i in range(500)]
            )
            # Killed before any checkpoint: the rows still live in the -wal file.
            os.kill(os.getpid(), signal.SIGKILL)

        asyncio.run(main())
        """
    )
    subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    wal = path.with_name(path.name + "-wal")
    assert wal.exists() and wal.stat().st_size > 0

    only_db = tmp_path / "only-db.db"
    shutil.copyfile(path, only_db)

    async def count(candidate: Path) -> int:
        from havoc.database import DatabaseError

        db = Database(candidate)
        await db.connect()
        try:
            return int(await db.scalar("SELECT COUNT(*) FROM t", default=0) or 0)
        except DatabaseError:
            # The schema itself never left the WAL, so the copied file is empty.
            return 0
        finally:
            await db.close()

    # Without its WAL the file is not just stale, it can have no schema at all.
    assert asyncio.run(count(only_db)) < 500
    assert asyncio.run(count(path)) == 500  # the whole directory is what counts


async def test_checkpoint_folds_the_wal_back_into_the_database(tmp_path: Path) -> None:
    """What the workflow does before saving: make the directory self-contained."""
    path = tmp_path / "checkpoint.db"
    db = Database(path)
    await db.connect()
    await db.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    await db.execute_many("INSERT INTO t(v) VALUES (?)", [(f"r{index}",) for index in range(200)])

    await db.checkpoint()
    await db.close()

    wal = path.with_name(path.name + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0, "the WAL was not truncated"


# ---------------------------------------------------------------------------
# Restart: every kind of user data must come back
# ---------------------------------------------------------------------------
async def test_everything_survives_a_restart(tmp_path, config) -> None:
    """Write through the real store, close it, reopen it, compare."""
    from conftest import make_config

    from havoc.services.storage import Store

    cfg = make_config(tmp_path)
    from havoc.database import Database as DB

    db = DB(cfg.database_path)
    await db.connect()
    from havoc.migrations import apply_migrations

    await apply_migrations(db)
    store = Store(db)

    await store.add_bookmark(100, kind="note", content="persistent bookmark")
    await store.set_setting("probe.key", "probe-value")
    await store.set_feature("autoreply", True)
    await store.set_language("chat", "100", "fa")
    await store.add_template("greet", "hello {name}", language="en")
    await store.add_profile_preset("work", first_name="Ali", bio="building things")
    from datetime import UTC, datetime

    reminder_id = await store.add_reminder(
        100, "drink water", datetime(2030, 1, 1, tzinfo=UTC)
    )
    assert reminder_id

    await db.close()

    # A brand-new connection, as after a runner restart.
    db2 = DB(cfg.database_path)
    await db2.connect()
    store2 = Store(db2)
    try:
        bookmarks = await store2.list_bookmarks(limit=10)
        assert [row["content"] for row in bookmarks] == ["persistent bookmark"]
        assert await store2.get_setting("probe.key") == "probe-value"
        assert await store2.feature_enabled("autoreply") is True
        assert await store2.get_language("chat", "100") == "fa"
        assert (await store2.get_template("greet"))["body"] == "hello {name}"
        assert (await store2.get_profile_preset("work"))["first_name"] == "Ali"
        reminders = await store2.list_reminders()
        assert [row["text"] for row in reminders] == ["drink water"]
    finally:
        await db2.close()


async def test_a_reminder_due_during_downtime_fires_once_after_restart(tmp_path) -> None:
    """A runner restart must not silently drop a reminder that came due meanwhile."""
    from conftest import FakeTelegram, make_config

    from havoc.migrations import apply_migrations
    from havoc.runtime import Runtime

    cfg = make_config(tmp_path)
    db = Database(cfg.database_path)
    await db.connect()
    await apply_migrations(db)
    from havoc.services.storage import Store

    store = Store(db)
    # Due two hours ago: the bot was "down" when it should have fired.
    from datetime import UTC, datetime

    await store.add_reminder(100, "missed one", datetime(2026, 1, 1, tzinfo=UTC))
    await db.close()

    import logging

    runtime = await Runtime.create(cfg, logging.getLogger("durability"))
    runtime.telegram = FakeTelegram()
    runtime.client = runtime.telegram.client
    runtime.sender.client = runtime.client
    runtime.permissions.set_self_id(42)
    runtime.load_features()
    try:
        report = await runtime.scheduler.recover()
        assert report["reminders_missed"] == 1
        await runtime.scheduler.tick()
        delivered = runtime.telegram.client.sent
        assert any("missed one" in message.text for message in delivered), (
            f"the missed reminder was not delivered: {report}"
        )
        # Delivery must be idempotent: later ticks must not resend it.
        before = len(runtime.telegram.client.sent)
        await runtime.scheduler.recover()
        await runtime.scheduler.tick()
        assert len(runtime.telegram.client.sent) == before, "the reminder fired twice"
    finally:
        await runtime.db.close()


# ---------------------------------------------------------------------------
# Backup and restore: the escape hatch when a cache is lost
# ---------------------------------------------------------------------------
async def test_backup_roundtrip_restores_every_table(tmp_path) -> None:
    from conftest import make_config

    from havoc.features.management import write_backup
    from havoc.migrations import apply_migrations
    from havoc.services.storage import Store

    cfg = make_config(tmp_path)
    db = Database(cfg.database_path)
    await db.connect()
    await apply_migrations(db)
    store = Store(db)

    await store.add_bookmark(100, kind="note", content="survives a backup")
    await store.set_feature("triggers", True)

    from types import SimpleNamespace

    from havoc.health import Metrics

    path = await write_backup(
        SimpleNamespace(store=store, config=cfg, metrics=Metrics()), kind="manual"
    )
    assert path.exists()

    # Wipe the data, then import the backup back over it.
    await db.execute("DELETE FROM bookmarks")
    await store.set_feature("triggers", False)

    import json

    payload = json.loads(path.read_text(encoding="utf-8"))
    await store.import_tables(payload["tables"], replace=True)

    assert [row["content"] for row in await store.list_bookmarks(limit=10)] == [
        "survives a backup"
    ]
    assert await store.feature_enabled("triggers") is True
    await db.close()


async def test_migrations_are_idempotent_on_an_existing_database(tmp_path) -> None:
    from havoc.migrations import LATEST_VERSION, apply_migrations, current_version

    db = Database(tmp_path / "mig.db")
    await db.connect()
    first = await apply_migrations(db)
    assert first
    assert await current_version(db) == LATEST_VERSION

    second = await apply_migrations(db)
    assert second == [], f"migrations ran twice: {second}"
    assert await current_version(db) == LATEST_VERSION
    await db.close()


@pytest.mark.skipif(not hasattr(os, "kill"), reason="POSIX only")
def test_sigkill_during_a_transaction_keeps_the_database_readable(tmp_path) -> None:
    """A half-finished transaction must roll back, not corrupt the file."""
    path = tmp_path / "midtx.db"
    script = textwrap.dedent(
        f"""
        import asyncio, os, signal, sys
        sys.path.insert(0, {str(ROOT)!r})
        from havoc.database import Database

        async def main():
            db = Database({str(path)!r})
            await db.connect()
            await db.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
            await db.execute("INSERT INTO t(v) VALUES ('committed')")
            async with db.transaction():
                await db.execute("INSERT INTO t(v) VALUES ('doomed')")
                os.kill(os.getpid(), signal.SIGKILL)

        asyncio.run(main())
        """
    )
    completed = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert completed.returncode == -signal.SIGKILL, completed.stderr

    async def read() -> tuple[list[str], str]:
        db = Database(path)
        await db.connect()
        try:
            rows = [row["v"] for row in await db.fetch_all("SELECT v FROM t ORDER BY id")]
            return rows, await db.integrity_check()
        finally:
            await db.close()

    rows, integrity = asyncio.run(read())
    assert rows == ["committed"], f"expected the uncommitted row to vanish, got {rows}"
    assert integrity == "ok"


async def test_row_counts_reports_what_is_stored(tmp_path) -> None:
    """The startup line is the cheapest way to notice a wiped database."""
    from havoc.database import Database
    from havoc.migrations import apply_migrations

    db = Database(tmp_path / "counts.db")
    await db.connect()
    try:
        await apply_migrations(db)
        empty = await db.row_counts()
        assert empty["reminders"] == 0
        await db.execute("INSERT INTO notes(chat_id, body) VALUES(1, 'remembered')")
        after = await db.row_counts()
        assert after["notes"] == 1
        assert "sqlite_sequence" not in after or after["sqlite_sequence"] >= 0
    finally:
        await db.close()


async def test_a_table_name_cannot_break_the_count(tmp_path) -> None:
    from havoc.database import Database

    db = Database(tmp_path / "odd.db")
    await db.connect()
    try:
        await db.execute('CREATE TABLE "odd ""name""" (x INTEGER)')
        counts = await db.row_counts()
        assert counts['odd "name"'] == 0
    finally:
        await db.close()


def test_the_boot_trail_grows_across_restarts(tmp_path, monkeypatch) -> None:
    """The trail beside the database is the evidence the cache restored."""
    import json
    import subprocess
    import sys

    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HAVOC_ENV_FILE": str(tmp_path / "none.env"),
        "DATA_DIR": str(tmp_path),
        "DATABASE_PATH": str(tmp_path / "x.db"),
        "LOG_DIR": str(tmp_path / "logs"),
        "HAVOC_CACHE_HIT": "true",
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
    }

    def boot(run_id: str) -> dict:
        out = tmp_path / f"status-{run_id}.json"
        result = subprocess.run(
            [sys.executable, "-m", "havoc", "migrate"],
            env={**env, "GITHUB_RUN_ID": run_id},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        result = subprocess.run(
            [sys.executable, "-m", "havoc", "data", "--boot-probe", "--json-out", str(out)],
            env={**env, "GITHUB_RUN_ID": run_id},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(out.read_text())

    first, second, third = boot("a"), boot("b"), boot("c")

    assert (first["boots"], second["boots"], third["boots"]) == (1, 2, 3)
    assert third["boot_log_lines"] == 3
    assert second["restored_from"]["cache_hit"] == "true"
    assert "run=b" in (tmp_path / "boots.log").read_text()


async def test_everything_survives_losing_the_machine_entirely(tmp_path, monkeypatch) -> None:
    """The worst case: the cache misses and the runner is gone.

    Real rows go in, a snapshot is committed through a fake contents API, the
    whole data directory is deleted, and a fresh database has to end up with
    the same data.
    """
    import shutil
    from datetime import UTC, datetime

    from conftest import make_config
    from test_snapshot import FakeGithub

    from havoc import snapshot
    from havoc.database import Database as DB
    from havoc.health import Metrics
    from havoc.migrations import apply_migrations
    from havoc.services.storage import Store

    api = FakeGithub()
    monkeypatch.setattr(snapshot, "request", api)
    settings = snapshot.SnapshotSettings(token="t", repo="o/r", branch="b")

    cfg = make_config(tmp_path / "first")
    db = DB(cfg.database_path)
    await db.connect()
    await apply_migrations(db)
    store = Store(db)
    await store.add_bookmark(7, kind="note", content="survive the runner dying")
    await store.add_reminder(7, "water the plants", datetime(2031, 5, 4, tzinfo=UTC))
    await store.set_setting("theme", "dark")
    await store.add_note(7, "milk", title="shopping")
    original = await store.export_tables()
    await snapshot.push(settings, snapshot.build_payload(original))
    await db.close()

    # Total loss: no database, no cache, nothing local.
    shutil.rmtree(tmp_path / "first", ignore_errors=True)
    from types import SimpleNamespace

    fresh_cfg = make_config(tmp_path / "second")
    db2 = DB(fresh_cfg.database_path)
    await db2.connect()
    await apply_migrations(db2)
    store2 = Store(db2)
    try:
        assert snapshot.is_empty(await store2.export_tables()) is True

        runtime = SimpleNamespace(store=store2, metrics=Metrics())
        from havoc import main as havoc_main

        monkeypatch.setenv("GITHUB_TOKEN", "t")
        monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
        monkeypatch.setenv("GITHUB_REF_NAME", "b")
        monkeypatch.setenv("HAVOC_SNAPSHOT_PATH", ".data/snapshot.json")
        report = await havoc_main._restore_from_snapshot(runtime, SimpleNamespace(
            info=lambda *a, **k: None, warning=lambda *a, **k: None
        ))
        assert "reminders=1" in report

        restored = await store2.export_tables()
        for table in ("bookmarks", "reminders", "settings", "notes", "features"):
            assert restored.get(table) == original.get(table), table
        assert [row["content"] for row in await store2.list_bookmarks(limit=5)] == [
            "survive the runner dying"
        ]
    finally:
        await db2.close()
