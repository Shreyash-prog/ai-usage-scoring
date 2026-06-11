# Deploying ai-usage-scoring to Fly.io

Operational guide for the public deployment. Build artifacts live at the repo
root: [`Dockerfile`](../Dockerfile), [`.dockerignore`](../.dockerignore),
[`fly.toml`](../fly.toml). **No deploy has happened yet** — this documents the
Phase 4 procedure.

> ⚠️ Public, no auth. The budget defenses (per-IP rate limits + global daily caps,
> Phase 2) and the $10 provider spend ceilings are the only things between this URL
> and a drained budget. Don't relax them.

## Prerequisites (one-time)

```bash
fly auth login
fly apps create ai-usage-scoring          # then set the same name as `app` in fly.toml
fly volumes create ai_usage_data --region ord --size 1   # 1 GB SQLite volume
```

## Required secrets

These load via pydantic Settings from the process environment (Fly injects
secrets as env vars) — never baked into the image, never committed. Set all three:

```bash
fly secrets set \
  OPENAI_API_KEY=sk-... \
  ANTHROPIC_API_KEY=sk-ant-... \
  JUDGE0_API_KEY=...          # RapidAPI key for judge0-ce
```

`TRUST_PROXY_HEADERS=true` is already set in `fly.toml` `[env]` (not secret) so
per-IP rate limiting keys on the real client IP from `X-Forwarded-For` behind
Fly's edge. `DB_PATH=/data/events.db` and `TASKS_DIR=/app/tasks` are also in
`[env]`.

## First-time deploy

```bash
fly deploy                    # builds the Dockerfile, releases a machine
fly status                    # confirm the machine is running + healthy
fly logs                      # tail logs (startup health line, request logs)
curl https://<app>.fly.dev/api/healthz   # should return 200 with daily counters
```

The `/api/healthz` check in `fly.toml` (GET every 30s, 30s grace) gates machine
health. `auto_stop_machines`/`min_machines_running = 0` means the machine scales to
zero when idle and cold-starts on the next request (a few seconds of first-request
latency — acceptable for a demo, and it keeps idle cost near zero).

## Rollback

```bash
fly releases                  # list releases with version numbers
fly deploy --image <previous-image-ref>   # or:
fly releases rollback         # roll back to the previous release
```

## Logs & debugging

```bash
fly logs                      # live tail
fly ssh console               # shell into the running machine
fly ssh console -C "ls -la /data"   # inspect the SQLite volume
```

## Cost expectations

- **VM:** `shared-cpu-1x` / 256 MB, scaled to zero when idle → roughly **$2–5/month**
  depending on traffic (idle is near-free).
- **Volume:** 1 GB → ~**$0.15/month**.
- **Providers:** capped at **$10 each** (OpenAI + Anthropic) at the provider console;
  the app's own per-session + global daily caps keep usage well under that.
- **Judge0:** free CE tier (~50 calls/day) — the hard ceiling on code executions.

Ballpark: **~$5/month** for the small VM + volume, plus metered LLM spend bounded
by the caps.

## Operational notes / risks to verify in Phase 4

- **256 MB is tight** for Python + uvicorn + the SQLite working set. If the machine
  OOMs (watch `fly logs` for OOM kills), bump `memory` in `fly.toml` to `512mb`.
- **Non-root + volume ownership.** The app runs as UID 1000, but the container
  starts as root so `docker-entrypoint.sh` can `chown /data` (Fly mounts the volume
  root-owned on first boot) before dropping privileges via `gosu`. Verify on first
  deploy that the app can write `/data/events.db` (no permission error in logs).
- **Rotate the Judge0 RapidAPI key** after deploy (see FINDINGS "Security
  housekeeping") and update it with `fly secrets set JUDGE0_API_KEY=...`.
- **Single instance only.** The WS sliding-window rate limiter and in-memory state
  are per-process; this is fine for the single small instance we deploy. Scaling out
  would require shared state.
