# ai-usage-scoring

**Live demo (no install required): <https://ai-usage-scoring.fly.dev/>** — see
[Try the Live Demo](#try-the-live-demo) below for a guided walkthrough.

A v0 proof-of-concept that scores **how** software-engineering candidates use AI
during interview tasks — not just what they produce. As a candidate works in a
browser editor with a chat assistant and a code runner, every interaction is logged
as an ordered event stream and scored across three dimensions — **Prompt Quality**,
**Verification Behavior**, and **Iteration Efficiency** — first live with deterministic
heuristics, then re-scored post-hoc by an LLM judge that answers constrained
YES/NO/UNCLEAR questions and cites real events as evidence. The point is to make the
*process* visible: a careful user and a careless one produce visibly different profiles.

## Try the Live Demo

The demo is deployed and needs no setup — it runs from two links, best opened side by side:

- **Candidate view:** <https://ai-usage-scoring.fly.dev/candidate?candidate_name=Demo>
- **Dashboard (interviewer) view:** <https://ai-usage-scoring.fly.dev/dashboard>

Open **both** in side-by-side tabs or windows. The **candidate view** is what an
interviewee sees: a code editor, an AI chat assistant, a Run button, and three short
tasks. The **dashboard view** is the interviewer's side: the three score bars update
live as the candidate works, and once a session ends it shows the final scores with
evidence citations you can click.

### What to try (about 5 minutes)

1. In the **candidate** tab, read the first task — *Substring Permutations* (find every
   index in a string where some permutation of a given pattern appears).
2. Send a couple of prompts to the AI assistant. Good prompts carry context — your
   current code and the specific error you hit — rather than just "fix it."
3. Edit the code in the editor and click **Run**. It executes on Judge0 (a hosted
   sandbox), not on the server.
4. Switch to the **dashboard** tab and watch the three score bars move in real time as
   you prompt and run.
5. Click **Submit task** to advance to the next task, or **End session** when you're done.
6. After you end, **stay on the candidate tab** through the ~30–60s **"Scoring…"** wait —
   that's the post-hoc LLM judge scoring the whole session.
7. When it finishes, the dashboard shows the final scores with evidence. **Click any
   evidence citation** to jump the replay scrubber straight to the event it cites.

### Try it twice: deliberate vs. careless

The whole point is that *how* you use the AI shows up in the profile. To see it, run two
sessions and compare them on the dashboard:

- **Deliberate:** read the task, send specific prompts that include your code and the
  errors you hit, actually run the AI's suggestions, and iterate on the results.
- **Careless:** fire vague one-shot prompts, never run the code, and accept the first
  answer the AI gives.

The two produce visibly different profiles — most clearly on Verification Behavior and
Iteration Efficiency.

### What to expect

- **First load may take a few seconds** — the hosted machine cold-starts.
- **Code execution is a shared free tier:** 50 runs per day across *all* visitors. If a
  Run fails, the daily cap may already be used up.
- **Public demo, no authentication.** Any session is visible to anyone who opens the
  dashboard URL — **don't paste anything sensitive.**
- **Rate limits apply:** 5 sessions per IP per hour, and 50 sessions per day in total.
- If the demo shows **"at capacity,"** the daily cap has been reached — try again tomorrow.

Curious what building this taught us — including what the Iteration dimension reveals
about minimum-effort sessions? See **[FINDINGS.md](FINDINGS.md)**.

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

`.env` holds three keys:

```
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
JUDGE0_API_KEY=...              # RapidAPI key for judge0-ce (hosted code execution)
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
