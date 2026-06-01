"""Post-hoc scoring (main spec §10.1): final judge + heuristic profile per session.

Triggered on session.ended. For each task: recompute the heuristic component
(deterministic, matches live), run the §10.2 judge questions, aggregate (§10.4),
and persist phase='final' rows with evidence (§10.5). Then a count-weighted
session-level aggregate, mark the session 'scored', push the final profile, and
tear down the per-session bus channel.
"""

import asyncio
import logging
import re

from app.bus import EventBus
from app.config import settings
from app.llm.judge_client import AnthropicJudgeClient
from app.models.events import EventType, PersistedEvent
from app.scoring.aggregation import aggregate
from app.scoring.heuristics import (
    PromptContext,
    contains_code_like,
    first_event_after,
    iteration_heuristic,
    prompt_quality_heuristic,
    verification_heuristic,
)
from app.scoring.judges import JudgeResult, judge_question
from app.storage import sessions as session_store
from app.storage.db import Database
from app.storage.events import EventLogger
from app.storage.scores import ScoreStore
from app.ws.manager import WSManager

logger = logging.getLogger(__name__)

_DIMENSIONS = ["prompt_quality", "verification", "iteration"]
_RECENT_ERROR_WINDOW_MS = 60_000
_EXCERPT = 1500
_JUDGE_CONCURRENCY = 4
_CODE_BLOCK = re.compile(r"```(?:python)?\n(.*?)```", re.DOTALL)


def _text(e: PersistedEvent, key: str = "text") -> str:
    return e.payload.get(key) or ""


def _extract_code(response_text: str) -> str:
    m = _CODE_BLOCK.search(response_text)
    return m.group(1).strip() if m else response_text[:_EXCERPT]


def _summarize(events: list[PersistedEvent]) -> str:
    parts = []
    for e in events:
        if e.type == EventType.CODE_EXECUTED:
            parts.append(f"code.executed exit={e.payload.get('exit_code')}")
        else:
            parts.append(e.type.value)
    return "; ".join(parts) if parts else "(no events)"


class PostHocScorer:
    def __init__(
        self,
        db: Database,
        event_logger: EventLogger,
        judge: AnthropicJudgeClient,
        scores: ScoreStore,
        tasks,
        ws_manager: WSManager,
        bus: EventBus,
    ) -> None:
        self._db = db
        self._events = event_logger
        self._judge = judge
        self._scores = scores
        self._tasks = tasks
        self._ws = ws_manager
        self._bus = bus

    async def score_session(self, session_id: str) -> None:
        try:
            await self._score_session(session_id)
        except Exception:
            logger.exception("Post-hoc scoring failed for session %s", session_id)
        finally:
            # Day 6 teardown: drain + stop the per-session bus channel.
            await self._bus.close_session(session_id)

    async def _score_session(self, session_id: str) -> None:
        events = await self._events.get_session_events(session_id)
        by_task: dict[str, list[PersistedEvent]] = {}
        for e in events:
            if e.task_id is not None:
                by_task.setdefault(e.task_id, []).append(e)

        task_finals: dict[str, dict[str, float]] = {}
        for task_id, task_events in by_task.items():
            task_finals[task_id] = await self._score_task(session_id, task_id, task_events)

        await self._write_session_aggregate(session_id, by_task, task_finals)
        await session_store.mark_scored(self._db, session_id)
        await self._ws.broadcast_dashboard(
            session_id, {"type": "profile.final", "session_id": session_id}
        )

    async def _score_task(
        self, session_id: str, task_id: str, task_events: list[PersistedEvent]
    ) -> dict[str, float]:
        task = self._tasks.get(task_id)
        heuristics = {
            "prompt_quality": self._prompt_quality_mean(task_events),
            "verification": verification_heuristic(task_events),
            "iteration": iteration_heuristic(task_events, task) if task else 70.0,
        }
        judge_by_dim = await self._run_judges(task_events)

        finals: dict[str, float] = {}
        for dim in _DIMENSIONS:
            results = judge_by_dim.get(dim, [])
            final, confidence = aggregate(heuristics[dim], results, task_events)
            finals[dim] = final
            evidence = {
                "heuristic": {
                    "score": round(heuristics[dim], 2),
                    "event_seqs": [e.seq for e in task_events],
                },
                "judges": [
                    {
                        "question_id": j.question_id,
                        "answer": j.answer,
                        "evidence": j.evidence,
                        "target_seq": j.target_seq,
                    }
                    for j in results
                ],
                "final_score": round(final, 2),
                "confidence": round(confidence, 2),
            }
            await self._scores.upsert(
                session_id, task_id, dim, "final", final, confidence, evidence
            )
            await self._push_score(session_id, task_id, dim, final, confidence, evidence)
        return finals

    def _prompt_quality_mean(self, task_events: list[PersistedEvent]) -> float:
        prompts = [e for e in task_events if e.type == EventType.CHAT_PROMPT_SENT]
        if not prompts:
            return 50.0
        scores: list[float] = []
        prev_text: str | None = None
        recent_error: str | None = None
        recent_error_ts = 0
        for e in task_events:
            if e.type == EventType.CODE_EXECUTED and (
                e.payload.get("exit_code") != 0 or e.payload.get("stderr")
            ):
                recent_error = e.payload.get("stderr") or "execution error"
                recent_error_ts = e.ts
            elif e.type == EventType.CHAT_PROMPT_SENT:
                within = (e.ts - recent_error_ts) <= _RECENT_ERROR_WINDOW_MS
                ctx = PromptContext(
                    recent_error=recent_error if (recent_error and within) else None,
                    prev_prompt_text=prev_text,
                )
                scores.append(
                    prompt_quality_heuristic(
                        _text(e),
                        e.payload.get("attached_code"),
                        e.payload.get("attached_output"),
                        ctx,
                    )
                )
                prev_text = _text(e)
        return sum(scores) / len(scores)

    async def _run_judges(self, task_events: list[PersistedEvent]) -> dict[str, list[JudgeResult]]:
        prompts = [e for e in task_events if e.type == EventType.CHAT_PROMPT_SENT]
        responses = [e for e in task_events if e.type == EventType.CHAT_RESPONSE_RECEIVED]
        execs = [e for e in task_events if e.type == EventType.CODE_EXECUTED]
        sem = asyncio.Semaphore(_JUDGE_CONCURRENCY)
        jobs: list = []

        def schedule(qid: str, seq: int, **fields: str) -> None:
            if self._judge.call_count + len(jobs) >= settings.session_max_judge_calls:
                return  # cost cap (§P.6.1)
            jobs.append((qid, seq, fields))

        for p in prompts:
            schedule("PQ1", p.seq, prompt_text=_text(p))
            schedule("PQ4", p.seq, prompt_text=_text(p))
            schedule(
                "PQ3",
                p.seq,
                prompt_text=_text(p),
                prompt_attached_code=p.payload.get("attached_code") or "",
            )
            err = self._recent_error_before(task_events, p.seq)
            if err:
                schedule(
                    "PQ2",
                    p.seq,
                    recent_error=err,
                    prompt_text=_text(p),
                    prompt_attached=(
                        p.payload.get("attached_output") or p.payload.get("attached_code") or ""
                    ),
                )

        for r in responses:
            if not ("```" in _text(r) or contains_code_like(_text(r))):
                continue
            boundary = first_event_after(
                task_events, r.seq, types=[EventType.CHAT_PROMPT_SENT, EventType.TASK_SUBMITTED]
            )
            bseq = boundary.seq if boundary else 1_000_000_000
            between = [e for e in task_events if r.seq < e.seq < bseq]
            prev_prompt = [p for p in prompts if p.seq < r.seq]
            schedule(
                "VB1",
                r.seq,
                prompt_text=_text(prev_prompt[-1]) if prev_prompt else "",
                response_excerpt=_text(r)[:_EXCERPT],
                between_events_summary=_summarize(between),
            )
            exec_after = next((e for e in execs if r.seq < e.seq < bseq), None)
            if exec_after:
                schedule(
                    "VB3",
                    r.seq,
                    ai_code=_extract_code(_text(r)),
                    executed_code=exec_after.payload.get("code") or "",
                )

        for ex in execs:
            nxt = first_event_after(task_events, ex.seq, types=[EventType.CHAT_PROMPT_SENT])
            if nxt:
                out = (ex.payload.get("stdout", "") + ex.payload.get("stderr", ""))[:_EXCERPT]
                schedule("VB2", ex.seq, stdout_stderr=out or "(no output)", next_prompt=_text(nxt))

        for i in range(1, len(prompts)):
            schedule(
                "IE1", prompts[i].seq, prompt_1=_text(prompts[i - 1]), prompt_2=_text(prompts[i])
            )

        for r in responses:
            nxt = first_event_after(task_events, r.seq, types=[EventType.CHAT_PROMPT_SENT])
            if nxt and (nxt.ts - r.ts) <= _RECENT_ERROR_WINDOW_MS:
                schedule("IE2", r.seq, response_excerpt=_text(r)[:_EXCERPT], next_prompt=_text(nxt))

        async def run(qid: str, seq: int, fields: dict) -> JudgeResult:
            async with sem:
                return await judge_question(self._judge, qid, seq, **fields)

        results = await asyncio.gather(*[run(q, s, f) for q, s, f in jobs])
        from app.scoring.judges import QUESTION_DIMENSION

        by_dim: dict[str, list[JudgeResult]] = {d: [] for d in _DIMENSIONS}
        for res in results:
            by_dim[QUESTION_DIMENSION[res.question_id]].append(res)
        return by_dim

    @staticmethod
    def _recent_error_before(task_events: list[PersistedEvent], seq: int) -> str | None:
        """Most recent execution error within the 5 events preceding `seq` (§10.2 PQ2)."""
        prior = [e for e in task_events if e.seq < seq][-5:]
        for e in reversed(prior):
            if e.type == EventType.CODE_EXECUTED and (
                e.payload.get("exit_code") != 0 or e.payload.get("stderr")
            ):
                return e.payload.get("stderr") or "execution error"
        return None

    async def _write_session_aggregate(
        self,
        session_id: str,
        by_task: dict[str, list[PersistedEvent]],
        task_finals: dict[str, dict[str, float]],
    ) -> None:
        """Count-weighted mean across tasks, weight = number of events (§10.1 step 3)."""
        # Carry event seqs so the dashboard's headline (session) bars still get
        # clickable evidence citations, not just the per-task rows.
        all_seqs = sorted(e.seq for evs in by_task.values() for e in evs)
        for dim in _DIMENSIONS:
            total_w = 0
            acc = 0.0
            for task_id, finals in task_finals.items():
                w = len(by_task[task_id])
                acc += finals[dim] * w
                total_w += w
            if total_w == 0:
                continue
            score = acc / total_w
            evidence = {
                "session_aggregate": True,
                "tasks": list(task_finals),
                "heuristic": {"event_seqs": all_seqs},
            }
            await self._scores.upsert(session_id, None, dim, "final", score, 0.5, evidence)
            await self._push_score(session_id, None, dim, score, 0.5, evidence)

    async def _push_score(
        self,
        session_id: str,
        task_id: str | None,
        dim: str,
        score: float,
        confidence: float,
        evidence: dict,
    ) -> None:
        seqs = evidence.get("heuristic", {}).get("event_seqs", [])
        await self._ws.broadcast_dashboard(
            session_id,
            {
                "type": "score.update",
                "task_id": task_id,
                "dimension": dim,
                "phase": "final",
                "score": score,
                "confidence": confidence,
                "evidence_snippets": seqs[-5:],
            },
        )
