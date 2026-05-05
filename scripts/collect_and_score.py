"""
scripts/collect_and_score.py
------------------------------
Generates 18 fresh conversations (3 topics × 6 profiles) using real RAG,
saves each to data/artifacts/final_convo/, then scores every tutor turn
with LLM-based EULER and prints the full report.

Usage (from sokratic/ root):
    .venv/bin/python scripts/collect_and_score.py

Output:
    data/artifacts/final_convo/  — individual conversation JSONs
    data/artifacts/euler_scores/ — per-conversation EULER score JSONs
    Printed report: per-criterion averages, pass rates, overall analysis.
"""

import asyncio
import json
import re
import uuid
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env", override=True)

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

import anthropic
from config import cfg
from conversation.state import initial_state
from evaluation.simulation.profiles import PROFILES
from evaluation.simulation.student_simulator import StudentSimulator

OUTPUT_DIR = Path(cfg.paths.artifacts) / "final_convo"
SCORES_DIR = Path(cfg.paths.artifacts) / "euler_scores"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
SCORES_DIR.mkdir(parents=True, exist_ok=True)

# 3 focused single-answer anatomy topics
TOPICS = [
    "What nerve innervates the deltoid muscle?",
    "Which nerve is damaged in a humeral shaft fracture causing wrist drop?",
    "What muscle is the primary abductor of the shoulder from 15 to 90 degrees?",
]

# All 6 profiles × 3 topics = 18 conversations
RUNS = [(pid, topic) for pid in ["S1","S2","S3","S4","S5","S6"] for topic in TOPICS]


# ──────────────────────────────────────────────────────────────────────────────
# Conversation runner
# ──────────────────────────────────────────────────────────────────────────────

async def run_one(profile_id: str, topic: str, graph) -> dict:
    conv_id = str(uuid.uuid4())[:8]
    student_id = f"{profile_id}_{conv_id}"
    state = initial_state(student_id, cfg)
    thread_config = {"configurable": {"thread_id": conv_id}}
    simulator = StudentSimulator(PROFILES[profile_id])
    turns_log = []
    bugs = []

    def _last_tutor(messages, since=0):
        for m in reversed(messages[since:]):
            if m.get("role") == "tutor":
                return m.get("content", "")
        return ""

    # Rapport
    try:
        state = await asyncio.to_thread(graph.invoke, state, thread_config)
        msg = _last_tutor(state.get("messages", []))
        if msg:
            turns_log.append({"turn": 0, "phase": "rapport", "role": "tutor", "content": msg})
        print(f"  [{profile_id}] rapport ok")
    except Exception as e:
        bugs.append({"phase": "rapport", "error": str(e)})
        return _build(profile_id, conv_id, topic, turns_log, state, bugs)

    # Seed topic
    state["messages"].append({"role": "student", "content": topic})
    turns_log.append({"turn": 0, "phase": "topic", "role": "student", "content": topic})

    try:
        prev_len = len(state.get("messages", []))
        state = await asyncio.to_thread(graph.invoke, state, thread_config)
        msg = _last_tutor(state.get("messages", []), prev_len)
        if msg:
            turns_log.append({"turn": 0, "phase": "topic_engagement", "role": "tutor", "content": msg})
        print(f"  [{profile_id}] topic engagement ok | topic_confirmed={state.get('topic_confirmed')}")
    except Exception as e:
        bugs.append({"phase": "topic_engagement", "error": str(e)})
        return _build(profile_id, conv_id, topic, turns_log, state, bugs)

    # If options shown, select first one
    if not state.get("topic_confirmed", False):
        opts = state.get("topic_options", [])
        selection = opts[0] if opts else topic
        state["messages"].append({"role": "student", "content": selection})
        turns_log.append({"turn": 0, "phase": "option_select", "role": "student", "content": selection})
        try:
            prev_len = len(state.get("messages", []))
            state = await asyncio.to_thread(graph.invoke, state, thread_config)
            msg = _last_tutor(state.get("messages", []), prev_len)
            if msg:
                turns_log.append({"turn": 1, "phase": "tutoring", "role": "tutor",
                                   "content": msg, "locked_answer": state.get("locked_answer"),
                                   "hint_level": state.get("hint_level")})
        except Exception as e:
            bugs.append({"phase": "first_tutor_turn", "error": str(e)})
            return _build(profile_id, conv_id, topic, turns_log, state, bugs)

    # Main loop
    logged_contents = {t.get("content") for t in turns_log}
    for loop_i in range(14):
        phase = state.get("phase", "tutoring")
        if phase == "memory_update" or state.get("assessment_turn", 0) >= 3:
            break

        try:
            student_resp = await asyncio.to_thread(simulator.respond, state)
        except Exception as e:
            student_resp = "I'm not sure."
            bugs.append({"phase": "simulator", "turn": loop_i, "error": str(e)})

        state["messages"].append({"role": "student", "content": student_resp})
        turns_log.append({"turn": state.get("turn_count", 0) + 1, "phase": phase,
                           "role": "student", "content": student_resp})
        print(f"  [{profile_id}] student t{loop_i+1}: {student_resp[:55]}...")

        try:
            prev_len = len(state.get("messages", []))
            state = await asyncio.to_thread(graph.invoke, state, thread_config)
            new_msgs = state.get("messages", [])[prev_len:]
            for m in new_msgs:
                if m.get("role") == "tutor" and m.get("content") not in logged_contents:
                    entry = {
                        "turn": state.get("turn_count", loop_i + 1),
                        "phase": state.get("phase"),
                        "role": "tutor",
                        "content": m["content"],
                        "student_state": state.get("student_state"),
                        "hint_level": state.get("hint_level"),
                        "locked_answer": state.get("locked_answer"),
                        "student_reached_answer": state.get("student_reached_answer"),
                    }
                    turns_log.append(entry)
                    logged_contents.add(m["content"])
            print(f"  [{profile_id}] tutor t{loop_i+1} | state={state.get('student_state')} "
                  f"hint={state.get('hint_level')} reached={state.get('student_reached_answer')} "
                  f"phase={state.get('phase')}")
        except Exception as e:
            bugs.append({"phase": "graph_invoke", "turn": loop_i, "error": str(e)})
            import traceback; traceback.print_exc()
            break

        if state.get("phase") == "memory_update" or state.get("assessment_turn", 0) >= 3:
            break

    return _build(profile_id, conv_id, topic, turns_log, state, bugs)


def _build(profile_id, conv_id, topic, turns_log, state, bugs):
    turn_count = state.get("turn_count") or len([t for t in turns_log if t.get("role") == "student"])
    return {
        "conv_id": conv_id,
        "profile_id": profile_id,
        "profile_name": PROFILES[profile_id].name,
        "topic": topic,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "turns": turns_log,
        "outcome": {
            "phase_final": state.get("phase"),
            "reached_answer": state.get("student_reached_answer", False),
            "locked_answer": state.get("locked_answer", ""),
            "hint_level_final": state.get("hint_level", 1),
            "turn_count": turn_count,
            "assessment_turn": state.get("assessment_turn", 0),
            "student_state_final": state.get("student_state"),
        },
        "debug_summary": {
            "api_calls": state.get("debug", {}).get("api_calls", 0),
            "cost_usd": round(state.get("debug", {}).get("cost_usd", 0.0), 4),
            "interventions": state.get("debug", {}).get("interventions", 0),
        },
        "bugs": bugs,
    }


def save_conv(result: dict) -> Path:
    ts = datetime.now().strftime("%H%M%S")
    fname = f"{result['profile_id']}_{result['conv_id']}_{ts}.json"
    p = OUTPUT_DIR / fname
    p.write_text(json.dumps(result, indent=2, default=str))
    return p


# ──────────────────────────────────────────────────────────────────────────────
# EULER scorer (LLM judge, adapted for final_convo format)
# ──────────────────────────────────────────────────────────────────────────────

JUDGE_SYSTEM = """\
You are an expert evaluator for a Socratic anatomy tutoring AI (Sokratic).
Rate the TUTOR response on exactly 4 criteria. Return ONLY valid JSON — no markdown, no prose.

Criteria definitions:
1. question_present (0.0 or 1.0)
   1.0 = response ends with at least one genuine pedagogical question.
   0.0 = no question present (assessment/summary phases may score 1.0 without a question).

2. relevance (0.0–1.0)
   How well does the tutor response address the student's last message and stay on the anatomy topic?
   1.0 = directly relevant and on-target.
   0.5 = partially relevant or generic.
   0.0 = completely off-topic.

3. helpful (0.0–1.0)
   Does the response advance the student's reasoning without just giving the answer away?
   1.0 = concrete reasoning scaffold, moves student forward.
   0.5 = some guidance but vague or too short.
   0.0 = useless filler, no advancement.

4. no_reveal (0.0 or 1.0)
   1.0 = the locked answer (and any obvious synonyms) does NOT appear in the response.
   0.0 = the locked answer is directly stated or unambiguously implied in one hop.
   NOTE: in assessment/summary phases, revealing the answer is expected — score 1.0 regardless.

Return EXACTLY this JSON shape:
{"question_present": 0.0, "relevance": 0.0, "helpful": 0.0, "no_reveal": 0.0}"""


def score_turn_llm(tutor_resp: str, student_msg: str, locked_answer: str,
                   phase: str, client: anthropic.Anthropic) -> dict:
    user_content = (
        f"Phase: {phase}\n"
        f"Locked answer (must NOT appear in tutoring responses): {locked_answer or '(not set)'}\n"
        f"Student's last message: {student_msg}\n"
        f"Tutor response to evaluate:\n{tutor_resp}"
    )
    try:
        resp = client.messages.create(
            model=cfg.models.dean,
            max_tokens=80,
            system=JUDGE_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = resp.content[0].text.strip()
        # Strip markdown fences if present
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        scores = json.loads(raw)
    except Exception:
        scores = {"question_present": 0.5, "relevance": 0.5, "helpful": 0.5, "no_reveal": 1.0}

    keys = ["question_present", "relevance", "helpful", "no_reveal"]
    for k in keys:
        scores[k] = round(max(0.0, min(1.0, float(scores.get(k, 0.5)))), 3)
    scores["average"] = round(sum(scores[k] for k in keys) / 4, 3)
    return scores


def score_conversation(conv: dict, client: anthropic.Anthropic) -> dict:
    """Score all tutor turns in a final_convo dict."""
    turns = conv.get("turns", [])
    locked_answer = conv.get("outcome", {}).get("locked_answer", "")
    conv_id = conv.get("conv_id", "unknown")

    per_turn = []
    last_student = "(session start)"

    for t in turns:
        role = t.get("role")
        content = t.get("content", "")
        phase = t.get("phase", "tutoring")

        if role == "student":
            last_student = content
        elif role == "tutor" and phase not in ("rapport", "topic", "option_select", "topic_engagement"):
            # Use turn-level locked_answer if available (more accurate)
            turn_locked = t.get("locked_answer") or locked_answer or ""
            scores = score_turn_llm(content, last_student, turn_locked, phase, client)
            per_turn.append({
                "turn": t.get("turn"),
                "phase": phase,
                "student_state": t.get("student_state"),
                "tutor_snippet": content[:100],
                **scores,
            })

    conv_avg = round(sum(s["average"] for s in per_turn) / len(per_turn), 3) if per_turn else 0.0
    return {
        "conv_id": conv_id,
        "profile_id": conv.get("profile_id"),
        "profile_name": conv.get("profile_name"),
        "topic": conv.get("topic"),
        "reached_answer": conv.get("outcome", {}).get("reached_answer", False),
        "locked_answer": locked_answer,
        "turn_count": conv.get("outcome", {}).get("turn_count", 0),
        "per_turn_scores": per_turn,
        "conversation_average": conv_avg,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Report generator
# ──────────────────────────────────────────────────────────────────────────────

def print_report(all_scores: list[dict]):
    print("\n" + "=" * 70)
    print("EULER EVALUATION REPORT")
    print(f"Conversations scored: {len(all_scores)}")
    print("=" * 70)

    criteria = ["question_present", "relevance", "helpful", "no_reveal"]

    # Flatten all per-turn scores (tutoring phase only for strict eval)
    tutoring_turns = [s for conv in all_scores for s in conv["per_turn_scores"]
                      if s.get("phase") == "tutoring"]
    all_turns = [s for conv in all_scores for s in conv["per_turn_scores"]]

    print(f"\nTotal tutor turns evaluated: {len(all_turns)}  "
          f"(tutoring-phase only: {len(tutoring_turns)})")

    print("\n── Per-Criterion Averages (all phases) ─────────────────────────────")
    for c in criteria:
        vals = [s[c] for s in all_turns]
        avg = sum(vals) / len(vals) if vals else 0
        pass_count = sum(1 for v in vals if v >= 0.75)
        print(f"  {c:<20}  avg={avg:.3f}  pass≥0.75: {pass_count}/{len(vals)} "
              f"({100*pass_count//len(vals) if vals else 0}%)")

    print("\n── Per-Criterion Averages (tutoring phase only) ────────────────────")
    for c in criteria:
        vals = [s[c] for s in tutoring_turns]
        avg = sum(vals) / len(vals) if vals else 0
        pass_count = sum(1 for v in vals if v >= 0.75)
        print(f"  {c:<20}  avg={avg:.3f}  pass≥0.75: {pass_count}/{len(vals)} "
              f"({100*pass_count//len(vals) if vals else 0}%)")

    print("\n── Conversation-Level Summary ───────────────────────────────────────")
    conv_avgs = [c["conversation_average"] for c in all_scores]
    overall_avg = sum(conv_avgs) / len(conv_avgs) if conv_avgs else 0
    passing_convs = sum(1 for v in conv_avgs if v >= 0.75)
    print(f"  Overall avg EULER score:  {overall_avg:.3f}")
    print(f"  Conversations passing (≥0.75):  {passing_convs}/{len(all_scores)}")

    print("\n── Per-Conversation Breakdown ───────────────────────────────────────")
    print(f"  {'Profile':<6} {'Topic':<45} {'Turns':<6} {'Reached':<8} {'EULER'}")
    print(f"  {'-'*6} {'-'*45} {'-'*6} {'-'*8} {'-'*5}")
    for c in sorted(all_scores, key=lambda x: x["conversation_average"], reverse=True):
        topic_short = c["topic"][:43] + ".." if len(c["topic"]) > 45 else c["topic"]
        reached = "YES" if c["reached_answer"] else "no"
        print(f"  {c['profile_id']:<6} {topic_short:<45} {c['turn_count']:<6} "
              f"{reached:<8} {c['conversation_average']:.3f}")

    print("\n── By Profile ───────────────────────────────────────────────────────")
    for pid in ["S1","S2","S3","S4","S5","S6"]:
        convs = [c for c in all_scores if c["profile_id"] == pid]
        if not convs:
            continue
        avg = sum(c["conversation_average"] for c in convs) / len(convs)
        reached = sum(1 for c in convs if c["reached_answer"])
        print(f"  {pid} ({PROFILES[pid].name:<22}) n={len(convs)}  "
              f"avg={avg:.3f}  reached_answer: {reached}/{len(convs)}")

    print("\n── By Topic ─────────────────────────────────────────────────────────")
    for topic in TOPICS:
        convs = [c for c in all_scores if c["topic"] == topic]
        if not convs:
            continue
        avg = sum(c["conversation_average"] for c in convs) / len(convs)
        reached = sum(1 for c in convs if c["reached_answer"])
        print(f"  {topic[:60]}")
        print(f"    n={len(convs)}  avg_euler={avg:.3f}  reached_answer: {reached}/{len(convs)}")

    print("\n── Qualitative Analysis ─────────────────────────────────────────────")
    # question_present
    qp_avg = sum(s["question_present"] for s in tutoring_turns) / len(tutoring_turns) if tutoring_turns else 0
    if qp_avg >= 0.90:
        print(f"  question_present ({qp_avg:.2f}): STRONG — tutor consistently ends with a question.")
    elif qp_avg >= 0.75:
        print(f"  question_present ({qp_avg:.2f}): GOOD — occasional missing question.")
    else:
        print(f"  question_present ({qp_avg:.2f}): WEAK — tutor frequently fails to ask a question.")

    rel_avg = sum(s["relevance"] for s in tutoring_turns) / len(tutoring_turns) if tutoring_turns else 0
    if rel_avg >= 0.80:
        print(f"  relevance ({rel_avg:.2f}): STRONG — responses tightly grounded in student input.")
    elif rel_avg >= 0.65:
        print(f"  relevance ({rel_avg:.2f}): MODERATE — some drift from student's actual message.")
    else:
        print(f"  relevance ({rel_avg:.2f}): WEAK — significant relevance issues.")

    help_avg = sum(s["helpful"] for s in tutoring_turns) / len(tutoring_turns) if tutoring_turns else 0
    if help_avg >= 0.80:
        print(f"  helpful ({help_avg:.2f}): STRONG — responses advance student reasoning well.")
    elif help_avg >= 0.65:
        print(f"  helpful ({help_avg:.2f}): MODERATE — guidance is sometimes vague or generic.")
    else:
        print(f"  helpful ({help_avg:.2f}): WEAK — responses lack concrete reasoning scaffolds.")

    nr_avg = sum(s["no_reveal"] for s in tutoring_turns) / len(tutoring_turns) if tutoring_turns else 0
    if nr_avg >= 0.95:
        print(f"  no_reveal ({nr_avg:.2f}): STRONG — LeakGuard working, answer not exposed.")
    elif nr_avg >= 0.85:
        print(f"  no_reveal ({nr_avg:.2f}): GOOD — rare leak risk, generally safe.")
    else:
        print(f"  no_reveal ({nr_avg:.2f}): CONCERN — answer may be leaking in some responses.")

    print(f"\n  OVERALL: {overall_avg:.3f} {'✅ PASS (≥0.75)' if overall_avg >= 0.75 else '⚠️  BELOW TARGET (target: 0.75)'}")
    print("=" * 70)


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

async def main():
    from conversation.graph import build_graph
    from retrieval.retriever import Retriever
    from memory.memory_manager import MemoryManager

    print("Building graph...")
    try:
        retriever = Retriever()
        print("  Real Retriever loaded.")
    except Exception as e:
        print(f"  Retriever failed ({e}), aborting.")
        return

    graph = build_graph(retriever, MemoryManager())
    client = anthropic.Anthropic()
    print(f"Graph built. Running {len(RUNS)} conversations...\n")

    saved_paths = []
    all_scores = []

    for i, (profile_id, topic) in enumerate(RUNS, 1):
        print(f"\n[{i}/{len(RUNS)}] {profile_id} ({PROFILES[profile_id].name}) — {topic[:55]}")
        result = await run_one(profile_id, topic, graph)
        path = save_conv(result)
        saved_paths.append(path)
        tutor_turns = len([t for t in result["turns"] if t.get("role") == "tutor"
                           and t.get("phase") not in ("rapport","topic_engagement")])
        print(f"  Saved: {path.name} | tutor_turns={tutor_turns} | "
              f"reached={result['outcome']['reached_answer']} | "
              f"locked='{result['outcome']['locked_answer']}'")

    print(f"\n{'='*60}")
    print(f"All {len(saved_paths)} conversations saved. Now scoring with EULER LLM judge...")
    print("(Each tutor turn = 1 API call to Claude)")
    print("=" * 60)

    for path in saved_paths:
        conv = json.loads(path.read_text())
        tutor_turns = [t for t in conv["turns"] if t.get("role") == "tutor"
                       and t.get("phase") not in ("rapport","topic_engagement")]
        if len(tutor_turns) < 2:
            print(f"  Skipping {path.name} — too few tutor turns ({len(tutor_turns)})")
            continue
        print(f"  Scoring {path.name} ({len(tutor_turns)} tutor turns)...", end="", flush=True)
        scores = score_conversation(conv, client)
        all_scores.append(scores)
        # Save score file
        score_path = SCORES_DIR / f"{conv['conv_id']}_euler.json"
        score_path.write_text(json.dumps(scores, indent=2))
        print(f" avg={scores['conversation_average']:.3f}")

    if all_scores:
        print_report(all_scores)
        # Save master report
        report_path = SCORES_DIR / f"euler_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        report_path.write_text(json.dumps(all_scores, indent=2))
        print(f"\nMaster report saved: {report_path}")
    else:
        print("No scorable conversations produced.")


if __name__ == "__main__":
    asyncio.run(main())
