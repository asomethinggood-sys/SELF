"""Database layer and migrations."""

from __future__ import annotations

from pathlib import Path

import pytest

from havoc.database import Database, DatabaseError
from havoc.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    Migration,
    MigrationError,
    apply_migrations,
    current_version,
)


async def test_connect_migrate_and_persist(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    db = Database(path)
    await db.connect()
    applied = await apply_migrations(db)
    assert "foundation" in applied
    assert await current_version(db) == LATEST_VERSION
    await db.execute("INSERT INTO settings(key, value) VALUES(?, ?)", ("k", "v"))
    await db.close()

    again = Database(path)
    await again.connect()
    assert (await apply_migrations(again)) == []
    row = await again.fetch_one("SELECT value FROM settings WHERE key = ?", ("k",))
    assert row is not None and row["value"] == "v"
    await again.close()


async def test_foreign_keys_and_wal_enabled(tmp_path: Path) -> None:
    db = Database(tmp_path / "f.db")
    await db.connect()
    await apply_migrations(db)
    assert await db.scalar("PRAGMA foreign_keys") == 1
    assert str(await db.scalar("PRAGMA journal_mode")).lower() == "wal"
    await db.close()


async def test_parameterized_queries_are_safe(tmp_path: Path) -> None:
    db = Database(tmp_path / "s.db")
    await db.connect()
    await apply_migrations(db)
    await db.execute("INSERT INTO settings(key, value) VALUES(?, ?)", ("x", "1"))
    injected = "1'; DROP TABLE settings; --"
    row = await db.fetch_one("SELECT value FROM settings WHERE value = ?", (injected,))
    assert row is None
    assert "settings" in await db.table_names()
    await db.close()


async def test_migration_failure_rolls_back(tmp_path: Path) -> None:
    db = Database(tmp_path / "m.db")
    await db.connect()
    broken = (
        Migration(version=1, name="ok", statements=("CREATE TABLE a(id INTEGER)",)),
        Migration(version=2, name="broken", statements=("CREATE TABLE b(id INTEGER)", "THIS IS NOT SQL")),
    )
    with pytest.raises(MigrationError):
        await apply_migrations(db, broken)
    tables = await db.table_names()
    assert "a" in tables
    assert "b" not in tables
    assert await current_version(db) == 1
    await db.close()


async def test_scalar_default_and_integrity(tmp_path: Path) -> None:
    db = Database(tmp_path / "i.db")
    await db.connect()
    await apply_migrations(db)
    assert await db.scalar("SELECT value FROM settings WHERE key = ?", ("nope",), default="fallback") == "fallback"
    assert await db.integrity_check() == "ok"
    assert await db.table_names()
    await db.close()


async def test_error_is_wrapped(tmp_path: Path) -> None:
    db = Database(tmp_path / "e.db")
    await db.connect()
    with pytest.raises(DatabaseError):
        await db.fetch_all("SELECT * FROM does_not_exist")
    await db.close()


async def test_transaction_rollback(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    await db.connect()
    await apply_migrations(db)
    with pytest.raises(RuntimeError):
        async with db.transaction():
            await db.execute("INSERT INTO settings(key, value) VALUES(?, ?)", ("a", "1"))
            raise RuntimeError("boom")
    assert await db.scalar("SELECT COUNT(*) FROM settings") == 0
    await db.close()


async def test_migration_versions_are_unique_and_ordered() -> None:
    versions = [migration.version for migration in MIGRATIONS]
    assert versions == sorted(versions)
    assert len(set(versions)) == len(versions)
