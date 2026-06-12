# FINDINGS — what the v0 POC revealed

Documentation of what building and dogfooding the v0 platform actually showed. Not a
redesign doc. See [`ai_usage_scoring_spec.md`](ai_usage_scoring_spec.md) for the spec
and [`CLAUDE.md`](CLAUDE.md) for build status.

> **Caveat on sample size:** all numbers below come from **two** scripted sessions
> (one careful, one careless) against the three real tasks with the live judge. Two
> sessions is not a sample. The magnitudes of the deltas mean far less than the fact
> that **two of three dimensions discriminate at all**. Treat this as a signal that
> the approach is plausible, not as calibrated thresholds. More sessions and external
> eyes come before any redesign.

---

## 1. Dogfood results

Two full sessions, real tasks (001 substring, 002 csv-debug, 003 rate-limiter), live
OpenAI chat + live Anthropic judge. Session-level aggregate scores (0–100):

| Dimension       | Good candidate | Careless candidate | Delta   | Discriminates? |
|-----------------|---------------:|-------------------:|--------:|----------------|
| Prompt Quality  | ~73            | 12                 | **+61** | ✅ strongly     |
| Verification    | ~72            | 55                 | **+17** | ✅ correctly    |
| Iteration       | 70             | 100                | **−30** | ❌ **inverted** |

Supporting signals (also checked on the dashboard):

| Signal                          | Good        | Careless    |
|---------------------------------|-------------|-------------|
| Judge questions fired           | ~35         | 9           |
| UNCLEAR rate                    | 2 / 35      | 0 / 9       |
| Evidence seq citations (per UI) | 15          | 15          |
| Scrubber jump from citation     | → seq 24    | → seq 12    |

No empty UNCLEAR clusters (which would have signaled a calibration regression).
Prompt Quality and Verification behaved as intended; the careful candidate's specific,
error/code-bearing prompts and run-after-suggestion behavior scored well above the
careless candidate's vague one-shot, never-run, paste-and-submit behavior.

**Behaviors driven:**
- *Good:* specific prompts with code/error context attached, ran code after every AI
  suggestion, read output and addressed it in the next prompt, iterated thoughtfully.
- *Careless:* vague one-shot prompts ("fix this"), never ran code, accepted the first
  AI suggestion verbatim, submitted without checking.

---

## 2. The Iteration Efficiency inversion

**Symptom.** The careless candidate scored **100** on Iteration; the careful candidate
scored **70**. The dimension ranks disengagement *above* skill.

**Diagnosis.**
- The iteration **heuristic** (spec §9.2.3) rewards *few prompts* (comp2: ≤baseline →
  30) + *no redundancy* (comp1: 40) + *finished* (comp3: 30). A one-shot
  "fix this → submit" with a single prompt per task hits **100**.
- With **< 2 prompts**, the iteration **judges don't fire**: IE1 needs a consecutive
  prompt pair; IE2 needs a response followed by another prompt. So the careless
  session's iteration score stays a pure-heuristic 100 (judge-less branch, §10.4).
- The careful candidate asked a thoughtful follow-up ("is the complexity acceptable?").
  That triggers IE2, which correctly answers **NO** ("not proposing a *different*
  approach") — pulling the judge component down to ~50 and the blended score to ~70.

**Why it's a design issue, not a code bug.** The formula is implemented exactly per
spec (verified: the same code passes the §9 unit tests and the Day-6 calibration). The
heuristic, the IE judges, and the aggregation all do what they were specified to do.
The problem is upstream of the implementation — in what the dimension *measures*.

**What it taught us.** *Iteration Efficiency was the wrong frame.* It conflates two
opposite things that both look like "minimal interaction": **minimal because skilled**
(solved it in few, well-aimed prompts) and **minimal because disengaged** (didn't
really try). Prompt count and "finished" can't separate those; the careless extreme
games them. The signal we actually want — *did the candidate iterate productively when
the AI wasn't immediately right* — needs a different operationalization, likely leaning
on Verification-style and judge-based evidence rather than counting prompts.

---

## 3. v0.1 backlog

Deferred deliberately; **do not start without more sessions and external review.**

1. **Iteration redesign** — the inversion above. Rethink what the dimension measures;
   prompt-count minimalism is not efficiency. Highest priority, most thought required.
2. **Dashboard headline bar** — currently shows the latest `score.update` per
   dimension, which for a multi-task session can be an arbitrary per-task final rather
   than the session aggregate (`list_scores` has no `ORDER BY`). Prefer `task_id IS NULL`.
3. **Flat session confidence** — the session-level aggregate hardcodes confidence 0.5
   instead of computing a real weighted confidence.
4. **NULL-task dedup** — `scores` upsert `ON CONFLICT` skips NULL `task_id` rows, so
   re-scoring a session would duplicate session-level rows (sessions score once in v0).
5. **Abandoned-session scoring** — post-hoc only triggers on explicit end / last-task
   submit; idle-abandoned sessions are never scored.
6. (minor) PQ4 could stop treating a bare language tag ("in Python") as a qualifying
   constraint; prompt-caching judge calls (~50% input cost cut, §P.6.2).

---

## 4. Lessons learned

**429s masquerade as UNCLEAR (Day 6 calibration).** The first calibration run looked
disastrous — VB3 at 40%, IE2 at 40%, UNCLEARs everywhere. It read like the judge
questions were broken. The tell was **empty-evidence UNCLEARs** plus a `call_count`
(118) well below the number of fixtures (180): the `judge()` UNCLEAR fallback was
firing on **rate-limit (429) errors at concurrency 8**, not on genuine model
uncertainty. A sequential probe (1 req/s) returned correct YES/NO answers immediately.
Fix: add the spec's §P.4 exponential backoff `[2s, 10s, 30s]` to `judge()` and drop
calibration concurrency to 4. The clean re-run hit ≥90% on all nine questions.
*Takeaway:* when LLM results look systematically wrong, rule out transient throttling
(empty fallbacks, call counts below expected) **before** concluding the prompt or model
is bad.

**Real runs find what tests can't.** Two issues only surfaced by running the actual
app, not the test suite: (a) the candidate editor came up **empty** when Monaco
finished loading after `task.presented` (the replay scrubber made it visible — a
`pre_chat` snapshot with `code_len=0`); (b) the Iteration inversion, which needed a
real good-vs-careless dogfood to expose. Browser verification and live dogfooding
earned their place in the process.

---

## 5. Deployment Migration

Moving from a local-only POC to a public Fly.io deployment (no auth, public URL).
This section tracks what changed and why, phase by phase.

### Phase 1 — Judge0 sandbox swap

**What changed.** The local `subprocess` runner (`python -I` + POSIX rlimits + temp
cwd) was replaced by a Judge0 CE HTTP client (`app/sandbox/runner.py`). Code is POSTed
to the Judge0 free tier (RapidAPI) in synchronous (`wait=true`) mode and the response
is mapped onto the **unchanged** `ExecResult` shape and `Sandbox.run_python` signature —
callers (`app/ws/candidate.py`, `app/main.py`) needed zero changes. Status mapping:
`3→exit 0`, `5→124` (TLE), `6→1` (compile error, from `compile_output`), `7–12→` the
process exit code / `128+signal` (runtime errors), anything else → `-1`. New enforced
input caps: code > 50 KB and stdin > 10 KB are rejected before a call is spent. Upstream
HTTP errors / timeouts / non-2xx / non-JSON all map to `exit_code=-1` with an explaining
`stderr`. `httpx` was promoted from a dev/transitive dep to a runtime dependency (no new
package — already present). Tests rewritten to mock the HTTP layer with
`httpx.MockTransport`; one `@pytest.mark.live` test hits real Judge0.

**Why.** The local subprocess sandbox has no network/filesystem/import isolation and was
explicitly "not production-safe" — unacceptable for a public URL. Offloading execution to
Judge0 removes candidate code execution from our box entirely.

**Tradeoffs.**
- **External dependency.** Execution now depends on a third-party service being up and on
  the RapidAPI key being valid. A Judge0 outage = no code runs (degrades to `exit_code=-1`,
  surfaced to the candidate rather than crashing).
- **Latency.** A run is now a network round-trip versus a local subprocess (tens of ms).
  **Observed end-to-end per-call latency: ~0.7–1.2 s** (synchronous `wait=true`, measured
  against the live free tier). Acceptable for an interview-pace "Run" button.
- **Free-tier quota.** 50 calls/day on the free CE tier — fine for demos, not for load.
- **Memory cap not honored per-call.** `mem_limit_mb` stays in the signature for interface
  compatibility but is not forwarded; the free tier enforces its own default memory cap.
  (The old cap was already a no-op on macOS, so no regression in practice.)

**Live verification (against the real Judge0 free tier).** The mocked contract held up:
`print('hello')` → `exit_code=0`, `stdout='hello\n'`; a basic loop returned the right sum.
Driving the **running server's candidate WebSocket** end-to-end (session → `code.run` →
`exec.result`) also passed, executing via Judge0 and returning correct output. One
contract wrinkle worth recording: a RapidAPI account that has generated a key but has
**not subscribed** to the specific Judge0 CE API returns **HTTP 403
`"You are not subscribed to this API."`** — the key authenticates but every call is
rejected pre-execution (so it doesn't burn quota). Our upstream-failure path mapped this
cleanly to `exit_code=-1` with the message in `stderr`, exactly as the mocked
`test_upstream_non_2xx_maps_to_minus_one` predicted. Verification consumed **4** of the
day's 50 free-tier calls (1 live test + 2 latency probes + 1 live WS session); the
earlier 403s did not count.

### Phase 2 — Cost cap enforcement + per-IP rate limiting

**What changed.** The §P.6.1 budget caps were nominal (values in Settings, never
enforced); the `llm_calls` cost-log table existed but was never written. Phase 2 makes
both real and adds public-deploy defenses.

- **Cost logging is now wired.** Every chat and judge call records one `llm_calls` row
  (`app/storage/llm_calls.py`); the judge client gained an injected `cost_sink` so it can
  log per call without holding a DB handle. Caps are enforced from these durable counts,
  not in-memory counters that reset on reconnect.
- **Per-session caps** (refuse the action, no LLM/Judge0 call): chat input tokens
  (`session_max_chat_tokens_in`, sums `llm_calls`), code executions
  (`session_max_code_executions=30`, counts `CODE_EXECUTED` events), and judge calls
  (`session_max_judge_calls`). When a cap truncates judge scheduling or chat is over
  budget, the score evidence carries `cost_capped=true` (§P.6.1). The candidate sees a
  `chat.capped` message or a `session execution limit reached` exec result.
- **Global daily caps** (NEW, no spec counterpart): `global_max_sessions_per_day=50`
  (checked at `POST /api/session` → 429) and `global_max_judge_calls_per_day=1000`
  (checked before judge scheduling). These are the last line of budget defense for a
  public URL — date-filtered queries over `sessions`/`llm_calls` (UTC day), no extra
  table or counter to race on.
- **Per-IP rate limiting.** slowapi (new dep, approved) throttles `POST /api/session`
  (`5/hour`); the candidate WebSocket can't go through slowapi, so chat (`20/min`) and
  code-run (`60/min`) messages use an in-handler in-memory sliding window. Client IP comes
  from `X-Forwarded-For` **only** when `trust_proxy_headers` is set (behind Fly's edge);
  off-platform it's the socket peer, since XFF is attacker-controlled.
- **New public endpoints.** `/api/healthz` (always 200, global counters only — no
  per-session info, for Fly health checks) and `/api/status` (`ok|degraded|capped`; the
  candidate UI checks it on load and shows a "demo is at capacity, try tomorrow" message
  when capped).

**Why these numbers.** With $10 ceilings at each provider and no auth, the caps are sized
so a single abusive session can't drain the budget and a botless flood is bounded:
50 sessions/day × bounded chat tokens + ≤30 Judge0 runs each stays well under $10, and
1000 judge calls/day is the hard ceiling on the most expensive (Sonnet) spend. Per-IP
limits raise the cost of scripted abuse; they're not sufficient alone (trivial IP
rotation defeats them — see below), which is exactly why the global daily caps exist as
the real backstop.

**Threat-model note (public, no auth).** Per-IP throttling is necessary but weak: an
attacker rotating IPs bypasses it. The load-bearing protection is the **global daily
caps**, which are IP-independent and enforced from durable DB state. The honest residual
risk: a distributed flood can still exhaust the *day's* free allotment (denying the demo
to others until UTC midnight) without exceeding spend — acceptable for a POC, called out
here so it isn't a surprise.

**Smoke (local).** 6 rapid `POST /api/session` from one IP → first 5 `200`, 6th `429`
with `X-RateLimit-*` + `Retry-After: 3600` headers. `/api/status` `ok`, `/api/healthz`
200 exposing only `{sessions_today, judge_calls_today, ...}`. 13 new unit/integration
tests cover each cap and both rate limiters; the existing end-to-end test still passes
(caps don't break the happy path). No Judge0/LLM spend — session creation makes no model
calls.

### Phase 3 — Containerization + Fly.io config (artifacts only, not deployed)

**What changed.** Added the build artifacts to ship to Fly: a multi-stage
[`Dockerfile`](Dockerfile), [`.dockerignore`](.dockerignore), [`fly.toml`](fly.toml),
[`docker-entrypoint.sh`](docker-entrypoint.sh), and a [`deploy/README.md`](deploy/README.md)
runbook. No deploy happened — that's Phase 4.

- **Image.** Stage 1 installs runtime deps into a venv with uv (`uv sync --frozen
  --no-dev --no-install-project` — deps only, project imported from source via
  `PYTHONPATH`, no hatchling build needed); stage 2 (`python:3.12-slim`) copies the venv
  + `app/`/`static/`/`tasks/`. The default `DB_PATH=/data/events.db` and
  `TASKS_DIR=/app/tasks` are baked as ENV; locally they fall back to the repo-relative
  defaults. Rootfs is effectively read-only at runtime; only the `/data` volume is written.
- **Config.** No code change was needed for env overrides — pydantic-settings already
  maps `DB_PATH`/`TASKS_DIR`/`TRUST_PROXY_HEADERS` to the matching fields (no direct
  `os.environ` reads, per the dual-account rule). `db.py` now `mkdir(parents=True,
  exist_ok=True)`s the DB's parent so an empty Fly volume works on first boot.
- **Non-root + volume ownership (the wrinkle).** Fly mounts the volume root-owned on
  first boot, but we want the app to run unprivileged. Resolution: the container starts as
  root so `docker-entrypoint.sh` can `chown /data`, then drops to UID 1000 via `gosu`
  before exec'ing uvicorn. The *app process* is non-root; only the brief entrypoint is
  root. Documented in the runbook with the alternative (run fully as root) called out.
- **`fly.toml`.** `shared-cpu-1x` / 256 MB (smallest, flagged as tight — bump to 512 MB if
  it OOMs), scale-to-zero (`min_machines_running = 0`), `force_https`, a `/api/healthz`
  check (30s interval, 30s grace), the `ai_usage_data` → `/data` mount, and
  `TRUST_PROXY_HEADERS=true` so per-IP limits key on the real XFF client IP behind Fly.

**Tests.** `test_deploy.py` asserts config honors `DB_PATH`/`TASKS_DIR`/`TRUST_PROXY_HEADERS`
env overrides and falls back to local defaults, plus a `@pytest.mark.docker` image-build
test (excluded from the default gate, skipped without Docker). CI now runs
`pytest -m "not live and not docker"`.

**Honest gap.** Docker isn't installed on this dev machine, so I could **not** build the
image to verify the Dockerfile end-to-end. It's reasoned carefully but unproven; the build
test will run in Phase 4 (and CI on a runner with Docker, if we choose to enable it).
First real deploy must watch for: the volume-ownership chown working, 256 MB not OOMing,
and `/data/events.db` being writable as UID 1000.

**Build break #1 (the unproven Dockerfile bit it).** The first `fly deploy` build failed:
the Dockerfile builder stage requires **curl + ca-certificates** because `python:3.12-slim`
is minimal and the uv install script curls the uv binary over HTTPS (and needs the certs to
verify). Fix: `apt-get install -y --no-install-recommends curl ca-certificates` in the
builder before the install. *Future improvement:* switch the builder to the official uv
Docker image (`ghcr.io/astral-sh/uv`), which has uv preinstalled — eliminates the bootstrap
step (and this whole class of failure) entirely. This is exactly the gap the "couldn't
build locally" note flagged would surface on first deploy.

### Phase 4 — Deployed (live)

- **URL:** https://ai-usage-scoring.fly.dev/
- **Deployed:** 2026-06-11 (Fly.io, region `ord`, single `shared-cpu-1x`/256 MB machine,
  `min_machines_running = 1`).
- **Smoke (SmokeTest2), end-to-end on the live deploy:** chat → run (`olleh`) → submit →
  end → **synchronous post-hoc completed**. Final scores `prompt_quality=47`,
  `verification=64`, `iteration=70`; **11 Anthropic judge calls**, every dimension citing
  real event seqs with judge evidence quotes; dashboard clickable citation → replay
  scrubber jump verified. **Total spend ≈ $0.10** (≈$0.08–0.12 Anthropic, ~$0.001 OpenAI,
  1 Judge0 call). Judge0 usage confirmed 1/50 for the day.

**The deployment lesson (the big one).** The first smoke failed at session-end + scoring,
and it *looked* like a code bug (orphaned post-hoc, dropped WebSocket). The real proximate
cause was **Fly's free-trial 5-minute machine cap**: the machine suspended mid-session,
which dropped the WS (so `session.end` never arrived) *and* raced the deploy's bind check
(the "not listening on 0.0.0.0:8000" warning). Adding billing — keeping the machine warm —
cleared both. **A platform billing/trial limit masqueraded as an application bug. Check
platform limits early in diagnosis, not late.** That said, the fix earned its keep
independently: fire-and-forget `asyncio.create_task` post-hoc was genuinely fragile (it
orphans on any redeploy/OOM regardless of the trial cap), so synchronous post-hoc +
120s `wait_for` + WS keepalive + warm machine is the correct hardening either way.

**Known limitations of the public version.**
- **Judge0 free tier:** 50 code-execution calls/day, **shared across all users** — a busy
  day exhausts it for everyone.
- **No auth:** anyone with the link can use it (the rate limits + global caps are the only
  guardrails; per-IP throttling is defeatable by IP rotation — the daily caps are the real
  backstop).
- **Session auto-creates on candidate page load:** every visit burns one of the 50 daily
  session slots even with no task done (v0.1: a "Try the demo" landing gate).
- **No per-user privacy:** all sessions are visible on the shared dashboard.
- **SQLite on a single Fly volume:** no replication. Redeploys preserve the volume, but
  there's no backup — volume loss/corruption loses all data.

### v0.1 backlog added during Phase 4

- **Landing-page "Try the demo" gate** so the candidate page stops auto-creating a session
  on every visit (stops casual visits from eating the 50/day cap).
- **Defer the OpenAI/Anthropic health probes off the blocking startup path** so uvicorn
  binds `:8000` immediately — eliminates the cosmetic "app is not listening" deploy warning
  caused by startup latency.
- **Persistent "pending-scoring" marker + startup recovery sweep** (Option B): make post-hoc
  self-heal across redeploys/OOMs, closing the last gap that synchronous scoring (Option A)
  leaves open.
- **Rotate the Judge0 RapidAPI key** (see §6 — the Phase 1 housekeeping note).

---

## 6. Security housekeeping (TODO list — clear before/at end of migration)

- **Rotate the Judge0 RapidAPI key.** During Phase 1 setup the key was briefly pasted
  into `.env` without its `JUDGE0_API_KEY=` prefix, which caused an inspection command to
  echo the raw value into the session transcript. Low stakes (free-tier key, 50/day cap,
  `.env` is gitignored so it never reached git, repo verifies clean) — but the value was
  exposed in plaintext. **Action:** regenerate the RapidAPI key as a final step after the
  Phase 4 deploy and update the Fly secret. Tracked so it isn't forgotten.
