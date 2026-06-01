"""Session row helpers (main spec §4 sessions table).

Day 1 needs just enough to satisfy the events FK and the smoke test: create a
session and read it back. Lifecycle transitions (ended/scored) land in later days.
"""

import json
import time
import uuid

from pydantic import BaseModel

from app.storage.db import Database


class SessionRow(BaseModel):
    id: str
    candidate_name: str
    task_sequence: list[str]
    current_task_idx: int
    started_at: int
    ended_at: int | None
    status: str
    schema_version: int


async def create_session(
    db: Database,
    candidate_name: str,
    task_sequence: list[str],
) -> str:
    """Insert a new active session and return its uuid4 id."""
    session_id = str(uuid.uuid4())
    started_at = int(time.time() * 1000)
    async with db.write() as conn:
        await conn.execute(
            "INSERT INTO sessions "
            "(id, candidate_name, task_sequence, current_task_idx, started_at, "
            " ended_at, status, schema_version) "
            "VALUES (?, ?, ?, 0, ?, NULL, 'active', 1)",
            (session_id, candidate_name, json.dumps(task_sequence), started_at),
        )
        await conn.commit()
    return session_id


async def set_current_task_idx(db: Database, session_id: str, idx: int) -> None:
    async with db.write() as conn:
        await conn.execute(
            "UPDATE sessions SET current_task_idx = ? WHERE id = ?",
            (idx, session_id),
        )
        await conn.commit()


async def end_session(db: Database, session_id: str) -> None:
    """Mark a session ended (idempotent: only transitions from 'active')."""
    ended_at = int(time.time() * 1000)
    async with db.write() as conn:
        await conn.execute(
            "UPDATE sessions SET status = 'ended', ended_at = ? WHERE id = ? AND status = 'active'",
            (ended_at, session_id),
        )
        await conn.commit()


async def mark_scored(db: Database, session_id: str) -> None:
    async with db.write() as conn:
        await conn.execute("UPDATE sessions SET status = 'scored' WHERE id = ?", (session_id,))
        await conn.commit()


async def list_sessions(db: Database) -> list[SessionRow]:
    """All sessions, most recently started first (for the dashboard list, §15.3)."""
    async with db.read() as conn:
        cur = await conn.execute(
            "SELECT id, candidate_name, task_sequence, current_task_idx, "
            "started_at, ended_at, status, schema_version "
            "FROM sessions ORDER BY started_at DESC"
        )
        rows = await cur.fetchall()
    return [
        SessionRow(
            id=r[0],
            candidate_name=r[1],
            task_sequence=json.loads(r[2]),
            current_task_idx=int(r[3]),
            started_at=int(r[4]),
            ended_at=int(r[5]) if r[5] is not None else None,
            status=r[6],
            schema_version=int(r[7]),
        )
        for r in rows
    ]


async def get_session(db: Database, session_id: str) -> SessionRow | None:
    async with db.read() as conn:
        cur = await conn.execute(
            "SELECT id, candidate_name, task_sequence, current_task_idx, "
            "started_at, ended_at, status, schema_version "
            "FROM sessions WHERE id = ?",
            (session_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    return SessionRow(
        id=row[0],
        candidate_name=row[1],
        task_sequence=json.loads(row[2]),
        current_task_idx=int(row[3]),
        started_at=int(row[4]),
        ended_at=int(row[5]) if row[5] is not None else None,
        status=row[6],
        schema_version=int(row[7]),
    )
