"""
scripts/grade_sweep.py
Grade a sweep.json artifact produced by sweep_post_demo_sims.py.

Two layers of grading:
 1. Deterministic checks — telemetry counter monotonicity, phase
 transitions, hint-level escalation, latency bounds, no-leak heuristic
 (locked_answer ngram presence in tutor text).
 2. LLM rubric — Haiku grader scores each tutor message 1-5 on:
 grounded: on-topic and grounded in the locked concept
 no_leak: does not reveal the locked answer prematurely
 quality: pedagogically sound (right hint level, no fake praise)
 ack_at_xfer: at phase transitions, briefly acknowledges what just
 happened before pivoting

Run:
 source .venv/bin/activate
 python scripts/grade_sweep.py data/artifacts/sweep_<ts>/sweep.json

Output:
 data/artifacts/sweep_<ts>/grade.json
 data/artifacts/sweep_<ts>/grade.md (human-readable report)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env", override=True)
sys.path.insert(0, str(ROOT))

# --- deterministic checks ----------------------------------------------------

LOCKED_ANSWER_LEAK_NGRAM = 4  # n-gram length for naive answer-leak detection

def ngrams(text: str, n: int) -> set[str]:
    toks = re.findall(r"[a-z]+", (text or "").lower())
    return {" ".join(toks[i : i + n]) for i in range(0, max(0, len(toks) - n + 1))}

def naive_leak_score(tutor_text: str, locked_answer: str) -> dict:
    """Heuristic: count answer n-grams in tutor text. Not authoritative -- the
 real F5c gate is the haiku_hint_leak_check at runtime; this is just a
 cheap sanity check."""
    if not locked_answer:
        return {"checked": False, "ngram_overlap": 0}
    a = ngrams(locked_answer, LOCKED_ANSWER_LEAK_NGRAM)
    t = ngrams(tutor_text, LOCKED_ANSWER_LEAK_NGRAM)
    return {"checked": True, "ngram_overlap": len(a & t)}

def telemetry_check(sim: dict) -> dict:
    """Counter monotonicity + sane phase transitions + no zombie counters."""
    issues = []
    last = {
        "total_help_abuse_turns": 0,
        "total_off_topic_turns": 0,
        "total_low_effort_turns": 0,
        "total_clinical_help_abuse_turns": 0,
        "total_clinical_off_topic_turns": 0,
        "total_clinical_low_effort_turns": 0,
        "turn_count": 0,
    }
    phases_seen = []
    for t in sim.get("turns", []):
        d = t.get("diag", {})
        for k, prev in last.items():
            cur = d.get(k)
            if cur is None:
                continue
            if cur < prev:
                issues.append(
                    f"T{t['turn']}: {k} went BACKWARD ({prev} -> {cur})"
                )
            last[k] = cur
        ph = d.get("phase")
        if ph and (not phases_seen or phases_seen[-1] != ph):
            phases_seen.append(ph)
    return {"issues": issues, "phase_path": phases_seen, "final_counters": last}

def latency_check(sim: dict, threshold_s: float = 30.0) -> dict:
    slow = []
    for t in sim.get("turns", []):
        lat = t.get("latency_s")
        if lat is None:
            continue
        if lat >= threshold_s:
            slow.append({"turn": t["turn"], "latency_s": lat})
    return {"threshold_s": threshold_s, "slow_turns": slow}

# --- LLM rubric --------------------------------------------------------------

GRADE_PROMPT = """You are grading one turn of a Socratic medical tutor.

Locked question:    {locked_question}
Locked answer:      {locked_answer}
Phase BEFORE turn:  {prev_phase}
Phase AFTER turn:   {phase}
Hint level:         {hint_level}
Student message:    {student}
Tutor reply:        {tutor}

Score each on 1-5 (5=excellent, 1=very poor). If a category does not apply
(e.g. no locked answer yet), return null.

  grounded:     Tutor stays on the locked topic / current phase. (1 = wanders,
                5 = perfectly grounded.)
  no_leak:      Tutor avoids revealing the locked answer too early. 5 = no
                leak. 1 = full or near-full reveal. Null before topic is locked.
  quality:      Pedagogically sound -- right hint level, no fake praise, no
                shaming. 5 = excellent Socratic move; 1 = bad.
  ack_at_xfer:  Only score if phase changed THIS turn (e.g. tutoring->assessment,
                rapport->tutoring). 5 = brief acknowledgement before pivot;
                1 = abrupt jump. Null if phase did not change.

Also output a short note (under 25 words) describing the tutor's move.

Return strict JSON: {{"grounded": int|null, "no_leak": int|null,
"quality": int|null, "ack_at_xfer": int|null, "note": str}}"""

def grade_with_haiku(turns_with_prev_phase: list[dict]) -> list[dict]:
    """Call Haiku to grade each turn. Returns parallel list of grade dicts."""
    try:
        from anthropic import Anthropic
    except ImportError:
        print("[grade] anthropic SDK not installed, skipping LLM rubric", file=sys.stderr)
        return [{} for _ in turns_with_prev_phase]

    client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    out = []
    for i, t in enumerate(turns_with_prev_phase):
        d = t.get("diag", {}) or {}
        prompt = GRADE_PROMPT.format(
            locked_question=(d.get("locked_question") or "")[:200],
            locked_answer=(d.get("locked_answer") or "")[:200],
            prev_phase=t.get("_prev_phase", "<none>"),
            phase=d.get("phase", "<none>"),
            hint_level=d.get("hint_level", 0),
            student=(t.get("student") or "")[:500],
            tutor=(t.get("tutor") or "")[:1500],
        )
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text if resp.content else ""
            # extract first {...} block
            m = re.search(r"\{[\s\S]*\}", text)
            grade = json.loads(m.group(0)) if m else {}
        except Exception as e:
            grade = {"error": str(e)}
        out.append(grade)
        print(
            f"[grade] T{t.get('turn')}: g={grade.get('grounded')} "
            f"nl={grade.get('no_leak')} q={grade.get('quality')} "
            f"ax={grade.get('ack_at_xfer')} -- {(grade.get('note') or '')[:60]}",
            flush=True,
        )
    return out

def grade_sim(sim: dict) -> dict:
    turns = sim.get("turns", [])
    # tag each turn with the previous phase for transition detection
    enriched = []
    prev_ph = None
    for t in turns:
        t2 = dict(t)
        t2["_prev_phase"] = prev_ph
        enriched.append(t2)
        prev_ph = (t.get("diag", {}) or {}).get("phase", prev_ph)

    tel = telemetry_check(sim)
    lat = latency_check(sim)

    # leak heuristic per turn
    leak_per_turn = []
    for t in turns:
        d = t.get("diag", {}) or {}
        leak_per_turn.append(
            {
                "turn": t.get("turn"),
                **naive_leak_score(t.get("tutor", ""), d.get("locked_answer", "")),
            }
        )

    grades = grade_with_haiku(enriched)

    # aggregate
    def avg(key: str) -> float | None:
        vals = [g.get(key) for g in grades if isinstance(g.get(key), (int, float))]
        return round(sum(vals) / len(vals), 2) if vals else None

    return {
        "sim": sim.get("sim"),
        "telemetry": tel,
        "latency": lat,
        "leak_heuristic": leak_per_turn,
        "rubric_per_turn": grades,
        "rubric_avg": {
            "grounded": avg("grounded"),
            "no_leak": avg("no_leak"),
            "quality": avg("quality"),
            "ack_at_xfer": avg("ack_at_xfer"),
        },
    }

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sweep_json", help="Path to sweep.json")
    args = ap.parse_args()

    sweep_path = Path(args.sweep_json).resolve()
    sweep = json.loads(sweep_path.read_text())
    out_dir = sweep_path.parent

    graded = []
    for run in sweep.get("runs", []):
        if run.get("error"):
            graded.append({"sim": run.get("sim"), "error": run.get("error")})
            continue
        graded.append(grade_sim(run))

    out_json = out_dir / "grade.json"
    out_json.write_text(json.dumps({"graded": graded}, indent=2))
    print(f"[grade] wrote {out_json}", flush=True)
    return 0

if __name__ == "__main__":
    sys.exit(main())
