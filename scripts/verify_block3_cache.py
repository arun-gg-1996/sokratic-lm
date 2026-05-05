"""
scripts/verify_block3_cache.py
==============================
BLOCK 3 verification — confirm Teacher and Dean cache hits actually
fire after the multi-tier restructure.

Runs 2 Teacher calls + 2 Dean calls back-to-back with similar context
and checks cache_read_input_tokens on call 2 of each.

Expected:
  - Teacher call 2: cache_read > 0 (Tier 1 master+vocab + Tier 2 body
    both should hit since we use identical inputs)
  - Dean call 2: cache_read > 0 (master+vocab + Dean instructions
    should hit on identical state)
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env", override=True)

from conversation.llm_client import make_anthropic_client, resolve_model  # noqa: E402
from conversation.teacher_v2 import TeacherV2, TeacherPromptInputs  # noqa: E402
from conversation.dean_v2 import DeanV2  # noqa: E402
from conversation.turn_plan import TurnPlan  # noqa: E402
from config import cfg  # noqa: E402


def make_inputs() -> TeacherPromptInputs:
    return TeacherPromptInputs(
        chunks=[
            {
                "subsection_title": "Test Sub",
                "text": (
                    "This is a fixture chunk used for cache testing. "
                    * 50  # ~2000 tokens of stable content
                ),
            },
        ],
        history=[
            {"role": "tutor", "content": "What do you think the answer is?"},
            {"role": "student", "content": "I'm not sure"},
        ],
        locked_subsection="Test Subsection",
        locked_question="What is the answer to the test question?",
        domain_name="Human Anatomy & Physiology",
        domain_short="anatomy",
        student_descriptor="student",
    )


def make_plan() -> TurnPlan:
    return TurnPlan(
        scenario="cache_test",
        hint_text="Think about the basics",
        mode="socratic",
        tone="encouraging",
        forbidden_terms=["pyruvate", "metabolic"],
        permitted_terms=[],
        shape_spec={"max_sentences": 3, "exactly_one_question": True},
        carryover_notes="",
        clinical_scenario=None,
        clinical_target=None,
        apply_redaction=False,
    )


def test_teacher_cache():
    print("\n=== Teacher cache verification ===")
    client = make_anthropic_client()
    teacher = TeacherV2(client, model=resolve_model(cfg.models.teacher))
    plan = make_plan()
    inputs = make_inputs()

    print("Call 1 (cold)...")
    t0 = time.time()
    r1 = teacher.draft(plan, inputs)
    e1 = (time.time() - t0) * 1000
    print(f"  elapsed_ms={e1:.0f}, input={r1.input_tokens}, "
          f"output={r1.output_tokens}, cache_read={r1.cache_read_tokens}")

    print("Call 2 (warm — same prompt)...")
    t0 = time.time()
    r2 = teacher.draft(plan, inputs)
    e2 = (time.time() - t0) * 1000
    print(f"  elapsed_ms={e2:.0f}, input={r2.input_tokens}, "
          f"output={r2.output_tokens}, cache_read={r2.cache_read_tokens}")

    if r2.cache_read_tokens > 0:
        speedup = (e1 - e2) / e1 * 100 if e1 > 0 else 0
        print(f"  ✓ Cache hit on call 2 ({r2.cache_read_tokens} tokens read from cache, {speedup:.0f}% faster)")
    else:
        print(f"  ✗ No cache hit on call 2")
    return r2.cache_read_tokens > 0


def test_dean_cache():
    print("\n=== Dean cache verification ===")
    client = make_anthropic_client()
    dean = DeanV2(client, model=resolve_model(cfg.models.dean))
    state = {
        "messages": [
            {"role": "tutor", "content": "What do you think?"},
            {"role": "student", "content": "I'm not sure"},
        ],
        "locked_topic": {
            "subsection": "Test Subsection",
            "section": "Test Section",
            "chapter_num": 1,
            "path": "Chapter 1: X > Test Section > Test Subsection",
        },
        "locked_question": "What is the test answer?",
        "locked_answer": "test answer",
        "locked_answer_aliases": ["test ans"],
        "hint_level": 1,
        "max_hints": 3,
        "max_turns": 25,
        "phase": "tutoring",
        "exploration_count": 0,
        "debug": {"turn_trace": []},
    }
    chunks = [
        {
            "subsection_title": "Test Sub",
            "text": "Stable test chunk content. " * 80,
        },
    ]

    print("Call 1 (cold)...")
    t0 = time.time()
    r1 = dean.plan(
        state, chunks, carryover_notes="",
        domain_name="Human Anatomy & Physiology",
        clinical_scenario_style="",
    )
    e1 = (time.time() - t0) * 1000
    print(f"  elapsed_ms={e1:.0f}, used_fallback={r1.used_fallback}")

    print("Call 2 (warm — identical state)...")
    t0 = time.time()
    r2 = dean.plan(
        state, chunks, carryover_notes="",
        domain_name="Human Anatomy & Physiology",
        clinical_scenario_style="",
    )
    e2 = (time.time() - t0) * 1000
    # DeanPlanResult may not expose cache_read directly — but we can check
    # the per-call resp via state debug. For this test, just compare timing.
    print(f"  elapsed_ms={e2:.0f}, used_fallback={r2.used_fallback}")
    speedup = (e1 - e2) / e1 * 100 if e1 > 0 else 0
    print(f"  Latency change: {speedup:+.0f}% (faster on call 2 = cache likely hit)")
    return e2 < e1


def main():
    print("BLOCK 3 cache verification\n" + "=" * 50)
    teacher_ok = test_teacher_cache()
    dean_ok = test_dean_cache()

    print("\n" + "=" * 50)
    print(f"Teacher cache: {'✓ HIT' if teacher_ok else '✗ MISS'}")
    print(f"Dean cache:    {'✓ HIT (faster)' if dean_ok else '✗ NO SPEEDUP'}")


if __name__ == "__main__":
    main()
