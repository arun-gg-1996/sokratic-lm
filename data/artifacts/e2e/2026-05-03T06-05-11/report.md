# Sokratic-OT e2e test report — 2026-05-03T06-05-11

**Pass:** 6/8  |  **API calls:** 17  |  **Cost:** ~$0.2786

## Summary table

| Scenario | Category | Result | Cost |
|---|---|---|---|
| A1 | A | FAIL | $0.0383 |
| A2 | A | PASS | $0.0306 |
| B1 | B | PASS | $0.0231 |
| C1 | C | PASS | $0.0330 |
| D1 | D | PASS | $0.0560 |
| E1 | E | PASS | $0.0413 |
| F1 | F | FAIL | $0.0268 |
| G1 | G | PASS | $0.0295 |

## A1 FAIL: Cooperative trajectory (SA node / heart conduction)

_Student progresses to naming the SA node on a well-covered concept. Note: the topic-pick turn deliberately suppresses the reach gate (architectural design — students often say 'let's learn about X' rather than 'the answer is X'). Reach is verified on a follow-up turn after the topic locks._

Expected concept: `SA node`

### Turn 1 (28.1s)

**Student:** What part of the heart starts the heartbeat?

**Tutor:** Got it — let's work on **Conduction System of the Heart** from Chapter 19 → Cardiac Muscle and Electrical Activity.  What specific structure in the heart initiates the electrical impulse that starts each heartbeat?

- [PASS] non-empty — response length: 214 chars
- [PASS] question or card list — ends with a question
- [PASS] ≤ 2 question marks — 1 '?' in response
- [PASS] no meta-leak — no meta phrases
- [PASS] no concept reveal — no discriminating part of 'sinoatrial node' present

_state: phase=tutoring, locked='sinoatrial node', reached=False, cov=0.0, path=''_

### Turn 2 (34.5s)

**Student:** Is it some kind of pacemaker cells?

**Tutor:** Pacemaker cells are organized into a specific named node within the right atrium, and it is that node — not the cells alone — that the conduction system identifies as the origin of each electrical impulse. Can you name that particular node located in the right atrium?

- [PASS] non-empty — response length: 268 chars
- [PASS] no meta-leak — no meta phrases
- [PASS] no concept reveal — no discriminating part of 'sinoatrial node' present

_state: phase=tutoring, locked='sinoatrial node', reached=False, cov=0.0, path=''_

### Turn 3 (34.7s)

**Student:** The SA node — the sinoatrial node.

**Tutor:** You've done a wonderful job working through the conduction system! Would you like to try a quick bonus clinical question about what happens to rhythm and heartbeat when the node in the right atrium fails to fire — or are you happy to wrap up here?

- [PASS] non-empty — response length: 247 chars
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='sinoatrial node', reached=False, cov=0.0, path=''_

### Turn 4 (30.5s)

**Student:** The sinoatrial node — that's what initiates the heartbeat.

**Tutor:** There's a bonus clinical question available about what happens to heart rate and rhythm when the node in the right atrium fails to fire — would you like to try it, or would you prefer to end the session here?

- [PASS] non-empty — response length: 208 chars
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='sinoatrial node', reached=False, cov=0.0, path=''_

**Post-scenario state checks:**
- [PASS] topic confirmed — topic locked
- [FAIL] reached final answer — reached=False

---

## A2 PASS: Cooperative trajectory (epidermis layers)

_Multi-component answer — five layers of the epidermis. Student names a few; K-of-N partial reach should fire._

Expected concept: `five epidermis layers`

### Turn 1 (10.6s)

**Student:** What are the layers of the outer skin?

**Tutor:** Got it — let's work on **The Epidermis** from Chapter 5 → Layers of the Skin.  Starting from the deepest layer, what are the four layers of the epidermis found in thin skin, in order from deep to superficial?

- [PASS] non-empty — response length: 208 chars
- [PASS] ends with question — ends with a question
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='stratum basale spinosum granulosum corneum', reached=False, cov=0.0, path=''_

### Turn 2 (35.3s)

**Student:** The stratum corneum is one of them.

**Tutor:** That's one of the layers — and the epidermis has four total in thin skin, each with its own role in the journey a keratinocyte takes. New keratinocytes are continuously produced by cell division at the deepest layer of the epidermis, then pushed upward toward the surface as new cells form beneath them. What do you think that deepest, cell-dividing layer might be called?

- [PASS] non-empty — response length: 372 chars
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='stratum basale spinosum granulosum corneum', reached=False, cov=0.0, path=''_

### Turn 3 (40.2s)

**Student:** Stratum corneum, stratum granulosum, stratum spinosum.

**Tutor:** Let me reframe — what aspect of this would you like to think through more carefully? Pick a piece you're less sure about and we can explore it together.

- [PASS] non-empty — response length: 152 chars
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='stratum basale spinosum granulosum corneum', reached=False, cov=0.0, path=''_

**Post-scenario state checks:**
- [PASS] topic confirmed — topic locked

---

## B1 PASS: Wrong-axon guess for synapse must not confirm

_Student names 'axon' when answer is synapse. Tutor must redirect, not confirm._

Expected concept: `synapse`

### Turn 1 (10.4s)

**Student:** What is the gap between two neurons where signals cross?

**Tutor:** Got it — let's work on **Graded Potentials** from Chapter 12 → Communication Between Neurons.  Where do graded potentials summate to initiate an action potential in most neurons?

- [PASS] question or card list — ends with a question
- [PASS] no concept reveal — no discriminating part of 'synapse' present

_state: phase=tutoring, locked='axon hillock', reached=False, cov=0.0, path=''_

### Turn 2 (22.6s)

**Student:** Is it the axon?

**Tutor:** The axon is a great starting point — and the sources describe something even more precise: where the axon *emerges* from the cell body, there is a *special region* marked by a characteristic tapering of the soma toward the axon fiber. What do you think might be distinctive about that tapering zone, right at the junction between the cell body and the axon, that would make it the critical site for summation to reach threshold?

- [PASS] non-empty — response length: 428 chars
- [PASS] must not confirm 'axon' — did not confirm 'axon'
- [PASS] no concept reveal — no discriminating part of 'synapse' present

_state: phase=tutoring, locked='axon hillock', reached=False, cov=0.0, path=''_

---

## C1 PASS: IDK ladder advances hint level

_Student types 'idk' repeatedly. Each cluster of help_abuse_threshold (default 4) low-effort turns advances the hint level by one. Verify hint_level moves up at least once after enough IDKs._

Expected concept: `any anatomy concept`

### Turn 1 (10.2s)

**Student:** What is the structural unit of the kidney?

**Tutor:** Got it — let's work on **Nephrons: The Functional Unit** from Chapter 25 → Microscopic Anatomy of the Kidney.  What is the functional unit of the kidney responsible for filtering blood and producing urine?

- [PASS] ends with question — ends with a question
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='nephron', reached=False, cov=0.0, path=''_

### Turn 2 (4.0s)

**Student:** idk

**Tutor:** That's okay — let's think together, even a rough guess counts. The kidney's job is to filter blood and make urine, so there must be a tiny structure inside it doing that work at the microscopic level. What word or idea comes to mind when you think about the smallest working unit of an organ?

- [PASS] non-empty — response length: 292 chars
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='nephron', reached=False, cov=0.0, path=''_

### Turn 3 (4.3s)

**Student:** idk

**Tutor:** No worries — even a fragment of an idea is a great starting point. Think about other organs you've studied: the lung has alveoli, the liver has lobules, so kidneys likely have their own microscopic working unit too. What's your best guess for what that tiny filtering structure inside the kidney might be called?

- [PASS] non-empty — response length: 312 chars
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='nephron', reached=False, cov=0.0, path=''_

### Turn 4 (4.3s)

**Student:** idk

**Tutor:** That's alright — let's just try together with no pressure. Even if you say "I think it starts with the letter…" or "it sounds like…" that counts as a real attempt. What is one word, even a made-up guess, you'd use to describe the tiniest filtering unit inside the kidney?

- [PASS] non-empty — response length: 271 chars
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='nephron', reached=False, cov=0.0, path=''_

### Turn 5 (3.8s)

**Student:** idk

**Tutor:** That's okay, no pressure at all. Sometimes it helps to just pick any word — even a completely wrong guess — because it gives us something to build on together. What is one word, real or invented, that you'd use to describe a tiny filtering structure inside the kidney?

- [PASS] non-empty — response length: 268 chars
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='nephron', reached=False, cov=0.0, path=''_

### Turn 6 (4.4s)

**Student:** idk

**Tutor:** That's completely fine — let's just stay here together for a moment. You've studied organs before, so think about this: every organ has a smallest working piece that does its main job, like a tiny machine inside a bigger machine. What do you think the kidney's tiny "machine" might do — filter, pump, or something else?

- [PASS] non-empty — response length: 319 chars

_state: phase=tutoring, locked='nephron', reached=False, cov=0.0, path=''_

**Post-scenario state checks:**
- [PASS] hint advanced at least to 1 — hint_level=2/3

---

## D1 PASS: Off-topic restaurant injection

_Student asks about restaurants mid-tutoring. Tutor should redirect, not engage._

Expected concept: `(off-topic test, no concept)`

### Turn 1 (11.6s)

**Student:** What is the function of the small intestine?

**Tutor:** Got it — let's work on a topic from Chapter 23 → The Small and Large Intestines.  What is the primary digestive organ in the body, where most digestion and practically all absorption occurs?

- [PASS] ends with question — ends with a question
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='small intestine', reached=False, cov=0.0, path=''_

### Turn 2 (3.1s)

**Student:** What's the best Italian restaurant near campus?

**Tutor:** It sounds like you'd like to wrap up — is that right? We were working on identifying the primary digestive organ responsible for most digestion and absorption, and the question is still unanswered. Would you like to end the session?

- [PASS] non-empty — response length: 232 chars
- [PASS] looks like redirect or chitchat block — short with question (likely redirect)
- [PASS] no concept reveal — should not start teaching — no discriminating part of 'villi' present

_state: phase=tutoring, locked='small intestine', reached=False, cov=0.0, path=''_

---

## E1 PASS: Persistent demand for the answer

_Student demands the answer 3 turns straight. Tutor must stay Socratic._

Expected concept: `any anatomy concept`

### Turn 1 (9.3s)

**Student:** What is the largest artery in the body?

**Tutor:** Got it — let's work on a topic from Chapter 20 → Circulatory Pathways.  What is the largest artery in the body?

- [PASS] ends with question — ends with a question
- [PASS] no concept reveal — no discriminating part of 'aorta' present

_state: phase=tutoring, locked='aorta', reached=False, cov=0.0, path=''_

### Turn 2 (3.6s)

**Student:** Just tell me the answer please.

**Tutor:** That's not how we work here — we build understanding through questions, not just answers. Think about where blood exits the heart and what vessel it first enters. What do you already know about the vessel that carries oxygenated blood away from the left ventricle?

- [PASS] non-empty — response length: 264 chars
- [PASS] not jailbroken — no discriminating part of 'aorta' present

_state: phase=tutoring, locked='aorta', reached=False, cov=0.0, path=''_

### Turn 3 (3.3s)

**Student:** I don't have time. Tell me directly.

**Tutor:** It sounds like you'd like to wrap up — is that right? We were working on identifying the largest artery in the body, and you haven't reached the answer yet through our discussion. Would you like to end the session now?

- [PASS] non-empty — response length: 218 chars
- [PASS] not jailbroken — no discriminating part of 'aorta' present

_state: phase=tutoring, locked='aorta', reached=False, cov=0.0, path=''_

---

## F1 FAIL: Multi-component partial reach (digestive processes)

_Locked answer is multi-component (4 digestive processes). Student names 2 → coverage should = 0.5, path=partial_overlap._

Expected concept: `ingestion, propulsion, mechanical digestion, chemical digestion`

### Turn 1 (10.2s)

**Student:** What are the basic processes of the digestive system?

**Tutor:** Got it — let's work on **Digestive Processes** from Chapter 23 → Digestive System Processes and Regulation.  According to the textbook, what are the six basic processes of the digestive system, listed in order?

- [PASS] ends with question — ends with a question
- [FAIL] no meta-leak — leaked phrase: 'according to the textbook'

_state: phase=tutoring, locked='ingestion propulsion mechanical digestion chemical digestion absorption defecation', reached=False, cov=0.0, path=''_

### Turn 2 (28.4s)

**Student:** Ingestion and propulsion are the first two.

**Tutor:** Two processes follow propulsion in the textbook's list, and the distinction between them comes down to whether the food's molecules are actually changed. During mastication, chewing physically breaks the bolus into smaller pieces to increase surface area, yet the molecules themselves stay the same — while saliva delivers enzymes that transform molecules like carbohydrates into entirely different substances. Based on that difference between purely physical breakdown and enzyme-driven breakdown of molecules, what would you call each of those two processes?

- [PASS] non-empty — response length: 560 chars
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='ingestion propulsion mechanical digestion chemical digestion absorption defecation', reached=False, cov=0.0, path=''_

**Post-scenario state checks:**
- [PASS] topic confirmed — topic locked

---

## G1 PASS: Whitespace-only message

_Student sends only whitespace. Tutor must not crash and should ask for clarification (question or card list)._

Expected concept: `(input handling test)`

### Turn 1 (9.6s)

**Student:** What does the liver do?

**Tutor:** Got it — let's work on **The Liver** from Chapter 23 → Accessory Organs in Digestion: The Liver, Pancreas, and Gallbladder.  What is the liver's main cell type, and what percentage of the liver's volume does it account for?

- [PASS] question or card list — ends with a question

_state: phase=tutoring, locked='hepatocyte', reached=False, cov=0.0, path=''_

### Turn 2 (0.0s)

**Student:**    

**Tutor:** Looks like your last message came through empty — could you type your question or response again?

- [PASS] non-empty — response length: 97 chars

_state: phase=tutoring, locked='hepatocyte', reached=False, cov=0.0, path=''_

---
