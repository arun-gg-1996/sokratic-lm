#!/usr/bin/env python
"""
demo_seed_nidhi.py
==================
Pre-seed 5 historical sessions for student `nidhi` so the My Mastery
page has visible progression for the live demo.

Each session gets:
  * sessions row (status=completed/ended_turn_limit, locked_question,
    locked_answer, mastery_tier, score, key_takeaways)
  * subsection_mastery row (EWMA score, attempt_count)
  * transcript snapshot at data/artifacts/conversations/...
    (so the analyze page renders real-looking content)

Run:
    python scripts/demo_seed_nidhi.py [--dry-run] [--reset]

Flags:
    --dry-run  print what would be inserted, don't write
    --reset    delete prior demo seeds for nidhi before inserting
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make project root importable when run from anywhere
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv
load_dotenv(REPO / ".env", override=True)

from config import cfg
from memory.sqlite_store import SQLiteStore, score_to_tier


STUDENT_ID = "nidhi"


# ─────────────────────────────────────────────────────────────────────
# 5 hand-crafted demo sessions — varied mastery tiers to show
# progression in My Mastery. Topics spread across chapters so the
# breadcrumb navigation looks natural.
# ─────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────
# Mem0 entries — what the system "learned" about Nidhi from each
# session. Categories per memory_manager.flush:
#   * misconception   — observed factual errors / confusions
#   * learning_style  — observable interaction patterns
# Each entry is 1 sentence with the topic baked in (per L4 write-style).
# ─────────────────────────────────────────────────────────────────────
MEM0_ENTRIES: list[dict] = [
    # S1 — Compact and Spongy Bone (mastered)
    {
        "session_label": "S1_compact_spongy_mastered",
        "category": "learning_style",
        "text": "Nidhi connects structural form to functional purpose quickly when given concrete spatial examples (e.g., femur shaft vs. femur head) — works well with 'where it sits in the body' framing.",
    },
    # S2 — Blood and Nerve Supply (partial)
    {
        "session_label": "S2_blood_nerve_supply_solid",
        "category": "learning_style",
        "text": "Nidhi learns vasomotor concepts through mechanical analogies — the 'dimmer switch' framing for nerve-driven control of vessel diameter landed cleanly when abstract physiology terms hadn't.",
    },
    {
        "session_label": "S2_blood_nerve_supply_solid",
        "category": "misconception",
        "text": "Nidhi initially treated nerves in bone as pain-signal-only, missing the vasomotor and bone-remodeling coordination roles — required scaffolding to recognize nerves regulate blood supply, not just sense it.",
    },
    # S3 — Directional Terms (partial)
    {
        "session_label": "S3_directional_terms_developing",
        "category": "misconception",
        "text": "Nidhi conflates anatomical directional axes — knows superior/inferior describes vertical position but mixes up anterior/posterior with the up-down pair rather than recognizing it as the front-back axis.",
    },
    # S4 — Connective Tissue (struggling)
    {
        "session_label": "S4_connective_tissue_needs_review",
        "category": "misconception",
        "text": "Nidhi has not internalized the extracellular matrix concept for connective tissue — couldn't list ground substance, fibers, and cells without prompting; the matrix as the unifying structural framework is the gap.",
    },
    {
        "session_label": "S4_connective_tissue_needs_review",
        "category": "learning_style",
        "text": "Nidhi tends to ask for direct answers when stuck rather than reasoning through scaffolding — benefits from step-by-step decomposition into smaller sub-questions she can attempt one at a time.",
    },
    # S5 — Body Cavities (ended at turn cap)
    {
        "session_label": "S5_body_cavities_partial",
        "category": "misconception",
        "text": "Nidhi knows the heart and lungs sit in the thoracic cavity but didn't recognize 'mediastinum' as a named subdivision of the thoracic cavity — the central column between the lungs is the missing label, not the spatial intuition.",
    },
]


SESSIONS: list[dict] = [
    # ─── Session 1: STRONG mastery (mastered example) ───────────────
    {
        "label": "S1_compact_spongy_mastered",
        "days_ago": 6,
        "subsection_path": "Bone Tissue and the Skeletal System > Bone Structure > Compact and Spongy Bone",
        "locked_question": "What is the structural difference between compact and spongy bone, and why does each shape match its function?",
        "locked_answer": "compact bone is dense lamellar layers; spongy bone is open trabecular network with marrow",
        "score": 0.85,
        "outcome": "reached",
        "status": "completed",
        "hint_level_final": 1,
        "turn_count": 6,
        "demonstrated": "Student correctly identified the dense vs trabecular structural distinction and connected compact bone's load-bearing role to its layered design.",
        "needs_work": "",
        "messages": [
            {"role": "tutor", "content": "Good morning, Nidhi. Picking up on Bone Microstructure — pick how you'd like to start below.", "phase": "rapport"},
            {"role": "student", "content": "What's the structural difference between compact and spongy bone?"},
            {"role": "tutor", "content": "Good question to dig into. Both are bone tissue, but they're organized very differently. Picture the long shaft of your femur on the outside versus the inside of the head of that same bone — those represent the two structures. What do you notice about how each surface looks if you imagine cutting through it?", "phase": "tutoring"},
            {"role": "student", "content": "compact looks dense and solid, spongy has lots of spaces"},
            {"role": "tutor", "content": "Yes — exactly that contrast. Now think about WHERE each one sits in a typical long bone, and what kind of forces it has to handle in that location. What do you notice?", "phase": "tutoring"},
            {"role": "student", "content": "compact is on outside taking the load, spongy is inside with marrow and trabeculae spreading force"},
            {"role": "tutor", "content": "Nailed it. That's exactly why compact's dense lamellar layers handle compression while spongy's open trabecular network distributes force AND holds marrow. You've got the structure-function link. Nice work today.", "phase": "memory_update"},
        ],
    },
    # ─── Session 2: SOLID mid-tier ──────────────────────────────────
    {
        "label": "S2_blood_nerve_supply_solid",
        "days_ago": 4,
        "subsection_path": "Bone Tissue and the Skeletal System > Bone Structure > Blood and Nerve Supply",
        "locked_question": "Beyond sensing pain, what two additional physiological roles do nerves appear to play once they enter bone tissue?",
        "locked_answer": "regulating blood supply and bone growth",
        "score": 0.68,
        "outcome": "partial",
        "status": "completed",
        "hint_level_final": 2,
        "turn_count": 9,
        "demonstrated": "Student identified that nerves coordinate with vasculature in metabolically active regions of bone.",
        "needs_work": "Direct nerve-driven control of vessel diameter (vasomotor) and the nerve role in bone remodeling cycles weren't fully landed — revisit.",
        "messages": [
            {"role": "tutor", "content": "Hi Nidhi — good to see you. Picking up on Blood and Nerve Supply.", "phase": "rapport"},
            {"role": "student", "content": "Beyond sensing pain, what two additional physiological roles do nerves appear to play once they enter bone tissue?"},
            {"role": "tutor", "content": "Nice question. Notice the text says nerves concentrate in the *metabolically active* regions — so think about what TWO distinct processes need coordination in those areas. One involves something flowing through the vessels already there; the other involves the physical structure of the bone changing over time. Take a stab at one.", "phase": "tutoring"},
            {"role": "student", "content": "i don't know"},
            {"role": "tutor", "content": "No rush. Let's just take the first one. Nerves travel right alongside blood vessels in many tissues, and one thing they're known to do is influence those vessels — like a dimmer switch that can open them wider or clamp them down. What do you think that kind of control over blood vessels in bone might be called?", "phase": "tutoring"},
            {"role": "student", "content": "blood flow control? something about vessel size"},
            {"role": "tutor", "content": "Yes — that's the right shape. The general idea is nerve-driven control over vessel diameter. Now the second role: bone is constantly being broken down and rebuilt throughout life. What's that ongoing turnover process called, and how might nerves play a role?", "phase": "tutoring"},
            {"role": "student", "content": "remodeling? the cycle that keeps bone fresh"},
            {"role": "tutor", "content": "Yes, remodeling. So the two roles together: regulating blood supply (the dimmer-switch idea) and helping coordinate the remodeling cycle. You got the shape. Great session.", "phase": "memory_update"},
        ],
    },
    # ─── Session 3: DEVELOPING ──────────────────────────────────────
    {
        "label": "S3_directional_terms_developing",
        "days_ago": 3,
        "subsection_path": "An Introduction to the Human Body > Anatomical Terminology > Directional Terms",
        "locked_question": "What is the difference between 'superior' and 'inferior' as anatomical directional terms, and how does that differ from 'anterior' and 'posterior'?",
        "locked_answer": "superior/inferior describe vertical position (up/down); anterior/posterior describe front/back",
        "score": 0.52,
        "outcome": "partial",
        "status": "completed",
        "hint_level_final": 2,
        "turn_count": 10,
        "demonstrated": "Student correctly identified superior as 'higher' and inferior as 'lower', and could place the head as superior to the chest.",
        "needs_work": "Anterior vs posterior wasn't fully landed — kept conflating with superior/inferior. Worth re-running with focus on the two distinct axis pairs (vertical vs front-back).",
        "messages": [
            {"role": "tutor", "content": "Welcome back. Picking up on Directional Terms.", "phase": "rapport"},
            {"role": "student", "content": "What's the difference between superior/inferior and anterior/posterior?"},
            {"role": "tutor", "content": "Solid question to dig into. These two pairs describe DIFFERENT axes of the body. Start with superior and inferior — what direction do you think those describe? Picture standing upright.", "phase": "tutoring"},
            {"role": "student", "content": "up and down? superior is higher?"},
            {"role": "tutor", "content": "Yes — superior is up, inferior is down. So your head is superior to your chest. Now anterior and posterior — these describe a DIFFERENT axis. Not up-down. What other axis can you think of for a body standing upright?", "phase": "tutoring"},
            {"role": "student", "content": "left and right? medial?"},
            {"role": "tutor", "content": "Different axis again — left/right is its own pair (lateral/medial). I'm thinking of the front-back axis. Your nose vs the back of your head — which is anterior, which is posterior?", "phase": "tutoring"},
            {"role": "student", "content": "i don't know"},
            {"role": "tutor", "content": "Anterior means 'front' — your nose is anterior to your spine. Posterior is 'back'. So the two pairs are: superior/inferior (vertical) and anterior/posterior (front-back). Worth a fresh pass to lock this in — you had the geography right, the naming pairs need solidifying.", "phase": "memory_update"},
        ],
    },
    # ─── Session 4: NEEDS REVIEW ────────────────────────────────────
    {
        "label": "S4_connective_tissue_needs_review",
        "days_ago": 2,
        "subsection_path": "The Tissue Level of Organization > Connective Tissue Supports and Protects > Classification of Connective Tissues",
        "locked_question": "What three structural elements distinguish all connective tissues, regardless of subtype?",
        "locked_answer": "ground substance, fibers, and cells (in an extracellular matrix)",
        "score": 0.22,
        "outcome": "not_reached",
        "status": "completed",
        "hint_level_final": 3,
        "turn_count": 14,
        "demonstrated": "",
        "needs_work": "Student didn't reach the three-element distinction (ground substance + fibers + cells in extracellular matrix). Repeatedly asked for the answer rather than attempting. Revisit Connective Tissue chapter from the start — the matrix concept is foundational for everything that follows.",
        "messages": [
            {"role": "tutor", "content": "Good evening, Nidhi.", "phase": "rapport"},
            {"role": "student", "content": "What three structural elements distinguish all connective tissues?"},
            {"role": "tutor", "content": "Good place to start. Connective tissue looks wildly different across types — think bone vs blood vs cartilage — but the textbook lists THREE shared structural elements that show up in all of them. One is the cells themselves. What about the stuff BETWEEN the cells?", "phase": "tutoring"},
            {"role": "student", "content": "i don't know"},
            {"role": "tutor", "content": "No worries. The space between connective tissue cells is called the extracellular matrix — it has two main components. One is a gel-like or fluid-like substance that fills the space; the other is thread-like protein structures that give the tissue its strength. Take a guess at one.", "phase": "tutoring"},
            {"role": "student", "content": "tell me"},
            {"role": "tutor", "content": "Staying Socratic. The thread-like structures — what general term do you know for protein threads in tissue?", "phase": "tutoring"},
            {"role": "student", "content": "fibers?"},
            {"role": "tutor", "content": "Yes — fibers. Now the gel/fluid component that surrounds them?", "phase": "tutoring"},
            {"role": "student", "content": "i don't know"},
            {"role": "tutor", "content": "Hint 2 of 3: it's a literal description — the 'background' material the fibers and cells sit in. Two words.", "phase": "tutoring"},
            {"role": "student", "content": "give me the answer"},
            {"role": "tutor", "content": "The pattern of asking rather than attempting is what we'd want to break into next time. The textbook calls it 'ground substance'. So the three elements: cells, fibers, ground substance — all sitting in the extracellular matrix. Worth a fresh start on this section soon — the matrix concept anchors everything else.", "phase": "memory_update"},
        ],
    },
    # ─── Session 5: ENDED EARLY (ran out of turns) ──────────────────
    {
        "label": "S5_body_cavities_partial",
        "days_ago": 1,
        "subsection_path": "An Introduction to the Human Body > Anatomical Terminology > Body Cavities and Serous Membranes",
        "locked_question": "How is the thoracic cavity subdivided, and what membrane lines each subdivision?",
        "locked_answer": "two pleural cavities (each with pleural membrane around a lung) and the mediastinum (containing the heart in its pericardial cavity)",
        "score": 0.38,
        "outcome": "partial",
        "status": "ended_turn_limit",
        "hint_level_final": 2,
        "turn_count": 25,
        "demonstrated": "Student knew the thoracic cavity contains the heart and lungs and that each organ has a serous membrane.",
        "needs_work": "Naming the THREE subdivisions (two pleural cavities + mediastinum) didn't quite land before the turn cap. The mediastinum as a distinct named region is the gap. Revisit when fresh.",
        "messages": [
            {"role": "tutor", "content": "Hi again. Picking up on Thoracic Cavity Subdivisions.", "phase": "rapport"},
            {"role": "student", "content": "How is the thoracic cavity subdivided?"},
            {"role": "tutor", "content": "Good. The thoracic cavity contains a few key organs — what are they?", "phase": "tutoring"},
            {"role": "student", "content": "heart and lungs"},
            {"role": "tutor", "content": "Right. Each one is wrapped in its own fluid-filled membrane space. The lungs sit in TWO of those (one for each lung), and the heart sits in its own. So how many separate cavity-like compartments does that give you in the thoracic cavity total?", "phase": "tutoring"},
            {"role": "student", "content": "three?"},
            {"role": "tutor", "content": "Yes. Two for the lungs (called pleural cavities) and one for the heart (called pericardial). But there's a fourth named region — the central column where the heart sits, between the lungs. What might that central region be called?", "phase": "tutoring"},
            {"role": "student", "content": "i don't know"},
            {"role": "tutor", "content": "(Session ended at the turn limit.) The mediastinum is the central column — and it's a distinct named subdivision worth locking in. You had the cavity geography down; the naming is what to revisit.", "phase": "memory_update"},
        ],
    },
]


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def shifted(days_ago: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago, hours=2)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def write_transcript_file(student_id: str, thread_id: str, turn_count: int, payload: dict) -> Path:
    """Mirror conversation/lifecycle_v2.py::_write_transcript_snapshot naming."""
    artifacts_dir = REPO / "data" / "artifacts" / "conversations"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    thread_suffix = thread_id.split("_", 1)[-1] if "_" in thread_id else thread_id
    out = artifacts_dir / f"{student_id}_{thread_suffix}_turn_{turn_count}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return out


def seed_mem0(student_id: str, session_metadata_by_label: dict) -> dict:
    """Write the curated mem0 entries for the demo sessions.

    Each entry carries metadata matching what memory_manager.flush() writes:
      * category  — 'misconception' / 'learning_style'
      * subsection_path / subsection_title / chapter_num — topic anchors
        used by mem0 filtered retrieval (rapport greeting + tutoring
        carryover injection points).

    Mem0 unavailable (no Qdrant / no OPENAI_API_KEY) → returns
    skipped/0/0; the script still completes so SQLite-backed seeds
    are usable on their own.
    """
    from memory.persistent_memory import PersistentMemory
    pm = PersistentMemory()
    if not pm.available:
        return {"available": False, "written": 0, "failed": 0, "reason": pm.unavailable_reason}

    written = 0
    failed = 0
    chapter_num_lookup = {
        "An Introduction to the Human Body": 1,
        "The Tissue Level of Organization": 4,
        "Bone Tissue and the Skeletal System": 6,
    }
    for entry in MEM0_ENTRIES:
        sess_meta = session_metadata_by_label.get(entry["session_label"])
        if not sess_meta:
            continue
        path = sess_meta["subsection_path"]
        chapter = path.split(" > ")[0]
        section = path.split(" > ")[1] if " > " in path else ""
        subsection = path.split(" > ")[-1]

        # Compute the per-session date (matches the days_ago shift used
        # for the SQLite session row) so the frontend grouper at
        # MemoryDrawer.tsx:groupBySession produces ONE bucket per
        # demo session, not one mega-bucket.
        session_dt = datetime.now(timezone.utc) - timedelta(days=sess_meta["days_ago"], hours=2)
        session_date = session_dt.strftime("%Y-%m-%d")
        session_at = session_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        outcome = sess_meta["outcome"]

        metadata = {
            "category": entry["category"],
            # ── Frontend grouping keys (MemoryDrawer.tsx:groupBySession) ──
            "session_date": session_date,
            "topic_path": path,           # alias of subsection_path; frontend reads this
            "outcome": outcome,
            # ── Topic anchors (used by mid-session injection retrieval) ──
            "chapter": chapter,
            "section": section,
            "subsection_title": subsection,
            "subsection_path": path,
            "section_path": f"{chapter} > {section}",
            "chapter_num": chapter_num_lookup.get(chapter, 0),
            # ── Session linkage ──
            "session_at": session_at,
            "session_label": entry["session_label"],
        }
        ok = pm.add(student_id, entry["text"], metadata=metadata)
        if ok:
            written += 1
        else:
            failed += 1
    return {"available": True, "written": written, "failed": failed, "total": len(MEM0_ENTRIES)}


def reset_mem0(student_id: str) -> int:
    """Wipe ALL mem0 entries for this student (demo cleanup).

    Intentionally aggressive — for demo prep we want a clean slate. If
    you need to preserve real entries, comment this out or scope it to
    metadata.session_label LIKE 'demo_seed_%'.
    """
    from memory.persistent_memory import PersistentMemory
    pm = PersistentMemory()
    if not pm.available:
        return 0
    return pm.delete_user(student_id)


def reset_demo_seeds(store: SQLiteStore, student_id: str) -> int:
    """Drop existing demo-seed sessions for this student.

    SAFE deletion — only touches:
      * sessions rows where thread_id starts with 'demo_seed_'
      * subsection_mastery rows where the path matches one of the
        demo subsections (preserves any pre-existing real mastery)
      * transcript files matching the demo thread_id pattern

    Real sessions (with thread_id like 'nidhi_<hex8>') and any mastery
    rows for non-demo subsections are NOT touched.
    """
    conn = store._conn()

    # 1. Find all demo-seed session thread_ids
    cur = conn.execute(
        "SELECT thread_id FROM sessions WHERE student_id=? AND thread_id LIKE 'demo_seed_%'",
        (student_id,),
    )
    demo_thread_ids = [r["thread_id"] if isinstance(r, dict) else r[0] for r in cur.fetchall()]
    n = len(demo_thread_ids)

    if n:
        # 2. Delete demo session rows
        conn.execute(
            "DELETE FROM sessions WHERE student_id=? AND thread_id LIKE 'demo_seed_%'",
            (student_id,),
        )
        # 3. Delete ONLY the subsection_mastery rows whose paths match
        #    our demo set — preserves any pre-existing real mastery.
        demo_paths = [s["subsection_path"] for s in SESSIONS]
        if demo_paths:
            placeholders = ",".join("?" * len(demo_paths))
            conn.execute(
                f"DELETE FROM subsection_mastery WHERE student_id=? AND subsection_path IN ({placeholders})",
                (student_id, *demo_paths),
            )
        conn.commit()

    # 4. Clean transcript files matching demo thread_ids
    artifacts_dir = REPO / "data" / "artifacts" / "conversations"
    if artifacts_dir.exists():
        for tid in demo_thread_ids:
            suffix = tid.split("_", 1)[-1] if "_" in tid else tid
            for f in artifacts_dir.glob(f"{student_id}_{suffix}_turn_*.json"):
                try:
                    f.unlink()
                except OSError:
                    pass
    return n


def seed_one(store: SQLiteStore, sess: dict, *, dry_run: bool) -> dict:
    """Insert ONE pre-seeded session row + mastery row + transcript file."""
    suffix = uuid.uuid4().hex[:8]
    thread_id = f"demo_seed_{sess['label']}_{suffix}"
    started_at = shifted(sess["days_ago"])
    ended_at_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00")) + timedelta(minutes=12)
    ended_at = ended_at_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    transcript_path = (
        REPO / "data" / "artifacts" / "conversations"
        / f"{STUDENT_ID}_{thread_id.split('_', 1)[-1]}_turn_{sess['turn_count']}.json"
    )

    if dry_run:
        return {
            "thread_id": thread_id,
            "would_insert_session": True,
            "would_insert_mastery": True,
            "would_write_transcript": str(transcript_path),
            "tier": score_to_tier(sess["score"]),
        }

    # 1. session row
    store.start_session(
        thread_id,
        STUDENT_ID,
        message_log_path=str(transcript_path),
    )
    # Manually set started_at (start_session uses utc_now)
    conn = store._conn()
    conn.execute(
        "UPDATE sessions SET started_at=? WHERE thread_id=?",
        (started_at, thread_id),
    )
    conn.commit()

    tier = score_to_tier(sess["score"])
    store.end_session(
        thread_id,
        status=sess["status"],
        ended_at=ended_at,
        locked_topic_path=sess["subsection_path"],
        locked_subsection_path=sess["subsection_path"],
        locked_question=sess["locked_question"],
        locked_answer=sess["locked_answer"],
        full_answer=sess["locked_answer"],
        reach_status=(sess["outcome"] == "reached"),
        mastery_tier=tier,
        core_mastery_tier=tier,
        clinical_mastery_tier="not_assessed",
        core_score=float(sess["score"]),
        clinical_score=None,
        hint_level_final=int(sess["hint_level_final"]),
        turn_count=int(sess["turn_count"]),
        key_takeaways={
            "demonstrated": sess["demonstrated"],
            "needs_work": sess["needs_work"],
            "close_reason": "tutoring_cap" if sess["status"] == "ended_turn_limit"
                            else ("reach_full" if sess["outcome"] == "reached"
                                  else "hints_exhausted"),
        },
    )

    # 2. subsection_mastery row
    store.upsert_subsection_mastery(
        STUDENT_ID,
        sess["subsection_path"],
        fresh_score=float(sess["score"]),
        outcome=sess["outcome"],
        session_at=ended_at,
    )

    # 3. transcript file
    transcript_payload = {
        "student_id": STUDENT_ID,
        "thread_id": thread_id,
        "phase": "memory_update",
        "close_reason": "tutoring_cap" if sess["status"] == "ended_turn_limit"
                        else ("reach_full" if sess["outcome"] == "reached"
                              else "hints_exhausted"),
        "session_ended": True,
        "locked_topic": {
            "path": sess["subsection_path"],
            "subsection": sess["subsection_path"].split(" > ")[-1],
        },
        "locked_question": sess["locked_question"],
        "locked_answer": sess["locked_answer"],
        "hint_level": sess["hint_level_final"],
        "messages": sess["messages"],
    }
    write_transcript_file(STUDENT_ID, thread_id, sess["turn_count"], transcript_payload)

    return {
        "thread_id": thread_id,
        "tier": tier,
        "score": sess["score"],
        "subsection": sess["subsection_path"],
        "messages_count": len(sess["messages"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="show what would be inserted")
    parser.add_argument("--reset", action="store_true", help="delete prior demo seeds first")
    args = parser.parse_args()

    store = SQLiteStore()
    store.ensure_student(STUDENT_ID, display_name="Nidhi (demo)")

    if args.reset:
        n = reset_demo_seeds(store, STUDENT_ID)
        print(f"  reset: removed {n} prior demo seed sessions (SQLite)")
        if not args.dry_run:
            mem0_deleted = reset_mem0(STUDENT_ID)
            print(f"  reset: removed {mem0_deleted} mem0 entries for {STUDENT_ID}")

    print(f"\nSeeding {len(SESSIONS)} demo sessions for student '{STUDENT_ID}'")
    print(f"  domain    : {getattr(cfg.domain, 'name', 'unknown')}")
    print(f"  dry_run   : {args.dry_run}")
    print()

    for i, sess in enumerate(SESSIONS, 1):
        result = seed_one(store, sess, dry_run=args.dry_run)
        tier = result.get("tier", "?")
        print(f"  [{i}/{len(SESSIONS)}] {sess['label']:<40} → tier={tier:>14}  score={sess['score']:.2f}")
        if not args.dry_run:
            print(f"             thread_id: {result['thread_id']}")
            print(f"             path:      {result['subsection']}")

    # Capture each session's metadata for mem0 seeding (after the loop
    # so all SQLite/transcript writes complete first)
    session_metadata_by_label = {sess["label"]: sess for sess in SESSIONS}

    print()
    if args.dry_run:
        print(f"Dry run — would also seed {len(MEM0_ENTRIES)} mem0 entries.")
        print("Re-run without --dry-run to seed.")
    else:
        # 4. mem0 seed (cross-session memory for rapport greeting + injection points)
        print()
        mem0_result = seed_mem0(STUDENT_ID, session_metadata_by_label)
        if mem0_result.get("available"):
            print(f"Mem0: wrote {mem0_result['written']}/{mem0_result['total']} entries"
                  + (f" ({mem0_result['failed']} failed)" if mem0_result['failed'] else ""))
        else:
            print(f"Mem0: SKIPPED — {mem0_result.get('reason', 'unavailable')}")
            print("       (SQLite + transcript seeds still complete — rapport greeting will work,")
            print("        just without cross-session misconception/learning_style hints)")
        print()
        print("Done. Open My Mastery to verify all 5 sessions appear with correct tiers.")


if __name__ == "__main__":
    main()
