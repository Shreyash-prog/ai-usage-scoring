"""Event catalog and payload models (main spec §5).

Every event shares a common persisted envelope (`PersistedEvent`). Payloads are
typed per event but stored as JSON blobs; `payload_version` lets schemas evolve
without rewriting historical rows (§5.2).
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel


class EventType(StrEnum):
    SESSION_STARTED = "session.started"
    SESSION_ENDED = "session.ended"
    TASK_PRESENTED = "task.presented"
    TASK_SUBMITTED = "task.submitted"
    CHAT_PROMPT_SENT = "chat.prompt_sent"
    CHAT_RESPONSE_RECEIVED = "chat.response_received"
    CHAT_ERROR = "chat.error"
    EDITOR_SNAPSHOT = "editor.snapshot"
    EDITOR_PASTE = "editor.paste"
    CODE_EXECUTED = "code.executed"


# --- §5.1 payload schemas -------------------------------------------------


class SessionStartedPayload(BaseModel):
    candidate_name: str
    task_sequence: list[str]


class TaskPresentedPayload(BaseModel):
    task_id: str
    task_idx: int


class TaskSubmittedPayload(BaseModel):
    task_id: str
    final_code: str
    duration_ms: int


class ChatPromptSentPayload(BaseModel):
    text: str
    attached_code: str | None = None
    attached_output: str | None = None


class ChatResponseReceivedPayload(BaseModel):
    text: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    model: str


class ChatErrorPayload(BaseModel):
    error: str
    after_prompt_seq: int


class EditorSnapshotPayload(BaseModel):
    code: str
    trigger: Literal["idle", "large_diff", "pre_chat", "pre_run", "manual"]
    char_count: int
    line_count: int


class EditorPastePayload(BaseModel):
    text: str
    source_hint: Literal["chat", "external", "unknown"]
    char_count: int


class CodeExecutedPayload(BaseModel):
    code: str
    stdin: str | None = None
    stdout: str
    stderr: str
    exit_code: int
    runtime_ms: int
    truncated: bool


# --- persisted envelope (§6.1) -------------------------------------------


class PersistedEvent(BaseModel):
    id: int
    session_id: str
    seq: int
    ts: int
    type: EventType
    payload: dict
    task_id: str | None = None


# --- §5.2 versioning ------------------------------------------------------

CURRENT_PAYLOAD_VERSION = 1


def migrate_payload(version: int, payload: dict) -> dict:
    """Upgrade a historical payload to the current schema version.

    No migrations exist in v0; this is the seam §5.2 mandates so future field
    additions never mutate historical rows. New migrations append a branch here.
    """
    return payload
