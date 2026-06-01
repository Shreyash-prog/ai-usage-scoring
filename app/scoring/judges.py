"""Judge question wrappers (main spec §10.2, §10.3).

Loads the per-question templates from the prompt files, fills in transcript fields,
wraps them in the shared preamble, and dispatches to AnthropicJudgeClient.judge.

Adaptation for forced tool use (PROVIDER_SPEC §P.3.4): the §10.3 wrapper's trailing
"respond ONLY with valid JSON {...}" instruction is dropped — the submit_judgment
tool already guarantees the structure, and the literal JSON braces would otherwise
collide with str.format. The conservative/UNCLEAR guidance is kept.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from app.llm.judge_client import AnthropicJudgeClient

_PROMPTS_DIR = Path(__file__).parent.parent / "llm" / "prompts"
_HEADER = re.compile(r"^\[(\w+)\]\s*$")

JUDGE_PREAMBLE = (
    "You are evaluating a software engineering interview transcript.\n"
    "Answer ONE specific yes/no question based on the evidence provided.\n"
    "Be conservative: if the evidence is ambiguous, answer UNCLEAR.\n"
    "Quote a brief piece of evidence (max 280 chars) supporting your answer.\n"
)


def _load_templates(filename: str) -> dict[str, str]:
    """Parse a prompt file of `[QID]`-delimited question templates."""
    text = (_PROMPTS_DIR / filename).read_text(encoding="utf-8")
    templates: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        m = _HEADER.match(line)
        if m:
            if current is not None:
                templates[current] = "\n".join(buf).strip()
            current = m.group(1)
            buf = []
        elif line.lstrip().startswith("#"):
            continue  # comment line — never sent to the model
        elif current is not None:
            buf.append(line)
    if current is not None:
        templates[current] = "\n".join(buf).strip()
    return templates


TEMPLATES: dict[str, str] = {
    **_load_templates("judge_prompt_quality.txt"),
    **_load_templates("judge_verification.txt"),
    **_load_templates("judge_iteration.txt"),
}

# Which dimension each question feeds into (§10.2).
QUESTION_DIMENSION = {
    "PQ1": "prompt_quality",
    "PQ2": "prompt_quality",
    "PQ3": "prompt_quality",
    "PQ4": "prompt_quality",
    "VB1": "verification",
    "VB2": "verification",
    "VB3": "verification",
    "IE1": "iteration",
    "IE2": "iteration",
}


@dataclass
class JudgeResult:
    question_id: str
    answer: str  # YES | NO | UNCLEAR
    evidence: str
    target_seq: int


def build_prompt(qid: str, **fields: str) -> str:
    """Fill a question template and prepend the shared preamble."""
    question = TEMPLATES[qid].format(**fields)
    return f"{JUDGE_PREAMBLE}\n{question}"


async def judge_question(
    client: AnthropicJudgeClient, qid: str, target_seq: int, **fields: str
) -> JudgeResult:
    prompt = build_prompt(qid, **fields)
    answer = await client.judge(prompt)
    return JudgeResult(
        question_id=qid, answer=answer.answer, evidence=answer.evidence, target_seq=target_seq
    )
