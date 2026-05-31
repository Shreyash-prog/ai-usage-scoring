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
