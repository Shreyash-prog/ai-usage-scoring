"""Hosted Python sandbox via Judge0 (CE, RapidAPI).

Replaces the original local `subprocess` runner for public deployment: the local
server never executes candidate code. Code is POSTed to Judge0 in synchronous
(`wait=true`) mode and the response is mapped onto the unchanged `ExecResult`
shape so callers (and the WS handler) need no changes.

The `Sandbox.run_python` signature and `ExecResult` shape are frozen — only the
internals changed. `mem_limit_mb` is accepted for interface compatibility but is
not forwarded per-call: the Judge0 free tier enforces its own default memory cap
(see FINDINGS "Deployment Migration").
"""

import time
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel

from app.config import settings

# Enforced input limits (reject before spending a Judge0 call).
_CODE_LIMIT_BYTES = 50 * 1024
_STDIN_LIMIT_BYTES = 10 * 1024

# Judge0 status ids (https://ce.judge0.com/#statuses-and-languages-status-get).
_STATUS_ACCEPTED = 3
_STATUS_TLE = 5
_STATUS_COMPILE_ERROR = 6
_STATUS_RUNTIME_ERRORS = frozenset({7, 8, 9, 10, 11, 12})  # SIGSEGV..Other / NZEC


class ExecResult(BaseModel):
    stdout: str
    stderr: str
    exit_code: int  # 0=ok, 124=timeout, 137=sigkill, -1=could-not-start, other=script error
    runtime_ms: int
    truncated: bool


def _truncate(text: str) -> tuple[str, bool]:
    """Clip `text` to the configured byte budget; report whether it was cut."""
    limit = settings.exec_output_limit_kb * 1024
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text, False
    return raw[:limit].decode("utf-8", errors="ignore"), True


def _map_response(data: dict, runtime_ms: int) -> ExecResult:
    """Translate a Judge0 submission result into an ExecResult."""
    status = data.get("status") or {}
    status_id = status.get("id")
    status_desc = status.get("description") or ""

    stdout_raw = data.get("stdout") or ""
    stderr_raw = data.get("stderr") or ""
    compile_output = data.get("compile_output") or ""
    message = data.get("message") or ""

    if status_id == _STATUS_ACCEPTED:
        exit_code = 0
        err_text = stderr_raw
    elif status_id == _STATUS_TLE:
        exit_code = 124
        err_text = stderr_raw or message or "Time limit exceeded"
    elif status_id == _STATUS_COMPILE_ERROR:
        exit_code = 1
        err_text = compile_output or message or "Compilation error"
    elif status_id in _STATUS_RUNTIME_ERRORS:
        rc = data.get("exit_code")
        if rc is None:
            sig = data.get("exit_signal")
            rc = 128 + sig if sig else 1
        exit_code = rc
        err_text = stderr_raw or message or status_desc or "Runtime error"
    else:
        # In-queue/processing (shouldn't happen with wait=true), wrong-answer
        # (no expected_output), internal/exec-format errors, or anything new.
        exit_code = -1
        err_text = f"Judge0 status {status_id}: {status_desc}".strip()

    stdout, t_out = _truncate(stdout_raw)
    stderr, t_err = _truncate(err_text)
    return ExecResult(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        runtime_ms=runtime_ms,
        truncated=t_out or t_err,
    )


def _input_rejection(reason: str) -> ExecResult:
    return ExecResult(stdout="", stderr=reason, exit_code=-1, runtime_ms=0, truncated=False)


def _upstream_failure(reason: str, runtime_ms: int) -> ExecResult:
    return ExecResult(
        stdout="", stderr=reason, exit_code=-1, runtime_ms=runtime_ms, truncated=False
    )


class Sandbox:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # `transport` lets tests inject an httpx.MockTransport; production uses
        # the default (real network) transport.
        self._transport = transport

    async def run_python(
        self,
        code: str,
        stdin: str | None = None,
        timeout_s: int | None = None,
        mem_limit_mb: int | None = None,
    ) -> ExecResult:
        """Execute `code` on Judge0 and capture its output as an ExecResult."""
        timeout_s = timeout_s if timeout_s is not None else settings.exec_timeout_s
        # mem_limit_mb is part of the frozen signature but intentionally not forwarded:
        # the Judge0 free tier enforces its own default memory cap (see module docstring).

        # Enforced input caps — fail fast, before spending a Judge0 call.
        if len(code.encode("utf-8")) > _CODE_LIMIT_BYTES:
            return _input_rejection("code too large")
        if stdin is not None and len(stdin.encode("utf-8")) > _STDIN_LIMIT_BYTES:
            return _input_rejection("stdin too large")

        payload = {
            "source_code": code,
            "language_id": settings.judge0_language_id,
            "stdin": stdin if stdin is not None else "",
            "expected_output": None,
            "cpu_time_limit": float(timeout_s),
            "wall_time_limit": float(timeout_s + 5),
        }
        headers = {
            "X-RapidAPI-Key": settings.judge0_api_key,
            "X-RapidAPI-Host": urlparse(settings.judge0_endpoint).netloc,
            "Content-Type": "application/json",
        }

        start = time.monotonic()
        try:
            async with httpx.AsyncClient(
                base_url=settings.judge0_endpoint,
                transport=self._transport,
                timeout=settings.judge0_request_timeout_s,
            ) as client:
                resp = await client.post(
                    "/submissions",
                    params={"base64_encoded": "false", "wait": "true"},
                    headers=headers,
                    json=payload,
                )
        except httpx.HTTPError as exc:
            runtime_ms = int((time.monotonic() - start) * 1000)
            return _upstream_failure(f"Judge0 request failed: {exc}", runtime_ms)

        runtime_ms = int((time.monotonic() - start) * 1000)
        if resp.status_code not in (200, 201):
            return _upstream_failure(
                f"Judge0 returned HTTP {resp.status_code}: {resp.text[:500]}", runtime_ms
            )
        try:
            data = resp.json()
        except ValueError:
            return _upstream_failure("Judge0 returned a non-JSON response", runtime_ms)

        return _map_response(data, runtime_ms)
