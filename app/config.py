"""Typed application settings.

Supersedes main spec §3 per PROVIDER_SPEC.md §P.2. Keys load from the project's
`.env` file (and, incidentally, the process environment) via pydantic-settings.
We never read `os.environ[...]` directly and never recommend exporting the keys
into the shell — see PROVIDER_SPEC §P.2 "Key Sourcing" and the dual-account note
in CLAUDE.md.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Server
    host: str = "127.0.0.1"
    port: int = 8000

    # Storage
    db_path: str = "./events.db"
    tasks_dir: str = "./tasks"

    # OpenAI (chat)
    openai_api_key: str  # required, from .env
    openai_chat_model: str = "gpt-4o-mini"
    openai_chat_timeout_s: int = 60
    openai_chat_max_history_messages: int = 40  # truncate older than this

    # Anthropic (judge)
    anthropic_api_key: str  # required, from .env
    anthropic_judge_model: str = "claude-sonnet-4-6"  # see PROVIDER_SPEC §P.7
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

    # Cost controls (PROVIDER_SPEC §P.6.1)
    session_max_judge_calls: int = 200
    session_max_chat_tokens_in: int = 200_000

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()  # type: ignore[call-arg]  # required keys come from .env at runtime
