"""AnthropicJudgeClient — post-hoc scoring judge (PROVIDER_SPEC §P.3.3, §P.3.4).

Structured output is enforced via forced tool use: a single `submit_judgment` tool
is defined and `tool_choice` forces it, so the model must return a YES/NO/UNCLEAR
verdict plus a short evidence quote. On a missing/invalid tool_use block we retry
once; on a second failure we return UNCLEAR (never raise into the scorer).

Token usage is accumulated on the instance for cost tracking (PROVIDER_SPEC §P.6).
"""

import asyncio
import logging
from typing import Literal

from anthropic import APIStatusError, AsyncAnthropic, RateLimitError
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

# Backoff schedule for rate-limit / overload (PROVIDER_SPEC §P.4). The leading 0 is
# the first immediate attempt; subsequent entries are the retry sleeps.
_BACKOFF_S = [0.0, 2.0, 10.0, 30.0]
_RETRYABLE_STATUS = {429, 500, 502, 503, 529}

JUDGE_TOOL = {
    "name": "submit_judgment",
    "description": "Submit your judgment about the question asked.",
    "input_schema": {
        "type": "object",
        "properties": {
            "answer": {
                "type": "string",
                "enum": ["YES", "NO", "UNCLEAR"],
                "description": "Your verdict on the question.",
            },
            "evidence": {
                "type": "string",
                "maxLength": 280,
                "description": "A brief quote from the transcript supporting your verdict. "
                "Empty if UNCLEAR.",
            },
        },
        "required": ["answer", "evidence"],
    },
}


class JudgeAnswer(BaseModel):
    answer: Literal["YES", "NO", "UNCLEAR"]
    evidence: str  # max 280 chars; may be empty if UNCLEAR


class AnthropicJudgeClient:
    def __init__(self, api_key: str, model: str, timeout_s: int, max_retries: int) -> None:
        self._client = AsyncAnthropic(api_key=api_key, timeout=timeout_s)
        self._model = model
        self._max_retries = max_retries
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.call_count = 0

    async def health(self) -> bool:
        """Liveness probe via the models-list endpoint (no generation, no budget)."""
        try:
            await self._client.models.list()
            return True
        except Exception:
            return False

    async def judge(self, prompt: str, temperature: float = 0.1) -> JudgeAnswer:
        """Send a judge prompt to Sonnet and return a structured YES/NO/UNCLEAR verdict.

        Retries on rate-limit/overload with exponential backoff (§P.4) and once on a
        missing/invalid tool_use block (§P.3.4). Falls back to UNCLEAR — never raises.
        """
        for delay in _BACKOFF_S:
            if delay:
                await asyncio.sleep(delay)
            try:
                answer = await self._judge_once(prompt, temperature)
                if answer is not None:
                    return answer
                # No/invalid tool_use block: retry once more, then fall through.
            except RateLimitError:
                continue  # back off and retry
            except APIStatusError as exc:
                if exc.status_code in _RETRYABLE_STATUS:
                    continue
                logger.exception("Judge API error (status %s)", exc.status_code)
                break
            except Exception:
                logger.exception("Judge call failed")
                break
        return JudgeAnswer(answer="UNCLEAR", evidence="")

    async def _judge_once(self, prompt: str, temperature: float) -> JudgeAnswer | None:
        resp = await self._client.messages.create(  # type: ignore[call-overload]
            model=self._model,
            max_tokens=512,
            temperature=temperature,
            tools=[JUDGE_TOOL],
            tool_choice={"type": "tool", "name": "submit_judgment"},
            messages=[{"role": "user", "content": prompt}],
        )
        self.call_count += 1
        self.total_input_tokens += resp.usage.input_tokens
        self.total_output_tokens += resp.usage.output_tokens

        for block in resp.content:
            if block.type == "tool_use" and block.name == "submit_judgment":
                try:
                    return JudgeAnswer(**block.input)
                except ValidationError:
                    logger.warning("submit_judgment returned invalid input: %r", block.input)
                    return None
        return None  # no tool_use block -> caller retries
