"""Sandbox tests (main spec §18.1, Day 2): normal, stderr, timeout, truncation, OOM.

Note on platform: the memory cap relies on RLIMIT_AS, which macOS refuses to set
(`_apply_rlimits` swallows that, leaving the cap a Linux-only no-op). The OOM test
is therefore skipped off Linux — running an uncapped allocation bomb on the host
would be reckless, not a useful assertion.
"""

import sys
import time

import pytest

from app.config import settings
from app.sandbox.runner import Sandbox


@pytest.fixture
def sandbox() -> Sandbox:
    return Sandbox()


async def test_normal_run(sandbox: Sandbox) -> None:
    result = await sandbox.run_python("print('hello sandbox')")
    assert result.exit_code == 0
    assert result.stdout == "hello sandbox\n"
    assert result.stderr == ""
    assert result.truncated is False
    assert result.runtime_ms >= 0


async def test_stdin_is_piped(sandbox: Sandbox) -> None:
    result = await sandbox.run_python(
        "import sys; print(sys.stdin.read().upper())", stdin="hi there"
    )
    assert result.exit_code == 0
    assert result.stdout == "HI THERE\n"


async def test_stderr_and_nonzero_exit(sandbox: Sandbox) -> None:
    result = await sandbox.run_python("raise ValueError('boom')")
    assert result.exit_code != 0
    assert "ValueError: boom" in result.stderr
    assert "Traceback" in result.stderr


async def test_timeout(sandbox: Sandbox) -> None:
    start = time.monotonic()
    result = await sandbox.run_python("import time\ntime.sleep(30)", timeout_s=1)
    elapsed_ms = (time.monotonic() - start) * 1000
    # Wall-clock timeout fires at timeout_s + 1; sleeping burns no CPU so RLIMIT_CPU
    # does not pre-empt it. Killed via process-group SIGKILL, reported as 124 (§16).
    assert result.exit_code == 124
    assert elapsed_ms < 4000  # nowhere near the 30s the script asked for


async def test_cpu_bound_is_bounded(sandbox: Sandbox) -> None:
    # A busy loop burns CPU and is cut off (RLIMIT_CPU SIGXCPU, or the wall timeout).
    start = time.monotonic()
    result = await sandbox.run_python("while True:\n    pass", timeout_s=1)
    elapsed_ms = (time.monotonic() - start) * 1000
    assert result.exit_code != 0
    assert elapsed_ms < 4000


async def test_output_truncation(sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "exec_output_limit_kb", 1)  # 1 KiB cap
    result = await sandbox.run_python("print('a' * 5000)")
    assert result.exit_code == 0
    assert result.truncated is True
    assert len(result.stdout.encode("utf-8")) <= 1024


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="RLIMIT_AS memory cap is only enforced on Linux (macOS refuses to set it)",
)
async def test_oom(sandbox: Sandbox) -> None:
    # Allocate well past the cap; under RLIMIT_AS the allocation fails (MemoryError)
    # or the process is killed. Either way it must not exit cleanly.
    result = await sandbox.run_python(
        "x = bytearray(512 * 1024 * 1024)\nprint(len(x))", mem_limit_mb=128
    )
    assert result.exit_code != 0
    assert "512" not in result.stdout
