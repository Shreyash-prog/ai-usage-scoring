"""ScoreStore — upsert/read score rows (main spec §4 scores table, §10.5 evidence)."""

import json
import time

from app.storage.db import Database

_COLS = [
    "session_id",
    "task_id",
    "dimension",
    "phase",
    "score",
    "confidence",
    "evidence",
    "updated_at",
]


class ScoreStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def list_scores(self, session_id: str, phase: str) -> list[dict]:
        async with self._db.read() as conn:
            cur = await conn.execute(
                f"SELECT {', '.join(_COLS)} FROM scores WHERE session_id = ? AND phase = ?",
                (session_id, phase),
            )
            rows = await cur.fetchall()
        out = []
        for r in rows:
            record = dict(zip(_COLS, r, strict=True))
            record["evidence"] = json.loads(record["evidence"])
            out.append(record)
        return out

    async def upsert(
        self,
        session_id: str,
        task_id: str | None,
        dimension: str,
        phase: str,
        score: float,
        confidence: float,
        evidence: dict,
    ) -> None:
        """Insert or replace a score row, keyed by (session, task, dimension, phase).

        TODO(Day 6): session-level NULL task_id aggregates handled here (the ON CONFLICT
        upsert only dedupes non-null task_id; NULLs compare distinct in SQLite).
        """
        updated_at = int(time.time() * 1000)
        async with self._db.write() as conn:
            await conn.execute(
                "INSERT INTO scores "
                "(session_id, task_id, dimension, phase, score, confidence, evidence, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id, task_id, dimension, phase) DO UPDATE SET "
                "score=excluded.score, confidence=excluded.confidence, "
                "evidence=excluded.evidence, updated_at=excluded.updated_at",
                (
                    session_id,
                    task_id,
                    dimension,
                    phase,
                    score,
                    confidence,
                    json.dumps(evidence),
                    updated_at,
                ),
            )
            await conn.commit()
