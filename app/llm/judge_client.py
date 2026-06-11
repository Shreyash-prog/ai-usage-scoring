"""AnthropicJudgeClient — post-hoc scoring judge (PROVIDER_SPEC §P.3.3, §P.3.4).

Structured output is enforced via forced tool use: a single `submit_judgment` tool
is defined and `tool_choice` forces it, so the model must return a YES/NO/UNCLEAR
verdict plus a short evidence quote. On a missing/invalid tool_use block we retry
once; on a second failure we return UNCLEAR (never raise into the scorer).

Token usage is accumulated on the instance for cost tracking (PROVIDER_SPEC §P.6).
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Literal

from anthropic import APIStatusError, AsyncAnthropic, RateLimitError
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

# Records one judge call: (session_id, provider, model, purpose,
# prompt_tokens, completion_tokens, latency_ms, status) -> awaitable.
CostSink = Callable[[str, str, str, str, int, int, int, str], Awaitable[None]]

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
    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_s: int,
        max_retries: int,
        cost_sink: CostSink | None = None,
    ) -> None:
        self._client = AsyncAnthropic(api_key=api_key, timeout=timeout_s)
        self._model = model
        self._max_retries = max_retries
        self._cost_sink = cost_sink
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.call_count = 0

    def set_cost_sink(self, cost_sink: CostSink) -> None:
        """Wire cost logging after construction (the DB isn't available at init)."""
        self._cost_sink = cost_sink

    async def health(self) -> bool:
        """Liveness probe via the models-list endpoint (no generation, no budget)."""
        try:
            await self._client.models.list()
            return True
        except Exception:
            return False

    async def judge(
        self,
        prompt: str,
        temperature: float = 0.1,
        *,
        session_id: str | None = None,
        purpose: str = "judge",
    ) -> JudgeAnswer:
        """Send a judge prompt to Sonnet and return a structured YES/NO/UNCLEAR verdict.

        Retries on rate-limit/overload with exponential backoff (§P.4) and once on a
        missing/invalid tool_use block (§P.3.4). Falls back to UNCLEAR — never raises.

        When a `cost_sink` is wired and `session_id` is given, records exactly one
        cost-log row per call (usage summed across internal retries) so the per-session
        and global judge caps can be enforced from durable state (§P.6.1).
        """
        start = time.monotonic()
        call_input = call_output = 0
        status = "ok"
        result: JudgeAnswer | None = None
        for delay in _BACKOFF_S:
            if delay:
                await asyncio.sleep(delay)
            try:
                answer, in_tok, out_tok = await self._judge_once(prompt, temperature)
                call_input += in_tok
                call_output += out_tok
                if answer is not None:
                    result = answer
                    break
                # No/invalid tool_use block: retry once more, then fall through.
            except RateLimitError:
                status = "rate_limited"
                continue  # back off and retry
            except APIStatusError as exc:
                if exc.status_code in _RETRYABLE_STATUS:
                    status = "rate_limited"
                    continue
                logger.exception("Judge API error (status %s)", exc.status_code)
                status = "error"
                break
            except Exception:
                logger.exception("Judge call failed")
                status = "error"
                break

        if result is None:
            result = JudgeAnswer(answer="UNCLEAR", evidence="")
            if status == "ok":
                status = "error"  # exhausted retries without a valid verdict

        if self._cost_sink is not None and session_id is not None:
            latency_ms = int((time.monotonic() - start) * 1000)
            try:
                await self._cost_sink(
                    session_id,
                    "anthropic",
                    self._model,
                    purpose,
                    call_input,
                    call_output,
                    latency_ms,
                    status,
                )
            except Exception:
                logger.exception("Judge cost logging failed (non-fatal)")
        return result

    async def _judge_once(
        self, prompt: str, temperature: float
    ) -> tuple[JudgeAnswer | None, int, int]:
        """One API call. Returns (verdict-or-None, input_tokens, output_tokens)."""
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

        in_tok, out_tok = resp.usage.input_tokens, resp.usage.output_tokens
        for block in resp.content:
            if block.type == "tool_use" and block.name == "submit_judgment":
                try:
                    return JudgeAnswer(**block.input), in_tok, out_tok
                except ValidationError:
                    logger.warning("submit_judgment returned invalid input: %r", block.input)
                    return None, in_tok, out_tok
        return None, in_tok, out_tok  # no tool_use block -> caller retries
