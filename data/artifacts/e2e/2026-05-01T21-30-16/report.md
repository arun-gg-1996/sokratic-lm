# Sokratic-OT e2e test report — 2026-05-01T21-30-16

**Pass:** 1/2  |  **API calls:** 18  |  **Cost:** ~$0.1668

## Summary table

| Scenario | Category | Result | Cost |
|---|---|---|---|
| A1 | A | FAIL | $0.1258 |
| G1 | G | PASS | $0.0410 |

## A1 FAIL: Cooperative trajectory (SA node / heart conduction)

_Student progresses to naming the SA node on a well-covered concept._

Expected concept: `SA node`

### Turn 1 (23.5s)

**Student:** What part of the heart starts the heartbeat?

**Tutor:** Heart development is a rich area, but let me point you toward some well-mapped topics where we can really dig in — pick one and we'll get specific fast.    1. Coronary Circulation   2. Internal Structure of the Heart   3. Respiratory Regulation of Acid-Base Balance

- [PASS] non-empty — response length: 265 chars
- [PASS] question or card list — numbered card-pick list present
- [PASS] ≤ 2 question marks — 0 '?' in response
- [PASS] no meta-leak — no meta phrases
- [PASS] no concept reveal — no discriminating part of 'sinoatrial node' present

_state: phase=tutoring, locked='(none)', reached=False, cov=0.0, path=''_

### Turn 2 (15.7s)

**Student:** Is it some kind of pacemaker cells?

**Tutor:** A few of your questions haven't lined up with what's well-covered in our material — let's get you on solid ground. Pick one of the cards below, or try rephrasing your question from a different angle.    1. Tissue and Aging   2. Autoimmune Responses   3. Cognitive Abilities

- [PASS] non-empty — response length: 273 chars
- [PASS] no meta-leak — no meta phrases
- [PASS] no concept reveal — no discriminating part of 'sinoatrial node' present

_state: phase=tutoring, locked='(none)', reached=False, cov=0.0, path=''_

### Turn 3 (23.4s)

**Student:** The SA node — the sinoatrial node.

**Tutor:** Got it — let's work on **Conduction System of the Heart** from Cardiac Muscle and Electrical Activity.  What structure in the heart has the highest inherent rate of depolarization and is known as the pacemaker of the heart?

- [PASS] non-empty — response length: 223 chars
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='sinoatrial node', reached=False, cov=0.0, path=''_

**Post-scenario state checks:**
- [PASS] topic confirmed — topic locked
- [FAIL] reached final answer — reached=False

---

## G1 PASS: Whitespace-only message

_Student sends only whitespace. Tutor must not crash and should ask for clarification (question or card list)._

Expected concept: `(input handling test)`

### Turn 1 (22.3s)

**Student:** What does the liver do?

**Tutor:** Got it — let's work on a topic from Circulatory Pathways.  What is the name of the specialized vessel that delivers nutrient-rich blood from the digestive organs directly to the liver for processing?

- [PASS] question or card list — ends with a question

_state: phase=tutoring, locked='hepatic portal vein', reached=False, cov=0.0, path=''_

### Turn 2 (0.0s)

**Student:**    

**Tutor:** Looks like your last message came through empty — could you type your question or response again?

- [PASS] non-empty — response length: 97 chars

_state: phase=tutoring, locked='hepatic portal vein', reached=False, cov=0.0, path=''_

---
