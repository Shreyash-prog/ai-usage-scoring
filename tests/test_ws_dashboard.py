"""Dashboard WS replay test (main spec §12.3): hello replays events from last_seq."""

import time

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.llm.chat_client import ChatChunk, OpenAIChatClient
from app.llm.judge_client import AnthropicJudgeClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "dash.db"))

    async def fake_health(self) -> bool:
        return True

    async def fake_stream(self, messages, temperature: float = 0.7):
        yield ChatChunk(
            text="```python\ndef reverse(s): return s[::-1]\n```", done=False, model="f"
        )
        yield ChatChunk(text="", done=True, prompt_tokens=4, completion_tokens=8, model="f")

    monkeypatch.setattr(OpenAIChatClient, "health", fake_health)
    monkeypatch.setattr(AnthropicJudgeClient, "health", fake_health)
    monkeypatch.setattr(OpenAIChatClient, "chat_stream", fake_stream)

    from app.main import app

    with TestClient(app) as c:
        yield c


def _seed_session(client: TestClient) -> str:
    session_id = client.post("/api/session", json={"candidate_name": "Dash"}).json()["session_id"]
    with client.websocket_connect(f"/ws/session/{session_id}") as ws:
        ws.send_json({"type": "hello", "last_seq": 0})
        ws.receive_json()  # task.presented
        ws.send_json(
            {
                "type": "chat.send",
                "text": "write reverse with detail please",
                "attach_editor": False,
                "attach_output": False,
            }
        )
        while ws.receive_json()["type"] != "chat.done":
            pass
        ws.send_json({"type": "code.run", "code": "print('ok')", "stdin": None})
        ws.receive_json()
        ws.send_json({"type": "task.submit", "final_code": "x = 1"})
        ws.receive_json()
    # let the bus consumer flush live scores
    for _ in range(15):
        if client.get(f"/api/session/{session_id}/scores", params={"phase": "live"}).json():
            break
        time.sleep(0.1)
    return session_id


def test_dashboard_replays_all_events_and_scores(client: TestClient) -> None:
    session_id = _seed_session(client)
    all_events = client.get(f"/api/session/{session_id}/events").json()
    count = len(all_events)
    assert count > 0

    with client.websocket_connect(f"/ws/dashboard/{session_id}") as ws:
        ws.send_json({"type": "hello", "last_seq": 0})
        replayed = [ws.receive_json() for _ in range(count)]
        assert all(m["type"] == "event" for m in replayed)
        seqs = [m["event"]["seq"] for m in replayed]
        assert seqs == sorted(seqs)
        # After the events, the current score snapshot arrives as score.update(s).
        snapshot = ws.receive_json()
        assert snapshot["type"] == "score.update"
        assert snapshot["dimension"] in {"prompt_quality", "verification", "iteration"}


def test_dashboard_replay_respects_last_seq(client: TestClient) -> None:
    session_id = _seed_session(client)
    with client.websocket_connect(f"/ws/dashboard/{session_id}") as ws:
        ws.send_json({"type": "hello", "last_seq": 3})
        first = ws.receive_json()
        assert first["type"] == "event"
        assert first["event"]["seq"] > 3  # only events after last_seq are replayed
