"""ScoreStore — upsert score rows (main spec §4 scores table, §10.5 evidence)."""

import json
import time

from app.storage.db import Database


class ScoreStore:
    def __init__(self, db: Database) -> None:
        self._db = db

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
        """Insert or replace a score row, keyed by (session, task, dimension, phase)."""
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
