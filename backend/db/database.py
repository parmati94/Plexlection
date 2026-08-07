"""SQLite connection service.

Stdlib sqlite3 driven from asyncio via to_thread. Deliberately not SQLAlchemy:
the rule compiler emits SQL text directly, and an ORM would sit between the
compiler and the expression indexes it depends on.

Concurrency model: one shared connection, one asyncio write lock. Reads are
concurrent (WAL); writes serialise. That is ample for a single-worker app whose
heaviest write burst is a scan flushing 25 rows at a time.
"""
import asyncio
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Sequence

from backend.common.config import config
from backend.common.logging_config import get_logger

logger = get_logger(__name__)


class Database:
    def __init__(self, path: Path | None = None):
        self.path = path or config.db_path()
        self._conn: sqlite3.Connection | None = None
        self._write_lock = asyncio.Lock()

    # ── lifecycle ─────────────────────────────────────────────────────────
    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self.path,
            check_same_thread=False,  # guarded by the write lock + to_thread
            isolation_level=None,     # autocommit; explicit BEGIN where needed
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        self._conn = conn
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database.start() has not been called")
        return self._conn

    async def start(self) -> None:
        await asyncio.to_thread(self.connect)
        version = await self.fetch_val("SELECT sqlite_version()")
        logger.info("🗄️  SQLite %s at %s", version, self.path)
        self._assert_json1()

    async def stop(self) -> None:
        if self._conn is not None:
            await asyncio.to_thread(self._conn.close)
            self._conn = None

    def _assert_json1(self) -> None:
        """The whole fact store is JSON1. Fail loudly at boot rather than
        mysteriously at query time if it's missing."""
        try:
            self.conn.execute("SELECT json_patch('{}', '{}')").fetchone()
        except sqlite3.OperationalError as exc:  # pragma: no cover
            raise RuntimeError(
                "This SQLite build lacks JSON1 (json_patch/json_extract). "
                "Plexlection cannot run without it."
            ) from exc

    # ── reads (concurrent) ────────────────────────────────────────────────
    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return await asyncio.to_thread(lambda: self.conn.execute(sql, params).fetchall())

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return await asyncio.to_thread(lambda: self.conn.execute(sql, params).fetchone())

    async def fetch_val(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = await self.fetch_one(sql, params)
        return row[0] if row is not None else default

    # ── writes (serialised) ───────────────────────────────────────────────
    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        async with self._write_lock:
            def _run() -> int:
                cur = self.conn.execute(sql, params)
                return cur.lastrowid
            return await asyncio.to_thread(_run)

    async def execute_many(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        rows = list(rows)
        if not rows:
            return
        async with self._write_lock:
            def _run() -> None:
                self.conn.execute("BEGIN")
                try:
                    self.conn.executemany(sql, rows)
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise
            await asyncio.to_thread(_run)

    async def transaction(self, statements: Iterable[tuple[str, Sequence[Any]]]) -> None:
        """Run several statements atomically. Used by the scan flush, which must
        update items, fact_provenance, scan_tasks and scan_runs as one unit."""
        statements = list(statements)
        if not statements:
            return
        async with self._write_lock:
            def _run() -> None:
                self.conn.execute("BEGIN")
                try:
                    for sql, params in statements:
                        self.conn.execute(sql, params)
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise
            await asyncio.to_thread(_run)

    # ── schema ────────────────────────────────────────────────────────────
    async def executescript(self, script: str) -> None:
        async with self._write_lock:
            await asyncio.to_thread(self.conn.executescript, script)

    async def table_columns(self, table: str) -> list[str]:
        rows = await self.fetch_all(f"PRAGMA table_info({table})")
        return [r[1] for r in rows]
