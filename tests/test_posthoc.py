"""Offline PostHocScorer test (main spec §10.1): final rows with evidence, no network.

Uses a fake judge (always YES) so it never calls Anthropic. Verifies that each task
gets three phase='final' dimension rows carrying heuristic + judge evidence, a
session-level aggregate is written, and the session is marked 'scored'.
"""

import pytest

from app.bus import EventBus
from app.llm.judge_client import JudgeAnswer
from app.models.events import EventType
from app.scoring.posthoc import PostHocScorer
from app.storage.db import Database
from app.storage.events import EventLogger
from app.storage.scores import ScoreStore
from app.storage.sessions import create_session, get_session
from app.storage.tasks import TaskStore
from app.ws.manager import WSManager


class FakeJudge:
    def __init__(self) -> None:
        self.call_count = 0

    async def judge(self, prompt: str, temperature: float = 0.1) -> JudgeAnswer:
        self.call_count += 1
        return JudgeAnswer(answer="YES", evidence="stub")


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "ph.db"))
    await database.connect()
    yield database
    await database.close()


async def _seed(logger: EventLogger, sid: str) -> None:
    await logger.write(sid, EventType.SESSION_STARTED, {"candidate_name": "PH"})
    await logger.write(sid, EventType.TASK_PRESENTED, {"task_id": "001"}, task_id="001")
    await logger.write(
        sid,
        EventType.CHAT_PROMPT_SENT,
        {
            "text": "write reverse(s) in O(n) handling empty input",
            "attached_code": None,
            "attached_output": None,
        },
        task_id="001",
    )
    await logger.write(
        sid,
        EventType.CHAT_RESPONSE_RECEIVED,
        {
            "text": "```python\ndef reverse(s):\n    return s[::-1]\n```",
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "latency_ms": 1,
            "model": "f",
        },
        task_id="001",
    )
    await logger.write(
        sid,
        EventType.EDITOR_PASTE,
        {"text": "def reverse(s):\n    return s[::-1]", "source_hint": "chat", "char_count": 33},
        task_id="001",
    )
    await logger.write(
        sid,
        EventType.CODE_EXECUTED,
        {
            "code": "assert reverse('ab') == 'ba'",
            "stdin": None,
            "stdout": "",
            "stderr": "",
            "exit_code": 0,
            "runtime_ms": 1,
            "truncated": False,
        },
        task_id="001",
    )
    await logger.write(
        sid,
        EventType.TASK_SUBMITTED,
        {"task_id": "001", "final_code": "def reverse(s): return s[::-1]", "duration_ms": 1},
        task_id="001",
    )
    await logger.write(sid, EventType.SESSION_ENDED, {})


async def test_posthoc_writes_final_profile(db: Database) -> None:
    logger = EventLogger(db)
    sid = await create_session(db, "PH", ["001"])
    await _seed(logger, sid)

    tasks = TaskStore("./tasks")
    tasks.load()
    scorer = PostHocScorer(db, logger, FakeJudge(), ScoreStore(db), tasks, WSManager(), EventBus())
    await scorer.score_session(sid)

    finals = await ScoreStore(db).list_scores(sid, "final")
    task_dims = {f["dimension"] for f in finals if f["task_id"] == "001"}
    assert task_dims == {"prompt_quality", "verification", "iteration"}

    pq = next(f for f in finals if f["dimension"] == "prompt_quality" and f["task_id"] == "001")
    assert "heuristic" in pq["evidence"]
    assert len(pq["evidence"]["judges"]) >= 1  # judge questions ran and were recorded
    assert pq["evidence"]["judges"][0]["question_id"].startswith("PQ")

    # Session-level aggregate (task_id NULL) for all three dimensions.
    agg = [f for f in finals if f["task_id"] is None]
    assert {f["dimension"] for f in agg} == {"prompt_quality", "verification", "iteration"}

    session = await get_session(db, sid)
    assert session is not None and session.status == "scored"
