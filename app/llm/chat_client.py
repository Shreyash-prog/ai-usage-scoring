"""OpenAIChatClient — the candidate's streaming chat assistant.

Supersedes the chat half of main spec §7 per PROVIDER_SPEC §P.3.2. Streams
tokens so the candidate sees them as they arrive (CLAUDE.md reminder); the final
chunk carries usage token counts via `stream_options.include_usage`.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Literal

from openai import APITimeoutError, AsyncOpenAI, RateLimitError
from pydantic import BaseModel


class LLMTimeout(Exception):
    """Raised when a chat completion exceeds its timeout."""


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatChunk(BaseModel):
    text: str
    done: bool
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""


class OpenAIChatClient:
    def __init__(self, api_key: str, model: str, timeout_s: int) -> None:
        self._client = AsyncOpenAI(api_key=api_key, timeout=timeout_s)
        self._model = model

    async def health(self) -> bool:
        """Cheap liveness probe: a 1-token completion (PROVIDER_SPEC §P.9).

        Deliberately tiny — a single output token, not a full chat — to keep the
        startup check effectively free.
        """
        try:
            await self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
            )
            return True
        except Exception:
            return False

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        temperature: float = 0.7,
    ) -> AsyncIterator[ChatChunk]:
        """Stream tokens from a chat completion.

        Yields one ChatChunk per token delta, then a final chunk with done=True
        carrying prompt/completion token counts from the usage payload. Raises
        LLMTimeout on timeout; retries once on a 429 before re-raising.
        """
        wire = [m.model_dump() for m in messages]
        try:
            stream = await self._client.chat.completions.create(  # type: ignore[call-overload]
                model=self._model,
                messages=wire,
                temperature=temperature,
                stream=True,
                stream_options={"include_usage": True},
            )
        except RateLimitError:
            await asyncio.sleep(2)
            stream = await self._client.chat.completions.create(  # type: ignore[call-overload]
                model=self._model,
                messages=wire,
                temperature=temperature,
                stream=True,
                stream_options={"include_usage": True},
            )
        except APITimeoutError as exc:
            raise LLMTimeout(str(exc)) from exc

        prompt_tokens = 0
        completion_tokens = 0
        try:
            async for event in stream:
                if event.usage is not None:
                    prompt_tokens = event.usage.prompt_tokens
                    completion_tokens = event.usage.completion_tokens
                delta = ""
                if event.choices:
                    delta = event.choices[0].delta.content or ""
                if delta:
                    yield ChatChunk(text=delta, done=False, model=self._model)
        except APITimeoutError as exc:
            raise LLMTimeout(str(exc)) from exc

        yield ChatChunk(
            text="",
            done=True,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=self._model,
        )
