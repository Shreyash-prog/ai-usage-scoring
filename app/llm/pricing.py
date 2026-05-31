"""Hardcoded per-model pricing and a cost estimator (PROVIDER_SPEC §P.6.4).

Update these rates when provider pricing changes. Models absent from the table
estimate to $0 rather than raising — cost logging must never break a call.
"""

PRICING_USD_PER_MTOK: dict[str, dict[str, float]] = {
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    # add more as needed
}


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    rates = PRICING_USD_PER_MTOK.get(model, {"input": 0.0, "output": 0.0})
    return (prompt_tokens * rates["input"] + completion_tokens * rates["output"]) / 1_000_000
