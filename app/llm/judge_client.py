"""AnthropicJudgeClient — post-hoc scoring judge.

Supersedes the judge half of main spec §7 per PROVIDER_SPEC §P.3.3.

DAY 1 SCOPE: class definition + health() only. `judge()` is a documented stub —
the forced-tool-use logic (PROVIDER_SPEC §P.3.4) lands on Day 6, gated behind the
≥90% calibration pre-flight. We make NO real judge() calls before then to protect
the API budget.
"""

from typing import Literal

from anthropic import AsyncAnthropic
from pydantic import BaseModel


class JudgeAnswer(BaseModel):
    answer: Literal["YES", "NO", "UNCLEAR"]
    evidence: str  # max 280 chars; may be empty if UNCLEAR


class AnthropicJudgeClient:
    def __init__(self, api_key: str, model: str, timeout_s: int, max_retries: int) -> None:
        self._client = AsyncAnthropic(api_key=api_key, timeout=timeout_s)
        self._model = model
        self._max_retries = max_retries

    async def health(self) -> bool:
        """Liveness probe via the models-list endpoint.

        Intentionally does NOT generate — no tokens, no judge call — so it is
        safe to run on every startup without touching the scoring budget.
        """
        try:
            await self._client.models.list()
            return True
        except Exception:
            return False

    async def judge(
        self,
        prompt: str,
        temperature: float = 0.1,
    ) -> JudgeAnswer:
        """Send a judge prompt to Sonnet and return a structured verdict.

        Implemented on Day 6 (PROVIDER_SPEC §P.3.4): defines a single
        `submit_judgment` tool, forces tool use for guaranteed structured output,
        and extracts the YES/NO/UNCLEAR verdict plus evidence. On schema violation
        it retries once; on a second failure it returns
        JudgeAnswer(answer="UNCLEAR", evidence="").
        """
        raise NotImplementedError("AnthropicJudgeClient.judge lands on Day 6.")
