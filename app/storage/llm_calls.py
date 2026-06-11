"""llm_calls cost-log writes + cap-enforcement queries (PROVIDER_SPEC §P.6.1/§P.6.3).

The `llm_calls` table was specced on Day 1 but never written to; Phase 2 (public
deploy) makes it real so the per-session and global daily caps can be enforced
from durable state rather than in-memory counters that reset on reconnect.

All timestamps are ms-since-epoch (UTC), matching `sessions.started_at`. "Today"
is the current UTC calendar day.
"""

import time

from app.llm.pricing import estimate_cost
from app.storage.db import Database

_MS_PER_DAY = 86_400_000


def _now_ms() -> int:
    return int(time.time() * 1000)


def utc_day_start_ms(now_ms: int | None = None) -> int:
    """Start (UTC midnight) of the calendar day containing `now_ms`.

    Epoch-ms is measured from 1970-01-01T00:00Z, so flooring to the day boundary
    is exact arithmetic — no tz database needed.
    """
    now = now_ms if now_ms is not None else _now_ms()
    return now - (now % _MS_PER_DAY)


async def record_llm_call(
    db: Database,
    session_id: str,
    provider: str,
    model: str,
    purpose: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    status: str,
) -> None:
    """Insert one cost-log row. Cost is estimated here (the single recording boundary)."""
    cost = estimate_cost(model, prompt_tokens, completion_tokens)
    async with db.write() as conn:
        await conn.execute(
            "INSERT INTO llm_calls "
            "(session_id, ts, provider, model, purpose, prompt_tokens, completion_tokens, "
            " latency_ms, cost_usd_estimate, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                _now_ms(),
                provider,
                model,
                purpose,
                prompt_tokens,
                completion_tokens,
                latency_ms,
                cost,
                status,
            ),
        )
        await conn.commit()


async def session_chat_tokens_in(db: Database, session_id: str) -> int:
    """Total input (prompt) tokens spent on chat for one session (§P.6.1 chat cap)."""
    async with db.read() as conn:
        cur = await conn.execute(
            "SELECT COALESCE(SUM(prompt_tokens), 0) FROM llm_calls "
            "WHERE session_id = ? AND purpose = 'chat'",
            (session_id,),
        )
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def session_judge_call_count(db: Database, session_id: str) -> int:
    """Number of judge calls recorded for one session (§P.6.1 judge cap)."""
    async with db.read() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) FROM llm_calls WHERE session_id = ? AND purpose LIKE 'judge:%'",
            (session_id,),
        )
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def global_sessions_today(db: Database, now_ms: int | None = None) -> int:
    """Sessions created since UTC midnight (global daily cap on new sessions)."""
    day_start = utc_day_start_ms(now_ms)
    async with db.read() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE started_at >= ?", (day_start,)
        )
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def global_judge_calls_today(db: Database, now_ms: int | None = None) -> int:
    """Judge calls recorded since UTC midnight (global daily cap on judge spend)."""
    day_start = utc_day_start_ms(now_ms)
    async with db.read() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) FROM llm_calls WHERE purpose LIKE 'judge:%' AND ts >= ?",
            (day_start,),
        )
        row = await cur.fetchone()
    return int(row[0]) if row else 0
