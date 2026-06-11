"""Candidate WS integration test (main spec §12.2, Day 3 end-to-end).

Drives the real app via Starlette's TestClient: create session, then over the
WebSocket exercise task.presented -> snapshot ack -> code.run (mocked sandbox) ->
chat (mocked stream) -> task.submit -> session.done.

The OpenAI/Anthropic clients and the Judge0 sandbox are monkeypatched so the test
never hits the network: `health()` returns True, `chat_stream` yields canned
chunks, and `run_python` returns a canned ExecResult.
"""

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.llm.chat_client import ChatChunk, OpenAIChatClient
from app.llm.judge_client import AnthropicJudgeClient
from app.sandbox.runner import ExecResult, Sandbox


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "ws.db"))

    async def fake_health(self) -> bool:
        return True

    async def fake_stream(self, messages, temperature: float = 0.7):
        for piece in ["Hel", "lo"]:
            yield ChatChunk(text=piece, done=False, model="fake")
        yield ChatChunk(text="", done=True, prompt_tokens=3, completion_tokens=2, model="fake")

    async def fake_judge(self, prompt: str, temperature: float = 0.1):
        from app.llm.judge_client import JudgeAnswer

        return JudgeAnswer(answer="YES", evidence="stub")

    async def fake_run_python(self, code, stdin=None, timeout_s=None, mem_limit_mb=None):
        return ExecResult(stdout="4\n", stderr="", exit_code=0, runtime_ms=1, truncated=False)

    monkeypatch.setattr(OpenAIChatClient, "health", fake_health)
    monkeypatch.setattr(AnthropicJudgeClient, "health", fake_health)
    monkeypatch.setattr(OpenAIChatClient, "chat_stream", fake_stream)
    monkeypatch.setattr(AnthropicJudgeClient, "judge", fake_judge)  # no real judge calls in tests
    monkeypatch.setattr(Sandbox, "run_python", fake_run_python)  # no real Judge0 calls in tests

    from app.main import app

    with TestClient(app) as c:
        yield c


def test_candidate_end_to_end(client: TestClient) -> None:
    resp = client.post("/api/session", json={"candidate_name": "Tester", "task_sequence": ["001"]})
    assert resp.status_code == 200
    session_id = resp.json()["session_id"]
    assert resp.json()["task_sequence"] == ["001"]

    with client.websocket_connect(f"/ws/session/{session_id}") as ws:
        ws.send_json({"type": "hello", "last_seq": 0})
        presented = ws.receive_json()
        assert presented["type"] == "task.presented"
        assert presented["task"]["id"] == "001"
        assert presented["total_tasks"] == 1

        ws.send_json({"type": "editor.snapshot", "code": "x = 1", "trigger": "manual"})
        ack = ws.receive_json()
        assert ack["type"] == "ack" and ack["event_type"] == "editor.snapshot"

        ws.send_json({"type": "code.run", "code": "print(2 + 2)", "stdin": None})
        result = ws.receive_json()
        assert result["type"] == "exec.result"
        assert result["exit_code"] == 0
        assert "4" in result["stdout"]

        ws.send_json(
            {"type": "chat.send", "text": "hi", "attach_editor": False, "attach_output": False}
        )
        streamed = ""
        while True:
            msg = ws.receive_json()
            if msg["type"] == "chat.token":
                streamed += msg["text"]
            elif msg["type"] == "chat.done":
                assert msg["full_text"] == "Hello"
                break
        assert streamed == "Hello"

        ws.send_json({"type": "task.submit", "final_code": "x = 1"})
        done = ws.receive_json()
        assert done["type"] == "session.done"

    # Events were persisted with monotonic seq across the whole flow.
    events = client.get(f"/api/session/{session_id}/events").json()
    types = [e["type"] for e in events]
    assert "session.started" in types
    assert "task.presented" in types
    assert "code.executed" in types
    assert "chat.prompt_sent" in types
    assert "chat.response_received" in types
    assert "task.submitted" in types
    assert "session.ended" in types
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))
