"""
scripts/sweep_via_ws.py
------------------------
Selenium-style fast harness — drives the LIVE backend over HTTP+WS,
exactly the way the React frontend would. Boots NOTHING in-process —
all conversation logic, retrieval, langgraph, mem0 happens in the
running uvicorn server. Same WS payload the sidebar reads.

Why this beats browser-sim:
  * No Chrome, no Playwright, no DOM rendering work.
  * Asserts on the WS debug_payload directly (single source of truth
    for telemetry: phase, hint_level, *_count, locked_question, ...).
  * Records full transcripts in metric-friendly JSONL — ready for
    RAGAS (faithfulness, answer_relevance, context_precision),
    EULER (per-domain knowledge accuracy), verbosity scoring,
    latency percentiles, and any custom rubric.
  * Per-sim wall time is reported live so you can see drift.

Prerequisites:
  * uvicorn running on :8000   (`uvicorn backend.main:app --port 8000`)
  * qdrant up (already running per docker ps)

Run:
  source .venv/bin/activate
  python scripts/sweep_via_ws.py                         # all sims
  python scripts/sweep_via_ws.py --only happy_path       # one sim
  python scripts/sweep_via_ws.py --user nidhi            # use seeded user

Output:
  data/artifacts/sweep_ws_<ts>/sweep.json   — per-turn state + tutor + debug payload
  data/artifacts/sweep_ws_<ts>/transcripts/<sim>.jsonl  — RAGAS/EULER-ready
  data/artifacts/sweep_ws_<ts>/findings.md  — issues to file in POST_DEMO_FIXES.md
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).parent.parent
BACKEND = os.environ.get("SOKRATIC_BACKEND", "http://localhost:8000")
WS_BASE = BACKEND.replace("http://", "ws://").replace("https://", "wss://")

# State fields the WS debug payload exposes that we want to assert on.
TELEMETRY_FIELDS = [
    "phase",
    "turn_count",
    "hint_level",
    "topic_confirmed",
    "prelock_loop_count",
    "topic_selection",
    "locked_question",
    "locked_answer",
    "answer_locked",
    "student_state",
    "assessment_turn",
    "help_abuse_count",
    "off_topic_count",
    "consecutive_low_effort_count",
    "total_help_abuse_turns",
    "total_off_topic_turns",
    "total_low_effort_turns",
    "clinical_help_abuse_count",
    "clinical_off_topic_count",
    "clinical_low_effort_count",
    "total_clinical_help_abuse_turns",
    "total_clinical_off_topic_turns",
    "total_clinical_low_effort_turns",
    "currently_exploring",
    "exploration_count",
    "exploration_query",
    "engaged_wrong_count",
    "dean_hint_override_count",
    "rule_hint_advance_count",
    "clinical_strike_threshold",
    "help_abuse_threshold",
    "off_topic_threshold",
    "low_effort_threshold",
]

# --- Sim scripts ---------------------------------------------------------
# Each sim is a list of dicts: {"text": str, "expect": dict-of-asserts}.
# expect uses: ge / le / eq / contains / not_contains / starts_with / phase.

SIMS: dict[str, dict] = {
    "happy_path": {
        "topic": "compact and spongy bone",
        "description": "Sim 1: rapport -> lock -> partial -> correct -> opt-in -> clinical answer",
        "turns": [
            {"text": "compact and spongy bone",
             "expect": {"locked_question_nonempty": True, "phase": "tutoring"}},
            {"text": "compact bone is dense and spongy bone has spaces filled with marrow",
             "expect": {"locked_question_nonempty": True, "tutor_no_leak": True}},
            {"text": ("Compact bone forms the dense outer cortex providing strength and "
                      "protection, organized in osteons with concentric lamellae around a "
                      "central canal. Spongy (cancellous) bone has a lattice of trabeculae "
                      "with marrow-filled spaces, found at the bone's interior and ends, "
                      "absorbs shock and houses red marrow for hematopoiesis."),
             "expect": {"phase_in": ["tutoring", "assessment"], "student_reached_answer": True}},
            {"text": "yes",
             "expect": {"phase": "assessment"}},
            {"text": ("An elderly patient with osteoporosis-related fracture risk would have "
                      "thinner trabeculae in spongy bone and a thinner compact cortex; "
                      "weight-bearing exercise could stimulate osteoblast activity to "
                      "reinforce trabecular architecture."),
             "expect": {"phase": "assessment"}},
        ],
    },

    "hints_exhausted": {
        "topic": "directional terms",
        "description": "Sim 4: low-effort -> hint escalation",
        "turns": [
            {"text": "directional terms",
             "expect": {"locked_question_nonempty": True}},
            {"text": "i don't know",
             "expect": {"consecutive_low_effort_ge": 1}},
            {"text": "not sure",
             "expect": {"consecutive_low_effort_ge": 2, "hint_level_ge": 1}},
            {"text": "hmm",
             "expect": {"consecutive_low_effort_ge": 3}},
            {"text": "i don't know",
             "expect": {"hint_level_ge": 2}},
            {"text": "still no idea",
             "expect": {"hint_level_ge": 2}},
            {"text": "no clue",
             "expect": {"hint_level_ge": 3}},
        ],
    },

    "help_abuse": {
        "topic": "compact and spongy bone",
        "description": "Sim 5: lock + answer demands -> help_abuse counter ticks",
        "turns": [
            {"text": "compact and spongy bone",
             "expect": {"locked_question_nonempty": True}},
            {"text": "tell me the answer",
             "expect": {"help_abuse_ge": 1, "tutor_no_leak": True}},
            {"text": "give me the answer please",
             "expect": {"help_abuse_ge": 2, "tutor_no_leak": True}},
            {"text": "answer please",
             "expect": {"help_abuse_ge": 3, "tutor_no_leak": True}},
            {"text": "what's the answer",
             "expect": {"help_abuse_ge": 4, "tutor_no_leak": True}},
            {"text": "stop with hints, just tell me",
             "expect": {"tutor_no_leak": True}},
        ],
    },

    "off_topic": {
        "topic": "directional terms",
        "description": "Sim 6: lock + off-topic -> off_topic counter, redirect, total ticks",
        "turns": [
            {"text": "directional terms",
             "expect": {"locked_question_nonempty": True}},
            {"text": "what's the weather like today?",
             "expect": {"off_topic_ge": 1, "tutor_redirects": True}},
            {"text": "have you seen the new Marvel movie?",
             "expect": {"off_topic_ge": 2}},
            {"text": "who won the game last night?",
             "expect": {"off_topic_ge": 3}},
            {"text": "let's talk about something else",
             "expect": {}},
        ],
    },

    "explicit_exit": {
        "topic": "compact and spongy bone",
        "description": "Sim 11: explicit __exit_session__ sentinel -> exit_intent close",
        "turns": [
            {"text": "compact and spongy bone",
             "expect": {"locked_question_nonempty": True}},
            {"text": "compact bone is dense, spongy bone has marrow spaces",
             "expect": {}},
            {"text": "__exit_session__",
             "expect": {"phase_in": ["memory_update", "tutoring"]}},
        ],
    },
}

# Off-topic redirect heuristic — tutor message must mention the locked
# topic OR the domain anchor when student went off-topic.
DOMAIN_ANCHORS = ["anatomy", "body", "structure", "subject", "topic"]


# --- Backend interaction --------------------------------------------------

async def wait_backend_ready(timeout_s: float = 240.0) -> bool:
    """Poll /health until 200 or timeout. Backend warmup loads spacy +
    chunks JSONL + cross-encoder; can take 60-220s cold."""
    deadline = time.time() + timeout_s
    async with httpx.AsyncClient() as client:
        while time.time() < deadline:
            try:
                r = await client.get(f"{BACKEND}/health", timeout=5.0)
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(2.0)
    return False


async def login(username: str, password: str) -> str:
    """POST /api/auth/login → bearer token (Codex demo-auth, 2026-05-06).

    Token is HMAC-signed with SOKRATIC_AUTH_SECRET; used as
    `Authorization: Bearer <token>` for HTTP calls and as `?token=<...>`
    for the WS handshake.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            f"{BACKEND}/api/auth/login",
            json={"username": username, "password": password},
        )
        if r.status_code != 200:
            raise RuntimeError(f"login failed ({r.status_code}): {r.text}")
        return r.json()["token"]


async def start_session(student_id: str, token: str,
                        memory_enabled: bool = True) -> dict:
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(
            f"{BACKEND}/api/session/start",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "student_id": student_id,
                "memory_enabled": memory_enabled,
                "client_hour": datetime.now().hour,
            },
        )
        r.raise_for_status()
        return r.json()


async def send_turn(ws, text: str, turn_timeout_s: float = 90.0) -> dict:
    """Send one student message; collect the next message_complete frame.
    Tokens, activities, and stream_resets are also collected for analysis."""
    await ws.send(json.dumps({"type": "student_message", "content": text}))
    tokens: list[str] = []
    activities: list[dict] = []
    stream_resets = 0
    deadline = time.time() + turn_timeout_s
    while time.time() < deadline:
        try:
            raw = await asyncio.wait_for(
                ws.recv(), timeout=max(1.0, deadline - time.time())
            )
        except asyncio.TimeoutError:
            return {
                "type": "timeout",
                "tokens": tokens,
                "activities": activities,
                "stream_resets": stream_resets,
                "error": f"no message_complete in {turn_timeout_s}s",
            }
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        kind = msg.get("type")
        if kind == "token":
            tokens.append(msg.get("content", ""))
        elif kind == "activity":
            activities.append({
                "label": msg.get("content", ""),
                "detail": msg.get("detail", ""),
            })
        elif kind == "stream_reset":
            stream_resets += 1
            tokens = []  # frontend resets streaming buffer
        elif kind == "message_complete":
            msg["tokens"] = tokens
            msg["activities"] = activities
            msg["stream_resets"] = stream_resets
            return msg
        elif kind == "error":
            return {"type": "error", "error": msg.get("content", "")}
    return {
        "type": "timeout",
        "tokens": tokens,
        "activities": activities,
        "stream_resets": stream_resets,
    }


# --- Assertions -----------------------------------------------------------

def evaluate_expectations(turn_idx: int, expect: dict, frame: dict,
                          locked_answer: str) -> list[str]:
    """Return list of failure descriptions (empty = all pass)."""
    failures: list[str] = []
    debug = frame.get("debug") or {}
    tutor = (frame.get("content") or "").lower()

    if expect.get("locked_question_nonempty"):
        if not str(debug.get("locked_question") or "").strip():
            failures.append(f"T{turn_idx}: expected locked_question to be set")

    if "phase" in expect:
        if debug.get("phase") != expect["phase"]:
            failures.append(
                f"T{turn_idx}: expected phase={expect['phase']!r}, got {debug.get('phase')!r}"
            )
    if "phase_in" in expect:
        if debug.get("phase") not in expect["phase_in"]:
            failures.append(
                f"T{turn_idx}: expected phase in {expect['phase_in']}, got {debug.get('phase')!r}"
            )

    for key, label in [
        ("hint_level_ge", "hint_level"),
        ("help_abuse_ge", "help_abuse_count"),
        ("off_topic_ge", "off_topic_count"),
        ("consecutive_low_effort_ge", "consecutive_low_effort_count"),
    ]:
        if key in expect:
            actual = int(debug.get(label) or 0)
            if actual < expect[key]:
                failures.append(
                    f"T{turn_idx}: expected {label} >= {expect[key]}, got {actual}"
                )

    if expect.get("student_reached_answer"):
        if not bool(debug.get("student_reached_answer", False)):
            failures.append(f"T{turn_idx}: expected student_reached_answer=True")

    if expect.get("tutor_no_leak") and locked_answer:
        ngram = _ngram_overlap(tutor, locked_answer.lower(), n=4)
        if ngram >= 1:
            failures.append(
                f"T{turn_idx}: tutor text shares {ngram} 4-grams with locked answer "
                f"(possible F5c leak)"
            )

    if expect.get("tutor_redirects"):
        if not any(a in tutor for a in DOMAIN_ANCHORS):
            failures.append(
                f"T{turn_idx}: tutor reply on off-topic turn does not mention domain anchor"
            )

    return failures


def _ngram_overlap(a: str, b: str, n: int = 4) -> int:
    def grams(s: str) -> set[str]:
        toks = re.findall(r"[a-z]+", s.lower())
        return {" ".join(toks[i : i + n]) for i in range(0, max(0, len(toks) - n + 1))}
    return len(grams(a) & grams(b))


# --- Verbosity / metric helpers (RAGAS-/EULER-friendly) -----------------

def turn_metrics(student: str, tutor: str, latency_s: float,
                 debug: dict, frame: dict) -> dict:
    """Per-turn metrics suitable for RAGAS / EULER / verbosity later."""
    tutor_words = len(re.findall(r"\b\w+\b", tutor or ""))
    student_words = len(re.findall(r"\b\w+\b", student or ""))
    tutor_chars = len(tutor or "")
    return {
        "latency_s": round(latency_s, 2),
        "verbosity": {
            "student_words": student_words,
            "tutor_words": tutor_words,
            "tutor_chars": tutor_chars,
            "tutor_paragraphs": len([p for p in (tutor or "").split("\n\n") if p.strip()]),
        },
        "tutor_uses_markdown_bold": "**" in (tutor or ""),
        "tutor_uses_markdown_list": bool(re.search(r"^\s*[-\*\d+\.]", tutor or "", re.M)),
        "stream_resets": int(frame.get("stream_resets", 0) or 0),
        "n_tokens_streamed": len(frame.get("tokens") or []),
        "n_activities": len(frame.get("activities") or []),
        "phase": debug.get("phase"),
        "hint_level": debug.get("hint_level"),
        "help_abuse_count": debug.get("help_abuse_count"),
        "off_topic_count": debug.get("off_topic_count"),
        "consecutive_low_effort_count": debug.get("consecutive_low_effort_count"),
        "total_help_abuse_turns": debug.get("total_help_abuse_turns"),
        "total_off_topic_turns": debug.get("total_off_topic_turns"),
        "total_low_effort_turns": debug.get("total_low_effort_turns"),
        "currently_exploring": debug.get("currently_exploring"),
        "student_reached_answer": debug.get("student_reached_answer"),
    }


# --- Sim runner ----------------------------------------------------------

async def run_sim(name: str, sim: dict, student_id: str, token: str,
                  out_dir: Path) -> dict:
    print(f"\n[{name}] starting (topic={sim['topic']!r}, "
          f"{len(sim['turns'])} turns)...", flush=True)
    t_sim_start = time.time()

    # Start session
    started = await start_session(student_id, token)
    thread_id = started["thread_id"]
    rapport_msg = started.get("initial_message", "")
    initial_debug = started.get("initial_debug") or {}
    print(f"[{name}] session={thread_id} | rapport opened", flush=True)

    transcript_path = out_dir / "transcripts" / f"{name}.jsonl"
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_lines = []

    # Record rapport open as turn 0
    transcript_lines.append({
        "turn": 0,
        "role": "rapport_open",
        "student": "<rapport open>",
        "tutor": rapport_msg,
        "debug": initial_debug,
        "metrics": {
            "latency_s": None,
            "verbosity": {
                "tutor_words": len(re.findall(r"\b\w+\b", rapport_msg or "")),
                "tutor_chars": len(rapport_msg or ""),
            },
        },
    })

    failures_all: list[dict] = []

    ws_url = f"{WS_BASE}/ws/chat/{thread_id}?token={token}"
    async with websockets.connect(ws_url, max_size=8 * 1024 * 1024) as ws:
        for i, step in enumerate(sim["turns"], start=1):
            t0 = time.time()
            frame = await send_turn(ws, step["text"], turn_timeout_s=90.0)
            dt = time.time() - t0
            debug = frame.get("debug") or {}
            tutor = frame.get("content") or ""
            locked_answer = str(debug.get("locked_answer") or "")

            failures = evaluate_expectations(i, step.get("expect", {}),
                                             frame, locked_answer)
            for f_ in failures:
                failures_all.append({"turn": i, "failure": f_})

            transcript_lines.append({
                "turn": i,
                "role": "tutoring_turn",
                "student": step["text"],
                "tutor": tutor,
                "debug": {k: debug.get(k) for k in TELEMETRY_FIELDS},
                "expectations": step.get("expect", {}),
                "expectation_failures": failures,
                "metrics": turn_metrics(step["text"], tutor, dt, debug, frame),
            })

            print(
                f"[{name}] T{i} {dt:.1f}s | phase={debug.get('phase')} "
                f"hint={debug.get('hint_level')} "
                f"ha={debug.get('help_abuse_count')}/{debug.get('total_help_abuse_turns')} "
                f"ot={debug.get('off_topic_count')}/{debug.get('total_off_topic_turns')} "
                f"le={debug.get('consecutive_low_effort_count')}/{debug.get('total_low_effort_turns')} "
                f"reach={debug.get('student_reached_answer')} "
                f"fail={len(failures)}",
                flush=True,
            )
            if failures:
                for f_ in failures:
                    print(f"  ✗ {f_}", flush=True)

            # Quick exit on critical errors (timeout, ws error)
            if frame.get("type") in ("timeout", "error"):
                print(f"[{name}] aborting on {frame.get('type')}: "
                      f"{frame.get('error', '?')}", flush=True)
                break

    # Save transcript
    with transcript_path.open("w") as fp:
        for line in transcript_lines:
            fp.write(json.dumps(line) + "\n")

    sim_dt = time.time() - t_sim_start
    n_turns = len(transcript_lines) - 1  # exclude rapport open
    print(f"[{name}] done in {sim_dt:.1f}s ({n_turns} turns) | "
          f"{len(failures_all)} expectation failures", flush=True)

    return {
        "sim": name,
        "thread_id": thread_id,
        "topic": sim["topic"],
        "wall_time_s": round(sim_dt, 2),
        "turns": transcript_lines,
        "failures": failures_all,
    }


# --- Main ----------------------------------------------------------------

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default=os.environ.get("SOKRATIC_SWEEP_USER", "grader"),
                    help="Username for /api/auth/login (default: grader; "
                         "respects SOKRATIC_SWEEP_USER env var)")
    ap.add_argument("--password", default=os.environ.get("SOKRATIC_SWEEP_PASSWORD"),
                    help="Password for /api/auth/login. If omitted, reads "
                         "from SOKRATIC_AUTH_USERS env var.")
    ap.add_argument("--only", default=None,
                    help="Run only one sim (name from SIMS dict)")
    ap.add_argument("--no-wait", action="store_true",
                    help="Skip backend health-check (assumes already up)")
    args = ap.parse_args()

    # Resolve password: explicit arg > SOKRATIC_SWEEP_PASSWORD > read from
    # SOKRATIC_AUTH_USERS pairs.
    password = args.password
    if not password:
        # Parse SOKRATIC_AUTH_USERS for the requested user's password —
        # convenient for local dev where the same .env drives both
        # backend auth and the harness.
        from dotenv import load_dotenv as _ld
        _ld(ROOT / ".env", override=False)
        raw = os.environ.get("SOKRATIC_AUTH_USERS", "")
        for pair in raw.split(","):
            if ":" not in pair:
                continue
            u, p = pair.split(":", 1)
            if u.strip().lower() == args.user.lower():
                password = p.strip()
                break
    if not password:
        print(f"[sweep] no password found for user {args.user!r}. "
              f"Set --password or SOKRATIC_SWEEP_PASSWORD or include "
              f"the user in SOKRATIC_AUTH_USERS.", flush=True)
        return 2

    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    out_dir = ROOT / "data" / "artifacts" / f"sweep_ws_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[sweep] artifact dir: {out_dir}", flush=True)
    print(f"[sweep] backend: {BACKEND}", flush=True)
    print(f"[sweep] user: {args.user}", flush=True)

    if not args.no_wait:
        print("[sweep] waiting for backend /health ...", flush=True)
        if not await wait_backend_ready(timeout_s=300.0):
            print("[sweep] backend never came up; abort.", flush=True)
            return 1
        print("[sweep] backend ready.", flush=True)

    # Login once (token TTL = 14d per backend/auth.py).
    try:
        token = await login(args.user, password)
        print(f"[sweep] auth OK for {args.user}", flush=True)
    except Exception as e:
        print(f"[sweep] login failed: {e}", flush=True)
        return 3

    overall_t0 = time.time()
    runs = []
    sims_to_run = (
        [args.only] if args.only else list(SIMS.keys())
    )
    for name in sims_to_run:
        if name not in SIMS:
            print(f"[sweep] unknown sim: {name}", flush=True)
            continue
        try:
            run = await run_sim(name, SIMS[name], args.user, token, out_dir)
        except Exception as e:
            import traceback
            traceback.print_exc()
            run = {"sim": name, "error": str(e), "turns": []}
        runs.append(run)

    overall_dt = time.time() - overall_t0

    # Aggregate failures + write report
    all_failures = []
    for run in runs:
        for f_ in run.get("failures", []) or []:
            all_failures.append({"sim": run["sim"], **f_})
    summary = {
        "ts": ts,
        "user": args.user,
        "backend": BACKEND,
        "wall_time_s": round(overall_dt, 2),
        "n_sims": len(runs),
        "n_turns": sum(max(0, len(r.get("turns") or []) - 1) for r in runs),
        "n_failures": len(all_failures),
        "failures": all_failures,
        "runs": runs,
    }
    (out_dir / "sweep.json").write_text(json.dumps(summary, indent=2))

    # Findings markdown — POST_DEMO_FIXES.md-friendly
    findings_md = render_findings(summary)
    (out_dir / "findings.md").write_text(findings_md)

    print("\n" + "=" * 70)
    print(f"[sweep] DONE in {overall_dt:.1f}s — "
          f"{summary['n_sims']} sims, {summary['n_turns']} turns, "
          f"{summary['n_failures']} expectation failures.")
    print(f"[sweep] artifacts: {out_dir}")
    print("=" * 70)
    return 0


def render_findings(summary: dict) -> str:
    out = []
    out.append(f"# Sweep findings — {summary['ts']}")
    out.append("")
    out.append(f"Backend: `{summary['backend']}` | User: `{summary['user']}` | "
               f"Sims: {summary['n_sims']} | Turns: {summary['n_turns']} | "
               f"Wall: {summary['wall_time_s']}s")
    out.append("")
    if not summary["failures"]:
        out.append("All expectations passed.")
        return "\n".join(out)
    out.append(f"## {summary['n_failures']} expectation failures")
    out.append("")
    for f_ in summary["failures"]:
        out.append(f"- **[{f_['sim']}]** {f_['failure']}")
    out.append("")
    out.append("## Per-sim wall time")
    out.append("")
    for run in summary["runs"]:
        out.append(f"- `{run['sim']}` → {run.get('wall_time_s', '?')}s "
                   f"({max(0, len(run.get('turns') or []) - 1)} turns)")
    return "\n".join(out)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
