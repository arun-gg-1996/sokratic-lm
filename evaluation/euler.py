"""
evaluation/euler.py
--------------------
Offline EULER evaluation. Run on saved conversation logs, NOT live.

Supports two scoring modes:
  - local (default): deterministic phase-aware heuristics, no API calls
  - llm: Claude-as-judge

Scores each tutor response on 4 criteria (0.0 to 1.0 each):
  1. question_present  — did the response contain at least one question?
  2. relevance         — is it relevant to the student's last message?
  3. helpful           — does it advance understanding without giving the answer?
  4. no_reveal         — is the locked answer absent from the response?

Target: average EULER score > 0.75 across a full conversation.

Results saved to data/artifacts/euler_scores/{conv_id}.json.

Usage:
    python -m evaluation.euler --conv_id <id>
    python -m evaluation.euler --all      # score all saved conversations
"""

import json
import re
from pathlib import Path
import anthropic
from dotenv import load_dotenv
from config import cfg

load_dotenv()

JUDGE_SYSTEM = """You are an evaluator for a Socratic tutoring AI. Rate the tutor response below
on 4 criteria, each from 0.0 to 1.0.

Criteria:
1. question_present (0 or 1): Does the response end with at least one genuine question?
2. relevance (0.0-1.0): Is the response relevant to the student's last message and on-topic?
3. helpful (0.0-1.0): Does the response advance understanding without directly giving the answer?
4. no_reveal (0 or 1): Is the locked answer (and clear synonyms) absent from the response?
   Score 1.0 if the answer is not revealed. Score 0.0 if the answer is clearly given.

Context:
- Locked answer (correct answer, should NOT appear in tutor response): {locked_answer}
- Student's last message: {student_message}
- Tutor response to evaluate: {tutor_response}

Return ONLY valid JSON in this exact format (no markdown, no explanation):
{{"question_present": 0.0, "relevance": 0.0, "helpful": 0.0, "no_reveal": 0.0}}"""


def _safe_json_parse(text: str) -> dict:
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:].strip()
    return json.loads(text)


def _phase_for_turn(messages: list[dict], idx: int) -> str:
    msg = messages[idx]
    if msg.get("phase"):
        return msg.get("phase")
    text = (msg.get("content") or "").lower()
    if not any(m.get("role") == "student" for m in messages[:idx]):
        return "rapport"
    if "session complete" in text or "mastery summary" in text or "since we didn't reach" in text:
        return "memory_update"
    if "clinical" in text or "client" in text or "ot clinic" in text:
        return "assessment"
    return "tutoring"


def _local_relevance(student_message: str, tutor_response: str) -> float:
    s_tokens = set(re.findall(r"[a-zA-Z]{4,}", (student_message or "").lower()))
    t_tokens = set(re.findall(r"[a-zA-Z]{4,}", (tutor_response or "").lower()))
    if not s_tokens:
        return 0.7
    overlap = len(s_tokens & t_tokens) / max(1, len(s_tokens))
    return max(0.0, min(1.0, overlap * 1.5))


def score_turn_local(
    tutor_response: str,
    student_message: str,
    locked_answer: str,
    phase: str = "tutoring",
) -> dict:
    """
    Score one turn without LLM calls (deterministic local heuristics).
    Phase-aware so non-tutoring turns are not unfairly penalized.
    """
    text = tutor_response or ""
    lower = text.lower()
    q_count = text.count("?")

    if phase == "tutoring":
        question_present = 1.0 if q_count == 1 else 0.0
        relevance = _local_relevance(student_message, text)
        helpful = 1.0 if (len(text) > 40 and q_count >= 1 and not lower.startswith(("i can see", "i notice", "i hear you"))) else 0.5
        no_reveal = 0.0 if (locked_answer and locked_answer.lower() in lower) else 1.0
    elif phase == "rapport":
        question_present = 1.0 if q_count >= 1 else 0.0
        relevance = 0.9
        helpful = 0.8 if len(text) > 20 else 0.5
        no_reveal = 1.0
    elif phase == "assessment":
        question_present = 1.0 if q_count >= 1 else 0.7
        relevance = _local_relevance(student_message, text)
        helpful = 0.9 if len(text) > 30 else 0.6
        no_reveal = 1.0  # assessment may legitimately reference the answer context
    else:  # memory_update / summary
        question_present = 1.0
        relevance = 0.9
        helpful = 0.9 if len(text) > 30 else 0.6
        no_reveal = 1.0

    scores = {
        "question_present": float(max(0.0, min(1.0, question_present))),
        "relevance": float(max(0.0, min(1.0, relevance))),
        "helpful": float(max(0.0, min(1.0, helpful))),
        "no_reveal": float(max(0.0, min(1.0, no_reveal))),
    }
    scores["average"] = sum(scores.values()) / 4
    return scores


def score_turn_llm(
    tutor_response: str,
    student_message: str,
    locked_answer: str,
    phase: str = "tutoring",
) -> dict:
    """
    Score a single tutor response on all 4 EULER criteria using Claude as judge.

    Args:
        tutor_response:  The tutor's response text.
        student_message: The student's preceding message.
        locked_answer:   The correct answer for this topic (used for no_reveal check).

    Returns:
        {
          "question_present": float,
          "relevance": float,
          "helpful": float,
          "no_reveal": float,
          "average": float
        }
    """
    client = anthropic.Anthropic()

    system = JUDGE_SYSTEM.format(
        locked_answer=locked_answer or "(not set)",
        student_message=f"{student_message}\nPhase: {phase}",
        tutor_response=tutor_response or "",
    )

    resp = client.messages.create(
        model=cfg.models.dean,
        max_tokens=128,
        system=system,
        messages=[{"role": "user", "content": "Rate this tutor response."}],
    )

    text = resp.content[0].text.strip()

    try:
        scores = _safe_json_parse(text)
    except (json.JSONDecodeError, IndexError):
        # Conservative fallback
        scores = {"question_present": 0.0, "relevance": 0.5, "helpful": 0.5, "no_reveal": 1.0}

    criteria = ["question_present", "relevance", "helpful", "no_reveal"]
    # Clamp all scores to [0, 1]
    for k in criteria:
        scores[k] = max(0.0, min(1.0, float(scores.get(k, 0.0))))

    scores["average"] = sum(scores[k] for k in criteria) / len(criteria)
    return scores


def score_turn(
    tutor_response: str,
    student_message: str,
    locked_answer: str,
    phase: str = "tutoring",
    mode: str = "local",
) -> dict:
    if mode == "llm":
        return score_turn_llm(tutor_response, student_message, locked_answer, phase=phase)
    return score_turn_local(tutor_response, student_message, locked_answer, phase=phase)


def score_conversation(conv_path: str, mode: str = "local") -> dict:
    """
    Load a saved conversation from data/artifacts/conversations/ and score every tutor turn.

    Args:
        conv_path: Path to the conversation JSON file.

    Returns:
        {
          "conv_id": str,
          "per_turn_scores": list[dict],
          "conversation_average": float
        }
    """
    path = Path(conv_path)
    if not path.exists():
        raise FileNotFoundError(f"Conversation file not found: {conv_path}")

    conv = json.loads(path.read_text())
    messages = conv.get("messages", [])
    locked_answer = conv.get("locked_answer", "")
    conv_id = path.stem  # filename without extension

    per_turn_scores = []
    last_student_msg = ""

    for idx, msg in enumerate(messages):
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "student":
            last_student_msg = content
        elif role == "tutor":
            phase = _phase_for_turn(messages, idx)
            prev_student = last_student_msg if last_student_msg else "(session start)"
            scores = score_turn(content, prev_student, locked_answer, phase=phase, mode=mode)
            per_turn_scores.append({
                "tutor_response": content[:120] + "..." if len(content) > 120 else content,
                "student_message": prev_student[:80] + "..." if len(prev_student) > 80 else prev_student,
                "phase": phase,
                **scores,
            })

    if per_turn_scores:
        conv_avg = sum(s["average"] for s in per_turn_scores) / len(per_turn_scores)
    else:
        conv_avg = 0.0

    result = {
        "conv_id": conv_id,
        "per_turn_scores": per_turn_scores,
        "conversation_average": conv_avg,
    }

    # Save results
    out_dir = Path(cfg.paths.artifacts) / "euler_scores"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{conv_id}.json"
    out_path.write_text(json.dumps(result, indent=2))
    print(f"Saved EULER scores to {out_path}")

    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--conv_id", type=str, help="Score a single conversation by file stem")
    parser.add_argument("--all", action="store_true", help="Score all saved conversations")
    parser.add_argument("--mode", choices=["local", "llm"], default="local",
                        help="Scoring backend: local heuristics (no API) or llm judge.")
    args = parser.parse_args()

    if args.conv_id:
        conv_path = f"{cfg.paths.artifacts}/conversations/{args.conv_id}.json"
        result = score_conversation(conv_path, mode=args.mode)
        print(f"Average EULER score: {result['conversation_average']:.3f}")
        for i, turn in enumerate(result["per_turn_scores"], 1):
            print(f"  Turn {i} ({turn['phase']}): Q={turn['question_present']:.1f} R={turn['relevance']:.2f} "
                  f"H={turn['helpful']:.2f} NR={turn['no_reveal']:.1f} avg={turn['average']:.2f}")
    elif args.all:
        conv_dir = Path(cfg.paths.artifacts) / "conversations"
        if not conv_dir.exists():
            print(f"No conversations directory found at {conv_dir}")
        else:
            conv_files = list(conv_dir.glob("*.json"))
            print(f"Scoring {len(conv_files)} conversations...")
            all_avgs = []
            for conv_file in conv_files:
                try:
                    result = score_conversation(str(conv_file), mode=args.mode)
                    all_avgs.append(result["conversation_average"])
                    print(f"  {conv_file.stem}: {result['conversation_average']:.3f}")
                except Exception as e:
                    print(f"  {conv_file.stem}: ERROR — {e}")
            if all_avgs:
                print(f"\nOverall average: {sum(all_avgs) / len(all_avgs):.3f}")
    else:
        parser.print_help()
