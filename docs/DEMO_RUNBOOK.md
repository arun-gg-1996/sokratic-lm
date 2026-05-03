# Sokratic Demo Runbook

Step-by-step guide for the recorded demo. Use this to drive the conversation
on screen — type the bolded text exactly. Each scenario shows the expected
tutor behavior so you know if something's off.

---

## Pre-flight (before recording)

```bash
# Qdrant up?
nc -z localhost 6333 && echo OK || docker start qdrant-sokratic

# Backend running on 8000?
lsof -i :8000 -sTCP:LISTEN

# Frontend running on 5173?
lsof -i :5173 -sTCP:LISTEN
```

If anything's down, restart:
```bash
# Backend
.venv/bin/uvicorn backend.main:app --reload --port 8000

# Frontend (in frontend/)
npm run dev
```

Open browser to **http://localhost:5173/chat**. Sign in / pick the demo
student account.

---

## Demo flow #1 — Strong learner, free-text topic

**Goal:** show the full happy path — natural topic resolution, scaffolded
hints, reach detection, clean closeout.

| Turn | What you type | What you should see |
|---|---|---|
| 1 (rapport) | _wait for tutor opener_ | "Good morning..." |
| 2 (topic ask) | `How does the immune system distinguish self from non-self?` | Activity feed: Reading → Resolving topic → topic confirm card |
| 3 (confirm) | Click **Yes** to "B Cell Differentiation and Activation" | Activity: Topic locked → Loading textbook → Setting up anchor → tutor asks an apoptosis question |
| 4 (vague answer) | `hmm i think the bone marrow has some kind of screening for B cells, like it checks if they react to self stuff and removes the ones that do, but i don't remember what that's called specifically` | Tutor scaffolds: hints at "central tolerance" + "permanent removal vs silenced" |
| 5 (hint nudge) | `oh okay so it's permanent removal — apoptosis means the cell actually dies and gets cleared out` | Tutor narrows to the term |
| 6 (correct answer) | `i think this whole process is called clonal deletion, where self-reactive B cells in the bone marrow get killed off so they never make it to circulation` | **Reach detected.** Tutor confirms + offers clinical bonus |
| 7 (decline bonus) | Click **No** on the bonus | **Closeout — must be positive** ("You reached the answer..." NOT "we didn't get to cover...") |

**What this demos:**
- Topic resolution (free text → textbook subsection)
- Multi-turn scaffolding without revealing the answer
- Reach detection
- B2 fix: positive closeout when student succeeds

---

## Demo flow #2 — VLM (image-driven session)

**Goal:** show that an uploaded anatomical image auto-locks the right topic.

| Step | What you do | What you should see |
|---|---|---|
| 1 | New chat | Tutor rapport opener |
| 2 | Click **+ button** on the message bar (left of mic) | File picker opens |
| 3 | Pick `data/eval/vlm_test_images/process_of_breathing.png` (or any HERO-tagged image in that folder) | Spinner: "Analyzing image..." |
| 4 | Wait ~5 sec | Topic auto-locks to **The Process of Breathing** at 0.95 confidence; tutor opens with first anchor question |
| 5 (engaged answer) | `breathing happens because the diaphragm contracts and pulls down, which expands the thoracic cavity and lowers pressure inside the lungs, so air rushes in to equalize` | Activity feed runs; tutor responds with follow-up |

**What this demos:**
- VLM lock confidence
- The new `+` button on the message bar (replaces the big top card)
- Activity feed visible during processing

---

## Demo flow #3 — My Mastery → Start (pre-seeded subsection)

**Goal:** show a student returning to a specific subsection from their
mastery dashboard. Demos the natural "queued auto-message after rapport"
flow.

| Step | What you do | What you should see |
|---|---|---|
| 1 | Click **My mastery** in the sidebar | Mastery tree loads. **Phase chip should disappear** from sidebar (chat-only) |
| 2 | Expand any chapter → expand a section → find an **untouched** subsection (gray dot) with a clear topic, e.g. **"Body Cavities and Serous Membranes"** under Ch1 → Anatomical Terminology | Subsection visible with **Start** button |
| 3 | Click **Start** | Navigates to fresh chat |
| 4 | Wait — flow should be: tutor rapport types out fully → ~half-second pause → student bubble auto-injects with the subsection name → activity feed runs → tutor topic_ack with anchor question | Sequential, no stacked tutor messages |
| 5 (answer) | Engage with the anchor question naturally | Normal tutoring loop |

**What this demos:**
- My Mastery dashboard
- Pre-seeded memory: "I want to revisit X" auto-fills
- Natural conversation rhythm (rapport waits to finish before student bubble)
- Activity feed during topic resolution

---

## Demo flow #4 — Pre-seeded MyMastery profile (memory carryover)

**Goal:** show that mastery + open threads from prior sessions carry over.

(For demo: use a student account that has 2-3 prior completed sessions in
their mastery store. Ideally one reached, one unreached, one with a specific
misconception flagged.)

| Step | What you do | What you should see |
|---|---|---|
| 1 | Navigate to `/mastery` | See chapter mastery breakdown with green/yellow/red bars + counters reflecting prior work |
| 2 | Find a **previously-touched** subsection (yellow or red dot — partial mastery) | Shows mastery score + attempt count |
| 3 | Click **Revisit** | Tutor rapport mentions the prior session ("Last time we worked on X — want to keep going?") via mem0 carryover |
| 4 | Continue normally | Hint progression starts at appropriate level for that student's tier |

**What this demos:**
- Persistent mastery store (BKT-style scoring)
- Mem0 carryover into prompts
- Per-concept knowledge tracing

---

## Things to watch for during recording

✅ **Phase chip in sidebar** changes color: blue (Rapport) → green (Tutoring) → amber (Clinical)
✅ **Phase chip disappears** when navigating to /mastery or /chats
✅ **Activity feed** runs during every tutor reply: avatar + bubble + spinner on current step + ✓ on completed steps
✅ **🔊 Listen button** below every tutor bubble — click → reads aloud
✅ **+ button** on message bar appears only on fresh chat (before topic lock); hides once topic is locked
✅ **Closeouts** when student declines bonus or reaches answer: positive language ("You reached..." / "Good work...")

❌ **Red flags** — if any of these happen, restart that demo flow:
- "Your message got cut off" reply when student typed a normal short message
- Two tutor messages stacking immediately (no student bubble between)
- "We didn't get to cover X" closeout when reached=true
- Activity feed missing entirely
- Student bubble appearing mid-rapport-stream

---

## Known issues (already logged for post-demo)

| ID | Issue | Doc |
|---|---|---|
| Q2 | Dean planning prose can occasionally leak into tutor message | PRE_DEMO_ISSUES.md |
| Q3 | 3 close-mode prompts is a band-aid (should be 1 mode that reads state) | PRE_DEMO_ISSUES.md |
| Q4 | Q2 verify-loop + v1-vs-v2 eval not run | PRE_DEMO_ISSUES.md |
| B3 | Topic resolver doesn't re-rank after rejection (disengaged path) | PRE_DEMO_ISSUES.md |

These will not show up in the demo flows above — they only appear on
disengaged students or specific edge cases.

---

## Quick troubleshooting

**Activity feed empty during a turn:**
- Check backend logs for fire_activity calls
- Make sure backend is restarted to pick up the v2 fire_activity wiring

**Phase chip showing on /mastery:**
- Hard-refresh browser (Cmd+Shift+R) to bust cache; the route check is client-side

**TTS doesn't play:**
- Some browsers (Firefox) have partial Web Speech API support; use Chrome/Safari

**"Tutor is responding…" never finishes:**
- Backend likely crashed; check terminal. Restart backend and refresh the chat page.
