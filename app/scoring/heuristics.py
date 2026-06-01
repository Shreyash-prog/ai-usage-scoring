"""Heuristic scoring formulas (main spec §9.2) and helpers (§9.4).

These are transcribed to the letter from the spec — the weights (e.g. verification
40+40+20) and thresholds are the contract. Do not "improve" them here; if one looks
wrong, surface it rather than silently changing it. The same functions run live
(§9) and post-hoc (§10.1b), so they must stay deterministic.
"""

import re
from dataclasses import dataclass

from app.models.events import EventType, PersistedEvent
from app.models.tasks import Task

# --- §9.4 helpers ---------------------------------------------------------

_CODE_LIKE = re.compile(r"^\s*(def |class |import |from .* import|for .* in |if .*:\s*$|return )")


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def jaccard_words(a: str, b: str) -> float:
    """Lowercase, tokenize on \\W+, drop tokens shorter than 3, |A∩B|/|A∪B|."""
    sa = {t for t in re.split(r"\W+", a.lower()) if len(t) >= 3}
    sb = {t for t in re.split(r"\W+", b.lower()) if len(t) >= 3}
    union = sa | sb
    if not union:
        return 0.0
    return len(sa & sb) / len(union)


def contains_code_like(text: str) -> bool:
    return any(_CODE_LIKE.match(line) for line in text.splitlines())


def first_event_after(
    events: list[PersistedEvent],
    seq: int,
    types: list[EventType] | None = None,
) -> PersistedEvent | None:
    for e in events:
        if e.seq > seq and (types is None or e.type in types):
            return e
    return None


# --- §9.2.1 Prompt Quality ------------------------------------------------


@dataclass
class PromptContext:
    recent_error: str | None = None
    prev_prompt_text: str | None = None
    task_type: str | None = None


def prompt_quality_heuristic(
    text: str, attached_code: str | None, attached_output: str | None, ctx: PromptContext
) -> float:
    score = 50.0  # neutral baseline

    # Length component (peak around 40-300 words)
    words = len(text.split())
    if words < 5:
        score -= 25
    elif words < 15:
        score -= 10
    elif words <= 300:
        score += 0
    elif words <= 600:
        score -= 5
    else:
        score -= 15

    # Code context
    if attached_code and len(attached_code) > 20:
        score += 10
    if "```" in text:  # inline code block
        score += 5

    # Error context: only matters if there WAS a recent error
    if ctx.recent_error:
        if ctx.recent_error[:80].lower() in text.lower()[:1000]:
            score += 20
        elif attached_output and ctx.recent_error[:80] in attached_output:
            score += 15
        else:
            score -= 10  # had an error, didn't share it

    # Imperative / question clarity (weak regex heuristic)
    if re.search(r"\b(how|why|what|fix|implement|write|debug|explain)\b", text.lower()):
        score += 5

    # Near-duplicate of immediately prior prompt
    if ctx.prev_prompt_text:
        sim = jaccard_words(text, ctx.prev_prompt_text)
        if sim > 0.85:
            score -= 20

    return clamp(score, 0, 100)


# --- §9.2.2 Verification Behavior ----------------------------------------


def verification_heuristic(task_events: list[PersistedEvent]) -> float:
    exec_events = [e for e in task_events if e.type == EventType.CODE_EXECUTED]
    chat_resp = [e for e in task_events if e.type == EventType.CHAT_RESPONSE_RECEIVED]
    pastes_from_chat = [
        e
        for e in task_events
        if e.type == EventType.EDITOR_PASTE and e.payload.get("source_hint") == "chat"
    ]

    if not chat_resp:
        # No AI usage. Verification isn't really applicable. Cap at 60.
        return 60.0

    # Component 1: exec-after-chat ratio
    chat_with_code = [
        r for r in chat_resp if "```" in r.payload["text"] or contains_code_like(r.payload["text"])
    ]
    runs_after = 0
    for r in chat_with_code:
        next_prompt = first_event_after(
            task_events, r.seq, types=[EventType.CHAT_PROMPT_SENT, EventType.TASK_SUBMITTED]
        )
        boundary = next_prompt.seq if next_prompt else 1_000_000_000
        if any(r.seq < e.seq < boundary for e in exec_events):
            runs_after += 1
    ratio = runs_after / len(chat_with_code) if chat_with_code else 1.0
    comp1 = 40 * ratio  # max 40

    # Component 2: paste-then-run latency
    paste_run_score = 0
    for p in pastes_from_chat:
        next_exec = first_event_after(exec_events, p.seq)
        if next_exec is None:
            paste_run_score -= 5
        else:
            dt = next_exec.ts - p.ts
            if dt < 30_000:
                paste_run_score += 5
            elif dt < 120_000:
                paste_run_score += 3
            else:
                paste_run_score += 0
    comp2 = clamp(20 + paste_run_score, 0, 40)  # baseline 20, max 40

    # Component 3: any test-like run?
    comp3 = (
        20
        if any(
            "test" in e.payload["code"].lower() or "assert" in e.payload["code"]
            for e in exec_events
        )
        else 0
    )

    return clamp(comp1 + comp2 + comp3, 0, 100)


# --- §9.2.3 Iteration Efficiency -----------------------------------------


def iteration_heuristic(task_events: list[PersistedEvent], task: Task) -> float:
    prompts = [e for e in task_events if e.type == EventType.CHAT_PROMPT_SENT]
    completion_event = next((e for e in task_events if e.type == EventType.TASK_SUBMITTED), None)

    if not prompts:
        return 70.0  # didn't use AI at all; neutral-positive

    # Component 1: redundancy penalty
    redundant = 0
    for i in range(1, len(prompts)):
        sim = jaccard_words(prompts[i].payload["text"], prompts[i - 1].payload["text"])
        if sim > 0.7:
            redundant += 1
    redundancy_rate = redundant / max(1, len(prompts) - 1)
    comp1 = 40 * (1 - redundancy_rate)  # max 40

    # Component 2: total prompts vs. task baseline
    n = len(prompts)
    baseline = task.baseline_prompts
    if n <= baseline:
        comp2 = 30
    elif n <= baseline * 2:
        comp2 = 20
    elif n <= baseline * 3:
        comp2 = 10
    else:
        comp2 = 0

    # Component 3: did they finish?
    comp3 = 30 if completion_event else 0

    return clamp(comp1 + comp2 + comp3, 0, 100)


def heuristic_coverage(task_events: list[PersistedEvent]) -> float:
    """1.0 when the task had >=3 chat prompts and >=1 exec event, else degraded (§10.4)."""
    prompts = sum(1 for e in task_events if e.type == EventType.CHAT_PROMPT_SENT)
    execs = sum(1 for e in task_events if e.type == EventType.CODE_EXECUTED)
    if prompts >= 3 and execs >= 1:
        return 1.0
    prompt_cov = min(prompts, 3) / 3.0
    exec_cov = 1.0 if execs >= 1 else 0.0
    return clamp(0.5 * prompt_cov + 0.5 * exec_cov, 0.0, 1.0)
