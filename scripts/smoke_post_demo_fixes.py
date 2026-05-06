#!/usr/bin/env python3
"""scripts/smoke_post_demo_fixes.py
─────────────────────────────────
Post-demo-fixes smoke test (2026-05-06 session).

Runs offline (no Bedrock, no live backend) and checks that every code
change shipped during the F4–F14 / N3–N8 / A1–A6 / M-T1–M-T3 work
landed correctly. Designed to run in <1 second so it can fire as a
pre-commit guard or pre-demo health check.

Usage:
    python scripts/smoke_post_demo_fixes.py

Exit code 0 = all checks pass. Non-zero = at least one check failed
(printed to stderr). Each check prints a 1-line PASS/FAIL summary so
diff-against-output is easy.

Companion to `docs/POST_DEMO_FIXES.md` — every DONE item should have a
corresponding check here so regressions surface fast.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

# Ensure repo root is on sys.path so this script runs the same way
# whether invoked via `python scripts/...` or `.venv/bin/python scripts/...`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

PASS, FAIL = "PASS", "FAIL"

CHECKS: list[tuple[str, Callable[[], tuple[str, str]]]] = []


def check(label: str):
    """Decorator — register a function as a smoke check."""
    def deco(fn: Callable[[], tuple[str, str]]) -> Callable[[], tuple[str, str]]:
        CHECKS.append((label, fn))
        return fn
    return deco


# ─────────────────────────────────────────────────────────────────────
# Checks
# ─────────────────────────────────────────────────────────────────────


@check("F1 — session_ended_off_domain in TutorState schema")
def _f1():
    from conversation.state import TutorState
    if "session_ended_off_domain" in TutorState.__annotations__:
        return PASS, "field present"
    return FAIL, "field missing from schema"


@check("F6 — total_help_abuse_turns + total_low_effort_turns + total_off_topic_turns in state")
def _f6():
    from conversation.state import TutorState
    needed = {
        "total_help_abuse_turns",
        "total_low_effort_turns",
        "total_off_topic_turns",
    }
    missing = needed - set(TutorState.__annotations__.keys())
    if not missing:
        return PASS, "all 3 totals present"
    return FAIL, f"missing: {sorted(missing)}"


@check("F13 — PRELOCK_CAP = 10")
def _f13():
    from conversation.topic_lock_v2 import PRELOCK_CAP
    if PRELOCK_CAP == 10:
        return PASS, f"PRELOCK_CAP={PRELOCK_CAP}"
    return FAIL, f"expected 10, got {PRELOCK_CAP}"


@check("F14 — sqlite_store EWMA alpha default = 0.7")
def _f14():
    import inspect
    from memory import sqlite_store
    sig = inspect.signature(sqlite_store.SQLiteStore.upsert_subsection_mastery)
    alpha = sig.parameters.get("alpha")
    if alpha is None or alpha.default != 0.7:
        return FAIL, f"alpha default = {alpha.default if alpha else 'missing'}"
    return PASS, "alpha=0.7"


@check("F5c — haiku_hint_leak_check accepts locked_question kwarg")
def _f5c():
    import inspect
    from conversation.verifier_quartet import haiku_hint_leak_check
    sig = inspect.signature(haiku_hint_leak_check)
    if "locked_question" in sig.parameters:
        return PASS, "locked_question kwarg present"
    return FAIL, "locked_question kwarg missing"


@check("F7 — anchor_history.fetch_prior_locked_questions importable")
def _f7():
    try:
        from conversation.anchor_history import fetch_prior_locked_questions  # noqa
        return PASS, "helper importable"
    except Exception as e:
        return FAIL, f"{type(e).__name__}: {e}"


@check("F11 — rapport prompt says 'SHOULD' (deterministic prior reference)")
def _f11():
    from config import cfg
    rapport = getattr(cfg.prompts, "teacher_rapport_delta", "") or getattr(cfg.prompts, "teacher_rapport", "")
    if "SHOULD" in rapport and "MAY (but are not required" not in rapport:
        return PASS, "prompt tightened to SHOULD"
    return FAIL, "still using MAY-style language"


@check("N3 — clinical counters in TutorState + CLINICAL_TURN_CAP=15")
def _n3():
    from conversation.state import TutorState
    from conversation.assessment_v2 import CLINICAL_TURN_CAP, _run_clinical_preflight  # noqa
    needed = {
        "clinical_help_abuse_count",
        "total_clinical_help_abuse_turns",
        "total_clinical_low_effort_turns",
        "total_clinical_off_topic_turns",
    }
    missing = needed - set(TutorState.__annotations__.keys())
    if missing:
        return FAIL, f"missing fields: {sorted(missing)}"
    if CLINICAL_TURN_CAP != 15:
        return FAIL, f"CLINICAL_TURN_CAP={CLINICAL_TURN_CAP}, expected 15"
    return PASS, f"4 fields present + cap={CLINICAL_TURN_CAP} + helper importable"


@check("N4 — opt_in teacher prompt has phase-transition ack instruction")
def _n4():
    from conversation.teacher_v2 import _MODE_INSTRUCTIONS
    opt_in = _MODE_INSTRUCTIONS.get("opt_in", "")
    if "phase transition" in opt_in.lower() or "PHASE TRANSITION" in opt_in:
        return PASS, "transition instruction present"
    return FAIL, "transition instruction missing from opt_in prompt"


@check("N5 — renderMarkdown.tsx exists + MessageBubble wires it")
def _n5():
    from pathlib import Path
    rm = Path(__file__).parent.parent / "frontend" / "src" / "utils" / "renderMarkdown.tsx"
    mb = Path(__file__).parent.parent / "frontend" / "src" / "components" / "chat" / "MessageBubble.tsx"
    if not rm.exists():
        return FAIL, "renderMarkdown.tsx missing"
    if "renderMarkdown" not in mb.read_text():
        return FAIL, "MessageBubble doesn't import renderMarkdown"
    return PASS, "renderer wired"


@check("N8 — suggest_replies endpoint registered")
def _n8():
    try:
        from backend.api.sessions import (
            suggest_replies, _N8_PROFILES, _color_for_kind,
        )
        if set(_N8_PROFILES.keys()) != {"S1", "S2", "S3", "S4", "S5", "S6"}:
            return FAIL, f"unexpected profiles: {sorted(_N8_PROFILES.keys())}"
        if _color_for_kind("correct") != "green":
            return FAIL, "color map drift"
        return PASS, "endpoint + 6 profiles + color map"
    except Exception as e:
        return FAIL, f"{type(e).__name__}: {e}"


@check("M-T2 — architecture_block defined + substituted into dean_base")
def _mt2():
    from config import cfg
    arch = getattr(cfg.prompts, "architecture_block", "")
    if not arch:
        return FAIL, "architecture_block missing"
    dean_base = getattr(cfg.prompts, "dean_base", "")
    if "{{architecture}}" in dean_base:
        return FAIL, "{{architecture}} token not substituted"
    if "Phase machine" not in dean_base:
        return FAIL, "architecture content not substituted into dean_base"
    return PASS, f"architecture_block={len(arch)}ch, dean_base substituted"


@check("M-T2 wave 2 — teacher_base + 3 high-leverage Dean prompts opt in")
def _mt2_wave2():
    from config import cfg
    targets = ["teacher_base", "dean_close_session_static",
               "mastery_scorer_static", "dean_clinical_turn_static"]
    failures = []
    for k in targets:
        v = getattr(cfg.prompts, k, "") or ""
        if "{{architecture}}" in v:
            failures.append(f"{k}: token not substituted")
        elif "Phase machine" not in v:
            failures.append(f"{k}: arch content missing")
    if failures:
        return FAIL, "; ".join(failures)
    return PASS, f"all {len(targets)} prompts substituted"


@check("N4 wave 2 — TeacherPromptInputs has topic_just_locked + is_first_clinical_turn")
def _n4_wave2():
    import dataclasses
    from conversation.teacher_v2 import TeacherPromptInputs
    fields = {f.name for f in dataclasses.fields(TeacherPromptInputs)}
    needed = {"topic_just_locked", "is_first_clinical_turn"}
    missing = needed - fields
    if missing:
        return FAIL, f"missing: {sorted(missing)}"
    return PASS, "both transition fields present"


@check("M-T3 — dean_memory_summary deleted from base.yaml")
def _mt3():
    from config import cfg
    if getattr(cfg.prompts, "dean_memory_summary", None):
        return FAIL, "dean_memory_summary still present (should be deleted)"
    return PASS, "deleted"


@check("A2 — lifecycle pulls 3 recent + total session count")
def _a2():
    from pathlib import Path
    src = (Path(__file__).parent.parent / "conversation" / "lifecycle_v2.py").read_text()
    if "limit=3, completed_only=True" not in src:
        return FAIL, "still pulling limit=1"
    if "[Session history]" not in src:
        return FAIL, "session count cue missing"
    return PASS, "3 recent + count cue"


@check("A4 — analysis_chat queries mem0 via safe_mem0_read")
def _a4():
    from pathlib import Path
    src = (Path(__file__).parent.parent / "backend" / "api" / "sessions.py").read_text()
    if "safe_mem0_read" not in src:
        return FAIL, "safe_mem0_read not called from analysis_chat"
    return PASS, "mem0 read wired"


@check("A6 — SessionAnalysis transcript uses MessageBubble-like styling")
def _a6():
    from pathlib import Path
    src = (Path(__file__).parent.parent / "frontend" / "src" / "routes" / "SessionAnalysis.tsx").read_text()
    if "renderMarkdown" not in src:
        return FAIL, "transcript not rendering markdown"
    if "sokratic_bot_icon" not in src:
        return FAIL, "tutor icon styling missing"
    return PASS, "icon + bubble + markdown wired"


# ─────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────


def main() -> int:
    fails = 0
    print(f"\n{'─' * 70}")
    print(f"  Post-demo-fixes smoke test ({len(CHECKS)} checks)")
    print(f"{'─' * 70}")
    for label, fn in CHECKS:
        try:
            verdict, detail = fn()
        except Exception as e:
            verdict, detail = FAIL, f"exception: {type(e).__name__}: {e}"
        status_color = "\033[32m" if verdict == PASS else "\033[31m"
        reset = "\033[0m"
        print(f"  {status_color}{verdict}{reset}  {label:<70}  {detail}")
        if verdict == FAIL:
            fails += 1
    print(f"{'─' * 70}")
    if fails == 0:
        print(f"  \033[32m✓ All {len(CHECKS)} checks passed.\033[0m\n")
        return 0
    print(f"  \033[31m✗ {fails}/{len(CHECKS)} checks failed.\033[0m\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
