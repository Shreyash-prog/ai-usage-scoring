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
