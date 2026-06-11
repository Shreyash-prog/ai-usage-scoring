"""Cost-log writes + cap-enforcement queries (Phase 2, llm_calls table)."""

import pytest

from app.storage import llm_calls
from app.storage.db import Database
from app.storage.llm_calls import utc_day_start_ms
from app.storage.sessions import create_session


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "llm.db"))
    await database.connect()
    yield database
    await database.close()


def test_utc_day_start_is_exact_floor() -> None:
    assert utc_day_start_ms(86_400_000) == 86_400_000  # exactly a day boundary
    assert utc_day_start_ms(86_400_000 + 5) == 86_400_000
    assert utc_day_start_ms(86_400_000 - 1) == 0


async def test_chat_tokens_sum_excludes_judge(db: Database) -> None:
    sid = await create_session(db, "X", ["001"])
    await llm_calls.record_llm_call(db, sid, "openai", "gpt-4o-mini", "chat", 100, 50, 12, "ok")
    await llm_calls.record_llm_call(db, sid, "openai", "gpt-4o-mini", "chat", 200, 30, 9, "ok")
    # A judge call must NOT count toward chat tokens.
    await llm_calls.record_llm_call(
        db, sid, "anthropic", "claude-sonnet-4-6", "judge:PQ1", 500, 20, 5, "ok"
    )
    assert await llm_calls.session_chat_tokens_in(db, sid) == 300
    assert await llm_calls.session_judge_call_count(db, sid) == 1


async def test_session_judge_count_is_per_session(db: Database) -> None:
    a = await create_session(db, "A", ["001"])
    b = await create_session(db, "B", ["001"])
    for q in ("PQ1", "PQ2", "VB1"):
        await llm_calls.record_llm_call(
            db, a, "anthropic", "claude-sonnet-4-6", f"judge:{q}", 1, 1, 1, "ok"
        )
    await llm_calls.record_llm_call(
        db, b, "anthropic", "claude-sonnet-4-6", "judge:PQ1", 1, 1, 1, "ok"
    )
    assert await llm_calls.session_judge_call_count(db, a) == 3
    assert await llm_calls.session_judge_call_count(db, b) == 1


async def test_global_daily_counters(db: Database) -> None:
    sid = await create_session(db, "X", ["001"])
    assert await llm_calls.global_sessions_today(db) == 1
    await llm_calls.record_llm_call(
        db, sid, "anthropic", "claude-sonnet-4-6", "judge:PQ1", 1, 1, 1, "ok"
    )
    await llm_calls.record_llm_call(
        db, sid, "anthropic", "claude-sonnet-4-6", "judge:VB1", 1, 1, 1, "ok"
    )
    # chat call doesn't count toward the global JUDGE counter
    await llm_calls.record_llm_call(db, sid, "openai", "gpt-4o-mini", "chat", 1, 1, 1, "ok")
    assert await llm_calls.global_judge_calls_today(db) == 2
