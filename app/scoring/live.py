"""LiveScorer — recomputes heuristic scores as events arrive (main spec §9).

Subscribed to the EventBus per session. Stateful per (session, task): it accumulates
that task's events and recomputes the three dimensions on the events that can move
them, writing phase='live' rows and pushing score.update to dashboard subscribers.

Live scores are heuristic-only, so confidence follows the judge-less branch of §10.4:
confidence = 0.5 * heuristic_coverage. Updates are debounced to one write per
dimension per 500ms per task (§9.3), forced through on task submission.
"""

import logging
import time
from dataclasses import dataclass, field

from app.models.events import EventType, PersistedEvent
from app.scoring.heuristics import (
    PromptContext,
    heuristic_coverage,
    iteration_heuristic,
    prompt_quality_heuristic,
    verification_heuristic,
)
from app.storage.scores import ScoreStore
from app.storage.tasks import TaskStore
from app.ws.manager import WSManager

logger = logging.getLogger(__name__)

_RECENT_ERROR_WINDOW_MS = 60_000
_DEBOUNCE_MS = 500

_PROMPT_QUALITY = "prompt_quality"
_VERIFICATION = "verification"
_ITERATION = "iteration"

# Events that can change verification/iteration scores.
_RECOMPUTE_TRIGGERS = {
    EventType.CHAT_RESPONSE_RECEIVED,
    EventType.CODE_EXECUTED,
    EventType.EDITOR_PASTE,
    EventType.TASK_SUBMITTED,
}


@dataclass
class _TaskState:
    events: list[PersistedEvent] = field(default_factory=list)
    prompt_scores: list[float] = field(default_factory=list)
    prev_prompt_text: str | None = None
    recent_error: str | None = None
    recent_error_ts: int = 0
    last_write_ms: dict[str, float] = field(default_factory=dict)


class LiveScorer:
    def __init__(self, scores: ScoreStore, ws_manager: WSManager, tasks: TaskStore) -> None:
        self._scores = scores
        self._ws = ws_manager
        self._tasks = tasks
        self._state: dict[tuple[str, str], _TaskState] = {}

    async def handle_event(self, event: PersistedEvent) -> None:
        task_id = event.task_id
        if task_id is None:
            return  # pre-task events (session.started/ended) don't carry scores
        st = self._state.setdefault((event.session_id, task_id), _TaskState())
        st.events.append(event)

        if event.type == EventType.CODE_EXECUTED:
            if event.payload.get("exit_code") != 0 or event.payload.get("stderr"):
                st.recent_error = event.payload.get("stderr") or "execution error"
                st.recent_error_ts = event.ts

        if event.type == EventType.CHAT_PROMPT_SENT:
            await self._score_prompt(event, st, task_id)

        if event.type in _RECOMPUTE_TRIGGERS:
            force = event.type == EventType.TASK_SUBMITTED
            await self._score_verification_iteration(event, st, task_id, force)

    async def _score_prompt(self, event: PersistedEvent, st: _TaskState, task_id: str) -> None:
        payload = event.payload
        within_window = (event.ts - st.recent_error_ts) <= _RECENT_ERROR_WINDOW_MS
        task = self._tasks.get(task_id)
        ctx = PromptContext(
            recent_error=st.recent_error if (st.recent_error and within_window) else None,
            prev_prompt_text=st.prev_prompt_text,
            task_type=task.type if task else None,
        )
        score = prompt_quality_heuristic(
            payload.get("text", ""),
            payload.get("attached_code"),
            payload.get("attached_output"),
            ctx,
        )
        st.prompt_scores.append(score)
        st.prev_prompt_text = payload.get("text", "")
        mean = sum(st.prompt_scores) / len(st.prompt_scores)
        await self._write(event.session_id, task_id, _PROMPT_QUALITY, mean, st, force=False)

    async def _score_verification_iteration(
        self, event: PersistedEvent, st: _TaskState, task_id: str, force: bool
    ) -> None:
        ver = verification_heuristic(st.events)
        await self._write(event.session_id, task_id, _VERIFICATION, ver, st, force)

        task = self._tasks.get(task_id)
        if task is not None:
            it = iteration_heuristic(st.events, task)
            await self._write(event.session_id, task_id, _ITERATION, it, st, force)

    async def _write(
        self,
        session_id: str,
        task_id: str,
        dimension: str,
        score: float,
        st: _TaskState,
        force: bool,
    ) -> None:
        now = time.monotonic() * 1000
        last = st.last_write_ms.get(dimension, 0.0)
        if not force and (now - last) < _DEBOUNCE_MS:
            return
        st.last_write_ms[dimension] = now

        confidence = 0.5 * heuristic_coverage(st.events)
        event_seqs = [e.seq for e in st.events]
        evidence = {"heuristic": {"score": round(score, 2), "event_seqs": event_seqs}}
        await self._scores.upsert(
            session_id, task_id, dimension, "live", score, confidence, evidence
        )
        await self._ws.broadcast_dashboard(
            session_id,
            {
                "type": "score.update",
                "task_id": task_id,
                "dimension": dimension,
                "phase": "live",
                "score": score,
                "confidence": confidence,
                "evidence_snippets": event_seqs[-5:],
            },
        )
