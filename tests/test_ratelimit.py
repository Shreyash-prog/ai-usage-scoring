"""Per-IP rate limiting (Phase 2): the WS sliding window, the WS handler wiring,
and slowapi's HTTP 429 on session creation."""

from types import SimpleNamespace

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from app.config import settings
from app.llm.chat_client import OpenAIChatClient
from app.llm.judge_client import AnthropicJudgeClient
from app.ratelimit import SlidingWindowLimiter, limiter, ws_chat_limiter
from app.storage.db import Database
from app.storage.events import EventLogger
from app.storage.sessions import create_session
from app.ws.candidate import CandidateDeps, CandidateSession


def test_sliding_window_allows_then_blocks_then_recovers() -> None:
    lim = SlidingWindowLimiter()
    # 3 allowed inside the window...
    assert lim.allow("ip", 3, 60, now=100.0)
    assert lim.allow("ip", 3, 60, now=101.0)
    assert lim.allow("ip", 3, 60, now=102.0)
    # ...4th in-window is blocked...
    assert not lim.allow("ip", 3, 60, now=103.0)
    # ...and once the oldest hits age out, it recovers.
    assert lim.allow("ip", 3, 60, now=162.0)
    # Different key is independent.
    assert lim.allow("other", 3, 60, now=103.0)


# --- WS handler integration -------------------------------------------------


class ScriptedWS:
    def __init__(self, messages: list[dict]) -> None:
        self.sent: list[dict] = []
        self.headers: dict[str, str] = {}
        self.client = SimpleNamespace(host="5.5.5.5")
        self._messages = list(messages)

    async def receive_json(self) -> dict:
        if self._messages:
            return self._messages.pop(0)
        raise WebSocketDisconnect()

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


class BoomChat:
    async def chat_stream(self, messages, temperature: float = 0.7):
        raise AssertionError("chat_stream must not run when rate-limited")
        yield  # pragma: no cover


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "rl.db"))
    await database.connect()
    yield database
    await database.close()


async def test_ws_chat_rate_limit_sends_message(db: Database, monkeypatch) -> None:
    monkeypatch.setattr(settings, "ratelimit_chat_per_minute", 1)
    sid = await create_session(db, "X", ["001"])
    ws = ScriptedWS([{"type": "chat.send", "text": "hi"}])
    deps = CandidateDeps(
        db=db,
        logger=EventLogger(db),
        sandbox=None,  # type: ignore[arg-type]
        chat=BoomChat(),  # type: ignore[arg-type]
        tasks=None,  # type: ignore[arg-type]
        ws_manager=None,  # type: ignore[arg-type]
        bus=None,  # type: ignore[arg-type]
        posthoc=None,  # type: ignore[arg-type]
        system_prompt="",
    )
    # Exhaust this IP's chat budget before the handler runs.
    assert ws_chat_limiter.allow("5.5.5.5", 1, 60)
    await CandidateSession(ws, sid, deps).run()  # type: ignore[arg-type]

    assert any(m["type"] == "rate_limited" and m["scope"] == "chat" for m in ws.sent)


# --- HTTP 429 on session creation -------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "rl_api.db"))

    async def fake_health(self) -> bool:
        return True

    monkeypatch.setattr(OpenAIChatClient, "health", fake_health)
    monkeypatch.setattr(AnthropicJudgeClient, "health", fake_health)

    from app.main import app

    with TestClient(app) as c:
        yield c


def test_http_session_rate_limit_429(client: TestClient) -> None:
    # Re-enable the limiter (conftest disables it by default) with a clean window.
    limiter.enabled = True
    limiter.reset()
    try:
        codes = [
            client.post(
                "/api/session", json={"candidate_name": "x", "task_sequence": ["001"]}
            ).status_code
            for _ in range(settings.ratelimit_sessions_per_hour + 1)
        ]
    finally:
        limiter.enabled = False
    assert codes[:-1] == [200] * settings.ratelimit_sessions_per_hour
    assert codes[-1] == 429
