# Verified Demo Script — actual transcripts

These are the exact conversations we just walked through manually and
verified work end-to-end (rapport, scaffolding, hint, reach, clinical
bonus, image upload). Use this verbatim — copy the **STUDENT** lines
and paste them in order. The **TUTOR** lines are what you should see
land back on screen (slight wording variation is normal — the LLM
isn't deterministic, but the structure should match).

---

## Demo Convo 1 — Strong learner, immune system

**Goal:** show full happy path. Multi-turn scaffolding → reach core
answer → opt-in to clinical bonus → multi-turn clinical reasoning →
positive closeout.

### Turn 0 — rapport

**TUTOR:** _"Good morning..."_ (rapport opener, varies)

### Turn 1 — topic ask

**STUDENT:**
```
How does the immune system distinguish self from non-self?
```

**TUTOR:** _"It sounds like you mean **B Cell Differentiation and Activation** — is that right?"_ (Yes/No card)

### Turn 2 — confirm

→ Click **Yes**

**TUTOR:** _"Got it — let's work on **B Cell Differentiation and Activation** from Chapter 21. What process eliminates immature B cells that bind strongly to self-antigens in the bone marrow by signaling them to destroy themselves via apoptosis?"_

### Turn 3 — vague first attempt

**STUDENT:**
```
hmm i think the bone marrow has some kind of screening for B cells, like it checks if they react to self stuff and removes the ones that do, but i don't remember what that's called specifically
```

**TUTOR:** _Scaffolds: introduces "central tolerance," contrasts apoptosis vs silencing vs modification, asks which best describes apoptosis._

### Turn 4 — narrowing in

**STUDENT:**
```
oh okay so it's permanent removal — apoptosis means the cell actually dies and gets cleared out, not just silenced. so the self-reactive B cells are physically eliminated from the population. that's why it works as a real checkpoint, the body just deletes the dangerous clones before they can do damage
```

**TUTOR:** _Confirms permanent removal, nudges toward the term._

### Turn 5 — name the term (REACH)

**STUDENT:**
```
oh yeah it's permanent removal — programmed cell death means the cell is gone for good. i think this whole process is called clonal deletion, where self-reactive B cells in the bone marrow get killed off so they never make it to circulation
```

**TUTOR:** _"Great work — you've reached the core answer for this topic. If you'd like, I can offer a brief clinical bonus question..."_ (Yes/No card)

### Turn 6 — opt in to clinical bonus

→ Click **Yes**

**TUTOR:** _Clinical scenario: "This 28-year-old woman's immune system is producing antibodies against her own nuclear proteins... what do you think went wrong during her immune development?"_

### Turn 7 — partial clinical reasoning

**STUDENT:**
```
hmm so somewhere along the way the apoptosis signal didn't fire properly for those self-reactive B cells. maybe the threshold for triggering it was off, or the cells didn't bind self-antigens strongly enough in the bone marrow to get flagged for deletion? feels like the checkpoint just failed to recognize them as dangerous
```

**TUTOR:** _"Let me reframe — what aspect of this would you like to think through more carefully?"_

### Turn 8 — pick a thread

**STUDENT:**
```
i guess i'm least sure about HOW the checkpoint actually decides which B cells to kill. like what determines if a self-reactive B cell gets flagged for apoptosis vs slipping through? is it just about how strongly it binds the self-antigen, or is something else going on?
```

**TUTOR:** _Introduces the two variables: affinity + antigen form (tissue-expressed vs soluble). Asks what combination explains the patient's escape._

### Turn 9 — reasoning through

**STUDENT:**
```
okay so combining those two — i think the self-reactive B cells probably had only intermediate affinity for the nuclear antigens, not strong enough to cross the apoptosis threshold. and nuclear stuff is mostly hidden inside cells, so during bone marrow development those B cells might have only encountered tiny amounts of leaked nuclear antigen in soluble form. so instead of being deleted they probably became anergic, and then something later broke that anergy and let them activate
```

**TUTOR:** _Confirms anergy = living silenced cell. Asks about stability of silencing under inflammation / T cell help._

### Turn 10 — mechanistic close (clinical reach)

**STUDENT:**
```
right, so anergy is fundamentally reversible — the cell is alive and still has the receptor, just not currently functional. if she develops inflammation or gets T cell help (like from a coincident infection or molecular mimicry), those silenced B cells can break tolerance and get reactivated. once they do, they class-switch, mature into plasma cells, and start pumping out anti-nuclear antibodies — which is exactly the SLE picture: lupus is essentially central tolerance failure compounded by anergy breakdown in the periphery
```

**TUTOR:** _Confirms complete mechanistic picture. Affirms strong command of layered tolerance._

### Demo end note

⚠️ **Don't type "let's wrap this up"** — there's a known B2-regression
where the closeout incorrectly says *"we didn't fully resolve..."*
even after this excellent reach. Just stop the recording on the
tutor's affirmation message above (Turn 10's response). Logged as
B2-extension for post-demo fix.

---

## Demo Convo 2 — VLM image-driven session

**Goal:** show that uploading an anatomical image auto-locks the right
topic. Demonstrates `+` button on message bar + image preview in
student bubble + topic resolution from VLM caption.

### Turn 0 — rapport

→ Click **+ New chat** in sidebar.

**TUTOR:** _Rapport opener._

### Turn 1 — image upload

→ Click **+** button on the message bar (left of mic).
→ Pick: `/Users/arun-ghontale/UB/NLP/sokratic/vlm/images/strong/01_HERO_strong_inhalation_breathing.jpg`

**WHAT YOU SEE:**
- Spinner runs while VLM analyzes
- A **single student bubble** appears with: image preview on top, caption "Pulmonary Ventilation" underneath
- Activity feed runs

**TUTOR:** _"Got it — let's work on **Pulmonary Ventilation** from Chapter 22 → The Process of Breathing. Which two muscle groups contract during normal quiet inspiration to expand the thoracic cavity?"_

### Turn 2 — partial first attempt

**STUDENT:**
```
i think the diaphragm is the main one — it contracts and flattens, pulling down so the thoracic cavity expands. there's also some chest wall muscles involved but i can't remember the exact name
```

**TUTOR:** _Scaffolds: thoracic cavity as 3D space, asks about floor + walls structures._

### Turn 3 — low-effort signal (triggers help-abuse strike)

**STUDENT:**
```
idk man can you just tell me
```

**TUTOR:** _"I'm here to guide you to the answer, not hand it over... when you take a deep breath in, do you notice your belly moving outward or inward?"_ (Sidebar shows: Off-topic 1/4)

### Turn 4 — engaged answer hitting diaphragm

**STUDENT:**
```
oh okay so my belly moves outward when i breathe in deep — i think that's the diaphragm contracting and pulling down, which kind of pushes everything below it forward. so the diaphragm is the muscle at the base of the thoracic cavity that drives inhalation
```

**TUTOR:** _Confirms diaphragm, nudges toward the second muscle group (external intercostals)._

### Turn 5 — finish naming both muscles (REACH)

**STUDENT:**
```
right, so along with the diaphragm there are also the external intercostals — those run between the ribs and contract to pull the ribcage up and out, which expands the thoracic cavity laterally. so quiet inspiration is the diaphragm flattening downward plus external intercostals lifting the ribs outward, both happening together to drop intrathoracic pressure
```

**TUTOR:** _Reach detected. Offers clinical bonus or wraps positively._

---

## Things to demo besides the conversation

- **🔊 Listen button** — click on any tutor bubble. Reads aloud via browser TTS. Click again to stop.
- **Phase chip** — sidebar header. Cycles Rapport (blue) → Tutoring (green) → Clinical (amber). Disappears when you click "My mastery" or "Chats" — those pages don't show session metadata.
- **Activity feed** — every tutor reply shows the live "Reading message → Resolving topic → Drafting → Reviewing" stages while it processes. Collapsed activity log appears under the final message.
- **Counters** — sidebar: Pre-lock 0/7 (rapport), Turn N/25 (tutoring), Hint N/3, Conversation health pills (Low-effort, Off-topic) appear when strikes accumulate.
- **My mastery → Start** — click any subsection's Start button → tutor rapport appears → ~half-second wait → student-side bubble auto-injects with the subsection name → tutor topic_ack lands.

---

## Known issues to avoid showing on camera

| Issue | Workaround |
|---|---|
| **B2-extension**: saying "let's wrap" or going off-topic causes failure-flavored close even after success | Don't say "wrap up" / "end session." Stop the recording on a tutor affirmation message. |
| Memory drawer ("What I remember about you") often empty | Don't open the drawer mid-demo. |
| Anchor question sometimes contains the answer's definition | Subsection-dependent — the immune-system + breathing topics tested above don't have this issue. Stick to those for the demo. |
| Disengaged path can suggest the same wrong topic 5 times then fall through to a random card | Don't reject a topic suggestion 5+ times. Just accept the first or second. |

---

## Pre-flight before recording

```bash
# Qdrant up?
nc -z localhost 6333 && echo OK || docker start qdrant-sokratic

# Backend running on 8000? Frontend on 5173?
lsof -i :8000 -sTCP:LISTEN | head -2
lsof -i :5173 -sTCP:LISTEN | head -2
```

If anything's down:
```bash
# Backend (from repo root)
.venv/bin/uvicorn backend.main:app --reload --port 8000

# Frontend (from frontend/)
npm run dev
```

Open `http://localhost:5173/chat`. Ensure logged in as `nidhi` (sidebar bottom shows the student id).
