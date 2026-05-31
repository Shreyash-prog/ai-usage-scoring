.PHONY: dev test lint fmt typecheck smoke reset

dev:
	uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

test:
	uv run pytest

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

typecheck:
	uv run mypy app

# Live LLM sanity check (PROVIDER_SPEC §P.9). Hits real APIs:
# OpenAI 1-token health + Anthropic models-list health. No judge() calls.
smoke:
	uv run python -m scripts.day1_smoke

# Wipe local state for a clean demo run.
reset:
	rm -f events.db events.db-*
