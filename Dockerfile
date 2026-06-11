# Multi-stage build for ai-usage-scoring (Phase 3 deploy artifact).
#
# Stage 1 installs runtime deps into a venv with uv; stage 2 is a slim runtime
# that copies the venv + source. The app process runs as the unprivileged UID
# 1000 — but the container starts as root so the entrypoint can fix ownership of
# the Fly volume (mounted root-owned at /data on first boot) before dropping
# privileges with gosu. Only /data is writable at runtime; the rest is rootfs.

FROM python:3.12-slim AS builder

# Faster, hermetic installs: bytecode-compile and copy (no hardlinks across layers).
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Official uv installer.
ADD https://astral.sh/uv/install.sh /uv-install.sh
RUN sh /uv-install.sh && rm /uv-install.sh
ENV PATH="/root/.local/bin:${PATH}"

WORKDIR /app
# Deps only (cached unless the lock changes); the project itself isn't installed
# as a package — uvicorn imports it from the copied source via PYTHONPATH.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project


FROM python:3.12-slim AS runtime

# gosu drops privileges cleanly (correct signal/PID handling) in the entrypoint.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -u 1000 -m -s /bin/bash appuser

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY app/ ./app/
COPY static/ ./static/
COPY tasks/ ./tasks/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh && mkdir -p /data && chown appuser:appuser /data

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH="/app" \
    PYTHONUNBUFFERED=1 \
    DB_PATH="/data/events.db" \
    TASKS_DIR="/app/tasks"

EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
