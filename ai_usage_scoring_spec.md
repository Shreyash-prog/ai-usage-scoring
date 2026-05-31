# AI Usage Scoring Platform — Implementation Spec

> **Status:** Implementation-ready. Every interface, schema, formula, and prompt that the build depends on is in this doc.
> **Companion docs:** `ai_usage_scoring_design.md` (the why), `ai_usage_scoring_poc.md` (the what). This doc is the *how*.
> **Rule:** If a decision isn't in here and it's load-bearing, stop and add it before coding it.

---

## 0. How to Read This Doc

Top-down. Each section depends only on sections above it. If you start building, work in §17's order; if you're reviewing, read §1–§4 to align on shape, then jump to whichever subsystem you own.

Code in this doc is **specification-grade pseudocode**: types are real, names are real, behaviors are real. Treat it as the contract. Implementation is allowed to differ in the small (e.g., a helper function's signature) but not in the load-bearing (e.g., the score formula).

---

## 1. System Overview

One FastAPI process. SQLite for storage. Ollama on localhost for LLMs. Static HTML/JS frontend served by the same process. In-process asyncio queue for events. Background tasks for post-hoc scoring.

```
                   ┌─────────────────────┐
   Candidate ────► │  /candidate (HTML)  │
                   │  /ws/session/{id}   │
                   └──────────┬──────────┘
                              │
                              ▼
   ┌─────────────────────────────────────────────────────────┐
   │   FastAPI process                                       │
   │                                                         │
   │   Routes ─► WSManager ─► EventBus (asyncio.Queue/sess)  │
   │                              │                          │
   │                              ├─► EventLogger ─► SQLite  │
   │                              ├─► LiveScorer  ─► SQLite  │
   │                              └─► WS fan-out             │
   │                                                         │
   │   On session.ended:  PostHocScorer task spawned         │
   │                                                         │
   │   /api/exec ─► Sandbox ─► subprocess                    │
   │   /api/chat ─► OllamaClient ─► localhost:11434          │
   └─────────────────────────────────────────────────────────┘
                              │
                              ▼
                   ┌─────────────────────┐
   Interviewer ──► │  /dashboard (HTML)  │
                   │  /ws/dashboard/{id} │
                   └─────────────────────┘
```

---

## 2. Project Layout

```
project/
├── app/
│   ├── __init__.py
│   ├── main.py                       # FastAPI app, route registration
│   ├── config.py                     # env loading, typed settings
│   ├── models/
│   │   ├── __init__.py
│   │   ├── events.py                 # Pydantic event models (§5)
│   │   ├── scores.py                 # Score, Evidence, Profile
│   │   └── tasks.py                  # Task, TaskType
│   ├── storage/
│   │   ├── __init__.py
│   │   ├── schema.sql                # DDL (§4)
│   │   ├── db.py                     # connection pool, init
│   │   ├── events.py                 # EventLogger (§6)
│   │   ├── scores.py                 # ScoreStore
│   │   ├── sessions.py               # SessionStore
│   │   └── tasks.py                  # TaskStore
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── ollama_client.py          # OllamaClient (§7)
│   │   └── prompts/
│   │       ├── system_chat.txt
│   │       ├── judge_prompt_quality.txt
│   │       ├── judge_verification.txt
│   │       └── judge_iteration.txt
│   ├── scoring/
│   │   ├── __init__.py
│   │   ├── live.py                   # LiveScorer (§9)
│   │   ├── posthoc.py                # PostHocScorer (§10)
│   │   ├── heuristics.py             # all heuristic formulas (§9.2)
│   │   ├── judges.py                 # judge wrappers (§10.2)
│   │   └── aggregation.py            # combine heuristic+judge (§10.4)
│   ├── sandbox/
│   │   ├── __init__.py
│   │   └── runner.py                 # Python subprocess sandbox (§11)
│   ├── ws/
│   │   ├── __init__.py
│   │   ├── manager.py                # WSManager (§12)
│   │   ├── candidate.py              # candidate WS handler
│   │   └── dashboard.py              # dashboard WS handler
│   └── bus.py                        # EventBus (§8)
├── static/
│   ├── candidate.html
│   ├── candidate.js
│   ├── dashboard.html
│   └── dashboard.js
├── tasks/
│   ├── 001-substring-permutations.yaml
│   ├── 002-csv-debug.yaml
│   └── 003-rate-limiter.yaml
├── tests/
│   ├── test_heuristics.py
│   ├── test_judges.py
│   ├── test_sandbox.py
│   └── fixtures/
├── pyproject.toml
├── Makefile
└── README.md
```

---

## 3. Configuration

A single `Settings` class, loaded from env or `.env`.

```python
# app/config.py
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Server
    host: str = "127.0.0.1"
    port: int = 8000

    # Storage
    db_path: str = "./events.db"
    tasks_dir: str = "./tasks"

    # Ollama
    ollama_host: str = "http://127.0.0.1:11434"
    ollama_chat_model: str = "qwen2.5-coder:7b"
    ollama_judge_model: str = "qwen2.5:7b"
    ollama_request_timeout_s: int = 90

    # Sandbox
    exec_timeout_s: int = 10
    exec_mem_limit_mb: int = 256
    exec_output_limit_kb: int = 1024

    # Scoring
    live_score_debounce_ms: int = 500
    judge_temperature: float = 0.1
    judge_max_retries: int = 1

    # Session
    session_idle_timeout_min: int = 30

    class Config:
        env_file = ".env"

settings = Settings()
```

---

## 4. Database Schema

SQLite, WAL mode, file at `./events.db`. Init on first run from `schema.sql`.

```sql
-- app/storage/schema.sql

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,                       -- uuid4
  candidate_name TEXT NOT NULL,
  task_sequence TEXT NOT NULL,               -- JSON array of task_ids
  current_task_idx INTEGER NOT NULL DEFAULT 0,
  started_at INTEGER NOT NULL,               -- ms since epoch
  ended_at INTEGER,
  status TEXT NOT NULL CHECK (status IN ('active','ended','scored','abandoned')),
  schema_version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  ts INTEGER NOT NULL,                       -- ms since epoch, monotonic per session
  seq INTEGER NOT NULL,                      -- per-session monotonic counter
  type TEXT NOT NULL,                        -- see §5
  payload_version INTEGER NOT NULL DEFAULT 1,
  payload TEXT NOT NULL,                     -- JSON
  task_id TEXT,                              -- denormalized for filtering; NULL if pre-task
  FOREIGN KEY (session_id) REFERENCES sessions(id)
);
CREATE INDEX IF NOT EXISTS idx_events_session_seq ON events(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_session_type ON events(session_id, type);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_session_seq_unique ON events(session_id, seq);

CREATE TABLE IF NOT EXISTS scores (
  session_id TEXT NOT NULL,
  task_id TEXT,                              -- NULL = session-level aggregate
  dimension TEXT NOT NULL,                   -- 'prompt_quality' | 'verification' | 'iteration'
  phase TEXT NOT NULL CHECK (phase IN ('live','final')),
  score REAL NOT NULL CHECK (score >= 0 AND score <= 100),
  confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  evidence TEXT NOT NULL,                    -- JSON (see §10.5)
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (session_id, task_id, dimension, phase),
  FOREIGN KEY (session_id) REFERENCES sessions(id)
);
```

**Notes:**
- `seq` is the **scoring-ground-truth ordering**. `ts` is best-effort wall clock; `seq` is what we use for replay and judge windowing. Server assigns `seq` on persist.
- Payloads are JSON blobs; `payload_version` lets us evolve schemas without rewrites.
- Scores are per-task *and* per-session (NULL task_id = session aggregate). Both rows coexist.

---

## 5. Event Catalog

Every event is a Pydantic model. All events share a common envelope.

```python
# app/models/events.py
from pydantic import BaseModel, Field
from typing import Literal, Optional
from enum import Enum

class EventType(str, Enum):
    SESSION_STARTED       = "session.started"
    SESSION_ENDED         = "session.ended"
    TASK_PRESENTED        = "task.presented"
    TASK_SUBMITTED        = "task.submitted"
    CHAT_PROMPT_SENT      = "chat.prompt_sent"
    CHAT_RESPONSE_RECEIVED = "chat.response_received"
    CHAT_ERROR            = "chat.error"
    EDITOR_SNAPSHOT       = "editor.snapshot"
    EDITOR_PASTE          = "editor.paste"
    CODE_EXECUTED         = "code.executed"
```

### 5.1 Payload schemas

```python
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
    attached_code: Optional[str] = None     # contents of editor at send time, may be None
    attached_output: Optional[str] = None   # last stderr/stdout if user attached it

class ChatResponseReceivedPayload(BaseModel):
    text: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    model: str

class ChatErrorPayload(BaseModel):
    error: str
    after_prompt_seq: int     # the seq of the prompt that failed

class EditorSnapshotPayload(BaseModel):
    code: str
    trigger: Literal["idle","large_diff","pre_chat","pre_run","manual"]
    char_count: int
    line_count: int

class EditorPastePayload(BaseModel):
    text: str
    source_hint: Literal["chat","external","unknown"]
    # 'chat' iff the paste content matches a recent chat.response_received text (fuzzy)
    char_count: int

class CodeExecutedPayload(BaseModel):
    code: str
    stdin: Optional[str] = None
    stdout: str                 # truncated to settings.exec_output_limit_kb
    stderr: str                 # truncated
    exit_code: int              # 0=ok, 124=timeout, 137=oom-killed, other=script error
    runtime_ms: int
    truncated: bool
```

### 5.2 Versioning rule

When adding a field: bump `payload_version`, make the field optional in code, write a migration helper in `models/events.py::migrate_payload(version, payload) -> latest`. Never mutate historical rows.

### 5.3 What's NOT logged

- Keystrokes
- Mouse position / clicks (other than via inferred actions like `code.run`)
- Focus / tab visibility
- Network requests from the candidate browser

If we want these later they get added as new event types.

---

## 6. Storage Layer

### 6.1 EventLogger interface

```python
# app/storage/events.py
from typing import Protocol, AsyncIterator
from app.models.events import EventType

class EventLogger(Protocol):
    async def write(
        self,
        session_id: str,
        type: EventType,
        payload: dict,
        task_id: Optional[str] = None,
    ) -> "PersistedEvent": ...

    async def get_session_events(
        self,
        session_id: str,
        from_seq: int = 0,
        types: Optional[list[EventType]] = None,
    ) -> list["PersistedEvent"]: ...

    async def get_task_events(
        self,
        session_id: str,
        task_id: str,
    ) -> list["PersistedEvent"]: ...

class PersistedEvent(BaseModel):
    id: int
    session_id: str
    seq: int
    ts: int
    type: EventType
    payload: dict
    task_id: Optional[str]
```

### 6.2 Transactional contract

- `write` is the only mutator. It allocates `seq = max(seq) + 1` for that session atomically.
- All reads are snapshot-consistent within a single call (SQLite handles this).
- Retry policy: on `sqlite3.OperationalError` matching "database is locked", retry with exponential backoff `[10ms, 50ms, 200ms]`. After 3 retries, raise.
- We use `aiosqlite` with a single connection in a `asyncio.Lock` for writes; reads can be concurrent via a connection pool of size 4.

### 6.3 Initialization

On startup:
1. If `db_path` doesn't exist, create and run `schema.sql`.
2. If it exists, run any pending migrations (none in v0).
3. Mark any `status='active'` sessions older than `session_idle_timeout_min` as `abandoned`.

---

## 7. LLM Integration (Ollama)

### 7.1 OllamaClient interface

```python
# app/llm/ollama_client.py
from typing import AsyncIterator
from pydantic import BaseModel

class ChatMessage(BaseModel):
    role: Literal["system","user","assistant"]
    content: str

class ChatChunk(BaseModel):
    text: str
    done: bool
    prompt_tokens: int = 0
    completion_tokens: int = 0

class OllamaClient:
    async def health(self) -> bool: ...

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str,
        temperature: float = 0.7,
        timeout_s: int = 90,
    ) -> AsyncIterator[ChatChunk]: ...

    async def judge(
        self,
        prompt: str,
        schema: dict,         # JSON schema for response_format
        model: str,
        temperature: float = 0.1,
        timeout_s: int = 30,
    ) -> dict: ...
```

### 7.2 Behavior

- **Health check** on startup. If Ollama unreachable, log error, allow server to start, but reject `session.started` requests with 503.
- **Chat** streams tokens. On timeout: cancel, emit `chat.error` event, return error to UI.
- **Judge** calls use Ollama's `format: "json"` mode plus a system prompt forcing JSON-schema-compliant output. Validate result against the schema. On invalid JSON: one retry with stricter system message; on second failure return `{"answer": "UNCLEAR", "evidence": ""}`.
- **No conversation memory in the client.** Each call carries the full message list. Multi-turn memory lives in the chat session at the handler layer.

### 7.3 System prompt for chat AI

```
# app/llm/prompts/system_chat.txt
You are a coding assistant helping a software engineering candidate during an interview.
Be helpful, accurate, and concise. When the candidate shares code, explain or modify it
clearly. When the candidate asks open-ended questions, ask one clarifying question if
the request is genuinely ambiguous; otherwise answer directly.

Do not refuse benign coding tasks. Do not warn about practice problems.
```

That's all. No "teach them" coaching, no scoring leakage.

---

## 8. Event Bus & Concurrency

### 8.1 EventBus

```python
# app/bus.py
import asyncio
from typing import Callable, Awaitable

class EventBus:
    """
    Per-session in-memory pub/sub for PersistedEvents.
    One bounded queue per session, one fanout task per session.
    """
    def __init__(self, queue_max: int = 1000):
        self._queues: dict[str, asyncio.Queue] = {}
        self._subscribers: dict[str, list[Callable[[PersistedEvent], Awaitable[None]]]] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._queue_max = queue_max

    async def publish(self, event: PersistedEvent) -> None: ...
    def subscribe(self, session_id: str, handler) -> None: ...
    async def close_session(self, session_id: str) -> None: ...
```

### 8.2 Concurrency model

- One asyncio queue per active session, max size 1000.
- One consumer task per session reads from its queue and fans out to subscribers (LiveScorer + WSManager).
- All event writes go: handler → `EventLogger.write` (persists to SQLite, assigns seq) → `EventBus.publish` (broadcasts the persisted form). **Persistence happens before fanout**, so subscribers always see a stable `seq`.

### 8.3 Backpressure

If a session's queue is full:
- **Drop policy by priority:** drop `editor.snapshot` first (lossy is fine), then `editor.paste`. Never drop `chat.*`, `code.executed`, `task.*`, or `session.*`. Dropped events are counted and surfaced as a metric on the dashboard.
- Backpressure on the *write* side: `EventLogger.write` is allowed to block briefly; if it blocks >2s, log a warning.

### 8.4 Lifecycle

- `session.started` event creates the queue, the consumer task, and registers handlers.
- `session.ended` event triggers: stop accepting new events for that session, drain queue, then spawn the post-hoc scoring task.
- After post-hoc completes, queue and task are torn down.

---

## 9. Live Scoring Engine

### 9.1 LiveScorer interface

```python
# app/scoring/live.py
class LiveScorer:
    def __init__(self, store: ScoreStore, ws_manager: WSManager): ...

    async def handle_event(self, event: PersistedEvent) -> None:
        """Subscribed to the event bus. Stateful per-session."""
```

### 9.2 Heuristic formulas

All scores are 0–100. All windows are per-task unless noted.

#### 9.2.1 Prompt Quality (heuristic component)

Computed per `chat.prompt_sent` event; rolling mean across the task.

```
def prompt_quality_heuristic(prompt: ChatPromptSentPayload, ctx) -> float:
    # ctx provides: recent error (if any) in last 60s, last prompt text, task type

    score = 50.0     # neutral baseline

    # Length component (peak around 40-300 words)
    words = len(prompt.text.split())
    if   words < 5:        score -= 25
    elif words < 15:       score -= 10
    elif words <= 300:     score +=  0
    elif words <= 600:     score -=  5
    else:                  score -= 15

    # Code context
    if prompt.attached_code and len(prompt.attached_code) > 20:
        score += 10
    if "```" in prompt.text:                # inline code block
        score +=  5

    # Error context: only matters if there WAS a recent error
    if ctx.recent_error:
        if ctx.recent_error[:80].lower() in prompt.text.lower()[:1000]:
            score += 20
        elif prompt.attached_output and ctx.recent_error[:80] in prompt.attached_output:
            score += 15
        else:
            score -= 10            # had an error, didn't share it

    # Imperative / question clarity (weak regex heuristic)
    if re.search(r"\b(how|why|what|fix|implement|write|debug|explain)\b",
                 prompt.text.lower()):
        score += 5

    # Near-duplicate of immediately prior prompt
    if ctx.prev_prompt_text:
        sim = jaccard_words(prompt.text, ctx.prev_prompt_text)
        if sim > 0.85:
            score -= 20

    return clamp(score, 0, 100)
```

Task-level score = mean of per-prompt scores. Session-level = mean across tasks.

#### 9.2.2 Verification Behavior (heuristic component)

Computed continuously; produces a task-level score on `task.submitted`.

```
def verification_heuristic(task_events) -> float:
    exec_events  = [e for e in task_events if e.type == CODE_EXECUTED]
    chat_resp    = [e for e in task_events if e.type == CHAT_RESPONSE_RECEIVED]
    pastes_from_chat = [e for e in task_events
                        if e.type == EDITOR_PASTE and e.payload["source_hint"] == "chat"]

    if not chat_resp:
        # No AI usage. Verification isn't really applicable. Cap at 60 with low confidence.
        return 60.0

    # Component 1: exec-after-chat ratio
    # For each chat response containing code, was there an exec before the NEXT prompt?
    chat_with_code = [r for r in chat_resp if "```" in r.payload["text"]
                                            or contains_code_like(r.payload["text"])]
    runs_after = 0
    for r in chat_with_code:
        next_prompt = first_event_after(task_events, r.seq,
                                        types=[CHAT_PROMPT_SENT, TASK_SUBMITTED])
        if any(e.type == CODE_EXECUTED and r.seq < e.seq < (next_prompt.seq if next_prompt else 1e9)
               for e in exec_events):
            runs_after += 1
    ratio = runs_after / len(chat_with_code) if chat_with_code else 1.0
    comp1 = 40 * ratio          # max 40

    # Component 2: paste-then-run latency
    # For each chat-source paste, time to first exec
    paste_run_score = 0
    for p in pastes_from_chat:
        next_exec = first_event_after(exec_events, p.seq)
        if next_exec is None:
            paste_run_score -= 5
        else:
            dt = next_exec.ts - p.ts
            if   dt < 30_000:    paste_run_score += 5
            elif dt < 120_000:   paste_run_score += 3
            else:                paste_run_score += 0
    comp2 = clamp(20 + paste_run_score, 0, 40)    # baseline 20, max 40

    # Component 3: any test-like run?
    # If task has test_code, did they exec something that runs the tests?
    comp3 = 20 if any("test" in e.payload["code"].lower() or
                      "assert" in e.payload["code"]
                      for e in exec_events) else 0

    return clamp(comp1 + comp2 + comp3, 0, 100)
```

#### 9.2.3 Iteration Efficiency (heuristic component)

```
def iteration_heuristic(task_events, task: Task) -> float:
    prompts = [e for e in task_events if e.type == CHAT_PROMPT_SENT]
    completion_event = next((e for e in task_events if e.type == TASK_SUBMITTED), None)

    if not prompts:
        return 70.0       # didn't use AI at all; neutral-positive

    # Component 1: redundancy penalty
    redundant = 0
    for i in range(1, len(prompts)):
        sim = jaccard_words(prompts[i].payload["text"], prompts[i-1].payload["text"])
        if sim > 0.7:
            redundant += 1
    redundancy_rate = redundant / max(1, len(prompts) - 1)
    comp1 = 40 * (1 - redundancy_rate)        # max 40

    # Component 2: total prompts vs. task baseline
    # baseline_prompts is part of the task spec (§14)
    n = len(prompts)
    baseline = task.baseline_prompts          # e.g., 3, 5, 7
    if n <= baseline:
        comp2 = 30
    elif n <= baseline * 2:
        comp2 = 20
    elif n <= baseline * 3:
        comp2 = 10
    else:
        comp2 = 0

    # Component 3: did they finish?
    if completion_event:
        comp3 = 30
    else:
        comp3 = 0

    return clamp(comp1 + comp2 + comp3, 0, 100)
```

### 9.3 Update cadence

- LiveScorer debounces updates per dimension to one write per 500ms per session.
- After each write, it publishes a `score.update` WS message to dashboard subscribers.

### 9.4 Helper functions (§9 references)

- `jaccard_words(a, b)`: lowercase, tokenize on `\W+`, drop tokens of length < 3, `|A∩B| / |A∪B|`.
- `clamp(x, lo, hi)`: `max(lo, min(hi, x))`.
- `contains_code_like(text)`: regex `r"^\s*(def |class |import |from .* import|for .* in |if .*:\s*$|return )"` over any line.
- `first_event_after(events, seq, types=None)`: first event with `e.seq > seq` matching `types`.

---

## 10. Post-Hoc Scoring

### 10.1 Trigger and flow

```
on session.ended:
  await event_bus.drain(session_id)
  await mark_session_ended(session_id)
  asyncio.create_task(PostHocScorer().score_session(session_id))
```

`PostHocScorer.score_session`:
1. Load all events.
2. For each task in the session:
   a. Run all judge questions for that task (§10.2).
   b. Compute heuristic component again (deterministic — should match live).
   c. Combine into final dimension scores (§10.4).
   d. Write `phase='final'` rows.
3. Compute session-level aggregates per dimension as the count-weighted mean across tasks (weight = number of events in task).
4. Mark session `status='scored'`.
5. Push final `score.update` WS messages.

### 10.2 Judge questions catalog

Each judge call is a separate Ollama request. Each returns a constrained JSON object.

**Response schema (all judges share this):**
```json
{
  "type": "object",
  "properties": {
    "answer": {"type": "string", "enum": ["YES", "NO", "UNCLEAR"]},
    "evidence": {"type": "string", "maxLength": 280}
  },
  "required": ["answer", "evidence"]
}
```

#### 10.2.1 Prompt Quality judges (run per `chat.prompt_sent`)

```
PQ1 — Goal clarity
"Read this prompt:
---
{prompt_text}
---
QUESTION: Does the prompt state a clear, specific goal or ask a clear question?
A clear goal names what the candidate wants to achieve or what they want answered.
A vague goal is e.g. 'help me with this'. Respond YES/NO/UNCLEAR."

PQ2 — Error inclusion
"The candidate's most recent code execution before this prompt produced this error:
---
{recent_error}
---
The candidate then sent this prompt:
---
{prompt_text}
---
ATTACHED CODE / OUTPUT (may be empty):
---
{prompt_attached}
---
QUESTION: Does the prompt or its attachments include the specific error message
or its key details? Respond YES/NO/UNCLEAR."
(Only asked when recent_error is non-empty within last 5 events.)

PQ3 — Code context
"Read this prompt:
---
{prompt_text}
---
ATTACHED CODE (may be empty):
---
{prompt_attached_code}
---
QUESTION: Does the prompt include the specific code the candidate is asking about,
either inline or as an attachment? Respond YES/NO/UNCLEAR."

PQ4 — Constraint specificity
"Read this prompt:
---
{prompt_text}
---
QUESTION: Does the prompt state at least one specific constraint, requirement,
or context detail (e.g., 'must be O(n)', 'Python 3.11', 'should handle empty input')?
Respond YES/NO/UNCLEAR."
```

#### 10.2.2 Verification judges

Run per `chat.response_received` that contained code-like content.

```
VB1 — Ran AI code
"The candidate sent this prompt:
---
{prompt_text}
---
The AI responded with (excerpt):
---
{response_excerpt}
---
Then, before the candidate's next prompt or task submission, these events occurred (in order):
---
{between_events_summary}
---
QUESTION: Did the candidate execute code after receiving this AI response and
before their next interaction? Respond YES/NO/UNCLEAR."

VB2 — Inspected output
"After this AI response, the candidate ran code and got this output:
---
{stdout_stderr}
---
The candidate's next prompt was:
---
{next_prompt}
---
QUESTION: Does the candidate's next prompt show that they read the output
(e.g., references the output, addresses what happened, mentions a specific value)?
Respond YES/NO/UNCLEAR. If there is no next prompt because they submitted, answer UNCLEAR."

VB3 — Modified AI code before running
"The AI provided this code:
---
{ai_code}
---
The code the candidate next executed was:
---
{executed_code}
---
QUESTION: Is the executed code meaningfully different from the AI code,
beyond trivial whitespace? 'Meaningful' = added/changed/removed at least one line
of logic. Respond YES/NO/UNCLEAR."
(Only asked when both an AI code block and a subsequent exec event exist.)
```

#### 10.2.3 Iteration Efficiency judges

```
IE1 — Different problem
"Read these two consecutive prompts from the same task:
---
PROMPT 1:
{prompt_1}

PROMPT 2:
{prompt_2}
---
QUESTION: Do these two prompts address substantively different sub-problems,
or is prompt 2 essentially re-asking prompt 1? Respond YES (different) /
NO (same/re-ask) / UNCLEAR."

IE2 — Direction change after non-help
"The AI's response to the candidate's prompt was (excerpt):
---
{response_excerpt}
---
The candidate's NEXT prompt was:
---
{next_prompt}
---
QUESTION: Did the candidate change their approach or topic in the next prompt
(suggesting the AI response wasn't useful), or continue down the same path?
Respond YES (changed) / NO (continued same path) / UNCLEAR."
(Only asked when the next prompt occurred within 60s, suggesting frustration vs. progress.)
```

### 10.3 Judge prompt template

All judge questions are wrapped in:

```
You are evaluating a software engineering interview transcript.
Answer ONE specific yes/no question based on the evidence provided.
Be conservative: if the evidence is ambiguous, answer UNCLEAR.
Quote a brief piece of evidence (max 280 chars) supporting your answer.

{specific_question}

Respond ONLY with valid JSON matching this schema:
{"answer": "YES" | "NO" | "UNCLEAR", "evidence": "<short quote, may be empty if UNCLEAR>"}
```

### 10.4 Aggregation

For each dimension, per task:

```python
def aggregate(heuristic_score: float,
              judge_results: list[JudgeResult]) -> tuple[float, float]:
    """Returns (final_score, confidence)."""

    # Judge component
    valid = [j for j in judge_results if j.answer != "UNCLEAR"]
    if valid:
        judge_score = 100.0 * sum(1 for j in valid if j.answer == "YES") / len(valid)
        judge_coverage = len(valid) / max(1, len(judge_results))
    else:
        judge_score = None
        judge_coverage = 0.0

    # Combine
    if judge_score is None:
        final = heuristic_score
        confidence = 0.5 * heuristic_coverage(heuristic_score)
    else:
        # Weighted: heuristic 0.4, judge 0.6 (judges are more semantic)
        final = 0.4 * heuristic_score + 0.6 * judge_score
        confidence = (
            0.30                                    # base
            + 0.40 * judge_coverage                 # judge agreement on findings
            + 0.30 * heuristic_coverage(heuristic_score)
        )

    return final, clamp(confidence, 0.0, 0.95)
```

`heuristic_coverage` is 1.0 when the task had >=3 chat prompts and >=1 exec event, else linearly degraded.

### 10.5 Evidence record (what `scores.evidence` contains)

```json
{
  "heuristic": {
    "score": 67.5,
    "components": {"length": 5, "code_context": 10, "error_match": 20, ...},
    "event_seqs": [12, 18, 23]
  },
  "judges": [
    {"question_id": "PQ1", "answer": "YES", "evidence": "I'm getting...", "target_seq": 12},
    {"question_id": "PQ2", "answer": "NO",  "evidence": "",                "target_seq": 12},
    ...
  ],
  "final_score": 72.4,
  "confidence": 0.68
}
```

---

## 11. Sandbox

### 11.1 Interface

```python
# app/sandbox/runner.py
class ExecResult(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    runtime_ms: int
    truncated: bool

class Sandbox:
    async def run_python(
        self,
        code: str,
        stdin: Optional[str] = None,
        timeout_s: int = 10,
        mem_limit_mb: int = 256,
    ) -> ExecResult: ...
```

### 11.2 Implementation contract

- Write `code` to a tempfile in a per-call temp directory (`tempfile.mkdtemp`).
- `subprocess.Popen([sys.executable, "-I", tempfile_path], ...)`:
  - `-I` for isolated mode (ignores user site-packages, env vars like PYTHON*).
  - `cwd=temp_dir`.
  - `stdin/stdout/stderr=PIPE`.
  - `start_new_session=True` (own process group).
  - `preexec_fn` calls:
    ```python
    resource.setrlimit(resource.RLIMIT_CPU, (timeout_s, timeout_s))
    resource.setrlimit(resource.RLIMIT_AS, (mem_limit_mb*1024*1024,)*2)
    resource.setrlimit(resource.RLIMIT_FSIZE, (10*1024*1024,)*2)
    resource.setrlimit(resource.RLIMIT_NPROC, (50, 50))
    ```
- Wait with `asyncio.wait_for(proc.communicate(...), timeout=timeout_s + 1)`.
- On timeout: `os.killpg(proc.pid, SIGKILL)`. Set `exit_code=124`.
- Truncate stdout/stderr at `settings.exec_output_limit_kb`. Set `truncated=True` if cut.
- `finally`: `shutil.rmtree(temp_dir, ignore_errors=True)`.

### 11.3 What is NOT enforced in v0

Explicitly documented and accepted for trusted local demo:
- **No network isolation.** The subprocess can hit localhost / internet.
- **No filesystem isolation beyond `cwd`.** Reads outside cwd are not blocked.
- **No import whitelist.**

**Loud warning in README:** *Do not expose this server to untrusted users without replacing the sandbox.*

---

## 12. WebSocket Layer

### 12.1 WSManager

```python
# app/ws/manager.py
class WSManager:
    """Registers websocket connections by session_id and audience ('candidate' | 'dashboard')."""

    async def connect(self, ws: WebSocket, session_id: str, role: str) -> None: ...
    async def disconnect(self, ws: WebSocket) -> None: ...
    async def send_to_session(self, session_id: str, role: str, msg: dict) -> None: ...
    async def broadcast_dashboard(self, session_id: str, msg: dict) -> None: ...
```

A WS may temporarily disconnect; reconnect resumes by replaying events from `last_seq` (client tells server in its hello).

### 12.2 Candidate WS protocol

URL: `/ws/session/{session_id}?candidate_name=...`

**Client → Server:**

```jsonc
// Hello (first message)
{"type": "hello", "last_seq": 0}

// Chat send
{"type": "chat.send", "text": "...", "attach_editor": true, "attach_output": false}

// Editor snapshot (debounced)
{"type": "editor.snapshot", "code": "...", "trigger": "idle"}

// Editor paste
{"type": "editor.paste", "text": "...", "source_hint": "chat"}

// Run code
{"type": "code.run", "code": "...", "stdin": null}

// Submit task
{"type": "task.submit", "final_code": "..."}

// End session
{"type": "session.end"}
```

**Server → Client:**

```jsonc
// Task presented
{"type": "task.presented", "task": {...full task...}, "task_idx": 0, "total_tasks": 3}

// Chat streaming
{"type": "chat.token", "text": "Hel"}
{"type": "chat.token", "text": "lo"}
{"type": "chat.done", "full_text": "Hello", "tokens": {"prompt": 12, "completion": 4}}

// Chat error
{"type": "chat.error", "error": "Ollama timeout"}

// Exec result
{"type": "exec.result", "stdout": "...", "stderr": "...", "exit_code": 0, "runtime_ms": 42}

// Acknowledgement (post-persist)
{"type": "ack", "seq": 17, "event_type": "editor.snapshot"}

// Session done
{"type": "session.done"}
```

### 12.3 Dashboard WS protocol

URL: `/ws/dashboard/{session_id}`

**Client → Server:**
```jsonc
{"type": "hello", "last_seq": 0}
{"type": "subscribe", "include_events": true, "include_scores": true}
```

**Server → Client:**
```jsonc
// Score update
{"type": "score.update",
 "task_id": "001",
 "dimension": "prompt_quality",
 "phase": "live",
 "score": 72.4,
 "confidence": 0.55,
 "evidence_snippets": [...]}

// Event for replay/live observation
{"type": "event", "event": {...PersistedEvent...}}

// Profile finalized
{"type": "profile.final", "session_id": "...", "scores": [...]}
```

### 12.4 Editor snapshot policy (the "significant change" definition)

A snapshot is taken when ANY of the following is true:
- Idle for >=5s since last edit.
- Levenshtein distance from last snapshot >100 chars.
- Just before sending a chat prompt (trigger=`pre_chat`).
- Just before running code (trigger=`pre_run`).
- Manual via the UI's "save snapshot" button (trigger=`manual`).

Rate limit: at most one snapshot per second per session; coalesce to the latest.

---

## 13. HTTP Routes (REST)

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Redirect to `/candidate` for v0 |
| GET | `/candidate` | Serve `candidate.html` |
| GET | `/dashboard` | Serve `dashboard.html` |
| GET | `/static/*` | Static asset serving |
| POST | `/api/session` | Create session. Body: `{candidate_name, task_sequence?}`. Returns `{session_id, ...}` |
| GET | `/api/session/{id}` | Session metadata + status |
| GET | `/api/session/{id}/events?from_seq=N` | Replay support |
| GET | `/api/session/{id}/scores?phase=final` | Profile fetch |
| GET | `/api/tasks` | List available tasks |
| GET | `/api/tasks/{id}` | Single task |
| GET | `/api/health` | Returns `{ollama: bool, db: bool}` |
| WS | `/ws/session/{id}` | Candidate WS |
| WS | `/ws/dashboard/{id}` | Dashboard WS |

Note: chat and exec do NOT have REST endpoints in v0. They go over WS. (This avoids dual code paths.)

---

## 14. Task Schema

```yaml
# tasks/001-substring-permutations.yaml
id: "001"
title: "Substring Permutations"
type: "code"                              # one of: code | debug | design
description_md: |
  Write a function `find_permutations(s: str, pattern: str) -> list[int]` that
  returns all starting indices in `s` where some permutation of `pattern`
  appears as a substring.

  Example: `find_permutations("cbaebabacd", "abc")` → `[0, 6]`.

  Aim for time complexity better than O(n*m*m!).
starter_code: |
  def find_permutations(s: str, pattern: str) -> list[int]:
      # your code here
      pass
test_code: |
  assert find_permutations("cbaebabacd", "abc") == [0, 6]
  assert find_permutations("abab", "ab") == [0, 1, 2]
  assert find_permutations("", "a") == []
  print("OK")
time_limit_minutes: 15
baseline_prompts: 3                       # for iteration heuristic
expected_signals:
  recent_error_likely: true               # affects judge questions enabled
  ai_codeblock_likely: true
```

Task files are loaded at startup. To add a task: drop a YAML file in `tasks/`, restart server.

---

## 15. Frontend

### 15.1 Candidate UI wireframe

```
┌─────────────────────────────────────────────────────────────────┐
│  AI Scoring Demo — Candidate: Alice    Task 1 of 3  [End sess.] │
├───────────────────────────┬─────────────────────────────────────┤
│                           │                                     │
│  TASK                     │  CHAT WITH AI                       │
│  ┌─────────────────────┐  │  ┌───────────────────────────────┐  │
│  │ # Substring Perm... │  │  │  > help me solve this         │  │
│  │ Write a function... │  │  │                               │  │
│  │ Example: ...        │  │  │  AI: Sure — can you share...  │  │
│  │                     │  │  │  ...                          │  │
│  │ [Submit task]       │  │  │                               │  │
│  └─────────────────────┘  │  └───────────────────────────────┘  │
│                           │  [□ attach editor] [□ attach out]   │
│  CODE                     │  ┌───────────────────────────────┐  │
│  ┌─────────────────────┐  │  │  Your message…                │  │
│  │ def find_permutati… │  │  └───────────────────────────────┘  │
│  │   # your code here  │  │            [Send]                   │
│  │   pass              │  │                                     │
│  │                     │  │                                     │
│  │  [Run] [Run tests]  │  │                                     │
│  └─────────────────────┘  │                                     │
│  OUTPUT                   │                                     │
│  ┌─────────────────────┐  │                                     │
│  │ stdout / stderr     │  │                                     │
│  └─────────────────────┘  │                                     │
└───────────────────────────┴─────────────────────────────────────┘
```

### 15.2 Candidate UI state machine

```
[INIT] --connect WS--> [WAITING_TASK]
[WAITING_TASK] --task.presented--> [TASK_ACTIVE]
[TASK_ACTIVE] --task.submit--> [TASK_SUBMITTING]
[TASK_SUBMITTING] --task.presented (next)--> [TASK_ACTIVE]
[TASK_SUBMITTING] --session.done--> [SESSION_DONE]
[* (any)] --session.end button OR ws close--> [SESSION_DONE]
[* (any)] --ws drop--> [RECONNECTING] --reconnect--> [restore prev state]
```

On `TASK_ACTIVE` entry: clear chat panel, clear editor (load starter_code), clear output. Reset paste-tracking buffer.

### 15.3 Dashboard UI wireframe

```
┌────────────────────────────────────────────────────────────────┐
│  AI Scoring — Dashboard                                        │
├────────────────────────────────────────────────────────────────┤
│  ACTIVE SESSIONS                                               │
│  • Alice — task 2/3 — 22 min — [view]                          │
│  • Bob — task 1/3 — 8 min — [view]                             │
│  ────────────────────────────────────────                      │
│  COMPLETED                                                     │
│  • Carol — scored — [view]                                     │
└────────────────────────────────────────────────────────────────┘
```

Click → session detail:

```
┌────────────────────────────────────────────────────────────────┐
│  Session: Alice — task 2/3 — LIVE                              │
├────────────────────────────────────────────────────────────────┤
│  DIMENSIONS (live, tentative)                                  │
│  Prompt Quality:    [████████░░] 72  conf 0.55                 │
│  Verification:      [██████░░░░] 58  conf 0.50                 │
│  Iteration:         [█████████░] 85  conf 0.60                 │
├────────────────────────────────────────────────────────────────┤
│  REPLAY  [⏮ ⏯ ⏭]  seq 32 / 47                                  │
│  ╔ chat ═══════════════════╗  ╔ editor ═══════════════════╗    │
│  ║ ...                     ║  ║ def find_permutations... ║    │
│  ╚═════════════════════════╝  ╚═══════════════════════════╝    │
├────────────────────────────────────────────────────────────────┤
│  EVIDENCE for Prompt Quality                                   │
│  • seq 12 (PQ2): NO — "no error message in prompt"             │
│  • seq 18 (PQ3): YES — quoted code block                       │
│  ...                                                           │
└────────────────────────────────────────────────────────────────┘
```

### 15.4 Frontend files

- `candidate.html`: shell with three panels. Monaco from CDN. ~100 lines.
- `candidate.js`: WS connect, state machine, message routing. ~400 lines target.
- `dashboard.html`: shell. ~80 lines.
- `dashboard.js`: list + detail views, replay scrubber, evidence drawer. ~500 lines target.

No build step. Plain `<script type="module">`.

### 15.5 Paste source detection

Candidate JS tracks the last N=10 chat responses received. On any paste event in the editor (capture via `paste` DOM event on the Monaco container), compute normalized text and check if it matches (substring of length >=40 chars) any recent chat response. If yes → `source_hint="chat"`, else `"unknown"`. We don't attempt to identify "external" definitively; lockdown browser is what blocks that.

---

## 16. Failure Mode Matrix

| Failure | Detection | Response | User-visible |
|---|---|---|---|
| Ollama unreachable at startup | health endpoint fails | server starts, `/api/session` returns 503 | error banner on candidate page |
| Ollama goes down mid-chat | stream exception | emit `chat.error` event, send WS error | "AI unavailable — try again" |
| Ollama timeout (>90s) | wait_for | same as above | same |
| Ollama returns invalid JSON to judge | schema validation | retry once stricter, else `UNCLEAR` | none (post-hoc only) |
| SQLite locked | `OperationalError` | retry 3x backoff, then raise | event temporarily not acked; client retries |
| WS disconnect mid-session | onclose | mark session "reconnecting"; keep state 60s | "reconnecting…" banner |
| WS reconnect | hello with last_seq | replay events from last_seq | seamless |
| Sandbox timeout | wait_for | kill process group, log with exit=124 | "Execution timed out" |
| Sandbox OOM | exit 137 / sigkill | log, return | "Process killed (out of memory)" |
| Subprocess can't start | Popen exception | log, return exit=-1, stderr=error | "Could not start interpreter" |
| Task YAML malformed | parse on startup | skip task, log; if task_sequence references it, fail session creation | error on session create |
| Session idle >30 min | background sweeper | mark `abandoned`, close WS | candidate sees disconnect |
| Post-hoc judge crashes partway | catch in scorer | persist whatever judges completed, mark phase=final with low confidence | dashboard shows partial profile + warning |
| Two simultaneous sessions starve Ollama | queue depth metric | per-session rate limit at 1 in-flight chat | candidate sees brief delay |

---

## 17. Build Order (Revised, Spec-Backed)

Day-by-day, each step references the section that specifies it. Each step ends in something runnable.

1. **Day 1 — Foundations.**
   - `pyproject.toml`, project layout (§2), `Settings` (§3).
   - DB init from `schema.sql` (§4), `EventLogger.write` and `get_session_events` (§6).
   - `OllamaClient.health` and `chat_stream` (§7).
   - Smoke test: write a session row, persist 3 events, read them back, stream a chat response to stdout.

2. **Day 2 — Sandbox.**
   - `Sandbox.run_python` (§11) with all rlimits and timeout.
   - Tests in `tests/test_sandbox.py`: timeout case, OOM case, normal case, stderr case, truncation case.

3. **Day 3 — WS plumbing + Candidate UI shell.**
   - WSManager (§12.1), candidate WS handler (§12.2) — wire up `chat.send`, `editor.snapshot`, `editor.paste`, `code.run`, `task.submit`.
   - `candidate.html` + `candidate.js`: WS connect, hello, render task, editor, run button, chat panel.
   - End-to-end: load page, get a task, send a prompt, stream response, run code, see output.

4. **Day 4 — EventBus + Live Scoring.**
   - `EventBus` (§8) per-session queues.
   - `LiveScorer.handle_event` + heuristic formulas (§9.2.1, 9.2.2, 9.2.3).
   - `ScoreStore` writes phase='live' rows.
   - Verify in DB after a manual session that scores update.

5. **Day 5 — Dashboard.**
   - `dashboard.html` + `dashboard.js`: session list, detail view, live score bars (§15.3).
   - Dashboard WS handler (§12.3): subscribe + push score updates.
   - End-to-end: candidate session in one tab, dashboard in another, see scores move.

6. **Day 6 — Post-hoc judge.**
   - `OllamaClient.judge` with schema-constrained output (§7).
   - Judge prompt files (§10.2), `judges.py` with one function per question.
   - `PostHocScorer.score_session` (§10.1).
   - `aggregation.py` (§10.4).
   - **Pre-flight test:** run all judges against a hand-built fixture transcript to verify the local model can answer reliably before wiring into the live flow.

7. **Day 7 — Tasks, polish, dogfood.**
   - Author the 3 task YAMLs (§14).
   - Replay scrubber on dashboard.
   - Run a session on yourself end-to-end. Note every bug. Fix top 5.
   - README with run instructions and the loud sandbox warning (§11.3).

If day 6's pre-flight reveals the judge can't answer reliably, **stop** and either pick a stronger Ollama model, simplify the questions further, or accept heuristic-only with confidence cap. Do not paper over.

---

## 18. Test Plan

### 18.1 Unit tests (required)

- `test_heuristics.py`: each scoring function on synthetic event lists. Each component (length, code context, error match, etc.) tested in isolation.
- `test_sandbox.py`: see day 2.
- `test_aggregation.py`: judge-only, heuristic-only, both-present, all-unclear.
- `test_event_versioning.py`: migrate v1 payload to current.

### 18.2 Integration tests

- Persist+replay: write a session of N events, read them back, verify seq monotonic and contents intact.
- WS reconnect: client connects, sends events, drops, reconnects with last_seq, receives missed `score.update`.

### 18.3 Judge calibration (Day 6 pre-flight)

Hand-label 20 example prompts for each of PQ1–PQ4: known YES/NO. Run judge against them. Accept threshold: ≥80% agreement with human label. Below threshold = the question is too hard for the model OR the prompt is bad.

### 18.4 Dogfood checklist (Day 7)

- Run session on yourself: hire-quality run.
- Run session pretending to be a bad candidate (one-shot prompts, no verification, no testing).
- Profile should visibly distinguish the two.

---

## 19. Explicit Non-Goals (Things That MUST NOT Be Added in v0)

This list overrides any feature suggestion that arises during build:

- Authentication, accounts, roles, RBAC.
- Lockdown browser / fullscreen / clipboard control.
- Multi-language sandbox (anything beyond Python).
- Real sandbox isolation (nsjail, firejail, gVisor, Docker).
- The 5 dimensions not in §4: Decomposition, Independence, Architectural Reasoning, Recovery, Critical Evaluation.
- Cross-candidate comparison views, leaderboards, percentiles.
- Email reports, PDF export, ATS integration.
- Configurable dimension weighting in a UI.
- LLM tool use / function calling in the candidate's chat AI.
- File uploads from candidate.
- Multi-file projects in the editor.
- HTTPS, production deployment, anything beyond localhost.
- Real-time feedback to the candidate.

If a feature is "easy" to add but on this list — it does not get added.

---

## 20. Done Definition

v0 is done when **all** of these are true:

1. One command (`make dev` or `uvicorn app.main:app --reload`) starts the server.
2. Opening `localhost:8000/candidate?candidate_name=Alice` starts a 3-task session that flows correctly.
3. Opening `localhost:8000/dashboard` shows live updating scores for the active session.
4. Ending the session triggers post-hoc scoring within 2 minutes.
5. Final dimension scores include heuristic + judge components, with evidence citations referencing actual event seqs and quoting actual prompt/response text.
6. Two sessions run by visibly different testers produce visibly different profiles.
7. README documents the sandbox warning (§11.3) prominently.
8. All unit tests pass.
9. Judge calibration (§18.3) achieves ≥90% on at least one dimension's questions; failures are documented, not hidden.

Anything beyond this is v0.1.
