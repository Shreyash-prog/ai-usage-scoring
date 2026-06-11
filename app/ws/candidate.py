"""Candidate WebSocket handler (main spec §12.2).

Owns one candidate connection: presents tasks, runs the chat AI (streaming),
executes code in the sandbox, and persists every interaction as an event. Events
are written via EventLogger (which assigns `seq`); the EventBus + LiveScorer wiring
arrives on Day 4, so for now we persist and reply directly to the candidate.

Multi-turn chat memory lives here, not in the LLM client (§7.2).
"""

import asyncio
import logging
import time
from dataclasses import dataclass

from fastapi import WebSocket, WebSocketDisconnect

from app.bus import EventBus
from app.config import settings
from app.llm.chat_client import ChatMessage, OpenAIChatClient
from app.models.events import EventType, PersistedEvent
from app.ratelimit import client_ip_ws, ws_chat_limiter, ws_run_limiter
from app.sandbox.runner import Sandbox
from app.scoring.posthoc import PostHocScorer
from app.storage import llm_calls
from app.storage import sessions as session_store
from app.storage.db import Database
from app.storage.events import EventLogger
from app.storage.tasks import TaskStore
from app.ws.manager import WSManager

logger = logging.getLogger(__name__)


@dataclass
class CandidateDeps:
    db: Database
    logger: EventLogger
    sandbox: Sandbox
    chat: OpenAIChatClient
    tasks: TaskStore
    ws_manager: WSManager
    bus: EventBus
    posthoc: PostHocScorer
    system_prompt: str


class CandidateSession:
    def __init__(self, ws: WebSocket, session_id: str, deps: CandidateDeps) -> None:
        self._ws = ws
        self._sid = session_id
        self._d = deps
        self._conversation: list[ChatMessage] = [
            ChatMessage(role="system", content=deps.system_prompt)
        ]
        self._task_id: str | None = None
        self._task_started_ms: int = 0
        self._last_snapshot_code: str | None = None
        self._last_output: str | None = None
        self._ip = client_ip_ws(ws)  # for per-IP WS rate limiting (Phase 2)

    async def run(self) -> None:
        """Receive-loop until the socket closes or the session ends."""
        try:
            while True:
                msg = await self._ws.receive_json()
                kind = msg.get("type")
                if kind == "hello":
                    await self._present_current_task()
                elif kind == "ping":
                    await self._safe_send({"type": "pong"})  # WS keepalive (candidate.js)
                elif kind == "chat.send":
                    if ws_chat_limiter.allow(self._ip, settings.ratelimit_chat_per_minute, 60):
                        await self._on_chat(msg)
                    else:
                        await self._rate_limited("chat")
                elif kind == "editor.snapshot":
                    await self._on_snapshot(msg)
                elif kind == "editor.paste":
                    await self._on_paste(msg)
                elif kind == "code.run":
                    if ws_run_limiter.allow(self._ip, settings.ratelimit_runs_per_minute, 60):
                        await self._on_run(msg)
                    else:
                        await self._rate_limited("run")
                elif kind == "task.submit":
                    await self._on_submit(msg)
                elif kind == "session.end":
                    await self._end_session()
                    break
                else:
                    logger.warning("Unknown candidate message type: %r", kind)
        except WebSocketDisconnect:
            logger.info("Candidate disconnected from session %s", self._sid)

    # --- task lifecycle ---------------------------------------------------

    async def _present_current_task(self) -> None:
        session = await session_store.get_session(self._d.db, self._sid)
        if session is None:
            await self._ws.send_json({"type": "session.done"})
            return
        idx = session.current_task_idx
        if idx >= len(session.task_sequence):
            await self._ws.send_json({"type": "session.done"})
            return

        task_id = session.task_sequence[idx]
        task = self._d.tasks.get(task_id)
        if task is None:
            await self._ws.send_json({"type": "chat.error", "error": f"Task {task_id} not found"})
            return

        # Reset per-task state.
        self._task_id = task_id
        self._task_started_ms = int(time.time() * 1000)
        self._conversation = [ChatMessage(role="system", content=self._d.system_prompt)]
        self._last_snapshot_code = None
        self._last_output = None

        await self._emit(
            EventType.TASK_PRESENTED,
            {"task_id": task_id, "task_idx": idx},
            task_id=task_id,
        )
        await self._ws.send_json(
            {
                "type": "task.presented",
                "task": task.model_dump(),
                "task_idx": idx,
                "total_tasks": len(session.task_sequence),
            }
        )

    async def _on_submit(self, msg: dict) -> None:
        final_code = msg.get("final_code", "")
        duration_ms = int(time.time() * 1000) - self._task_started_ms
        await self._emit(
            EventType.TASK_SUBMITTED,
            {"task_id": self._task_id, "final_code": final_code, "duration_ms": duration_ms},
            task_id=self._task_id,
        )
        session = await session_store.get_session(self._d.db, self._sid)
        if session is None:
            return
        next_idx = session.current_task_idx + 1
        await session_store.set_current_task_idx(self._d.db, self._sid, next_idx)
        if next_idx < len(session.task_sequence):
            await self._present_current_task()
        else:
            await self._end_session()

    async def _end_session(self) -> None:
        await self._emit(EventType.SESSION_ENDED, {})
        await session_store.end_session(self._d.db, self._sid)
        # Score SYNCHRONOUSLY before returning (Option A). A fire-and-forget task is
        # orphaned when a scale-to-zero/restartable host suspends after the socket
        # closes; awaiting it couples completion to this still-open request. The
        # candidate waits ~10-60s ("Scoring…"). The 120s wait_for bounds a hung
        # post-hoc (e.g. Anthropic rate limits) so it can't hold the WS open until
        # Fly kills the request mid-scoring. If the socket drops, scoring still
        # completes (it's awaited) — only the final sends are best-effort.
        await self._safe_send({"type": "scoring"})
        try:
            await asyncio.wait_for(
                self._d.posthoc.score_session(self._sid),
                timeout=120,
            )
        except TimeoutError:
            logger.warning("Post-hoc scoring exceeded 120s for session %s", self._sid)
            await self._safe_send({"type": "scoring.timeout"})
        await self._safe_send({"type": "session.done"})

    async def _safe_send(self, payload: dict) -> None:
        try:
            await self._ws.send_json(payload)
        except Exception:
            pass  # WS may have dropped during long post-hoc; scores are already persisted

    # --- editor events ----------------------------------------------------

    async def _on_snapshot(self, msg: dict) -> None:
        code = msg.get("code", "")
        self._last_snapshot_code = code
        event = await self._emit(
            EventType.EDITOR_SNAPSHOT,
            {
                "code": code,
                "trigger": msg.get("trigger", "manual"),
                "char_count": len(code),
                "line_count": code.count("\n") + 1 if code else 0,
            },
            task_id=self._task_id,
        )
        await self._ack(event.seq, "editor.snapshot")

    async def _on_paste(self, msg: dict) -> None:
        text = msg.get("text", "")
        # source_hint is computed client-side (§15.5); we record what it reports.
        source_hint = msg.get("source_hint", "unknown")
        if source_hint not in ("chat", "external", "unknown"):
            source_hint = "unknown"
        event = await self._emit(
            EventType.EDITOR_PASTE,
            {"text": text, "source_hint": source_hint, "char_count": len(text)},
            task_id=self._task_id,
        )
        await self._ack(event.seq, "editor.paste")

    # --- code execution ---------------------------------------------------

    async def _on_run(self, msg: dict) -> None:
        # Per-session execution cap (§P.6.1 public addition): count durable
        # CODE_EXECUTED events so a reconnect can't reset the budget.
        executed = await self._d.logger.count_session_events(self._sid, EventType.CODE_EXECUTED)
        if executed >= settings.session_max_code_executions:
            logger.warning("Session %s hit code-execution cap", self._sid)
            await self._ws.send_json(
                {
                    "type": "exec.result",
                    "stdout": "",
                    "stderr": "session execution limit reached",
                    "exit_code": -1,
                    "runtime_ms": 0,
                }
            )
            return

        code = msg.get("code", "")
        stdin = msg.get("stdin")
        result = await self._d.sandbox.run_python(code, stdin=stdin)
        self._last_output = (result.stdout + result.stderr).strip() or None
        await self._emit(
            EventType.CODE_EXECUTED,
            {
                "code": code,
                "stdin": stdin,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.exit_code,
                "runtime_ms": result.runtime_ms,
                "truncated": result.truncated,
            },
            task_id=self._task_id,
        )
        await self._ws.send_json(
            {
                "type": "exec.result",
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.exit_code,
                "runtime_ms": result.runtime_ms,
            }
        )

    # --- chat -------------------------------------------------------------

    async def _on_chat(self, msg: dict) -> None:
        # Per-session chat-token cap (§P.6.1): refuse before spending another call.
        tokens_in = await llm_calls.session_chat_tokens_in(self._d.db, self._sid)
        if tokens_in >= settings.session_max_chat_tokens_in:
            logger.warning("Session %s hit chat-token cap (%d)", self._sid, tokens_in)
            await self._ws.send_json(
                {
                    "type": "chat.capped",
                    "message": "This session has reached its AI usage limit.",
                }
            )
            return

        text = msg.get("text", "")
        attached_code = self._last_snapshot_code if msg.get("attach_editor") else None
        attached_output = self._last_output if msg.get("attach_output") else None

        prompt_event = await self._emit(
            EventType.CHAT_PROMPT_SENT,
            {"text": text, "attached_code": attached_code, "attached_output": attached_output},
            task_id=self._task_id,
        )

        user_content = self._compose_user_message(text, attached_code, attached_output)
        self._conversation.append(ChatMessage(role="user", content=user_content))
        self._trim_history()

        started = time.monotonic()
        full_text = ""
        prompt_tokens = completion_tokens = 0
        model = ""
        try:
            async for chunk in self._d.chat.chat_stream(self._conversation):
                if chunk.done:
                    prompt_tokens = chunk.prompt_tokens
                    completion_tokens = chunk.completion_tokens
                    model = chunk.model
                elif chunk.text:
                    full_text += chunk.text
                    await self._ws.send_json({"type": "chat.token", "text": chunk.text})
        except Exception as exc:
            logger.exception("Chat stream failed for session %s", self._sid)
            await self._emit(
                EventType.CHAT_ERROR,
                {"error": str(exc), "after_prompt_seq": prompt_event.seq},
                task_id=self._task_id,
            )
            await llm_calls.record_llm_call(
                self._d.db,
                self._sid,
                "openai",
                model or settings.openai_chat_model,
                "chat",
                prompt_tokens,
                completion_tokens,
                int((time.monotonic() - started) * 1000),
                "error",
            )
            await self._ws.send_json({"type": "chat.error", "error": "AI unavailable — try again"})
            # Drop the unanswered user turn so history stays consistent.
            self._conversation.pop()
            return

        latency_ms = int((time.monotonic() - started) * 1000)
        await llm_calls.record_llm_call(
            self._d.db,
            self._sid,
            "openai",
            model or settings.openai_chat_model,
            "chat",
            prompt_tokens,
            completion_tokens,
            latency_ms,
            "ok",
        )
        self._conversation.append(ChatMessage(role="assistant", content=full_text))
        await self._emit(
            EventType.CHAT_RESPONSE_RECEIVED,
            {
                "text": full_text,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "latency_ms": latency_ms,
                "model": model,
            },
            task_id=self._task_id,
        )
        await self._ws.send_json(
            {
                "type": "chat.done",
                "full_text": full_text,
                "tokens": {"prompt": prompt_tokens, "completion": completion_tokens},
            }
        )

    @staticmethod
    def _compose_user_message(
        text: str, attached_code: str | None, attached_output: str | None
    ) -> str:
        parts = [text]
        if attached_code:
            parts.append(f"\n\nMy current code:\n```python\n{attached_code}\n```")
        if attached_output:
            parts.append(f"\n\nLast execution output:\n```\n{attached_output}\n```")
        return "".join(parts)

    def _trim_history(self) -> None:
        limit = settings.openai_chat_max_history_messages
        if len(self._conversation) <= limit + 1:  # +1 for the system message
            return
        system, rest = self._conversation[0], self._conversation[1:]
        self._conversation = [system, *rest[-limit:]]

    async def _emit(
        self, event_type: EventType, payload: dict, task_id: str | None = None
    ) -> PersistedEvent:
        """Persist an event (assigns seq) then publish it to the bus (§8.2)."""
        event = await self._d.logger.write(self._sid, event_type, payload, task_id=task_id)
        await self._d.bus.publish(event)
        return event

    async def _ack(self, seq: int, event_type: str) -> None:
        await self._ws.send_json({"type": "ack", "seq": seq, "event_type": event_type})

    async def _rate_limited(self, scope: str) -> None:
        logger.warning("Rate-limited %s on session %s from ip=%s", scope, self._sid, self._ip)
        await self._ws.send_json(
            {
                "type": "rate_limited",
                "scope": scope,
                "message": "You're doing that too fast — please slow down.",
            }
        )
