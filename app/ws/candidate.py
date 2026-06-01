"""Candidate WebSocket handler (main spec §12.2).

Owns one candidate connection: presents tasks, runs the chat AI (streaming),
executes code in the sandbox, and persists every interaction as an event. Events
are written via EventLogger (which assigns `seq`); the EventBus + LiveScorer wiring
arrives on Day 4, so for now we persist and reply directly to the candidate.

Multi-turn chat memory lives here, not in the LLM client (§7.2).
"""

import logging
import time
from dataclasses import dataclass

from fastapi import WebSocket, WebSocketDisconnect

from app.llm.chat_client import ChatMessage, OpenAIChatClient
from app.models.events import EventType
from app.sandbox.runner import Sandbox
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

    async def run(self) -> None:
        """Receive-loop until the socket closes or the session ends."""
        try:
            while True:
                msg = await self._ws.receive_json()
                kind = msg.get("type")
                if kind == "hello":
                    await self._present_current_task()
                elif kind == "chat.send":
                    await self._on_chat(msg)
                elif kind == "editor.snapshot":
                    await self._on_snapshot(msg)
                elif kind == "editor.paste":
                    await self._on_paste(msg)
                elif kind == "code.run":
                    await self._on_run(msg)
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

        await self._d.logger.write(
            self._sid,
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
        await self._d.logger.write(
            self._sid,
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
        await self._d.logger.write(self._sid, EventType.SESSION_ENDED, {})
        await session_store.end_session(self._d.db, self._sid)
        await self._ws.send_json({"type": "session.done"})

    # --- editor events ----------------------------------------------------

    async def _on_snapshot(self, msg: dict) -> None:
        code = msg.get("code", "")
        self._last_snapshot_code = code
        event = await self._d.logger.write(
            self._sid,
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
        event = await self._d.logger.write(
            self._sid,
            EventType.EDITOR_PASTE,
            {"text": text, "source_hint": source_hint, "char_count": len(text)},
            task_id=self._task_id,
        )
        await self._ack(event.seq, "editor.paste")

    # --- code execution ---------------------------------------------------

    async def _on_run(self, msg: dict) -> None:
        code = msg.get("code", "")
        stdin = msg.get("stdin")
        result = await self._d.sandbox.run_python(code, stdin=stdin)
        self._last_output = (result.stdout + result.stderr).strip() or None
        await self._d.logger.write(
            self._sid,
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
        text = msg.get("text", "")
        attached_code = self._last_snapshot_code if msg.get("attach_editor") else None
        attached_output = self._last_output if msg.get("attach_output") else None

        prompt_event = await self._d.logger.write(
            self._sid,
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
            await self._d.logger.write(
                self._sid,
                EventType.CHAT_ERROR,
                {"error": str(exc), "after_prompt_seq": prompt_event.seq},
                task_id=self._task_id,
            )
            await self._ws.send_json({"type": "chat.error", "error": "AI unavailable — try again"})
            # Drop the unanswered user turn so history stays consistent.
            self._conversation.pop()
            return

        latency_ms = int((time.monotonic() - started) * 1000)
        self._conversation.append(ChatMessage(role="assistant", content=full_text))
        await self._d.logger.write(
            self._sid,
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
        from app.config import settings

        limit = settings.openai_chat_max_history_messages
        if len(self._conversation) <= limit + 1:  # +1 for the system message
            return
        system, rest = self._conversation[0], self._conversation[1:]
        self._conversation = [system, *rest[-limit:]]

    async def _ack(self, seq: int, event_type: str) -> None:
        await self._ws.send_json({"type": "ack", "seq": seq, "event_type": event_type})
