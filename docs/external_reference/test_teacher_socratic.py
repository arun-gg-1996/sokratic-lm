"""
test_teacher_socratic.py — acceptance tests for Step 13

Run from project root:
    PYTHONPATH=. python3 test_teacher_socratic.py

4 acceptance criteria:
  1. Smoke test: non-empty draft is produced
  2. No-reveal (turn 0): concept name NOT in draft
  3. Draft ends with exactly one question mark
  4. Reveal turn (turn >= SOCRATIC_TURN_GATE, student_attempted): concept MAY appear

All 4 must pass before Step 14.
"""

import sys
from langchain_core.messages import HumanMessage, AIMessage

import config
from graph.nodes.teacher_socratic import teacher_socratic

CONCEPT = "ulnar nerve"

BASE_CHUNKS = [
    (
        "The ulnar nerve is a branch of the medial cord of the brachial plexus "
        "and arises from nerve roots C8 and T1. It travels along the medial aspect "
        "of the arm, passes posterior to the medial epicondyle of the humerus "
        "through the cubital tunnel, and continues into the forearm and hand. "
        "The nerve innervates the flexor carpi ulnaris and the medial half of "
        "the flexor digitorum profundus in the forearm."
    )
]

# ── Helpers ────────────────────────────────────────────────────────────────────

def count_question_marks(text: str) -> int:
    return text.count("?")


def make_state(turn_count: int, student_attempted: bool, messages=None):
    return {
        "domain": config.DOMAIN,
        "current_concept": CONCEPT,
        "retrieved_chunks": BASE_CHUNKS,
        "turn_count": turn_count,
        "student_attempted": student_attempted,
        "weak_topics": [],
        "messages": messages or [
            HumanMessage(content="What nerve causes the funny bone sensation?")
        ],
    }


# ── Tests ──────────────────────────────────────────────────────────────────────

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


# Test 1: smoke — non-empty draft produced
state_t0 = make_state(turn_count=0, student_attempted=False)
result_t0 = teacher_socratic(state_t0)
draft_t0 = result_t0.get("draft_response", "")
check("Smoke test: non-empty draft", bool(draft_t0), repr(draft_t0[:80]))

# Test 2: no-reveal at turn 0 — concept name must NOT appear in draft
concept_in_draft = CONCEPT.lower() in draft_t0.lower()
check(
    "No-reveal at turn 0: concept name absent",
    not concept_in_draft,
    f"draft preview: {draft_t0[:120]!r}",
)

# Test 3: draft ends with exactly one question mark
qmarks = count_question_marks(draft_t0)
check(
    "Ends with exactly one question mark",
    qmarks == 1,
    f"found {qmarks} question mark(s) in: {draft_t0[-80:]!r}",
)

# Test 4: reveal turn — concept name MAY appear, draft is non-empty
state_reveal = make_state(
    turn_count=config.SOCRATIC_TURN_GATE,
    student_attempted=True,
    messages=[
        HumanMessage(content="What nerve causes the funny bone sensation?"),
        AIMessage(content="Think about the nerve that passes through the cubital tunnel. What do you know about the medial side of the elbow?"),
        HumanMessage(content="Is it related to the median nerve?"),
        AIMessage(content="You're close — consider which nerve travels posterior to the medial epicondyle. Can you name it?"),
        HumanMessage(content="I'm not sure, maybe the radial nerve?"),
    ],
)
result_reveal = teacher_socratic(state_reveal)
draft_reveal = result_reveal.get("draft_response", "")
check(
    "Reveal turn: non-empty draft produced",
    bool(draft_reveal),
    repr(draft_reveal[:80]),
)

# ── Summary ────────────────────────────────────────────────────────────────────

print()
print(f"Results: {passed}/4 passed")

if failed:
    print("FAILED — fix before Step 14.")
    sys.exit(1)
else:
    print("All tests passed. Step 13 complete.")
