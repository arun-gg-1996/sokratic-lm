"""
test_full_loop.py — Step 21 integration test

Tests the assembled graph end-to-end across 5 scenarios.
Pre-seeds retrieved_chunks to avoid requiring ChromaDB/ollama.
Each scenario invokes the graph at a specific entry point by pre-setting
the relevant state fields.

Run from project root:
    PYTHONPATH=. python3 test_full_loop.py

5 scenarios:
  1. Choice flow: student_phase=choice_pending, "clinical" → clinical question produced
  2. Clinical flow: student_phase=clinical_pending → synthesis_assessor scores
  3. Done flow: student_phase=choice_pending, "done" → graph reaches END cleanly
  4. Topic choice flow: student_phase=topic_choice_pending, "weak" → resets to learning
  5. Teach path: student correct (mastery) → choice offer in response
"""

import sys
from langchain_core.messages import HumanMessage, AIMessage

import config
from graph.graph_builder import graph

CONCEPT = "ulnar nerve"
CHUNKS = [
    (
        "The ulnar nerve is a branch of the medial cord of the brachial plexus "
        "and arises from nerve roots C8 and T1. It passes posterior to the medial "
        "epicondyle of the humerus and continues into the forearm and hand. "
        "It innervates the flexor carpi ulnaris and the medial half of the "
        "flexor digitorum profundus in the forearm."
    ),
]

passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    status = "PASS" if condition else "FAIL"
    suffix = f" — {detail}" if detail else ""
    print(f"  [{status}] {name}{suffix}")
    if condition:
        passed += 1
    else:
        failed += 1


def base_state(**overrides) -> dict:
    state = {
        "domain": config.DOMAIN,
        "current_concept": CONCEPT,
        "retrieved_chunks": CHUNKS,
        "turn_count": 0,
        "student_attempted": False,
        "weak_topics": ["brachial plexus"],
        "classifier_output": "",
        "dean_passed": False,
        "dean_revisions": 0,
        "draft_response": "",
        "dean_revision_instruction": "",
        "crag_decision": "",
        "concept_mastered": False,
        "mastery_level": "",
        "student_phase": "learning",
        "mastery_choice": "",
        "topic_choice": "",
        "chunk_sources": [],
        "image_pending": False,
        "image_b64": "",
        "session_id": "test",
        "messages": [HumanMessage(content="What nerve causes the funny bone sensation?")],
    }
    state.update(overrides)
    return state


# ── Scenario 1: choice_pending + "clinical" → clinical question ───────────────
print("\nScenario 1: choice_pending + clinical intent → clinical_question_node")
try:
    s1 = base_state(
        student_phase="choice_pending",
        messages=[
            HumanMessage(content="What nerve causes the funny bone sensation?"),
            AIMessage(content="Correct! The ulnar nerve. Want A) clinical Q B) next C) done?"),
            HumanMessage(content="I'd like to try the clinical application question"),
        ],
    )
    result1 = graph.invoke(s1)
    msgs1 = result1.get("messages", [])
    last_ai1 = next((m for m in reversed(msgs1) if m.type == "ai"), None)
    check("Clinical flow: AI response delivered", bool(last_ai1),
          repr(last_ai1.content[:80]) if last_ai1 else "no AI message")
    check("student_phase='clinical_pending'", result1.get("student_phase") == "clinical_pending",
          f"got {result1.get('student_phase')!r}")
except Exception as e:
    check("Clinical flow: no crash", False, str(e))
    check("student_phase='clinical_pending'", False, "exception above")

# ── Scenario 2: clinical_pending → synthesis_assessor ────────────────────────
print("\nScenario 2: clinical_pending → synthesis_assessor scores response")
try:
    s2 = base_state(
        student_phase="clinical_pending",
        messages=[
            HumanMessage(content="What nerve causes the funny bone sensation?"),
            AIMessage(content="A patient presents with weakness in finger flexion..."),
            HumanMessage(content="If the ulnar nerve is compressed, the patient would have "
                                  "difficulty with pinch grip and fine motor tasks like buttoning."),
        ],
    )
    result2 = graph.invoke(s2)
    # O-C3: synthesis_assessor now sets student_phase="choice_pending" so the
    # next student message routes to mastery_choice_classifier (clinical/next/done).
    # Previously it set "learning" but never delivered the feedback message,
    # silently dropping the score.
    check("Synthesis flow: student_phase advances to 'choice_pending'",
          result2.get("student_phase") == "choice_pending",
          f"got {result2.get('student_phase')!r}")
    msgs2 = result2.get("messages", [])
    last_ai2 = next((m for m in reversed(msgs2) if m.type == "ai"), None)
    # Semantic check (not exact substring): the synthesis feedback message
    # must mention scoring concepts. Decoupling from the exact prompt wording
    # so prompt edits don't silently break this test.
    content2 = last_ai2.content if last_ai2 else ""
    has_feedback = bool(last_ai2) and len(content2) > 20 and any(
        kw in content2.lower()
        for kw in ("score", "structure", "function", "ot relevance"))
    check("Synthesis flow: feedback delivered as AIMessage",
          has_feedback,
          repr(content2[:80]) if last_ai2 else "no AI message")
except Exception as e:
    check("Synthesis flow: no crash", False, str(e))
    check("Synthesis flow: feedback delivered", False, "exception above")

# ── Scenario 3: choice_pending + "done" → END ────────────────────────────────
print("\nScenario 3: choice_pending + 'done' → graph ends cleanly")
try:
    s3 = base_state(
        student_phase="choice_pending",
        messages=[
            HumanMessage(content="What nerve causes the funny bone sensation?"),
            AIMessage(content="Correct! Want A) clinical Q B) next C) done?"),
            HumanMessage(content="I'm done for now, thanks"),
        ],
    )
    result3 = graph.invoke(s3)
    check("Done flow: no crash and mastery_choice='done'",
          result3.get("mastery_choice") == "done",
          f"mastery_choice={result3.get('mastery_choice')!r}")
except Exception as e:
    check("Done flow: no crash", False, str(e))

# ── Scenario 4: topic_choice_pending + "weak" → learning ─────────────────────
print("\nScenario 4: topic_choice_pending + 'weak topics' → resets to learning")
try:
    s4 = base_state(
        student_phase="topic_choice_pending",
        weak_topics=["brachial plexus", "rotator cuff"],
        messages=[
            AIMessage(content="Want to practice weak topics or name your own?"),
            HumanMessage(content="Let's work on my weak topics"),
        ],
    )
    result4 = graph.invoke(s4)
    check("Topic choice: student_phase reset to 'learning'",
          result4.get("student_phase") == "learning",
          f"got {result4.get('student_phase')!r}")
    check("Topic choice: concept picked from weak list",
          result4.get("current_concept") in ("brachial plexus", "rotator cuff"),
          f"current_concept={result4.get('current_concept')!r}")
except Exception as e:
    check("Topic choice: no crash", False, str(e))
    check("Topic choice: topic_choice set", False, "exception above")

# ── Scenario 5: step_advancer produces choice offer ──────────────────────────
print("\nScenario 5: step_advancer → choice prompt contains three options")
try:
    from graph.nodes.step_advancer import step_advancer
    s5 = {"domain": config.DOMAIN, "current_concept": CONCEPT,
          "turn_count": 0, "dean_revision_instruction": ""}
    result5 = step_advancer(s5)
    draft5 = result5.get("draft_response", "")
    lower5 = draft5.lower()
    has_all_three = ("clinical" in lower5 and
                     ("next" in lower5 or "topic" in lower5) and
                     ("done" in lower5 or "stop" in lower5))
    check("step_advancer: all three choices in draft", has_all_three,
          repr(draft5[:120]))
    check("step_advancer: mastery_level='strong'",
          result5.get("mastery_level") == "strong",
          f"mastery_level={result5.get('mastery_level')!r}")
except Exception as e:
    check("step_advancer: no crash", False, str(e))
    check("step_advancer: mastery_level='strong'", False, "exception above")

# ── Summary ───────────────────────────────────────────────────────────────────
print(f"\nResults: {passed}/{passed + failed} passed")
if failed:
    print("FAILED — check errors above.")
    sys.exit(1)
else:
    print("All scenarios passed. Step 21 complete.")
