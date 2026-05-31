# PROVIDER_SPEC — LLM Provider Update

> **This document supersedes §3 (Configuration) and §7 (LLM Integration) of `ai_usage_scoring_spec.md`.**
> Where there is conflict between this doc and the main spec, this doc wins.
> All other sections of the main spec are unchanged.

---

## P.1 What Changed and Why

The main spec was written against Ollama as a fully-local LLM provider. We're swapping to a hybrid hosted setup:

- **Chat AI** (the candidate's assistant): **OpenAI GPT-4o-mini**
- **Judge** (post-hoc scoring): **Anthropic Claude Sonnet 4.6**

Rationale: GPT-4o-mini is cheap and responsive enough to feel like a normal coding assistant for the candidate. Judging is the harder cognitive task and warrants a stronger model; Sonnet 4.6 is the best-quality affordable choice for binary judgments with evidence quoting.

Estimated cost: ~$1 per 2-3 hour session (dominated by ~150 judge calls).

---

## P.2 Configuration (Supersedes §3)

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

    # OpenAI (chat)
    openai_api_key: str                        # required, from .env
    openai_chat_model: str = "gpt-4o-mini"
    openai_chat_timeout_s: int = 60
    openai_chat_max_history_messages: int = 40 # truncate older than this

    # Anthropic (judge)
    anthropic_api_key: str                     # required, from .env
    anthropic_judge_model: str = "claude-sonnet-4-5"  # see P.7 for model string note
    anthropic_judge_timeout_s: int = 30
    anthropic_judge_max_retries: int = 1
    anthropic_judge_temperature: float = 0.1

    # Sandbox
    exec_timeout_s: int = 10
    exec_mem_limit_mb: int = 256
    exec_output_limit_kb: int = 1024

    # Scoring
    live_score_debounce_ms: int = 500

    # Session
    session_idle_timeout_min: int = 30

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

settings = Settings()
```

### Key Sourcing — `.env` Only, Not Shell

The two API keys must load from the project's `.env` file, not the user's shell environment. The user's Claude Code is authenticated against a different Anthropic account (Max subscription); if `ANTHROPIC_API_KEY` is present in the shell, Claude Code picks it up and burns the project's paid credits instead of using the subscription.

Concretely: `Settings` reads `.env`. We do not document or recommend `export ANTHROPIC_API_KEY=...` anywhere. We do not have code that calls `os.environ["ANTHROPIC_API_KEY"]` directly. The pattern is exactly: `.env` → `Settings` → `OpenAIChatClient` / `AnthropicJudgeClient`.

By default `pydantic-settings` will also consult `os.environ`. That's fine — if the user *does* set the shell var, `Settings` will still work — but we don't tell them to.

### `.env.example` (commit this)

```
OPENAI_API_KEY=sk-replace-me
ANTHROPIC_API_KEY=sk-ant-replace-me
```

### `.env` (gitignored, never committed)

```
OPENAI_API_KEY=sk-actual-key
ANTHROPIC_API_KEY=sk-ant-actual-key
```

`.gitignore` MUST include `.env` from the very first commit. Repo is public.

---

## P.3 LLM Integration (Supersedes §7)

Two separate client classes, one per provider. Each lives in its own module.

### P.3.1 Project layout addition

```
app/llm/
├── __init__.py
├── chat_client.py         # OpenAIChatClient (formerly OllamaClient.chat_stream)
├── judge_client.py        # AnthropicJudgeClient (formerly OllamaClient.judge)
└── prompts/
    ├── system_chat.txt    (unchanged from §7.3)
    ├── judge_prompt_quality.txt
    ├── judge_verification.txt
    └── judge_iteration.txt
```

The single `OllamaClient` from the main spec is removed. References elsewhere in the spec to `OllamaClient.chat_stream` map to `OpenAIChatClient.chat_stream`; references to `OllamaClient.judge` map to `AnthropicJudgeClient.judge`.

### P.3.2 OpenAIChatClient interface

```python
# app/llm/chat_client.py
from typing import AsyncIterator, Literal
from pydantic import BaseModel
from openai import AsyncOpenAI

class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str

class ChatChunk(BaseModel):
    text: str
    done: bool
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""

class OpenAIChatClient:
    def __init__(self, api_key: str, model: str, timeout_s: int):
        self._client = AsyncOpenAI(api_key=api_key, timeout=timeout_s)
        self._model = model

    async def health(self) -> bool:
        """Cheap health check — list models or send 1-token request."""
        ...

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        temperature: float = 0.7,
    ) -> AsyncIterator[ChatChunk]:
        """
        Stream tokens from chat completion.
        Final chunk has done=True and includes token counts from usage.
        Raises on timeout or API error.
        """
        ...
```

**Implementation notes:**
- Use OpenAI Python SDK with `AsyncOpenAI`.
- Stream with `client.chat.completions.create(model=..., messages=..., stream=True, stream_options={"include_usage": True})`.
- The last streamed chunk with `stream_options.include_usage=True` carries `usage` data — extract `prompt_tokens` and `completion_tokens` there.
- On timeout: cancel the iterator, raise `LLMTimeout`.
- On rate limit (429): one retry with 2s sleep, then raise.
- System message in `messages[0]` carries the contents of `prompts/system_chat.txt` (unchanged from main spec §7.3).

### P.3.3 AnthropicJudgeClient interface

```python
# app/llm/judge_client.py
from anthropic import AsyncAnthropic
from pydantic import BaseModel
from typing import Literal

class JudgeAnswer(BaseModel):
    answer: Literal["YES", "NO", "UNCLEAR"]
    evidence: str    # max 280 chars; may be empty if UNCLEAR

class AnthropicJudgeClient:
    def __init__(self, api_key: str, model: str, timeout_s: int, max_retries: int):
        self._client = AsyncAnthropic(api_key=api_key, timeout=timeout_s)
        self._model = model
        self._max_retries = max_retries

    async def health(self) -> bool: ...

    async def judge(
        self,
        prompt: str,
        temperature: float = 0.1,
    ) -> JudgeAnswer:
        """
        Send a judge prompt to Sonnet, get back a structured YES/NO/UNCLEAR.
        Uses tool-use forcing for guaranteed structured output (see P.3.4).
        On schema violation: retry once. On second failure: return
        JudgeAnswer(answer="UNCLEAR", evidence="").
        """
        ...
```

### P.3.4 How structured output is enforced

Anthropic's most reliable structured-output path is **forced tool use**. The judge call defines a single tool, forces the model to use it, and extracts the result.

```python
JUDGE_TOOL = {
    "name": "submit_judgment",
    "description": "Submit your judgment about the question asked.",
    "input_schema": {
        "type": "object",
        "properties": {
            "answer": {
                "type": "string",
                "enum": ["YES", "NO", "UNCLEAR"],
                "description": "Your verdict on the question.",
            },
            "evidence": {
                "type": "string",
                "maxLength": 280,
                "description": "A brief quote from the transcript supporting your verdict. Empty if UNCLEAR.",
            },
        },
        "required": ["answer", "evidence"],
    },
}

response = await client.messages.create(
    model=settings.anthropic_judge_model,
    max_tokens=512,
    temperature=settings.anthropic_judge_temperature,
    tools=[JUDGE_TOOL],
    tool_choice={"type": "tool", "name": "submit_judgment"},
    messages=[{"role": "user", "content": prompt}],
)

# Extract the tool_use block
for block in response.content:
    if block.type == "tool_use" and block.name == "submit_judgment":
        return JudgeAnswer(**block.input)
```

If the model returns no `tool_use` block (rare but possible): retry once. On second failure: return UNCLEAR.

### P.3.5 Health check behavior on startup

- Both clients have `health()`. Called on FastAPI startup.
- If **OpenAI** is down: server still starts; `/api/session` returns 503 with message "Chat AI unavailable".
- If **Anthropic** is down: server still starts; chat works fine; post-hoc scoring jobs queue up and retry on the next health success. Surface this on the dashboard.
- If both are down: server starts in degraded mode. UI shows a banner.

---

## P.4 Updated Failure Modes (Supersedes the Ollama rows in §16)

Replace any "Ollama" row in §16 with these:

| Failure | Detection | Response | User-visible |
|---|---|---|---|
| OpenAI unreachable at startup | health() throws | server starts; chat endpoints return 503 | banner: "Chat AI unavailable" |
| OpenAI rate limit (429) | exception | retry once with 2s backoff; then surface | "AI is busy — try again in a moment" |
| OpenAI timeout (>60s) | wait_for / SDK | cancel, emit `chat.error` event | "AI took too long — try again" |
| Anthropic unreachable | health() / call exception | post-hoc jobs queued; retry every 30s for 5 min | dashboard shows "Scoring delayed" |
| Anthropic timeout (>30s) | wait_for | retry once; then count as UNCLEAR for that question | none visible (post-hoc only) |
| Anthropic tool-use not produced | response parsing | retry once with stricter system msg; else UNCLEAR | none visible |
| Anthropic rate limit (429) | exception | exponential backoff [2s, 10s, 30s], then fail that judge | confidence drops for affected dimensions |

All other failure modes from §16 (SQLite, WS, sandbox, etc.) are unchanged.

---

## P.5 Updated Build Order Touchpoints (Supersedes the LLM bits of §17)

Day 1 — Foundations: instead of `OllamaClient.health` and `chat_stream`, build `OpenAIChatClient` with health + streaming smoke test.

Day 6 — Post-hoc judge: instead of `OllamaClient.judge`, build `AnthropicJudgeClient` with the tool-use forced output. The Day 6 pre-flight calibration in §18.3 still applies — but the acceptance threshold goes up:

- **Old threshold (Ollama 7B):** ≥80% agreement with human label.
- **New threshold (Sonnet 4.6):** ≥90% agreement on PQ1–PQ4, VB1–VB3, IE1–IE2. If we can't hit 90% on Sonnet 4.6, the judge questions themselves are bad, not the model.

---

## P.6 Cost Controls (New, Has No Counterpart in Main Spec)

Because we're paying per token, we add light cost controls:

### P.6.1 Per-session budget

- `Settings.session_max_judge_calls: int = 200` (hard cap)
- `Settings.session_max_chat_tokens_in: int = 200_000` (hard cap)
- On hitting either cap mid-session: log a warning, mark the session with `cost_capped=True` in the scores evidence, continue without further LLM calls for that dimension.

### P.6.2 Prompt caching (deferred, but noted)

Sonnet 4.6 supports prompt caching. Judge prompts share a large stable template prefix. **For v0 we don't implement caching** — keep code simple. v0.1 should add `cache_control: ephemeral` on the system / template portion of judge calls, expected ~50% input cost reduction.

### P.6.3 Cost logging

Every chat and judge call records to a `llm_calls` table (new — add to schema §4):

```sql
CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  ts INTEGER NOT NULL,
  provider TEXT NOT NULL,            -- 'openai' | 'anthropic'
  model TEXT NOT NULL,
  purpose TEXT NOT NULL,             -- 'chat' | 'judge:PQ1' | etc.
  prompt_tokens INTEGER NOT NULL,
  completion_tokens INTEGER NOT NULL,
  latency_ms INTEGER NOT NULL,
  cost_usd_estimate REAL NOT NULL,   -- computed at call time using P.6.4 rates
  status TEXT NOT NULL               -- 'ok' | 'error' | 'timeout' | 'rate_limited'
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_session ON llm_calls(session_id);
```

Dashboard surfaces total session cost. Useful for sanity-checking and demoing.

### P.6.4 Cost calculation table

Hardcoded into a small helper module (update when pricing changes):

```python
# app/llm/pricing.py
PRICING_USD_PER_MTOK = {
    "gpt-4o-mini":       {"input": 0.15,  "output": 0.60},
    "claude-sonnet-4-5": {"input": 3.00,  "output": 15.00},
    # add more as needed
}

def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    rates = PRICING_USD_PER_MTOK.get(model, {"input": 0, "output": 0})
    return (prompt_tokens * rates["input"] + completion_tokens * rates["output"]) / 1_000_000
```

---

## P.7 Model String Notes

Anthropic's API model strings can shift. Check the API docs for the current valid string when implementing. As of build time, use the most recent Sonnet 4.x snapshot string available from `https://docs.claude.com`. If `claude-sonnet-4-5` isn't valid at your build time, query the models list endpoint to get the current string and update settings accordingly. Same for OpenAI — `gpt-4o-mini` is stable but verify.

---

## P.8 Dependencies (Adds to `pyproject.toml`)

```toml
[project]
dependencies = [
    "fastapi>=0.110",
    "uvicorn[standard]>=0.27",
    "pydantic>=2.5",
    "pydantic-settings>=2.0",
    "aiosqlite>=0.20",
    "openai>=1.30",
    "anthropic>=0.30",
    "pyyaml>=6.0",
    "python-multipart>=0.0.9",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-cov>=4.1",
    "ruff>=0.4",
    "mypy>=1.10",
]
```

---

## P.9 Sanity Check Before You Hand Off to Claude Code

After Claude Code finishes Day 1, verify by hand:

```bash
# In the project root, with .env populated:
uv run python -c "
import asyncio
from app.config import settings
from app.llm.chat_client import OpenAIChatClient
from app.llm.judge_client import AnthropicJudgeClient

async def main():
    chat = OpenAIChatClient(settings.openai_api_key, settings.openai_chat_model, 30)
    judge = AnthropicJudgeClient(settings.anthropic_api_key, settings.anthropic_judge_model, 30, 1)
    print('OpenAI health:', await chat.health())
    print('Anthropic health:', await judge.health())

asyncio.run(main())
"
```

Both should print `True`. If not, Day 1 is not done.
