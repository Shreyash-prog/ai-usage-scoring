-- app/storage/schema.sql
-- Main spec §4 + llm_calls table from PROVIDER_SPEC §P.6.3.
-- Idempotent: safe to run on every startup.

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

-- PROVIDER_SPEC §P.6.3: per-call cost logging.
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
