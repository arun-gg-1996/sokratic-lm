# Eval Resume Journal

Active task survives `/compact` via this doc. After compact, I'll
re-read this and continue from where the table says.

## Current task (2026-05-03)

**Goal:** run 8 hard v2 conversations sequentially, report after each.
Memory-safe (1 worker at a time). Already proven approach.

**Constraint:** v2 multi-worker eval crashed system 3× today (memory).
Sequential is the only reliable path on this 16GB Mac.

**Flow per session:**
```bash
SOKRATIC_USE_V2_FLOW=1 SOKRATIC_RETRIEVER=chunks \
  .venv/bin/python scripts/run_eval_chain.py <student_id>
```

The `run_eval_chain.py` script handles one student chain (all sessions
in that chain). For these 8 single-session runs, the chain has 1
session each.

## The 8 sessions — checklist

| # | student_id (in run_eval_18_convos.PLAN) | Profile | Topic shorthand | Status |
|---|---|---|---|---|
| 1 | `eval18_solo1_S1` | S1 Strong | immune self/non-self | ⏳ |
| 2 | `eval18_solo3_S3` | S3 Weak | long bone structure | ⏳ |
| 3 | `eval18_solo4_S4` | S4 Overconfident | elbow joint motion | ⏳ |
| 4 | `eval18_solo5_S5` | S5 Disengaged | spleen function | ⏳ |
| 5 | `eval18_solo2_S2` | S2 Moderate | brainstem breathing | ⏳ |
| 6 | `eval18_solo6_S6` | S6 Anxious | thyroid metabolism | ⏳ |
| 7 | `eval18_triple1_progressing` (just session 1) | S3 Weak | nephron parts | ⏳ |
| 8 | `eval18_pair3_disengaged` (just session 1) | S5 Disengaged | cardiac cycle phases | ⏳ |

Mark `✓` reached / `✗` not-reached / `🔥` crashed as we go.

## After each session, report:

```
Session N: <id>  profile=<P>  reached=<bool>  turns=<n>  asmt_turn=<n>
locked_topic: <subsection>
cost: $<X.XX>
bugs: <count>
key observation: <1-line — was scaffolding good? did Dean leak? etc>
```

## After all 8 done

Aggregate observations into `docs/PRE_DEMO_ISSUES.md`:
- Pick worst 2-3 patterns observed
- Add to P0/P1 sections
- Decide what to fix before demo

## Pre-flight (before re-running anything post-compact)

Run these to confirm system state:
```bash
# Qdrant up?
nc -z localhost 6333 && echo OK || docker start qdrant-sokratic

# No leftover workers?
ps aux | grep run_eval_chain | grep -v grep

# Memory ok? (>1GB free recommended)
vm_stat | grep "Pages free"
```

## Open todos for after eval

- Q1: Replace `_classify_opt_in` regex with Haiku call
- Q2: Investigate Dean hint_text leak (one-off observed in sanity)
- P1: Per-site A/B compact TOC for `haiku_off_domain`
- P2: Same for `dean._exploration_judge`
- Demo recording → deploy

## How to launch one session (the runbook)

```bash
cd /Users/arun-ghontale/UB/NLP/sokratic
SOKRATIC_USE_V2_FLOW=1 SOKRATIC_RETRIEVER=chunks \
  .venv/bin/python scripts/run_eval_chain.py eval18_solo1_S1
```

Output JSON lands at `data/artifacts/eval_run_18/<student_id>_session1.json`.

To extract reach/turns/cost/locked from a result JSON:
```bash
python3 -c "
import json
d = json.loads(open('data/artifacts/eval_run_18/eval18_solo1_S1_session1.json').read())
o = d['outcome']; fs = d['final_state']
locked = (fs.get('locked_topic') or {}).get('subsection') or '(none)'
print(f\"reached={o['reached_answer']} turns={o['turn_count']} asmt={o['assessment_turn']} \
locked={locked} cost=\${d['debug_summary']['cost_usd']:.4f}\")
"
```
