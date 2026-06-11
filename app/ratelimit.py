"""Per-IP rate limiting + client-IP extraction (Phase 2, public deploy).

No auth means IP is the only throttle key we have. HTTP routes use slowapi
(`limiter`); the candidate WebSocket can't go through slowapi (no Request), so
chat/run messages are throttled in-handler via `SlidingWindowLimiter`.

Client IP comes from `X-Forwarded-For` ONLY when `trust_proxy_headers` is set —
i.e. when we're actually behind Fly's edge proxy. Off-platform that header is
attacker-controlled, so we fall back to the socket peer address.
"""

import time
from collections import defaultdict, deque
from collections.abc import Mapping

from fastapi import Request, WebSocket
from slowapi import Limiter

from app.config import settings


def _ip_from(headers: Mapping[str, str], peer: str | None) -> str:
    if settings.trust_proxy_headers:
        xff = headers.get("x-forwarded-for")
        if xff:
            # First hop is the original client; the rest are proxies.
            return xff.split(",")[0].strip()
    return peer or "unknown"


def client_ip_request(request: Request) -> str:
    return _ip_from(request.headers, request.client.host if request.client else None)


def client_ip_ws(ws: WebSocket) -> str:
    return _ip_from(ws.headers, ws.client.host if ws.client else None)


# HTTP limiter. headers_enabled adds standard X-RateLimit-* response headers.
limiter = Limiter(key_func=client_ip_request, headers_enabled=True)


def sessions_per_hour_limit() -> str:
    """Callable limit so tests can override the setting at runtime."""
    return f"{settings.ratelimit_sessions_per_hour}/hour"


class SlidingWindowLimiter:
    """In-memory per-key sliding-window limiter for WS messages.

    Single-process only (fine for one Fly instance). `now` is injectable for tests.
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, limit: int, window_s: float, now: float | None = None) -> bool:
        t = now if now is not None else time.monotonic()
        dq = self._hits[key]
        cutoff = t - window_s
        while dq and dq[0] <= cutoff:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(t)
        return True

    def reset(self) -> None:
        self._hits.clear()


ws_chat_limiter = SlidingWindowLimiter()
ws_run_limiter = SlidingWindowLimiter()
