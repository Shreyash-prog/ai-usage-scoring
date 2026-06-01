"""Offline judge-infra tests (no API calls): template loading + prompt building."""

from app.scoring.judges import QUESTION_DIMENSION, TEMPLATES, build_prompt

ALL_QIDS = ["PQ1", "PQ2", "PQ3", "PQ4", "VB1", "VB2", "VB3", "IE1", "IE2"]


def test_all_questions_loaded():
    assert set(TEMPLATES) == set(ALL_QIDS)
    assert set(QUESTION_DIMENSION) == set(ALL_QIDS)


def test_build_prompt_fills_placeholders():
    out = build_prompt(
        "PQ2",
        recent_error="ZeroDivisionError",
        prompt_text="how do I fix this?",
        prompt_attached="ZeroDivisionError: division by zero",
    )
    assert "ZeroDivisionError" in out
    assert "how do I fix this?" in out
    assert "{" not in out  # all placeholders substituted
    assert out.startswith("You are evaluating")


def test_build_prompt_each_question_has_no_leftover_placeholders():
    # Minimal field set covering every placeholder used across templates.
    fields = dict(
        prompt_text="p",
        recent_error="e",
        prompt_attached="a",
        prompt_attached_code="c",
        response_excerpt="r",
        between_events_summary="b",
        stdout_stderr="o",
        next_prompt="n",
        ai_code="ai",
        executed_code="ex",
        prompt_1="p1",
        prompt_2="p2",
    )
    for qid in ALL_QIDS:
        out = build_prompt(qid, **fields)
        assert "{" not in out and "}" not in out, f"{qid} has leftover placeholder"
