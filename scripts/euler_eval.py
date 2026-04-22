#!/usr/bin/env python3
"""EULER evaluation script for Sokratic tutor conversations."""

import json
import os
import sys
from pathlib import Path
from statistics import mean

from dotenv import load_dotenv
load_dotenv(Path('/Users/arun-ghontale/UB/NLP/sokratic/.env'), override=True)

import anthropic

client = anthropic.Anthropic()

CONV_FILES = [
    "S6_8279f8d4_231930.json", "S6_4d3e8608_231843.json", "S6_2a4a1fdf_232014.json",
    "S5_b8b91b94_231608.json", "S5_9e75e7a9_231704.json", "S5_e8513042_222621.json",
    "S4_a4816fd1_231520.json", "S4_376ae7ea_231245.json", "S4_c8601904_222425.json",
    "S3_932a12a5_231151.json", "S3_77a0bd11_230845.json", "S3_4340a8d0_230946.json",
    "S2_929d034c_230627.json", "S2_24ffcb6c_230525.json", "S2_ee3eb9d5_230734.json",
    "S1_04cc9a5f_230250.json", "S1_c38403e0_230154.json", "S1_d6e28297_230412.json",
]

SKIP_PHASES = {"rapport", "topic_engagement", "topic", "option_select"}

SYSTEM_PROMPT = """You are an expert evaluator for a Socratic anatomy tutoring AI.
Rate the TUTOR response on 4 criteria. Return ONLY valid JSON, no markdown.

1. question_present (0.0 or 1.0): Response ends with at least one genuine pedagogical question. Exception: assessment/summary phases score 1.0 automatically.
2. relevance (0.0-1.0): How well does the tutor response address the student's last message and stay on-topic.
3. helpful (0.0-1.0): Does it advance student reasoning without giving the answer away directly.
4. no_reveal (0.0 or 1.0): Locked answer NOT present or unambiguously implied. 1.0=safe. 0.0=leaked. Exception: assessment/summary phases score 1.0.

Return: {"question_present":0.0,"relevance":0.0,"helpful":0.0,"no_reveal":0.0}"""

FALLBACK = {"question_present": 0.5, "relevance": 0.5, "helpful": 0.5, "no_reveal": 1.0}

BASE = Path('/Users/arun-ghontale/UB/NLP/sokratic/data/artifacts/final_convo')
OUT = Path('/Users/arun-ghontale/UB/NLP/sokratic/data/artifacts/euler_scores')
OUT.mkdir(parents=True, exist_ok=True)


def score_turn(phase, locked_answer, last_student_msg, tutor_content):
    user_msg = f"""Phase: {phase}
Locked answer (must NOT appear in tutoring responses): {locked_answer or "(not set)"}
Student's last message: {last_student_msg or "(none yet)"}
Tutor response: {tutor_content}"""

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}]
        )
        text = resp.content[0].text.strip()
        scores = json.loads(text)
        return scores
    except Exception as e:
        print(f"    [WARN] parse fail: {e}")
        return dict(FALLBACK)


def bar(val, width=20):
    filled = int(round(val * width))
    return "█" * filled + "░" * (width - filled)


all_results = []

for idx, fname in enumerate(CONV_FILES, 1):
    fpath = BASE / fname
    conv = json.loads(fpath.read_text())

    # Extract metadata
    profile = fname.split('_')[0]
    conv_id = fname.replace('.json', '')
    topic = conv.get("topic", conv.get("outcome", {}).get("topic", "Unknown"))
    outcome = conv.get("outcome", {})
    reached = outcome.get("reached_answer", False)
    conv_locked = outcome.get("locked_answer", "")

    print(f"[conv {idx}/18] {profile} {topic[:50]}...")

    turns = conv.get("turns", [])
    last_student_msg = None
    turn_scores = []
    all_turn_scores = []

    for turn in turns:
        role = turn.get("role", "")
        phase = turn.get("phase", "")

        if role == "student":
            last_student_msg = turn.get("content", "")

        if role == "tutor":
            locked_answer = turn.get("locked_answer") or conv_locked
            tutor_content = turn.get("content", "")

            if phase not in SKIP_PHASES:
                scores = score_turn(phase, locked_answer, last_student_msg, tutor_content)
                turn_avg = mean(scores.values())
                turn_scores.append({"phase": phase, "scores": scores, "avg": turn_avg})
                all_turn_scores.append({"phase": phase, "scores": scores, "avg": turn_avg, "tutoring": True})
                print(f"    [{phase}] avg={turn_avg:.3f} {scores}")
            else:
                all_turn_scores.append({"phase": phase, "scores": None, "avg": None, "tutoring": False})

    conv_avg = mean([t["avg"] for t in turn_scores]) if turn_scores else 0.0
    print(f"  => conv_avg={conv_avg:.3f} (tutoring turns={len(turn_scores)})")

    result = {
        "conv_id": conv_id,
        "profile": profile,
        "topic": topic,
        "reached_answer": reached,
        "turn_scores": turn_scores,
        "all_turn_scores": all_turn_scores,
        "conversation_average": conv_avg,
        "tutoring_turn_count": len(turn_scores),
        "total_tutor_turns": len([t for t in all_turn_scores]),
    }
    all_results.append(result)

    # Save individual
    (OUT / f"{conv_id}_euler.json").write_text(json.dumps(result, indent=2))


# Aggregate
all_tutoring_turns = [t for r in all_results for t in r["turn_scores"]]
all_turns_flat = [t for r in all_results for t in r["all_turn_scores"] if t["scores"] is not None]

def criterion_stats(turns_list, crit):
    vals = [t["scores"][crit] for t in turns_list]
    avg = mean(vals) if vals else 0.0
    passing = sum(1 for v in vals if v >= 0.75)
    return avg, passing, len(vals)

criteria = ["question_present", "relevance", "helpful", "no_reveal"]

tut_stats = {c: criterion_stats(all_tutoring_turns, c) for c in criteria}
all_stats = {c: criterion_stats(all_turns_flat, c) for c in criteria}

overall_euler = mean([r["conversation_average"] for r in all_results])
conversations_passing = sum(1 for r in all_results if r["conversation_average"] >= 0.75)
reached_count = sum(1 for r in all_results if r["reached_answer"])

total_tutor_turns = sum(r["total_tutor_turns"] for r in all_results)
tutoring_turns_count = len(all_tutoring_turns)

# Save master report
master = {
    "conversations_scored": 18,
    "total_tutor_turns": total_tutor_turns,
    "tutoring_phase_turns": tutoring_turns_count,
    "overall_euler": overall_euler,
    "conversations_passing": conversations_passing,
    "reached_answer_count": reached_count,
    "criterion_averages_tutoring": {c: tut_stats[c][0] for c in criteria},
    "criterion_averages_all": {c: all_stats[c][0] for c in criteria},
    "conversations": all_results,
}
(OUT / "euler_report_final.json").write_text(json.dumps(master, indent=2))

# Print report
print()
print("=" * 70)
print("EULER EVALUATION REPORT")
print("Conversations scored: 18")
print("=" * 70)
print()
print(f"Total tutor turns evaluated: {total_tutor_turns}  (tutoring-phase only: {tutoring_turns_count})")
print()
print("── Criterion Averages (tutoring turns only) ────────────────────────")
for c in criteria:
    avg, passing, total = tut_stats[c]
    pct = int(100 * passing / total) if total else 0
    print(f"  {c:<20} [{bar(avg)}] {avg:.3f}  pass≥0.75: {passing}/{total} ({pct}%)")

print()
print("── Criterion Averages (all phases) ──────────────────────────────────")
for c in criteria:
    avg, passing, total = all_stats[c]
    pct = int(100 * passing / total) if total else 0
    print(f"  {c:<20} [{bar(avg)}] {avg:.3f}  pass≥0.75: {passing}/{total} ({pct}%)")

print()
print("── Overall ──────────────────────────────────────────────────────────")
print(f"  Mean EULER score:              {overall_euler:.3f}")
print(f"  Conversations passing (≥0.75): {conversations_passing}/18")
print(f"  reached_answer rate:           {reached_count}/18")

print()
print("── Per-Conversation (sorted by EULER desc) ──────────────────────────")
print(f"  {'#':<4} {'P':<5} {'Topic':<45} {'T':<4} {'R?':<5} EULER")
sorted_results = sorted(all_results, key=lambda r: r["conversation_average"], reverse=True)
for i, r in enumerate(sorted_results, 1):
    topic_short = (r["topic"] or "")[:44]
    reached_sym = "✓" if r["reached_answer"] else "✗"
    print(f"  {i:<4} {r['profile']:<5} {topic_short:<45} {r['tutoring_turn_count']:<4} {reached_sym:<5} {r['conversation_average']:.3f}")

print()
print("── By Profile ───────────────────────────────────────────────────────")
profiles = ["S1", "S2", "S3", "S4", "S5", "S6"]
profile_labels = {"S1": "Strong", "S2": "Confident", "S3": "Average", "S4": "Struggling", "S5": "Anxious", "S6": "Disengaged"}
for p in profiles:
    pr = [r for r in all_results if r["profile"] == p]
    if not pr:
        continue
    pavg = mean([r["conversation_average"] for r in pr])
    preached = sum(1 for r in pr if r["reached_answer"])
    label = profile_labels.get(p, p)
    print(f"  {p} ({label:<12}) n={len(pr)}  avg={pavg:.3f}  reached: {preached}/{len(pr)}")

print()
print("── By Topic ─────────────────────────────────────────────────────────")
topics = {}
for r in all_results:
    t = r["topic"] or "Unknown"
    if t not in topics:
        topics[t] = []
    topics[t].append(r)
for t, rs in sorted(topics.items(), key=lambda x: -mean([r["conversation_average"] for r in x[1]])):
    tavg = mean([r["conversation_average"] for r in rs])
    treached = sum(1 for r in rs if r["reached_answer"])
    print(f"  {t}")
    print(f"    n={len(rs)}  avg={tavg:.3f}  reached: {treached}/{len(rs)}")

print()
print("── Qualitative Analysis ─────────────────────────────────────────────")

def qual_label(crit, val):
    if crit == "question_present":
        if val >= 0.85: return "STRONG"
        if val >= 0.70: return "GOOD"
        return "WEAK"
    elif crit == "relevance":
        if val >= 0.85: return "STRONG"
        if val >= 0.70: return "MODERATE"
        return "WEAK"
    elif crit == "helpful":
        if val >= 0.85: return "STRONG"
        if val >= 0.70: return "MODERATE"
        return "WEAK"
    elif crit == "no_reveal":
        if val >= 0.90: return "STRONG"
        if val >= 0.75: return "GOOD"
        return "CONCERN"

descriptions = {
    "question_present": "Tutor consistently ends responses with pedagogical questions",
    "relevance": "Tutor responses stay on-topic and address student messages",
    "helpful": "Tutor advances reasoning without directly revealing answers",
    "no_reveal": "Tutor maintains answer confidentiality throughout sessions",
}

for c in criteria:
    avg = tut_stats[c][0]
    label = qual_label(c, avg)
    desc = descriptions[c]
    print(f"  {c:<20} {avg:.3f}: [{label}] — {desc}")

print()
overall_label = "✅ PASS (target ≥0.75)" if overall_euler >= 0.75 else "⚠️  BELOW TARGET"
print(f"  OVERALL {overall_euler:.3f} — [{overall_label}]")
print("=" * 70)
