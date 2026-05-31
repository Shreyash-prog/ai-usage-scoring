"""Day 1 live smoke check (main spec §17 + PROVIDER_SPEC §P.9).

Hits real APIs, so it lives outside the pytest suite. It performs ONLY:
  - OpenAI health (a single output token) + a short streamed completion to stdout.
  - Anthropic health (models-list endpoint).

It makes NO real judge() calls — that is gated behind Day 6 calibration to
protect the API budget. Run with `make smoke` once .env is populated.
"""

import asyncio

from app.config import settings
from app.llm.chat_client import ChatMessage, OpenAIChatClient
from app.llm.judge_client import AnthropicJudgeClient


async def main() -> None:
    chat = OpenAIChatClient(
        settings.openai_api_key, settings.openai_chat_model, settings.openai_chat_timeout_s
    )
    judge = AnthropicJudgeClient(
        settings.anthropic_api_key,
        settings.anthropic_judge_model,
        settings.anthropic_judge_timeout_s,
        settings.anthropic_judge_max_retries,
    )

    print("OpenAI health:", await chat.health())
    print("Anthropic health:", await judge.health())

    print(f"\nStreamed chat (model={settings.openai_chat_model}):")
    messages = [
        ChatMessage(role="system", content="You are concise."),
        ChatMessage(role="user", content="Say 'hello from day 1' and nothing else."),
    ]
    async for chunk in chat.chat_stream(messages, temperature=0.0):
        if chunk.done:
            print(
                f"\n[done] prompt_tokens={chunk.prompt_tokens} "
                f"completion_tokens={chunk.completion_tokens}"
            )
        else:
            print(chunk.text, end="", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
