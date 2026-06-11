"""Deploy artifacts (Phase 3): config env overrides + Docker image build."""

import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import Settings

_REPO_ROOT = Path(__file__).parent.parent
_REQUIRED_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "JUDGE0_API_KEY")


def _set_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in _REQUIRED_KEYS:
        monkeypatch.setenv(k, "test-key")


def test_config_reads_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_keys(monkeypatch)
    monkeypatch.setenv("DB_PATH", "/tmp/test.db")
    monkeypatch.setenv("TASKS_DIR", "/tmp/tasks")
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "true")
    s = Settings()  # type: ignore[call-arg]
    assert s.db_path == "/tmp/test.db"
    assert s.tasks_dir == "/tmp/tasks"
    assert s.trust_proxy_headers is True


def test_config_falls_back_to_local_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_keys(monkeypatch)
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.delenv("TASKS_DIR", raising=False)
    s = Settings()  # type: ignore[call-arg]
    assert s.db_path == "./events.db"
    assert s.tasks_dir == "./tasks"


@pytest.mark.docker
def test_docker_image_builds() -> None:
    """Build the image end-to-end. Slow; excluded from the default gate via
    `-m 'not docker'`. Skipped where Docker isn't available."""
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    result = subprocess.run(
        [
            "docker",
            "build",
            "-t",
            "ai-usage-scoring:pytest",
            "-f",
            str(_REPO_ROOT / "Dockerfile"),
            str(_REPO_ROOT),
        ],
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert result.returncode == 0, (
        f"docker build failed:\n{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
    )
