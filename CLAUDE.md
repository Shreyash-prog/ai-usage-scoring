# CLAUDE.md — Project Guide for Claude Code

> This file is your project handbook. Read it first when starting any session in this repo.

---

## Project Snapshot

**ai-usage-scoring** — A POC platform that scores **how** software engineering candidates use AI during interview tasks, not just what they produce. Multi-dimensional profile (Prompt Quality, Verification Behavior, Iteration Efficiency in v0). Built on FastAPI + SQLite + OpenAI (chat) + Anthropic (judge), runs on localhost.

This is a **v0 POC**. Working demo first; polish, scale, and security later.

---

## Source-of-Truth Documents

Read in this order before doing any work:

1. **`CLAUDE.md`** (this file) — how to work in this repo.
2. **`ai_usage_scoring_spec.md`** — full implementation spec. Every interface, schema, heuristic formula, judge prompt, WS protocol. Authoritative for everything except LLM provider details.
3. **`PROVIDER_SPEC.md`** — **supersedes §3 and §7 of the main spec**, plus adds cost controls (§P.6). Where this and the main spec disagree, this wins.

If something isn't in any of these three docs and it's load-bearing, **stop and ask the human** before improvising. The spec is the contract.

---

## Working Conventions

### Language and Tooling
- **Python 3.12+** (3.12 minimum; 3.13 fine).
- **uv** for dependency management. Never use `pip` directly.
- **FastAPI** + **uvicorn** for the server.
- **aiosqlite** for async SQLite access.
- **pytest** + **pytest-asyncio** for tests.
- **ruff** for lint + format (no black, no isort).
- **mypy** for type checking on `app/` only.

### Code Style
- Type hints on every function signature.
- Pydantic v2 models for all data structures crossing boundaries (HTTP, WS, DB payloads, LLM responses).
- `async def` everywhere in the request path. No sync I/O on hot paths.
- Docstrings on public functions; skip them on trivial helpers.
- Imports: stdlib, third-party, local — separated by blank lines.

### File Size Discipline
- Keep modules under ~400 lines. If a file grows past that, split it.
- One class per file unless the classes are tiny and tightly coupled.

---

## Project Structure

Authoritative layout in `ai_usage_scoring_spec.md` §2. **Follow it exactly.** Don't reorganize without asking.

The layout is reproduced here for quick reference (deltas from main spec come from PROVIDER_SPEC.md):

```
ai-usage-scoring/
├── CLAUDE.md                       # this file
├── ai_usage_scoring_spec.md        # main spec
├── PROVIDER_SPEC.md                # provider overrides
├── README.md                       # to be written
├── LICENSE                         # MIT
├── pyproject.toml
├── uv.lock
├── .env                            # GITIGNORED, never commit
├── .env.example                    # placeholder, committed
├── .gitignore
├── .github/workflows/ci.yml
├── Makefile
├── app/
│   ├── ... (per spec §2)
│   └── llm/
│       ├── chat_client.py          # OpenAIChatClient (NOT OllamaClient)
│       ├── judge_client.py         # AnthropicJudgeClient
│       ├── pricing.py              # cost estimator (PROVIDER_SPEC P.6.4)
│       └── prompts/
├── static/
├── tasks/
└── tests/
```

---

## Commands

Everything is via `uv` and/or `make`. Add the targets to the `Makefile` as you go.

```bash
# Setup (Day 1)
uv sync                              # install deps from pyproject.toml + uv.lock

# Run server
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

# Tests
uv run pytest
uv run pytest -k <pattern>           # subset
uv run pytest --cov=app --cov-report=term-missing

# Lint + format
uv run ruff check .
uv run ruff format .

# Type check
uv run mypy app

# Reset state for a clean demo run
rm -f events.db
```

Whenever you finish a logical chunk, run **lint + tests** before committing. Don't commit a broken `main`.

---

## Build Plan & Status Tracker

Source plan: `ai_usage_scoring_spec.md` §17 (with provider tweaks from `PROVIDER_SPEC.md` §P.5).

Update this checklist as each day completes:

- [x] **Day 1 — Foundations.** uv project, settings, schema, EventLogger, OpenAI + Anthropic clients with health checks.
- [ ] **Day 2 — Sandbox.** Python subprocess runner with rlimits, timeout, output truncation. Tests.
- [ ] **Day 3 — WS plumbing + Candidate UI shell.** WSManager, candidate WS handler, basic candidate.html with editor + chat + run.
- [ ] **Day 4 — EventBus + Live Scoring.** Per-session asyncio queues, LiveScorer with all three heuristic formulas.
- [ ] **Day 5 — Dashboard.** dashboard.html with session list, detail view, live score bars, replay scrubber.
- [ ] **Day 6 — Post-hoc judge.** AnthropicJudgeClient with forced tool use. All judge questions. Calibration pre-flight on 20 hand-labeled fixtures (must hit ≥90%).
- [ ] **Day 7 — Tasks + polish + dogfood.** Three task YAMLs. End-to-end self-test. README. Fix top 5 bugs.

---

## Git Workflow

- **Branch:** `main` only. Solo project, no PRs needed.
- **Commit cadence:** one commit per meaningful chunk (typically end of a day, but smaller is fine if a logical unit is done).
- **Commit messages:** Conventional Commits format.
  - `feat: ` for new functionality
  - `fix: ` for bug fixes
  - `chore: ` for tooling/config
  - `test: ` for tests-only changes
  - `docs: ` for docs-only changes
  - `refactor: ` for non-behavioral code changes
- After each commit: **push to `origin/main`** immediately. The repo is the live trail.
- Don't squash. The history is the story.

### What Goes in `.gitignore` (Day 1)

```
# Python
__pycache__/
*.pyc
*.pyo
*.pyd
.Python
*.egg-info/
.pytest_cache/
.mypy_cache/
.ruff_cache/
.coverage
htmlcov/

# uv
.venv/

# Secrets
.env
*.key

# Project state
events.db
events.db-*

# Editor
.vscode/
.idea/
*.swp
.DS_Store
```

---

## Secrets — This Repo Is PUBLIC

- `.env` is gitignored. **Never commit it.** First Day 1 task: write `.gitignore` before anything else lands in git.
- `.env.example` is committed with placeholder values only (`sk-replace-me`).
- API keys load **only from the `.env` file** via Pydantic Settings. They are **not** exported to the user's shell. This is intentional — see "Dual Account Note" below.
- Before every commit: scan the staged diff for `sk-`, `sk-ant-`, anything that looks like a key. If you find one, **stop, remove, rotate the key, re-commit**.
- If a key ever lands in git history: rotate the key immediately at the provider console, then deal with the history (BFG or `git filter-repo`).

### Dual-Account Note

The user runs Claude Code on one Anthropic account (with a Max subscription) and uses a *different* Anthropic account's API key (with paid credits) for this project's judge calls. The API key must stay isolated in `.env` only — never sourced into the shell — so that Claude Code itself continues to authenticate via its subscription, not via the project's API credits.

Practical implication for you: `Settings` reads `.env` via `pydantic-settings`. Don't add code that reads `os.environ["ANTHROPIC_API_KEY"]` directly or that documents/encourages `export`-ing the key. The pattern is: `.env` → `Settings` → clients. That's it.

---

## Out of Scope (DO NOT ADD)

These are explicitly excluded from v0. The main spec §19 is the canonical list. Highlights:

- Authentication / login systems
- Multi-language sandbox (Python only)
- Real sandbox isolation (gVisor, Docker, nsjail) — `subprocess` + rlimits is the v0 plan
- The 5 dimensions beyond the three v0 dimensions
- Cross-candidate comparison, leaderboards, percentiles
- HTTPS / production deployment
- File uploads from the candidate
- Multi-file projects in the editor
- Real-time feedback to the candidate
- Anything pretty / polished beyond the wireframes in §15

If during the build you find yourself implementing one of these, **stop and re-read this section**.

---

## When to Ask, When to Decide

**Decide on your own:**
- Naming of internal helpers, file organization within spec-defined modules, choice of micro-libraries (e.g., `python-levenshtein` vs `difflib`).
- Test fixture details, exact assertion phrasing.
- Implementation of well-specified algorithms (e.g., `jaccard_words` in §9.4).
- Minor style choices not in conflict with conventions above.

**Ask the human first:**
- Anything that changes a public interface listed in the spec.
- Anything that changes the data model (schema, event types, payloads).
- Adding a new dependency not already in `pyproject.toml`.
- Adding a new feature, even a small one, not in the spec.
- Whenever a spec instruction conflicts with reality (e.g., a library doesn't behave as expected) — surface it.
- Whenever Day N's pre-flight or smoke test fails — never paper over.

**Always stop and ask:**
- If the Day 6 judge calibration fails (<90% on hand-labeled fixtures). Don't continue to wire judges into the live flow.
- If costs are running unexpectedly high (judge call >$0.10 each, or session total >$3).

---

## Handoff Pattern at Day End

When you finish a day's worth of work:

1. Run `uv run ruff check . && uv run ruff format . && uv run pytest`.
2. Commit per the conventions above. Push to `origin/main`.
3. Update the checklist at the top of this file (the "Build Plan & Status Tracker" section).
4. Write a one-paragraph status update to the human covering:
   - What's done.
   - What was harder than expected.
   - What you'd do differently next time.
   - What you propose to start next (and confirm before starting).
5. **Wait for "continue" before starting the next day's work.**

---

## Specific Reminders

- **Sequence numbers**: events carry `seq` (per-session monotonic) AND `ts` (wall clock). Always use `seq` for ordering in scoring code. `ts` is for display only.
- **Score evidence**: every score row's `evidence` JSON must reference real event `seq`s. No making up evidence to satisfy a schema.
- **Judge "UNCLEAR"** is a legitimate answer. Don't try to force YES/NO when the model says UNCLEAR. Aggregation handles it (see main spec §10.4).
- **Streaming chat**: candidate must see tokens as they arrive. Don't buffer the whole response server-side.
- **Sandbox is `subprocess`** with `-I` flag and rlimits. README warns this is not production-safe.
- **Frontend has no build step.** Vanilla JS modules, Monaco from CDN. Resist the urge to add Vite / npm / bundlers.

---

## The North Star

We're done when:
1. Two visibly different testers (a careful user and a careless one) produce visibly different profiles.
2. Final dimension scores cite real evidence from real event seqs.
3. The whole thing runs with one command on a fresh clone after `uv sync` and a `.env` populated.

Build to that bar. Not below, not above.
