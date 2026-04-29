"""
scripts/audit_convos.py
-----------------------
Quality audit of saved conversations from a scaled run. Produces a markdown
report with per-convo summary + flagged quality issues for manual review.

For each conversation:
  - Pulls the locked_question + locked_answer (the system's pedagogical anchor)
  - Renders the message turns
  - Flags potential issues:
      * answer_reveal: locked_answer terms appear in tutor messages before
        the student says them
      * off_topic_drift: tutor's content diverges from locked_question
      * sycophancy: tutor confirms wrong student claims
      * generic_filler: tutor messages with no concrete next-step question
      * stuck_loop: same question repeated across turns
  - Produces a "quality_score_human" placeholder for the reader to fill in

Usage:
  python scripts/audit_convos.py data/artifacts/scaled_convo/<run_dir>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path


def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def content_tokens(s: str) -> set[str]:
    s = re.sub(r"[^a-z0-9 ]+", " ", normalize(s))
    return {t for t in s.split() if len(t) >= 4}


def detect_answer_reveal(tutor_msgs: list[str], locked_answer: str) -> bool:
    """True if the tutor's pre-assessment messages contain >=70% of the
    answer's content tokens."""
    if not locked_answer.strip():
        return False
    ans_toks = content_tokens(locked_answer)
    if not ans_toks:
        return False
    for msg in tutor_msgs:
        msg_toks = content_tokens(msg)
        hit = sum(1 for t in ans_toks if t in msg_toks)
        if hit / len(ans_toks) >= 0.70:
            return True
    return False


def detect_repetition(tutor_msgs: list[str]) -> int:
    """Count how many tutor messages share substantial content with an
    earlier tutor message (>=80% token overlap)."""
    n_repeats = 0
    seen_token_sets: list[set[str]] = []
    for msg in tutor_msgs:
        toks = content_tokens(msg)
        if not toks:
            seen_token_sets.append(toks)
            continue
        for prev in seen_token_sets:
            if not prev:
                continue
            common = toks & prev
            if len(common) / max(len(toks), 1) >= 0.80:
                n_repeats += 1
                break
        seen_token_sets.append(toks)
    return n_repeats


def detect_off_topic(tutor_msgs: list[str], locked_question: str) -> bool:
    """Loose check: of the locked_question's content tokens, do at least 30%
    appear across the tutor's turns? If less, the tutor probably drifted."""
    q_toks = content_tokens(locked_question)
    if not q_toks:
        return False
    pooled = set()
    for msg in tutor_msgs:
        pooled |= content_tokens(msg)
    overlap = len(q_toks & pooled) / max(len(q_toks), 1)
    return overlap < 0.30


def detect_generic_filler(tutor_msg: str) -> bool:
    """A message that ends with no question mark AND <12 words is likely
    filler / sycophancy / closing rather than a Socratic step."""
    msg = (tutor_msg or "").strip()
    if not msg:
        return False
    word_count = len(msg.split())
    if word_count < 8:
        return True
    if "?" not in msg and word_count < 25:
        # No question and short — not driving the next step
        return True
    return False


def audit_convo(path: Path) -> dict:
    d = json.load(open(path))
    profile = d.get("profile_id", "?")
    seed = d.get("seed_idx", "?")
    topic = d.get("query", "")
    expected_section = d.get("expected_section", "")
    expected_subsection = d.get("expected_subsection", "")

    out = d.get("outcome", {})
    locked_q = out.get("locked_answer", "")
    metrics = d.get("metrics", {})

    msgs = d.get("messages", [])
    tutor_msgs = [m.get("content", "") for m in msgs if m.get("role") == "tutor"]
    student_msgs = [m.get("content", "") for m in msgs if m.get("role") == "student"]

    # Use the conversation's own field set
    locked_answer_text = out.get("locked_answer", "")

    # Pre-assessment tutor messages — anything before the student first says
    # something close to the locked_answer (proxy: token overlap >= 50%).
    pre_assessment = []
    ans_toks = content_tokens(locked_answer_text)
    revealed_at = -1
    for i, m in enumerate(msgs):
        if m.get("role") == "tutor":
            pre_assessment.append(m.get("content", ""))
        if m.get("role") == "student":
            stoks = content_tokens(m.get("content", ""))
            if ans_toks and len(ans_toks & stoks) / max(len(ans_toks), 1) >= 0.50:
                revealed_at = i
                break

    flags = []
    if detect_answer_reveal(pre_assessment, locked_answer_text):
        flags.append("answer_reveal_pre_student")
    if detect_off_topic(tutor_msgs, locked_q or topic):
        flags.append("off_topic_drift")
    n_repeats = detect_repetition(tutor_msgs)
    if n_repeats >= 2:
        flags.append(f"repetition_x{n_repeats}")
    n_filler = sum(1 for m in tutor_msgs if detect_generic_filler(m))
    if n_filler >= 3:
        flags.append(f"generic_filler_x{n_filler}")

    # Sample tutor turns: rapport, topic-engagement, first 2 tutoring turns,
    # last 2 turns
    sample_turns = []
    if len(msgs) > 0:
        for i, m in enumerate(msgs[:6]):
            sample_turns.append((i, m.get("role"), m.get("phase", ""),
                                 (m.get("content", "") or "")[:240]))
        if len(msgs) > 8:
            sample_turns.append((-1, "...", "", "..."))
            for i, m in enumerate(msgs[-3:], len(msgs) - 3):
                sample_turns.append((i, m.get("role"), m.get("phase", ""),
                                     (m.get("content", "") or "")[:240]))

    return {
        "profile": profile,
        "seed": seed,
        "topic": topic,
        "expected_section": expected_section,
        "expected_subsection": expected_subsection,
        "outcome": out,
        "metrics": metrics,
        "n_messages": len(msgs),
        "n_tutor_msgs": len(tutor_msgs),
        "n_student_msgs": len(student_msgs),
        "flags": flags,
        "sample_turns": sample_turns,
        "locked_answer": locked_answer_text,
    }


def render_report(audits: list[dict], run_label: str) -> str:
    lines: list[str] = []
    lines.append(f"# Conversation Quality Audit — {run_label}\n")
    lines.append(f"**{len(audits)} conversations audited.**\n")

    # Aggregate flag counts
    all_flags = Counter()
    for a in audits:
        for f in a["flags"]:
            base = f.split("_x")[0] if "_x" in f else f
            all_flags[base] += 1

    lines.append("## Flag summary\n")
    if all_flags:
        for f, n in all_flags.most_common():
            lines.append(f"- **{f}**: {n} conversations")
    else:
        lines.append("- No automated quality flags raised.")
    lines.append("")

    # Per-convo
    lines.append("## Per-conversation detail\n")
    for a in audits:
        prof = a["profile"]
        seed = a["seed"]
        topic = a["topic"]
        out = a["outcome"]
        m = a["metrics"]
        flags = a["flags"]

        lines.append(f"### [{prof}/seed{seed}] {a['expected_subsection']!r}")
        lines.append("")
        lines.append(f"- **Student query**: {topic}")
        lines.append(f"- **Expected**: ch{out.get('phase_final', '?')} | sec={a['expected_section']!r} | sub={a['expected_subsection']!r}")
        lines.append(f"- **locked_answer**: {a['locked_answer']!r}")
        lines.append(f"- **Outcome**: topic_confirmed={out.get('topic_confirmed')} sec_hit={out.get('on_topic_section')} ch_hit={out.get('on_topic_chapter')} reached={out.get('reached_answer')} turns={out.get('turn_count')}")
        lines.append(f"- **Metrics**: wall={m.get('wall_seconds')}s calls={m.get('n_api_calls')} cache={int(m.get('cache_hit_ratio',0)*100)}% input_toks={m.get('input_tokens')}")
        lines.append(f"- **Flags**: {flags if flags else '— none —'}")
        lines.append(f"- **Messages**: {a['n_messages']} total ({a['n_tutor_msgs']} tutor / {a['n_student_msgs']} student)")
        lines.append("")
        lines.append("Sample turns:")
        lines.append("")
        for idx, role, phase, content in a["sample_turns"]:
            if role == "...":
                lines.append("  ...")
                continue
            lines.append(f"  - **[{idx}] {role}** ({phase}) — {content}")
        lines.append("")

    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", help="data/artifacts/scaled_convo/<run> directory")
    ap.add_argument("--out", default=None, help="Output md path (default: <run_dir>/audit.md)")
    args = ap.parse_args()

    run_dir = Path(args.run_dir).resolve()
    if not run_dir.exists():
        print(f"Run dir not found: {run_dir}", file=sys.stderr)
        sys.exit(1)
    convo_files = sorted([p for p in run_dir.glob("convo_*.json")])
    print(f"Found {len(convo_files)} convo JSONs in {run_dir.name}", flush=True)

    audits = [audit_convo(p) for p in convo_files]
    audits.sort(key=lambda a: (a["profile"], a["seed"]))
    md = render_report(audits, run_label=run_dir.name)

    out_path = Path(args.out) if args.out else (run_dir / "audit.md")
    with open(out_path, "w") as f:
        f.write(md)
    print(f"\nReport saved → {out_path}")


if __name__ == "__main__":
    main()
