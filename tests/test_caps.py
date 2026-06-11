"""Cost-cap enforcement (Phase 2): per-session chat/exec caps, global session cap,
per-session + global judge caps. Happy-path non-regression is covered by the
existing test_ws_candidate end-to-end test."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.bus import EventBus
from app.config import settings
from app.llm.chat_client import OpenAIChatClient
from app.llm.judge_client import AnthropicJudgeClient, JudgeAnswer
from app.models.events import EventType
from app.sandbox.runner import ExecResult
from app.scoring.posthoc import PostHocScorer
from app.storage import llm_calls
from app.storage.db import Database
from app.storage.events import EventLogger
from app.storage.scores import ScoreStore
from app.storage.sessions import create_session
from app.storage.tasks import TaskStore
from app.ws.candidate import CandidateDeps, CandidateSession
from app.ws.manager import WSManager

# --- fakes ------------------------------------------------------------------


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.headers: dict[str, str] = {}
        self.client = SimpleNamespace(host="9.9.9.9")

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


class FakeSandbox:
    def __init__(self) -> None:
        self.calls = 0

    async def run_python(self, code, stdin=None, timeout_s=None, mem_limit_mb=None) -> ExecResult:
        self.calls += 1
        return ExecResult(stdout="ok", stderr="", exit_code=0, runtime_ms=1, truncated=False)


class FakeChat:
    async def chat_stream(self, messages, temperature: float = 0.7):
        raise AssertionError("chat_stream must not be called when the cap is hit")
        yield  # pragma: no cover  (makes this an async generator)


class FakeJudge:
    def __init__(self) -> None:
        self.call_count = 0

    async def judge(
        self, prompt, temperature=0.1, *, session_id=None, purpose="judge"
    ) -> JudgeAnswer:
        self.call_count += 1
        return JudgeAnswer(answer="YES", evidence="stub")


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "caps.db"))
    await database.connect()
    yield database
    await database.close()


def _make_session(db: Database, sid: str, ws: FakeWS, sandbox=None, chat=None) -> CandidateSession:
    deps = CandidateDeps(
        db=db,
        logger=EventLogger(db),
        sandbox=sandbox or FakeSandbox(),
        chat=chat or FakeChat(),
        tasks=None,  # type: ignore[arg-type]
        ws_manager=None,  # type: ignore[arg-type]
        bus=None,  # type: ignore[arg-type]
        posthoc=None,  # type: ignore[arg-type]
        system_prompt="",
    )
    return CandidateSession(ws, sid, deps)  # type: ignore[arg-type]


# --- per-session chat-token cap ---------------------------------------------


async def test_chat_token_cap_refuses(db: Database, monkeypatch) -> None:
    monkeypatch.setattr(settings, "session_max_chat_tokens_in", 100)
    sid = await create_session(db, "X", ["001"])
    # Already over the cap from prior chat usage.
    await llm_calls.record_llm_call(db, sid, "openai", "gpt-4o-mini", "chat", 150, 10, 1, "ok")

    ws = FakeWS()
    chat = FakeChat()
    session = _make_session(db, sid, ws, chat=chat)
    await session._on_chat({"text": "hello"})

    assert ws.sent and ws.sent[-1]["type"] == "chat.capped"
    # No CHAT_PROMPT_SENT event was emitted and no LLM call was made.
    events = await EventLogger(db).get_session_events(sid)
    assert not any(e.type == EventType.CHAT_PROMPT_SENT for e in events)


# --- per-session code-execution cap -----------------------------------------


async def test_code_execution_cap_refuses(db: Database, monkeypatch) -> None:
    monkeypatch.setattr(settings, "session_max_code_executions", 2)
    sid = await create_session(db, "X", ["001"])
    logger = EventLogger(db)
    for _ in range(2):
        await logger.write(
            sid,
            EventType.CODE_EXECUTED,
            {"code": "x", "exit_code": 0, "stdout": "", "stderr": ""},
            task_id="001",
        )

    ws = FakeWS()
    sandbox = FakeSandbox()
    session = _make_session(db, sid, ws, sandbox=sandbox)
    await session._on_run({"code": "print(1)"})

    assert sandbox.calls == 0  # Judge0 was never called
    assert ws.sent[-1]["type"] == "exec.result"
    assert ws.sent[-1]["exit_code"] == -1
    assert ws.sent[-1]["stderr"] == "session execution limit reached"


# --- global daily session cap -----------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "caps_api.db"))

    async def fake_health(self) -> bool:
        return True

    monkeypatch.setattr(OpenAIChatClient, "health", fake_health)
    monkeypatch.setattr(AnthropicJudgeClient, "health", fake_health)

    from app.main import app

    with TestClient(app) as c:
        yield c


def test_global_session_cap_returns_429(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(settings, "global_max_sessions_per_day", 2)
    ok1 = client.post("/api/session", json={"candidate_name": "a", "task_sequence": ["001"]})
    ok2 = client.post("/api/session", json={"candidate_name": "b", "task_sequence": ["001"]})
    capped = client.post("/api/session", json={"candidate_name": "c", "task_sequence": ["001"]})
    assert ok1.status_code == 200 and ok2.status_code == 200
    assert capped.status_code == 429
    assert "capacity" in capped.json()["detail"].lower()


def test_status_endpoint_reports_capped(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(settings, "global_max_sessions_per_day", 1)
    assert client.get("/api/status").json()["status"] == "ok"
    client.post("/api/session", json={"candidate_name": "a", "task_sequence": ["001"]})
    body = client.get("/api/status").json()
    assert body["status"] == "capped" and "capacity" in body["message"].lower()
    # healthz stays 200 even when capped, and exposes no per-session info.
    hz = client.get("/api/healthz")
    assert hz.status_code == 200
    assert "sessions_today" in hz.json() and "session_id" not in hz.text


# --- judge caps (per-session + global) --------------------------------------


async def _seed_two_prompts(logger: EventLogger, sid: str) -> None:
    await logger.write(sid, EventType.TASK_PRESENTED, {"task_id": "001"}, task_id="001")
    for txt in ("first question about the task", "second different question entirely"):
        await logger.write(
            sid,
            EventType.CHAT_PROMPT_SENT,
            {"text": txt, "attached_code": None, "attached_output": None},
            task_id="001",
        )
    await logger.write(sid, EventType.SESSION_ENDED, {})


def _scorer(db: Database, judge: FakeJudge) -> PostHocScorer:
    tasks = TaskStore("./tasks")
    tasks.load()
    return PostHocScorer(db, EventLogger(db), judge, ScoreStore(db), tasks, WSManager(), EventBus())


async def test_per_session_judge_cap_truncates_jobs(db: Database, monkeypatch) -> None:
    monkeypatch.setattr(settings, "session_max_judge_calls", 2)
    sid = await create_session(db, "X", ["001"])
    await _seed_two_prompts(EventLogger(db), sid)
    judge = FakeJudge()
    await _scorer(db, judge).score_session(sid)

    assert judge.call_count == 2  # capped at the per-session limit
    finals = await ScoreStore(db).list_scores(sid, "final")
    assert any(f["evidence"].get("cost_capped") for f in finals)


async def test_global_judge_cap_blocks_scheduling(db: Database, monkeypatch) -> None:
    monkeypatch.setattr(settings, "global_max_judge_calls_per_day", 1)
    sid = await create_session(db, "X", ["001"])
    # Pre-existing judge call today pushes us to the global ceiling.
    await llm_calls.record_llm_call(
        db, sid, "anthropic", "claude-sonnet-4-6", "judge:PQ1", 1, 1, 1, "ok"
    )
    await _seed_two_prompts(EventLogger(db), sid)
    judge = FakeJudge()
    await _scorer(db, judge).score_session(sid)

    assert judge.call_count == 0  # global ceiling already reached
    finals = await ScoreStore(db).list_scores(sid, "final")
    assert any(f["evidence"].get("cost_capped") for f in finals)
