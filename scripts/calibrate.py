"""Day 6 judge calibration runner (main spec §18.3, threshold 90% per PROVIDER_SPEC §P.5).

Runs each hand-labeled fixture through the real AnthropicJudgeClient and reports
per-question agreement + disagreements + total cost. NOT a pytest test (it spends
money). Usage: `uv run python -m scripts.calibrate [QID ...]` (default: all 9).

Threshold: <=2 disagreements per 20 (>=90%). On failure, review the disagreements
by hand before deciding whether the label or the question is wrong.
"""

import asyncio
import json
import sys
from pathlib import Path

from app.config import settings
from app.llm.judge_client import AnthropicJudgeClient
from app.llm.pricing import estimate_cost
from app.scoring.judges import judge_question

FIX_DIR = Path(__file__).parent.parent / "tests" / "fixtures" / "calibration"
ALL_QIDS = ["PQ1", "PQ2", "PQ3", "PQ4", "VB1", "VB2", "VB3", "IE1", "IE2"]
_SEM = asyncio.Semaphore(4)


async def _run_one(client: AnthropicJudgeClient, qid: str, fx: dict) -> tuple:
    fields = {k: v for k, v in fx.items() if k not in ("id", "label")}
    async with _SEM:
        res = await judge_question(client, qid, target_seq=0, **fields)
    return fx["id"], fx["label"], res.answer, res.evidence


async def main(qids: list[str]) -> None:
    client = AnthropicJudgeClient(settings.anthropic_api_key, settings.anthropic_judge_model, 30, 1)
    overall_pass = True
    for qid in qids:
        fixtures = json.loads((FIX_DIR / f"{qid}.json").read_text())
        results = await asyncio.gather(*[_run_one(client, qid, fx) for fx in fixtures])
        disagreements = [r for r in results if r[1] != r[2]]
        agree = len(results) - len(disagreements)
        pct = 100 * agree / len(results)
        status = "PASS" if len(disagreements) <= 2 else "FAIL"
        overall_pass = overall_pass and status == "PASS"
        print(f"\n=== {qid}: {agree}/{len(results)} = {pct:.0f}%  [{status}]")
        for fid, label, ans, ev in disagreements:
            print(f"  DISAGREE {fid}: label={label} model={ans} :: {ev[:90]}")

    cost = estimate_cost(
        settings.anthropic_judge_model, client.total_input_tokens, client.total_output_tokens
    )
    print(
        f"\ncalls={client.call_count} in_tok={client.total_input_tokens} "
        f"out_tok={client.total_output_tokens} cost=${cost:.4f}"
    )
    print("OVERALL:", "PASS" if overall_pass else "FAIL")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or ALL_QIDS))
