#!/usr/bin/env python3
"""
EULER evaluation script — uses claude-sonnet to score each tutor turn.

EULER criteria:
  E — Engages with a question        (question_present:  0 or 1)
  U — Understands student context    (relevance:         0–1)
  L — Leads without leaking          (no_reveal:         0 or 1)
  E — Elevates reasoning             (helpful:           0–1)
  R — (implicit average)

Usage:
    python scripts/score_euler.py [--min-turns N] [--outdir path]
"""

import argparse
import json
import os
import sys
from pathlib import Path
from statistics import mean

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

import anthropic
from tqdm import tqdm

# ── config ───────────────────────────────────────────────────────────────────
MODEL = "claude-sonnet-4-5"
BASE = Path(__file__).parent.parent / "data/artifacts/final_convo"
DEFAULT_OUT = Path(__file__).parent.parent / "data/artifacts/euler_scores"

SKIP_PHASES = {"rapport", "topic_engagement", "topic", "option_select"}

SYSTEM_PROMPT = """You are an expert evaluator for a Socratic anatomy tutoring AI called Dean.
Score the TUTOR's single response on exactly 4 EULER criteria.
Return ONLY a JSON object — no markdown, no explanation, no extra keys.

Criteria:
1. question_present (0.0 or 1.0)
   - 1.0 if the response ends with at least one genuine pedagogical question that invites the student to think
   - 0.0 if no such question is present
   - Exception: assessment / mastery_summary / memory_update phases always score 1.0

2. relevance (0.0–1.0)
   - How well the tutor addresses the student's last message and stays on the anatomy topic
   - 1.0 = directly and precisely relevant; 0.0 = entirely off-topic

3. helpful (0.0–1.0)
   - Does the response meaningfully advance the student's reasoning without directly giving away the answer?
   - 1.0 = provides a powerful scaffold or hint; 0.0 = either useless or just restates the question

4. no_reveal (0.0 or 1.0)
   - 1.0 = the locked answer is NOT explicitly stated or unambiguously implied
   - 0.0 = the locked answer IS leaked
   - Exception: assessment / mastery_summary / memory_update phases always score 1.0

Return exactly: {"question_present": <float>, "relevance": <float>, "helpful": <float>, "no_reveal": <float>}"""

FALLBACK = {"question_present": 0.5, "relevance": 0.5, "helpful": 0.5, "no_reveal": 1.0}

PROFILE_LABELS = {
    "S1": "Strong",
    "S2": "Confident-wrong",
    "S3": "Average",
    "S4": "Struggling",
    "S5": "Anxious",
    "S6": "Disengaged",
}
CRITERIA = ["question_present", "relevance", "helpful", "no_reveal"]


# ── helpers ──────────────────────────────────────────────────────────────────
def bar(val: float, width: int = 20) -> str:
    filled = int(round(val * width))
    return "█" * filled + "░" * (width - filled)


def score_turn(
    client: anthropic.Anthropic,
    phase: str,
    locked_answer: str,
    last_student_msg: str,
    tutor_content: str,
) -> dict:
    user_msg = (
        f"Phase: {phase}\n"
        f"Locked answer (must NOT appear in tutoring responses): {locked_answer or '(not set yet)'}\n"
        f"Student's last message: {last_student_msg or '(none yet)'}\n"
        f"Tutor response to score:\n{tutor_content}"
    )
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=120,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = resp.content[0].text.strip()
        # strip accidental markdown fences
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        scores = json.loads(text)
        # clamp values
        for k in CRITERIA:
            scores[k] = max(0.0, min(1.0, float(scores.get(k, FALLBACK[k]))))
        return scores
    except Exception as exc:
        tqdm.write(f"      [WARN] score_turn failed: {exc}")
        return dict(FALLBACK)


def discover_conversations(base: Path, min_turns: int) -> list[Path]:
    files = sorted(base.glob("*.json"))
    usable = []
    for f in files:
        try:
            d = json.loads(f.read_text())
            if len(d.get("turns", [])) >= min_turns:
                usable.append(f)
        except Exception:
            pass
    return usable


def criterion_stats(turns_list: list, crit: str) -> tuple:
    vals = [t["scores"][crit] for t in turns_list]
    if not vals:
        return 0.0, 0, 0
    avg = mean(vals)
    passing = sum(1 for v in vals if v >= 0.75)
    return avg, passing, len(vals)


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Score conversations with EULER rubric")
    parser.add_argument("--min-turns", type=int, default=5, help="Minimum total turns to include a conversation")
    parser.add_argument("--outdir", type=str, default=str(DEFAULT_OUT))
    parser.add_argument("--limit", type=int, default=0, help="Score only first N conversations (0 = all)")
    args = parser.parse_args()

    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)

    client = anthropic.Anthropic()

    conv_files = discover_conversations(BASE, args.min_turns)
    if args.limit:
        conv_files = conv_files[: args.limit]

    print(f"\nFound {len(conv_files)} usable conversations (≥{args.min_turns} turns)")
    print(f"Model: {MODEL}\n")

    all_results = []

    for fpath in tqdm(conv_files, desc="Conversations", unit="conv"):
        conv = json.loads(fpath.read_text())
        fname = fpath.name
        profile = fname.split("_")[0]
        conv_id = fname.replace(".json", "")

        topic = conv.get("topic", "")
        if not topic:
            topic = conv.get("outcome", {}).get("topic", "Unknown")

        outcome = conv.get("outcome", {})
        reached = outcome.get("reached_answer", False)
        conv_locked = outcome.get("locked_answer", "") or ""

        turns = conv.get("turns", [])
        last_student_msg = None
        turn_scores = []   # tutoring-phase scored turns only
        all_turn_records = []

        for turn in turns:
            role = turn.get("role", "")
            phase = turn.get("phase", "")

            if role == "student":
                last_student_msg = turn.get("content", "")

            if role == "tutor":
                locked_answer = turn.get("locked_answer") or conv_locked
                tutor_content = turn.get("content", "")

                if phase in SKIP_PHASES:
                    all_turn_records.append({"phase": phase, "scores": None, "avg": None, "scored": False})
                    continue

                scores = score_turn(client, phase, locked_answer, last_student_msg, tutor_content)
                turn_avg = mean(scores.values())
                record = {"phase": phase, "scores": scores, "avg": turn_avg, "scored": True}
                turn_scores.append(record)
                all_turn_records.append(record)

        conv_avg = mean([t["avg"] for t in turn_scores]) if turn_scores else 0.0
        tqdm.write(
            f"  {conv_id}  turns_scored={len(turn_scores)}  "
            f"conv_avg={conv_avg:.3f}  reached={reached}"
        )

        result = {
            "conv_id": conv_id,
            "profile": profile,
            "topic": topic,
            "reached_answer": reached,
            "locked_answer": conv_locked,
            "turn_scores": turn_scores,
            "conversation_average": conv_avg,
            "tutoring_turn_count": len(turn_scores),
        }
        all_results.append(result)
        (out / f"{conv_id}_euler.json").write_text(json.dumps(result, indent=2))

    # ── aggregate ─────────────────────────────────────────────────────────────
    all_tutoring_turns = [t for r in all_results for t in r["turn_scores"]]
    tut_stats = {c: criterion_stats(all_tutoring_turns, c) for c in CRITERIA}

    overall_euler = mean([r["conversation_average"] for r in all_results]) if all_results else 0.0
    conversations_passing = sum(1 for r in all_results if r["conversation_average"] >= 0.75)
    reached_count = sum(1 for r in all_results if r["reached_answer"])
    n = len(all_results)

    master = {
        "conversations_scored": n,
        "model": MODEL,
        "tutoring_phase_turns": len(all_tutoring_turns),
        "overall_euler": overall_euler,
        "conversations_passing_0_75": conversations_passing,
        "reached_answer_count": reached_count,
        "criterion_averages": {c: tut_stats[c][0] for c in CRITERIA},
        "criterion_pass_counts": {c: {"passing": tut_stats[c][1], "total": tut_stats[c][2]} for c in CRITERIA},
        "conversations": all_results,
    }
    report_path = out / "euler_report.json"
    report_path.write_text(json.dumps(master, indent=2))

    # ── print report ──────────────────────────────────────────────────────────
    W = 72
    print()
    print("=" * W)
    print("  EULER EVALUATION REPORT")
    print(f"  Model: {MODEL}")
    print("=" * W)
    print()
    print(f"  Conversations scored:   {n}")
    print(f"  Tutoring turns scored:  {len(all_tutoring_turns)}")
    print()

    print("── EULER Criterion Averages (tutoring turns) " + "─" * (W - 44))
    crit_labels = {
        "question_present": "E  question_present",
        "relevance":        "U  relevance       ",
        "no_reveal":        "L  no_reveal       ",
        "helpful":          "E  helpful         ",
    }
    for c in CRITERIA:
        avg, passing, total = tut_stats[c]
        pct = int(100 * passing / total) if total else 0
        label = crit_labels.get(c, c)
        print(f"  {label}  [{bar(avg)}]  {avg:.3f}   pass≥0.75: {passing}/{total} ({pct}%)")

    print()
    print("── Overall " + "─" * (W - 10))
    overall_label = "PASS ✓" if overall_euler >= 0.75 else "BELOW TARGET ✗"
    print(f"  Mean EULER score:              {overall_euler:.3f}  [{overall_label}]")
    print(f"  Conversations passing (≥0.75): {conversations_passing}/{n}  ({int(100*conversations_passing/n) if n else 0}%)")
    print(f"  reached_answer rate:           {reached_count}/{n}  ({int(100*reached_count/n) if n else 0}%)")

    print()
    print("── Per-Conversation (sorted by EULER ↓) " + "─" * (W - 40))
    header = f"  {'#':<4} {'Profile':<18} {'Turns':>5} {'R?':<4} {'EULER':>6}  Topic"
    print(header)
    for i, r in enumerate(sorted(all_results, key=lambda x: x["conversation_average"], reverse=True), 1):
        profile_label = f"{r['profile']} ({PROFILE_LABELS.get(r['profile'], '?')})"
        topic_short = (r["topic"] or "")[:40]
        reached_sym = "✓" if r["reached_answer"] else "✗"
        print(
            f"  {i:<4} {profile_label:<18} {r['tutoring_turn_count']:>5} "
            f"{reached_sym:<4} {r['conversation_average']:>6.3f}  {topic_short}"
        )

    print()
    print("── By Student Profile " + "─" * (W - 22))
    for p in ["S1", "S2", "S3", "S4", "S5", "S6"]:
        pr = [r for r in all_results if r["profile"] == p]
        if not pr:
            continue
        pavg = mean([r["conversation_average"] for r in pr])
        preached = sum(1 for r in pr if r["reached_answer"])
        label = PROFILE_LABELS.get(p, p)
        print(f"  {p} ({label:<14})  n={len(pr)}  avg={pavg:.3f}  reached={preached}/{len(pr)}")

    print()
    print("── By Topic " + "─" * (W - 12))
    topics: dict[str, list] = {}
    for r in all_results:
        t = (r["topic"] or "Unknown")
        topics.setdefault(t, []).append(r)
    for t, rs in sorted(topics.items(), key=lambda x: -mean([r["conversation_average"] for r in x[1]])):
        tavg = mean([r["conversation_average"] for r in rs])
        treached = sum(1 for r in rs if r["reached_answer"])
        short = t[:60]
        print(f"  {short}")
        print(f"    n={len(rs)}  avg={tavg:.3f}  reached={treached}/{len(rs)}")

    print()
    print("── Qualitative Analysis " + "─" * (W - 24))
    qual_map = {
        "question_present": {
            "desc": "Tutor consistently ends responses with a pedagogical question",
            "thresholds": [(0.85, "STRONG"), (0.70, "GOOD"), (0.0, "WEAK")],
        },
        "relevance": {
            "desc": "Tutor stays on-topic and addresses student's message",
            "thresholds": [(0.85, "STRONG"), (0.70, "MODERATE"), (0.0, "WEAK")],
        },
        "helpful": {
            "desc": "Tutor advances reasoning without directly revealing the answer",
            "thresholds": [(0.85, "STRONG"), (0.70, "MODERATE"), (0.0, "WEAK")],
        },
        "no_reveal": {
            "desc": "Tutor maintains answer confidentiality throughout",
            "thresholds": [(0.90, "STRONG"), (0.75, "GOOD"), (0.0, "CONCERN ⚠")],
        },
    }
    for c in CRITERIA:
        avg = tut_stats[c][0]
        info = qual_map[c]
        q_label = next(lbl for thr, lbl in info["thresholds"] if avg >= thr)
        print(f"  {crit_labels[c]}  {avg:.3f}  [{q_label}]")
        print(f"    {info['desc']}")

    print()
    print(f"  OVERALL EULER: {overall_euler:.3f}  —  [{overall_label}]")
    print("=" * W)
    print(f"\nFull report saved → {report_path}")


if __name__ == "__main__":
    main()
