"""EventLogger — the sole mutator for the events table (main spec §6).

`write` allocates a per-session monotonic `seq` atomically and persists the
event before anyone fans it out, so subscribers always observe a stable `seq`
(§8.2). Reads are snapshot-consistent within a single call.
"""

import asyncio
import json
import sqlite3
import time

import aiosqlite

from app.models.events import EventType, PersistedEvent
from app.storage.db import Database

# §6.2 retry schedule for "database is locked".
_RETRY_BACKOFF_S = [0.010, 0.050, 0.200]


class EventLogger:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def write(
        self,
        session_id: str,
        type: EventType,
        payload: dict,
        task_id: str | None = None,
    ) -> PersistedEvent:
        """Persist one event, allocating `seq = max(seq)+1` for the session.

        `ts` is wall-clock ms, forced monotonic per session for display. Ordering
        in scoring always uses `seq`, never `ts` (see CLAUDE.md reminders).
        """
        last_exc: Exception | None = None
        for delay in [*_RETRY_BACKOFF_S, None]:
            try:
                return await self._write_once(session_id, type, payload, task_id)
            except sqlite3.OperationalError as exc:
                if "database is locked" not in str(exc).lower() or delay is None:
                    raise
                last_exc = exc
                await asyncio.sleep(delay)
        # Unreachable: the final iteration has delay=None and re-raises.
        assert last_exc is not None
        raise last_exc

    async def _write_once(
        self,
        session_id: str,
        type: EventType,
        payload: dict,
        task_id: str | None,
    ) -> PersistedEvent:
        now_ms = int(time.time() * 1000)
        async with self._db.write() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                cur = await conn.execute(
                    "SELECT COALESCE(MAX(seq), 0), COALESCE(MAX(ts), 0) "
                    "FROM events WHERE session_id = ?",
                    (session_id,),
                )
                row = await cur.fetchone()
                assert row is not None  # aggregate query always returns one row
                seq = int(row[0]) + 1
                ts = max(now_ms, int(row[1]) + 1)

                cur = await conn.execute(
                    "INSERT INTO events "
                    "(session_id, ts, seq, type, payload_version, payload, task_id) "
                    "VALUES (?, ?, ?, ?, 1, ?, ?)",
                    (session_id, ts, seq, type.value, json.dumps(payload), task_id),
                )
                new_id = cur.lastrowid
                assert new_id is not None
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

        return PersistedEvent(
            id=int(new_id),
            session_id=session_id,
            seq=seq,
            ts=ts,
            type=type,
            payload=payload,
            task_id=task_id,
        )

    async def get_session_events(
        self,
        session_id: str,
        from_seq: int = 0,
        types: list[EventType] | None = None,
    ) -> list[PersistedEvent]:
        """Return events with `seq > from_seq`, ordered by `seq` ascending."""
        sql = (
            "SELECT id, session_id, seq, ts, type, payload, task_id "
            "FROM events WHERE session_id = ? AND seq > ?"
        )
        params: list[object] = [session_id, from_seq]
        if types:
            placeholders = ",".join("?" for _ in types)
            sql += f" AND type IN ({placeholders})"
            params.extend(t.value for t in types)
        sql += " ORDER BY seq ASC"

        async with self._db.read() as conn:
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
        return [self._row_to_event(r) for r in rows]

    async def count_session_events(self, session_id: str, type: EventType) -> int:
        """Count persisted events of one type for a session (Phase 2 exec cap)."""
        async with self._db.read() as conn:
            cur = await conn.execute(
                "SELECT COUNT(*) FROM events WHERE session_id = ? AND type = ?",
                (session_id, type.value),
            )
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def get_task_events(self, session_id: str, task_id: str) -> list[PersistedEvent]:
        """Return all events tagged with `task_id`, ordered by `seq`."""
        async with self._db.read() as conn:
            cur = await conn.execute(
                "SELECT id, session_id, seq, ts, type, payload, task_id "
                "FROM events WHERE session_id = ? AND task_id = ? ORDER BY seq ASC",
                (session_id, task_id),
            )
            rows = await cur.fetchall()
        return [self._row_to_event(r) for r in rows]

    @staticmethod
    def _row_to_event(row: aiosqlite.Row) -> PersistedEvent:
        return PersistedEvent(
            id=int(row[0]),
            session_id=row[1],
            seq=int(row[2]),
            ts=int(row[3]),
            type=EventType(row[4]),
            payload=json.loads(row[5]),
            task_id=row[6],
        )
