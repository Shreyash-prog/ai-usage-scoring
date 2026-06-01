"""Heuristic unit tests (main spec §18.1): each component in isolation (§9.2, §9.4)."""

from app.models.events import EventType, PersistedEvent
from app.models.tasks import Task
from app.scoring.heuristics import (
    PromptContext,
    clamp,
    contains_code_like,
    first_event_after,
    iteration_heuristic,
    jaccard_words,
    prompt_quality_heuristic,
    verification_heuristic,
)

# 16 neutral words: in the 15-300 band (+0), no trigger words, no code fence.
NEUTRAL = (
    "alpha bravo charlie delta echo foxtrot golf hotel "
    "india juliet kilo lima mike november oscar papa"
)


def ev(seq, type, payload=None, ts=None) -> PersistedEvent:
    return PersistedEvent(
        id=seq,
        session_id="s",
        seq=seq,
        ts=ts if ts is not None else seq * 1000,
        type=type,
        payload=payload or {},
        task_id="001",
    )


# --- helpers (§9.4) -------------------------------------------------------


def test_clamp():
    assert clamp(-5, 0, 100) == 0
    assert clamp(150, 0, 100) == 100
    assert clamp(42, 0, 100) == 42


def test_jaccard_words():
    assert jaccard_words("the quick brown fox", "the quick brown fox") == 1.0
    assert jaccard_words("alpha bravo charlie", "xray yankee zulu") == 0.0
    assert jaccard_words("", "") == 0.0  # empty union -> 0, not ZeroDivision


def test_contains_code_like():
    assert contains_code_like("def reverse(s):")
    assert contains_code_like("just some text\nreturn x")
    assert not contains_code_like("this is plain english")


def test_first_event_after():
    events = [
        ev(1, EventType.CHAT_PROMPT_SENT),
        ev(2, EventType.CODE_EXECUTED),
        ev(3, EventType.CHAT_PROMPT_SENT),
    ]
    assert first_event_after(events, 1).seq == 2
    assert first_event_after(events, 1, types=[EventType.CHAT_PROMPT_SENT]).seq == 3
    assert first_event_after(events, 3) is None


# --- prompt quality (§9.2.1) ----------------------------------------------


def test_pq_neutral_baseline():
    assert prompt_quality_heuristic(NEUTRAL, None, None, PromptContext()) == 50.0


def test_pq_too_short():
    # 2 words -> -25; no trigger words -> base 50 -> 25
    assert prompt_quality_heuristic("aa bb", None, None, PromptContext()) == 25.0


def test_pq_code_context_bonus():
    assert prompt_quality_heuristic(NEUTRAL, "x = " + "y" * 30, None, PromptContext()) == 60.0


def test_pq_inline_code_block_bonus():
    assert prompt_quality_heuristic(NEUTRAL + " ```", None, None, PromptContext()) == 55.0


def test_pq_error_included_bonus():
    ctx = PromptContext(recent_error="ZeroDivisionError: division by zero")
    text = NEUTRAL + " ZeroDivisionError: division by zero"
    assert prompt_quality_heuristic(text, None, None, ctx) == 70.0


def test_pq_error_not_shared_penalty():
    ctx = PromptContext(recent_error="ZeroDivisionError: division by zero")
    assert prompt_quality_heuristic(NEUTRAL, None, None, ctx) == 40.0


def test_pq_duplicate_penalty():
    ctx = PromptContext(prev_prompt_text=NEUTRAL)
    assert prompt_quality_heuristic(NEUTRAL, None, None, ctx) == 30.0


def test_pq_trigger_word_bonus():
    text = "how " + NEUTRAL  # adds a trigger word; still in band
    assert prompt_quality_heuristic(text, None, None, PromptContext()) == 55.0


# --- verification (§9.2.2) ------------------------------------------------


def test_verification_no_ai_usage_floor():
    events = [ev(1, EventType.CODE_EXECUTED, {"code": "print(1)"})]
    assert verification_heuristic(events) == 60.0


def test_verification_exec_after_chat():
    events = [
        ev(1, EventType.CHAT_RESPONSE_RECEIVED, {"text": "```\nprint(1)\n```"}),
        ev(2, EventType.CODE_EXECUTED, {"code": "print(1)"}),
    ]
    # comp1=40 (ran after chat), comp2=20 (no pastes baseline), comp3=0 (no test) -> 60
    assert verification_heuristic(events) == 60.0


def test_verification_test_run_bonus():
    events = [
        ev(1, EventType.CHAT_RESPONSE_RECEIVED, {"text": "```\nprint(1)\n```"}),
        ev(2, EventType.CODE_EXECUTED, {"code": "assert reverse('ab') == 'ba'"}),
    ]
    # comp1=40, comp2=20, comp3=20 -> 80
    assert verification_heuristic(events) == 80.0


def test_verification_paste_then_quick_run():
    events = [
        ev(1, EventType.CHAT_RESPONSE_RECEIVED, {"text": "```\nprint(1)\n```"}, ts=0),
        ev(2, EventType.EDITOR_PASTE, {"source_hint": "chat"}, ts=1_000),
        ev(3, EventType.CODE_EXECUTED, {"code": "print(1)"}, ts=2_000),
    ]
    # comp1=40, comp2=clamp(20+5)=25 (paste->exec within 30s), comp3=0 -> 65
    assert verification_heuristic(events) == 65.0


# --- iteration (§9.2.3) ---------------------------------------------------

TASK = Task(id="001", title="t", description_md="d", baseline_prompts=3)


def test_iteration_no_prompts():
    assert iteration_heuristic([ev(1, EventType.CODE_EXECUTED, {"code": "x"})], TASK) == 70.0


def test_iteration_clean_run_completed():
    events = [
        ev(1, EventType.CHAT_PROMPT_SENT, {"text": "alpha bravo charlie"}),
        ev(2, EventType.CHAT_PROMPT_SENT, {"text": "xray yankee zulu different"}),
        ev(3, EventType.TASK_SUBMITTED, {"final_code": "x"}),
    ]
    # comp1=40 (no redundancy), comp2=30 (n=2<=baseline 3), comp3=30 (completed) -> 100
    assert iteration_heuristic(events, TASK) == 100.0


def test_iteration_redundant_and_unfinished():
    events = [
        ev(1, EventType.CHAT_PROMPT_SENT, {"text": "please reverse the given string now"}),
        ev(2, EventType.CHAT_PROMPT_SENT, {"text": "please reverse the given string now"}),
    ]
    # redundancy_rate=1 -> comp1=0, comp2=30 (n=2<=3), comp3=0 (unfinished) -> 30
    assert iteration_heuristic(events, TASK) == 30.0
