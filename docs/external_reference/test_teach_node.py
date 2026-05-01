"""
test_teach_node.py — acceptance tests for Step 18b

Run from project root:
    PYTHONPATH=. python3 test_teach_node.py

4 acceptance criteria:
  1. Smoke test: non-empty draft produced
  2. mastery_level="failed" and concept_mastered=False in result
  3. student_phase="choice_pending" in result
  4. Draft reveals the concept (concept name appears — teach always reveals)

All 4 must pass before Step 18c.
"""

import sys
from langchain_core.messages import HumanMessage, AIMessage

import config
from graph.nodes.teach_node import teach_node

CONCEPT = "ulnar nerve"

BASE_CHUNKS = [
    (
        "Several major nerves supply the upper extremity. The brachial plexus "
        "originates from nerve roots C5 through T1. The medial cord gives rise "
        "to nerves that travel along the medial aspect of the arm."
    ),
    (
        "The ulnar nerve is a branch of the medial cord of the brachial plexus "
        "and arises from nerve roots C8 and T1. It passes posterior to the medial "
        "epicondyle of the humerus and continues into the forearm and hand."
    ),
]


def make_state(turn_count: int = 2, dean_revision_instruction: str = ""):
    return {
        "domain": config.DOMAIN,
        "current_concept": CONCEPT,
        "retrieved_chunks": BASE_CHUNKS,
        "turn_count": turn_count,
        "dean_revision_instruction": dean_revision_instruction,
    }


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


# ── Test 1: Smoke test ────────────────────────────────────────────────────────
print("  [INFO] Calling teach_node (failed mastery, turn_count=2)...")
result1 = teach_node(make_state())
draft1 = result1.get("draft_response", "")
check("Smoke test: non-empty draft", bool(draft1), repr(draft1[:80]))

# ── Test 2: mastery_level="failed", concept_mastered=False ───────────────────
check(
    "mastery_level='failed' and concept_mastered=False",
    result1.get("mastery_level") == "failed" and result1.get("concept_mastered") is False,
    f"mastery_level={result1.get('mastery_level')!r}, concept_mastered={result1.get('concept_mastered')}",
)

# ── Test 3: student_phase="choice_pending" ───────────────────────────────────
check(
    "student_phase='choice_pending'",
    result1.get("student_phase") == "choice_pending",
    f"student_phase={result1.get('student_phase')!r}",
)

# ── Test 4: Draft reveals the concept ────────────────────────────────────────
concept_in_draft = CONCEPT.lower() in draft1.lower()
check(
    "Draft reveals the concept (teach always reveals)",
    concept_in_draft,
    f"draft preview: {draft1[:120]!r}",
)

# ── Summary ───────────────────────────────────────────────────────────────────
print()
print(f"Results: {passed}/4 passed")

if failed:
    print("FAILED — fix before Step 18c.")
    sys.exit(1)
else:
    print("All tests passed. Step 18b complete.")
