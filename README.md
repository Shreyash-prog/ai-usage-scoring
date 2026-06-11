# ai-usage-scoring

A v0 proof-of-concept that scores **how** software-engineering candidates use AI
during interview tasks — not just what they produce. As a candidate works in a
browser editor with a chat assistant and a code runner, every interaction is logged
as an ordered event stream and scored across three dimensions — **Prompt Quality**,
**Verification Behavior**, and **Iteration Efficiency** — first live with deterministic
heuristics, then re-scored post-hoc by an LLM judge that answers constrained
YES/NO/UNCLEAR questions and cites real events as evidence. The point is to make the
*process* visible: a careful user and a careless one produce visibly different profiles.

It is one FastAPI process: SQLite storage, WebSockets, a no-build vanilla-JS frontend,
OpenAI `gpt-4o-mini` for the candidate's chat, and Anthropic `claude-sonnet-4-6` for
the judge.

## Quickstart

```bash
uv sync --all-extras            # install deps (Python 3.12+, uv required)
cp .env.example .env            # then put real keys in .env (see below)
make dev                        # uvicorn on http://127.0.0.1:8000
```

- Candidate: `http://localhost:8000/candidate?candidate_name=Alice`
- Dashboard: `http://localhost:8000/dashboard`

`.env` holds two keys:

```
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
```

Common commands: `make test` · `make lint` · `make fmt` · `make typecheck` ·
`make reset` (wipe local `events.db`) · `make smoke` (live API health check).

## Code execution

Code execution runs on Judge0 (hosted service). The local server never executes
candidate code. Submissions go to the Judge0 CE free tier (RapidAPI) in synchronous
mode; set `JUDGE0_API_KEY` in `.env`. Input is capped (50 KB code, 10 KB stdin) and
output is truncated to `exec_output_limit_kb`.

## API key isolation (dual-account note)

If you use Claude Code under a *different* Anthropic account from the one funding this
project's API key, keep the API key in `.env` only and **never export it to your
shell**. Claude Code reads `ANTHROPIC_API_KEY` from the environment and will burn
project credits at API rates instead of using your subscription. The pattern is
`.env` → `Settings` → clients; nothing reads `os.environ` directly.

## Documentation

- [`ai_usage_scoring_spec.md`](ai_usage_scoring_spec.md) — full implementation spec
  (interfaces, schema, heuristic formulas, judge prompts, WS protocol).
- [`PROVIDER_SPEC.md`](PROVIDER_SPEC.md) — LLM provider setup; supersedes spec §3 and §7.
- [`CLAUDE.md`](CLAUDE.md) — how to work in this repo.

## License

MIT — see [`LICENSE`](LICENSE).
