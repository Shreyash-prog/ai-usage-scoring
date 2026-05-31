"""Day 1 smoke test (main spec §17): persist a session + events, read them back.

Offline only — no network, no API keys required. The live LLM checks (OpenAI
1-token health, Anthropic health) are run by hand via `make smoke` / PROVIDER_SPEC
§P.9, deliberately kept out of the automated suite.
"""

import pytest

from app.models.events import EventType
from app.storage.db import Database
from app.storage.events import EventLogger
from app.storage.sessions import create_session, get_session


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "smoke.db"))
    await database.connect()
    yield database
    await database.close()


async def test_event_roundtrip(db: Database) -> None:
    session_id = await create_session(db, "Alice", ["001", "002", "003"])

    session = await get_session(db, session_id)
    assert session is not None
    assert session.candidate_name == "Alice"
    assert session.status == "active"
    assert session.task_sequence == ["001", "002", "003"]

    logger = EventLogger(db)
    e1 = await logger.write(
        session_id,
        EventType.SESSION_STARTED,
        {"candidate_name": "Alice", "task_sequence": ["001", "002", "003"]},
    )
    e2 = await logger.write(
        session_id,
        EventType.TASK_PRESENTED,
        {"task_id": "001", "task_idx": 0},
        task_id="001",
    )
    e3 = await logger.write(
        session_id,
        EventType.CODE_EXECUTED,
        {
            "code": "print('hi')",
            "stdout": "hi\n",
            "stderr": "",
            "exit_code": 0,
            "runtime_ms": 5,
            "truncated": False,
        },
        task_id="001",
    )

    # seq is per-session monotonic, assigned on persist.
    assert [e1.seq, e2.seq, e3.seq] == [1, 2, 3]
    # ts is monotonic non-decreasing for display ordering.
    assert e1.ts <= e2.ts <= e3.ts

    events = await logger.get_session_events(session_id)
    assert [e.seq for e in events] == [1, 2, 3]
    assert events[0].type == EventType.SESSION_STARTED
    assert events[2].payload["stdout"] == "hi\n"


async def test_from_seq_filter(db: Database) -> None:
    session_id = await create_session(db, "Bob", ["001"])
    logger = EventLogger(db)
    for _ in range(3):
        await logger.write(session_id, EventType.EDITOR_SNAPSHOT, {"code": "x"}, task_id="001")

    tail = await logger.get_session_events(session_id, from_seq=1)
    assert [e.seq for e in tail] == [2, 3]


async def test_type_and_task_filters(db: Database) -> None:
    session_id = await create_session(db, "Carol", ["001"])
    logger = EventLogger(db)
    await logger.write(session_id, EventType.SESSION_STARTED, {"x": 1})
    await logger.write(session_id, EventType.TASK_PRESENTED, {"task_id": "001"}, task_id="001")
    await logger.write(session_id, EventType.CODE_EXECUTED, {"code": "y"}, task_id="001")

    only_exec = await logger.get_session_events(session_id, types=[EventType.CODE_EXECUTED])
    assert len(only_exec) == 1
    assert only_exec[0].type == EventType.CODE_EXECUTED

    task_events = await logger.get_task_events(session_id, "001")
    assert [e.type for e in task_events] == [EventType.TASK_PRESENTED, EventType.CODE_EXECUTED]
