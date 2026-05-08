#!/usr/bin/env python3
"""
Same classifier prompt + few-shots, but routed via direct Anthropic API
instead of Bedrock. Tests whether the call-to-call variance we observed
is Bedrock-specific or model-level.

Run:  ANTHROPIC_API_KEY=... python scripts/test_classifier_direct_anthropic.py

The script bypasses `_haiku_call` / `_cached_system_block` (which both
key off SOKRATIC_USE_BEDROCK=1 in .env) and constructs an
anthropic.Anthropic() client directly.
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

import anthropic

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Direct-Anthropic Haiku 4.5 — bypass our llm_client wrapper entirely so
# this run is provably not going through Bedrock.
HAIKU_MODEL = "claude-haiku-4-5-20251001"

# Prompt — the SAME shape as the live classifier, with the few-shot
# block re-attached. Kept inline so this script stands alone.
SYSTEM_PROMPT = """\
You are an intent classifier for a Socratic tutoring system. Classify
the student's LATEST message into ONE of these 9 categories. The user
prompt gives you DOMAIN (the textbook's subject), LOCKED SUBSECTION
(the current topic), LOCKED QUESTION, PHASE, recent turns, and the
student's message.

Apply this decision tree IN ORDER. Return as soon as one rule matches.

  1. DEFLECTION — student wants to end/leave the session.
     Markers: "let's stop", "I have to go", "I'm done", "wrap up",
     "we can be done", "I'll pass". Apologetic phrasing
     ("sorry, I have to go") still counts.
     → deflection

  2. OPT-IN reply — only when phase=assessment AND the prior tutor
     turn offered a yes/no clinical bonus.
       affirmative ("yes", "sure")          → opt_in_yes
       negative    ("no", "skip")           → opt_in_no
       unclear     ("ok", "maybe")          → opt_in_ambiguous

  3. LOW-EFFORT — passive minimum response with no attempt or demand.
     Markers: bare "idk", "i don't know", "no idea", "not sure",
     "no clue", "dunno", "?", "??", ".".
     → low_effort

  4. HELP-ABUSE — explicit demand for the answer or to skip.
     Markers: "just tell me", "give me the answer", "skip", "skip this",
     "next", "make it easier", "stop quizzing me", "I don't want to guess".
     The OBJECT of the demand is the locked answer / skipping the
     question, not the concept itself.
     → help_abuse

  5. OFF-DOMAIN — clearly outside the DOMAIN named in the user prompt.
     If DOMAIN is "human anatomy", things like "what's the weather",
     "did you see the game", "tell me a joke", "write me a python
     function", "what's the capital of France" are off_domain.
     → off_domain

  6. EXPLORATION — student is asking for content, context, or
     definition (NOT committing to an answer). Three shapes count:
       (a) in-topic scaffolding — "what is X?", "tell me about X first"
       (b) term clarification — "what does X mean?"
       (c) adjacent concept — "how does X compare to Y?"

     The disambiguator from help_abuse is the OBJECT of the request:
       exploration → asking for context / definitions / concepts
       help_abuse  → asking for the locked answer itself / skip
     → exploration

  7. ON-TOPIC ENGAGED — student is committing to a guess, partial
     answer, hedge with reasoning, or follow-up that takes a stance
     on the locked question.
     Markers: "is it X?" (committed guess), "I think it's X",
     "could it be X because <reason>", "the answer is X".
     → on_topic_engaged

KEY DISAMBIGUATIONS:

  ASKING vs COMMITTING (rule 6 vs 7): "Is it the SA node?" without any
  reasoning attached can be either — when the message is a guess
  (committing to a candidate), prefer on_topic_engaged. When the
  message is a request for explanation, prefer exploration.

  EXPLORATION vs HELP-ABUSE: the object of the request decides.
  Background → exploration. Locked answer / skip → help_abuse.
  When ambiguous on this axis, prefer exploration.

  WRONG-ANSWER vs OFF-DOMAIN: a concept WITHIN the domain but wrong
  for the locked question (e.g. "is it the SA node?" while studying
  the kidney) is on_topic_engaged, NOT off_domain. Off_domain is for
  content outside the domain entirely.

CALIBRATION EXAMPLES — anchors for each verdict. Match the underlying
intent of real student messages; do NOT echo verbatim.

  STUDENT: "I think it's the proximal tubule"
  → {"verdict": "on_topic_engaged", "rationale": "committed guess"}

  STUDENT: "is it the SA node?"
  → {"verdict": "on_topic_engaged", "rationale": "guess in question form"}

  STUDENT: "tell me about the structure of X first, then I'll try"
  → {"verdict": "exploration", "rationale": "asking for context, not the answer"}

  STUDENT: "what does podocyte mean?"
  → {"verdict": "exploration", "rationale": "term clarification"}

  STUDENT: "just tell me the answer"
  → {"verdict": "help_abuse", "rationale": "direct demand for the answer"}

  STUDENT: "skip this question"
  → {"verdict": "help_abuse", "rationale": "demand to skip"}

  STUDENT: "make it easier"
  → {"verdict": "help_abuse", "rationale": "demand for simplification"}

  STUDENT: "idk"
  → {"verdict": "low_effort", "rationale": "passive non-engagement"}

  STUDENT: "?"
  → {"verdict": "low_effort", "rationale": "single-token non-substantive reply"}

  STUDENT: "what's the weather like?"   (DOMAIN = human anatomy)
  → {"verdict": "off_domain", "rationale": "weather is unrelated to anatomy"}

  STUDENT: "can you write me a python function?"   (DOMAIN = anatomy)
  → {"verdict": "off_domain", "rationale": "programming is outside the subject"}

  STUDENT: "I have to go now, sorry"
  → {"verdict": "deflection", "rationale": "explicit session-ending intent"}

  STUDENT: "let's stop here"
  → {"verdict": "deflection", "rationale": "request to end the session"}

Output STRICT JSON only — no markdown:
{
  "verdict": "<one of the 9 categories>",
  "evidence": "<verbatim substring from the student message>",
  "rationale": "<1-sentence>"
}
"""

USER_TEMPLATE = """\
DOMAIN:            {domain_name}
LOCKED SUBSECTION: {locked_subsection}
LOCKED QUESTION:   {locked_question}
PHASE:             {phase}

STUDENT'S LATEST MESSAGE:
{message}
"""

# (subsection, locked_q, message, expected)
TESTS: list[tuple[str, str, str, str]] = [
    ("Articulations of the Temporomandibular Joint",
     "What structure sits between the temporal bone and the mandibular condyle?",
     "I think it's the articular disc", "on_topic_engaged"),
    ("Cardiac Conduction System",
     "What initiates the heartbeat?",
     "Is it the SA node?", "on_topic_engaged"),
    ("The Nephron",
     "What forces drive glomerular filtration?",
     "is it hydrostatic pressure?", "on_topic_engaged"),
    ("Articulations of the Temporomandibular Joint",
     "What structure sits between the temporal bone and the mandibular condyle?",
     "is it like a ligament or something", "on_topic_engaged"),
    ("Articulations of the Temporomandibular Joint",
     "What structure sits between the temporal bone and the mandibular condyle?",
     "in detail can you first tell me about the characteristics of the TMJ firstly, that will help me answer better",
     "exploration"),
    ("The Nephron",
     "What forces drive glomerular filtration?",
     "tell me about the nephron's structure first, then I'll answer", "exploration"),
    ("Articulations of the Temporomandibular Joint",
     "What structure sits between the temporal bone and the mandibular condyle?",
     "what does mandibular condyle mean?", "exploration"),
    ("The Nephron", "What forces drive glomerular filtration?", "what is a podocyte?", "exploration"),
    ("Cardiac Conduction System", "What initiates the heartbeat?",
     "how does the SA node compare to the AV node?", "exploration"),
    ("The Nephron", "What forces drive glomerular filtration?",
     "remind me what diffusion is, then I'll apply it", "exploration"),
    ("The Nephron", "What forces drive glomerular filtration?",
     "how does the nephron filter under diabetes?", "exploration"),
    ("The Nephron", "What forces drive glomerular filtration?", "just tell me the answer", "help_abuse"),
    ("The Nephron", "What forces drive glomerular filtration?", "idk, give me the answer", "help_abuse"),
    ("The Nephron", "What forces drive glomerular filtration?", "skip this question", "help_abuse"),
    ("The Nephron", "What forces drive glomerular filtration?",
     "I don't want to guess, just tell me the answer", "help_abuse"),
    ("The Nephron", "What forces drive glomerular filtration?", "make it easier", "help_abuse"),
    ("The Nephron", "What forces drive glomerular filtration?", "idk", "low_effort"),
    ("The Nephron", "What forces drive glomerular filtration?", "not sure", "low_effort"),
    ("The Nephron", "What forces drive glomerular filtration?", "?", "low_effort"),
    ("The Nephron", "What forces drive glomerular filtration?", "dunno", "low_effort"),
    ("The Nephron", "What forces drive glomerular filtration?", "what's the weather like?", "off_domain"),
    ("The Nephron", "What forces drive glomerular filtration?",
     "did you see the basketball game last night?", "off_domain"),
    ("The Nephron", "What forces drive glomerular filtration?",
     "can you write me a python function?", "off_domain"),
    ("The Nephron", "What forces drive glomerular filtration?",
     "what's the capital of France?", "off_domain"),
    ("The Nephron", "What forces drive glomerular filtration?", "tell me a joke", "off_domain"),
    ("The Nephron", "What forces drive glomerular filtration?", "I have to go now, sorry", "deflection"),
    ("The Nephron", "What forces drive glomerular filtration?", "let's stop here", "deflection"),
    ("The Nephron", "What forces drive glomerular filtration?", "I'm done with this", "deflection"),
    ("The Nephron", "What forces drive glomerular filtration?", "can we wrap up?", "deflection"),
]

N_RUNS = 3


def classify(client: anthropic.Anthropic, sub: str, q: str, msg: str) -> dict:
    """Pure direct-Anthropic call. No Bedrock, no caching wrapper."""
    user_text = USER_TEMPLATE.format(
        domain_name="human anatomy",
        locked_subsection=sub,
        locked_question=q,
        phase="tutoring",
        message=msg,
    )
    resp = client.messages.create(
        model=HAIKU_MODEL,
        temperature=0.0,
        max_tokens=200,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_text}],
    )
    text = resp.content[0].text if resp.content else ""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"verdict": "PARSE_FAIL", "rationale": text[:200]}


def main() -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY missing — cannot run direct-Anthropic test")
        sys.exit(1)
    client = anthropic.Anthropic(api_key=api_key)

    runs: list[list[tuple[str, str, str]]] = []  # per-run: list of (msg, expected, verdict)
    print(f"Running {N_RUNS} passes via direct Anthropic API (no Bedrock, no cache)…")
    print()

    for run_idx in range(N_RUNS):
        t0 = time.time()
        run_results: list[tuple[str, str, str]] = []
        correct = 0
        for sub, q, msg, expected in TESTS:
            r = classify(client, sub, q, msg)
            v = str(r.get("verdict", "PARSE_FAIL"))
            run_results.append((msg, expected, v))
            if v == expected:
                correct += 1
        runs.append(run_results)
        elapsed = time.time() - t0
        print(f"Run {run_idx+1}: {correct}/{len(TESTS)} ({100*correct/len(TESTS):.0f}%)  [{elapsed:.1f}s]")

    # Per-category accuracy across runs
    print()
    print("=== Per-category accuracy across runs ===")
    cats: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for run in runs:
        per_cat: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for _, expected, v in run:
            per_cat[expected][1] += 1
            if v == expected:
                per_cat[expected][0] += 1
        for cat, (ok, total) in per_cat.items():
            cats[cat].append((ok, total))

    for cat in sorted(cats.keys()):
        triples = " ".join(f"{ok}/{tot}" for ok, tot in cats[cat])
        print(f"  {cat:<22} {triples}")

    # Stability — cases that flipped at least once
    print()
    print("=== Cases that flipped between runs ===")
    by_case: dict[tuple[str, str], list[str]] = {}
    for run in runs:
        for msg, expected, v in run:
            by_case.setdefault((msg, expected), []).append(v)
    flipped = 0
    stable_correct = 0
    stable_wrong = 0
    for (msg, expected), verdicts in by_case.items():
        if len(set(verdicts)) > 1:
            flipped += 1
            print(f"  expected={expected:<22} verdicts={verdicts}  | {msg[:55]}")
        elif verdicts[0] == expected:
            stable_correct += 1
        else:
            stable_wrong += 1
    print()
    print(f"Stable correct:  {stable_correct}/{len(TESTS)}")
    print(f"Stable wrong:    {stable_wrong}/{len(TESTS)}")
    print(f"Flipped at least once: {flipped}/{len(TESTS)}")


if __name__ == "__main__":
    main()
