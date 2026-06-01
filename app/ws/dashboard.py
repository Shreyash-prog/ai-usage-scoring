"""Dashboard WebSocket handler (main spec §12.3).

On `hello` it replays ALL events with seq > last_seq (not just the latest) and a
snapshot of current live scores, so a freshly-opened or reconnecting dashboard sees
the full picture. After that it stays open; live `event` and `score.update` messages
are pushed to it by the EventBus dashboard-forwarder and the LiveScorer via WSManager.
"""

import logging
from dataclasses import dataclass

from fastapi import WebSocket, WebSocketDisconnect

from app.storage.events import EventLogger
from app.storage.scores import ScoreStore
from app.ws.manager import WSManager

logger = logging.getLogger(__name__)


@dataclass
class DashboardDeps:
    logger: EventLogger
    scores: ScoreStore
    ws_manager: WSManager


def score_row_to_update(row: dict) -> dict:
    """Render a persisted score row as a score.update WS message (§12.3)."""
    evidence = row.get("evidence") or {}
    seqs = (evidence.get("heuristic") or {}).get("event_seqs", [])
    return {
        "type": "score.update",
        "task_id": row.get("task_id"),
        "dimension": row["dimension"],
        "phase": row["phase"],
        "score": row["score"],
        "confidence": row["confidence"],
        "evidence_snippets": seqs[-5:],
    }


class DashboardSession:
    def __init__(self, ws: WebSocket, session_id: str, deps: DashboardDeps) -> None:
        self._ws = ws
        self._sid = session_id
        self._d = deps

    async def run(self) -> None:
        try:
            while True:
                msg = await self._ws.receive_json()
                kind = msg.get("type")
                if kind == "hello":
                    await self._replay(int(msg.get("last_seq", 0)))
                elif kind == "subscribe":
                    # v0 always sends both events and scores; just acknowledge.
                    await self._ws.send_json({"type": "subscribed"})
                else:
                    logger.warning("Unknown dashboard message type: %r", kind)
        except WebSocketDisconnect:
            logger.info("Dashboard disconnected from session %s", self._sid)

    async def _replay(self, last_seq: int) -> None:
        events = await self._d.logger.get_session_events(self._sid, from_seq=last_seq)
        for e in events:
            await self._ws.send_json({"type": "event", "event": e.model_dump(mode="json")})
        for phase in ("live", "final"):
            for row in await self._d.scores.list_scores(self._sid, phase):
                await self._ws.send_json(score_row_to_update(row))
