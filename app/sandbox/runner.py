"""Python subprocess sandbox (main spec §11).

NOT production-safe. No network/filesystem/import isolation — just `python -I`
in a throwaway temp dir under POSIX rlimits (CPU, address space, file size,
process count), a wall-clock timeout, and output truncation. The README must
carry the loud warning from §11.3.
"""

import asyncio
import os
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from pydantic import BaseModel

from app.config import settings

_FSIZE_LIMIT_BYTES = 10 * 1024 * 1024  # §11.2
_NPROC_LIMIT = 50


class ExecResult(BaseModel):
    stdout: str
    stderr: str
    exit_code: int  # 0=ok, 124=timeout, 137=sigkill, -1=could-not-start, other=script error
    runtime_ms: int
    truncated: bool


def _apply_rlimits(timeout_s: int, mem_limit_mb: int) -> None:
    """preexec_fn: tighten resource limits in the child before exec (§11.2).

    CPU, FSIZE, and NPROC are portable across Linux and macOS and are enforced.
    RLIMIT_AS is BEST-EFFORT: macOS rejects it (`ValueError: current limit exceeds
    maximum limit`), and a raising preexec_fn aborts the whole launch — so we
    swallow that failure. Net effect: the memory cap is enforced on Linux and is a
    no-op on macOS. The wall-clock timeout still bounds runtime everywhere. This is
    an accepted v0 gap (§11.3) — see the README's sandbox warning.
    """
    resource.setrlimit(resource.RLIMIT_CPU, (timeout_s, timeout_s))
    resource.setrlimit(resource.RLIMIT_FSIZE, (_FSIZE_LIMIT_BYTES, _FSIZE_LIMIT_BYTES))
    if hasattr(resource, "RLIMIT_NPROC"):
        resource.setrlimit(resource.RLIMIT_NPROC, (_NPROC_LIMIT, _NPROC_LIMIT))
    if hasattr(resource, "RLIMIT_AS"):
        as_bytes = mem_limit_mb * 1024 * 1024
        try:
            resource.setrlimit(resource.RLIMIT_AS, (as_bytes, as_bytes))
        except (ValueError, OSError):
            pass  # platform refuses RLIMIT_AS (e.g. macOS); memory cap is Linux-only


def _truncate(raw: bytes) -> tuple[str, bool]:
    limit = settings.exec_output_limit_kb * 1024
    truncated = len(raw) > limit
    clipped = raw[:limit] if truncated else raw
    return clipped.decode("utf-8", errors="replace"), truncated


def _normalize_returncode(rc: int | None) -> int:
    """Map a Popen returncode to our exit_code convention.

    Signal deaths (negative returncodes) become 128+signal, matching the shell
    convention — so a SIGKILL surfaces as 137 (§16 'oom-killed') and SIGXCPU as 152.
    """
    if rc is None:
        return -1
    if rc < 0:
        return 128 + (-rc)
    return rc


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class Sandbox:
    async def run_python(
        self,
        code: str,
        stdin: str | None = None,
        timeout_s: int | None = None,
        mem_limit_mb: int | None = None,
    ) -> ExecResult:
        """Run `code` as an isolated-mode Python script and capture its output."""
        timeout_s = timeout_s if timeout_s is not None else settings.exec_timeout_s
        mem_limit_mb = mem_limit_mb if mem_limit_mb is not None else settings.exec_mem_limit_mb

        temp_dir = tempfile.mkdtemp(prefix="sbx_")
        script_path = Path(temp_dir) / "main.py"
        script_path.write_text(code, encoding="utf-8")
        start = time.monotonic()

        try:
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-I",
                    str(script_path),
                    cwd=temp_dir,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                    preexec_fn=lambda: _apply_rlimits(timeout_s, mem_limit_mb),
                )
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                runtime_ms = int((time.monotonic() - start) * 1000)
                return ExecResult(
                    stdout="",
                    stderr=f"Could not start interpreter: {exc}",
                    exit_code=-1,
                    runtime_ms=runtime_ms,
                    truncated=False,
                )

            stdin_bytes = stdin.encode("utf-8") if stdin is not None else None
            timed_out = False
            try:
                out, err = await asyncio.wait_for(
                    proc.communicate(stdin_bytes), timeout=timeout_s + 1
                )
            except TimeoutError:
                timed_out = True
                _kill_process_group(proc)
                try:
                    out, err = await asyncio.wait_for(proc.communicate(), timeout=2)
                except (TimeoutError, Exception):
                    out, err = b"", b""

            runtime_ms = int((time.monotonic() - start) * 1000)
            stdout, t_out = _truncate(out)
            stderr, t_err = _truncate(err)
            exit_code = 124 if timed_out else _normalize_returncode(proc.returncode)
            return ExecResult(
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                runtime_ms=runtime_ms,
                truncated=t_out or t_err,
            )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
