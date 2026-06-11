"""Sandbox tests — Judge0 HTTP client (deployment migration).

The Judge0 layer is mocked via httpx.MockTransport: each test wires a canned
Judge0 submission response (or an upstream failure) and asserts the ExecResult
mapping. One @pytest.mark.live test hits the real Judge0 free tier.

Status ids used: 3=Accepted, 5=Time Limit Exceeded, 6=Compilation Error,
7=Runtime Error (SIGSEGV), 11=Runtime Error (NZEC), 14=Exec Format Error.
"""

import json
from collections.abc import Callable

import httpx
import pytest

from app.config import settings
from app.sandbox.runner import Sandbox


def _sandbox(handler: Callable[[httpx.Request], httpx.Response]) -> Sandbox:
    return Sandbox(transport=httpx.MockTransport(handler))


def _responder(payload: dict, status_code: int = 201) -> Callable[[httpx.Request], httpx.Response]:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    return handler


async def test_normal_run() -> None:
    sandbox = _sandbox(
        _responder({"stdout": "hello sandbox\n", "stderr": None, "status": {"id": 3}})
    )
    result = await sandbox.run_python("print('hello sandbox')")
    assert result.exit_code == 0
    assert result.stdout == "hello sandbox\n"
    assert result.stderr == ""
    assert result.truncated is False
    assert result.runtime_ms >= 0


async def test_stdin_is_piped() -> None:
    # Assert the candidate's stdin is forwarded to Judge0 in the request body.
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["stdin"] == "hi there"
        assert body["language_id"] == settings.judge0_language_id
        return httpx.Response(201, json={"stdout": "HI THERE\n", "status": {"id": 3}})

    sandbox = _sandbox(handler)
    result = await sandbox.run_python(
        "import sys; print(sys.stdin.read().upper())", stdin="hi there"
    )
    assert result.exit_code == 0
    assert result.stdout == "HI THERE\n"


async def test_stderr_and_nonzero_exit() -> None:
    traceback = "Traceback (most recent call last):\n  ...\nValueError: boom\n"
    sandbox = _sandbox(_responder({"stderr": traceback, "exit_code": 1, "status": {"id": 11}}))
    result = await sandbox.run_python("raise ValueError('boom')")
    assert result.exit_code == 1
    assert result.exit_code != 0
    assert "ValueError: boom" in result.stderr
    assert "Traceback" in result.stderr


async def test_timeout() -> None:
    # Judge0 reports Time Limit Exceeded (status 5) → exit_code 124.
    sandbox = _sandbox(_responder({"stdout": None, "status": {"id": 5}}))
    result = await sandbox.run_python("import time\ntime.sleep(30)", timeout_s=1)
    assert result.exit_code == 124


async def test_cpu_bound_is_bounded() -> None:
    # A runaway loop also surfaces as Judge0 TLE; it must not exit cleanly.
    sandbox = _sandbox(_responder({"status": {"id": 5}}))
    result = await sandbox.run_python("while True:\n    pass", timeout_s=1)
    assert result.exit_code != 0


async def test_compilation_error() -> None:
    sandbox = _sandbox(_responder({"compile_output": "SyntaxError: bad", "status": {"id": 6}}))
    result = await sandbox.run_python("def (:")
    assert result.exit_code == 1
    assert "SyntaxError" in result.stderr


async def test_output_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "exec_output_limit_kb", 1)  # 1 KiB cap
    sandbox = _sandbox(_responder({"stdout": "a" * 5000, "status": {"id": 3}}))
    result = await sandbox.run_python("print('a' * 5000)")
    assert result.exit_code == 0
    assert result.truncated is True
    assert len(result.stdout.encode("utf-8")) <= 1024


async def test_judge0_reports_mle() -> None:
    # Memory-limit-exceeded surfaces from Judge0 as a runtime error (e.g. SIGSEGV,
    # status 7) — the OOM allocation must not exit cleanly.
    sandbox = _sandbox(
        _responder(
            {
                "stdout": None,
                "stderr": None,
                "message": "Memory Limit Exceeded",
                "exit_signal": 9,
                "status": {"id": 7, "description": "Runtime Error (SIGSEGV)"},
            }
        )
    )
    result = await sandbox.run_python(
        "x = bytearray(512 * 1024 * 1024)\nprint(len(x))", mem_limit_mb=128
    )
    assert result.exit_code != 0
    assert "512" not in result.stdout


async def test_unknown_status_maps_to_minus_one() -> None:
    sandbox = _sandbox(_responder({"status": {"id": 14, "description": "Exec Format Error"}}))
    result = await sandbox.run_python("print('x')")
    assert result.exit_code == -1
    assert "Exec Format Error" in result.stderr


async def test_code_too_large() -> None:
    # Rejected before any HTTP call — a transport that errors proves we never call it.
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("Judge0 must not be called for oversized code")

    sandbox = _sandbox(handler)
    result = await sandbox.run_python("x = '" + "a" * (50 * 1024 + 1) + "'")
    assert result.exit_code == -1
    assert result.stderr == "code too large"


async def test_stdin_too_large() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("Judge0 must not be called for oversized stdin")

    sandbox = _sandbox(handler)
    result = await sandbox.run_python("pass", stdin="a" * (10 * 1024 + 1))
    assert result.exit_code == -1
    assert result.stderr == "stdin too large"


async def test_upstream_http_error_maps_to_minus_one() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    sandbox = _sandbox(handler)
    result = await sandbox.run_python("print('x')")
    assert result.exit_code == -1
    assert "Judge0 request failed" in result.stderr
    assert result.truncated is False


async def test_upstream_non_2xx_maps_to_minus_one() -> None:
    sandbox = _sandbox(_responder({"error": "rate limited"}, status_code=429))
    result = await sandbox.run_python("print('x')")
    assert result.exit_code == -1
    assert "429" in result.stderr


@pytest.mark.live
async def test_live_judge0_hello() -> None:
    # Hits the real Judge0 free tier; requires JUDGE0_API_KEY in .env.
    sandbox = Sandbox()
    result = await sandbox.run_python("print('hello')")
    assert result.exit_code == 0
    assert result.stdout == "hello\n"
