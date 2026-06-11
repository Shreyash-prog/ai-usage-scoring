"""Async SQLite connection management and startup init (main spec §6.3).

One write connection guarded by an asyncio.Lock (SQLite serializes writes
anyway), plus a small pool of read connections for concurrent reads. WAL mode
lets readers proceed while a write is in flight.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"
_READ_POOL_SIZE = 4


class Database:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._write_conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._read_pool: asyncio.Queue[aiosqlite.Connection] = asyncio.Queue()
        self._all_conns: list[aiosqlite.Connection] = []

    async def connect(self) -> None:
        """Open connections, apply the schema, and run startup housekeeping."""
        # Ensure the DB's parent dir exists. On Fly the volume mounts an empty
        # /data on first boot; locally this is a no-op for ./events.db. Everything
        # else in the container runs on a read-only rootfs — only /data is writable.
        parent = Path(self._db_path).parent
        parent.mkdir(parents=True, exist_ok=True)
        self._write_conn = await self._open_conn()
        await self._ensure_schema(self._write_conn)
        for _ in range(_READ_POOL_SIZE):
            conn = await self._open_conn()
            self._read_pool.put_nowait(conn)
        await self._abandon_stale_sessions()

    async def _open_conn(self) -> aiosqlite.Connection:
        conn = await aiosqlite.connect(self._db_path)
        await conn.execute("PRAGMA journal_mode = WAL")
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute("PRAGMA busy_timeout = 2000")
        await conn.commit()
        self._all_conns.append(conn)
        return conn

    async def _ensure_schema(self, conn: aiosqlite.Connection) -> None:
        sql = _SCHEMA_PATH.read_text(encoding="utf-8")
        await conn.executescript(sql)
        await conn.commit()

    async def _abandon_stale_sessions(self) -> None:
        """§6.3: mark long-idle active sessions as abandoned on startup.

        We approximate idleness by `started_at` here (Day 1 has no per-session
        last-activity tracking yet); refined once events drive activity.
        """
        from app.config import settings

        cutoff = int(time.time() * 1000) - settings.session_idle_timeout_min * 60_000
        assert self._write_conn is not None
        async with self._lock:
            await self._write_conn.execute(
                "UPDATE sessions SET status='abandoned' WHERE status='active' AND started_at < ?",
                (cutoff,),
            )
            await self._write_conn.commit()

    @asynccontextmanager
    async def write(self) -> AsyncIterator[aiosqlite.Connection]:
        """Acquire the single write connection under the write lock."""
        assert self._write_conn is not None, "Database not connected"
        async with self._lock:
            yield self._write_conn

    @asynccontextmanager
    async def read(self) -> AsyncIterator[aiosqlite.Connection]:
        """Check a read connection out of the pool for the duration of the block."""
        conn = await self._read_pool.get()
        try:
            yield conn
        finally:
            self._read_pool.put_nowait(conn)

    async def close(self) -> None:
        # De-duplicate: write conn may also be tracked in _all_conns.
        seen: set[int] = set()
        for conn in self._all_conns:
            if id(conn) in seen:
                continue
            seen.add(id(conn))
            await conn.close()
        self._all_conns.clear()
        self._write_conn = None
