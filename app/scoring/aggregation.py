"""Combine heuristic + judge components into final dimension scores (main spec §10.4).

YES is the "good" verdict for every judge question (clear goal, error included, ran
the code, meaningfully modified it, distinct prompts, changed approach after non-help),
so the judge component is the share of YES among non-UNCLEAR answers.
"""

from app.models.events import PersistedEvent
from app.scoring.heuristics import clamp, heuristic_coverage
from app.scoring.judges import JudgeResult


def aggregate(
    heuristic_score: float,
    judge_results: list[JudgeResult],
    task_events: list[PersistedEvent],
) -> tuple[float, float]:
    """Return (final_score, confidence) for one dimension on one task."""
    valid = [j for j in judge_results if j.answer != "UNCLEAR"]
    if valid:
        judge_score: float | None = 100.0 * sum(1 for j in valid if j.answer == "YES") / len(valid)
        judge_coverage = len(valid) / max(1, len(judge_results))
    else:
        judge_score = None
        judge_coverage = 0.0

    if judge_score is None:
        final = heuristic_score
        confidence = 0.5 * heuristic_coverage(task_events)
    else:
        # Judges are more semantic: heuristic 0.4, judge 0.6.
        final = 0.4 * heuristic_score + 0.6 * judge_score
        confidence = (
            0.30  # base
            + 0.40 * judge_coverage  # judge agreement on findings
            + 0.30 * heuristic_coverage(task_events)
        )

    return final, clamp(confidence, 0.0, 0.95)
