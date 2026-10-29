"""Thin async SQLite layer.

Why not an ORM
--------------
HAVOC stores a handful of well-known tables. A thin wrapper keeps queries
visible (parameterized, auditable) and makes tests trivial.

Concurrency model
-----------------
One ``sqlite3.Connection`` guarded by an :class:`asyncio.Lock`; every call is
offloaded to a worker thread with :func:`asyncio.to_thread`. Statements are
short, so a transaction is never held across ``await`` boundaries that perform
network I/O. WAL mode lets reads proceed while a write commits.

Errors are surfaced as :class:`DatabaseError` with the original SQLite
message, never swallowed.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

Row = sqlite3.Row


class DatabaseError(Exception):
    """Raised for any SQLite failure HAVOC cannot recover from locally."""


class Database:
    def __init__(self, path: Path | str, *, timeout: float = 10.0) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        # Who is inside a transaction right now. It must be task-scoped: a
        # shared depth counter let an unrelated task's statement slip into
        # someone else's transaction, where a rollback silently discarded it.
        self._tx_owner: asyncio.Task[Any] | None = None
        self._tx_depth = 0
        self._closed = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def connect_sync(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.path),
            timeout=self.timeout,
            check_same_thread=False,
            isolation_level=None,  # explicit transactions
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute(f"PRAGMA busy_timeout = {int(self.timeout * 1000)}")
        return conn

    async def connect(self) -> Database:
        if self._conn is not None:
            return self
        try:
            self._conn = await asyncio.to_thread(self.connect_sync)
        except sqlite3.Error as exc:
            raise DatabaseError(f"cannot open database at {self.path}: {exc}") from exc
        self._closed = False
        return self

    async def close(self) -> None:
        conn, self._conn = self._conn, None
        self._closed = True
        if conn is None:
            return
        try:
            await asyncio.to_thread(conn.close)
        except sqlite3.Error as exc:  # pragma: no cover - rare
            raise DatabaseError(f"error closing database: {exc}") from exc

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    def _require(self) -> sqlite3.Connection:
        if self._conn is None:
            raise DatabaseError("database is not connected")
        return self._conn

    # ------------------------------------------------------------------
    # Basic operations
    # ------------------------------------------------------------------
    def _in_transaction(self) -> bool:
        """True only for the task that opened the transaction."""
        return self._tx_depth > 0 and self._tx_owner is asyncio.current_task()

    @asynccontextmanager
    async def _guard(self) -> AsyncIterator[None]:
        """Serialise writes, but never self-deadlock inside a transaction.

        Only the task that owns the open transaction may bypass the lock; every
        other task waits, so its statements cannot be captured by (and later
        rolled back with) that transaction.
        """
        if self._in_transaction():
            yield
            return
        async with self._lock:
            yield

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Execute a statement, returning ``cursor.rowcount``."""
        conn = self._require()

        def _run() -> int:
            cur = conn.execute(sql, tuple(params))
            return cur.rowcount

        try:
            async with self._guard():
                return await asyncio.to_thread(_run)
        except sqlite3.Error as exc:
            raise DatabaseError(f"{sql.split()[0].upper()} failed: {exc}") from exc

    async def execute_many(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        conn = self._require()
        data = [tuple(r) for r in rows]

        def _run() -> int:
            conn.execute("BEGIN")
            try:
                cur = conn.executemany(sql, data)
                conn.execute("COMMIT")
                return cur.rowcount
            except Exception:
                conn.execute("ROLLBACK")
                raise

        try:
            async with self._guard():
                return await asyncio.to_thread(_run)
        except sqlite3.Error as exc:
            raise DatabaseError(f"batch write failed: {exc}") from exc

    async def insert(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Execute an INSERT and return ``lastrowid``."""
        conn = self._require()

        def _run() -> int:
            cur = conn.execute(sql, tuple(params))
            return int(cur.lastrowid or 0)

        try:
            async with self._guard():
                return await asyncio.to_thread(_run)
        except sqlite3.Error as exc:
            raise DatabaseError(f"INSERT failed: {exc}") from exc

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> Row | None:
        conn = self._require()

        def _run() -> Row | None:
            return conn.execute(sql, tuple(params)).fetchone()

        try:
            async with self._guard():
                return await asyncio.to_thread(_run)
        except sqlite3.Error as exc:
            raise DatabaseError(f"query failed: {exc}") from exc

    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        conn = self._require()

        def _run() -> list[Row]:
            return list(conn.execute(sql, tuple(params)).fetchall())

        try:
            async with self._guard():
                return await asyncio.to_thread(_run)
        except sqlite3.Error as exc:
            raise DatabaseError(f"query failed: {exc}") from exc

    async def scalar(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = await self.fetch_one(sql, params)
        if row is None:
            return default
        value = row[0]
        return default if value is None else value

    async def execute_script(self, statements: Iterable[str]) -> None:
        """Run a list of statements atomically (used by migrations).

        All statements execute on the same connection under one lock and one
        transaction, so a failure mid-script rolls the whole batch back.
        """
        conn = self._require()
        batch = list(statements)

        def _run() -> None:
            conn.execute("BEGIN")
            try:
                for statement in batch:
                    conn.execute(statement)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        try:
            async with self._lock:
                await asyncio.to_thread(_run)
        except sqlite3.Error as exc:
            raise DatabaseError(f"script failed: {exc}") from exc

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Database]:
        """Serialize a multi-statement transaction.

        The caller must not perform network I/O inside the block; keep it to
        local bookkeeping.
        """
        self._require()

        # Nested use by the same task joins the outer transaction instead of
        # issuing a second BEGIN (which SQLite rejects).
        if self._in_transaction():
            self._tx_depth += 1
            try:
                yield self
            finally:
                self._tx_depth -= 1
            return

        async with self._lock:
            self._tx_owner = asyncio.current_task()
            self._tx_depth += 1
            try:
                await asyncio.to_thread(self._begin)
                yield self
                await asyncio.to_thread(self._commit)
            except Exception:
                await asyncio.to_thread(self._rollback)
                raise
            finally:
                self._tx_depth -= 1
                self._tx_owner = None

    def _begin(self) -> None:
        assert self._conn is not None
        self._conn.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        assert self._conn is not None
        self._conn.execute("COMMIT")

    def _rollback(self) -> None:
        assert self._conn is not None
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.Error:  # pragma: no cover - nothing to roll back
            pass

    # ------------------------------------------------------------------
    # Durability
    # ------------------------------------------------------------------
    async def checkpoint(self, *, truncate: bool = True) -> tuple[int, int, int]:
        """Fold the write-ahead log back into the database file.

        Everything a running HAVOC writes first lands in ``-wal``. Copying the
        database directory while the WAL holds pages is still safe *if* the WAL
        travels with it, but folding it in makes the directory self-contained and
        turns a partial copy from "maybe loses recent rows" into "impossible".

        Returns SQLite's ``(busy, log_frames, checkpointed_frames)``.
        """
        mode = "TRUNCATE" if truncate else "PASSIVE"
        async with self._guard():
            row = await asyncio.to_thread(
                lambda: self._require().execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
            )
        values = tuple(int(value or 0) for value in row) if row is not None else (0, 0, 0)
        return (values + (0, 0, 0))[:3]

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    async def integrity_check(self) -> str:
        try:
            value = await self.scalar("PRAGMA integrity_check", default="unknown")
            return str(value)
        except DatabaseError as exc:
            return str(exc)

    async def table_names(self) -> list[str]:
        rows = await self.fetch_all("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        return [r["name"] for r in rows]

    async def row_counts(self) -> dict[str, int]:
        """Rows per table, so persistence can be reported instead of assumed."""
        counts: dict[str, int] = {}
        for name in await self.table_names():
            quoted = '"' + name.replace('"', '""') + '"'
            try:
                counts[name] = int(await self.scalar(f"SELECT COUNT(*) FROM {quoted}", default=0) or 0)
            except DatabaseError:
                counts[name] = -1
        return counts

    async def schema_version(self) -> int:
        try:
            return int(await self.scalar("SELECT MAX(version) FROM schema_migrations", default=0) or 0)
        except DatabaseError:
            return 0

    async def db_size_bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0
