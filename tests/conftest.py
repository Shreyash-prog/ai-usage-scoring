"""Shared test fixtures.

The app + slowapi limiter are module-level singletons shared across the whole
suite, so per-IP limits would accumulate across unrelated tests (all from the
TestClient's single "testclient" IP) and trip spuriously. Disable rate limiting
by default; the rate-limit tests re-enable it explicitly.
"""

import pytest

from app.ratelimit import limiter, ws_chat_limiter, ws_run_limiter


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    limiter.enabled = False
    ws_chat_limiter.reset()
    ws_run_limiter.reset()
    yield
    limiter.enabled = True
    ws_chat_limiter.reset()
    ws_run_limiter.reset()
