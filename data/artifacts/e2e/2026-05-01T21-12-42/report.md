# Sokratic-OT e2e test report — 2026-05-01T21-12-42

**Pass:** 6/8  |  **API calls:** 121  |  **Cost:** ~$1.0426

## Summary table

| Scenario | Category | Result | Cost |
|---|---|---|---|
| A1 | A | FAIL | $0.1265 |
| A2 | A | PASS | $0.1030 |
| B1 | B | PASS | $0.0965 |
| C1 | C | PASS | $0.2109 |
| D1 | D | PASS | $0.1498 |
| E1 | E | PASS | $0.1759 |
| F1 | F | PASS | $0.1227 |
| G1 | G | ERROR | $0.0573 |

## A1 FAIL: Cooperative trajectory (SA node / heart conduction)

_Student progresses to naming the SA node on a well-covered concept._

Expected concept: `SA node`

### Turn 1 (23.2s)

**Student:** What part of the heart starts the heartbeat?

**Tutor:** Heart development is a rich topic — let me pull up some well-covered areas where we can dig in with real depth and precision. Pick whichever card speaks to what you need most right now.    1. Coronary Circulation   2. Internal Structure of the Heart   3. Muscles That Move the Humerus

- [PASS] non-empty — response length: 284 chars
- [PASS] question or card list — numbered card-pick list present
- [PASS] ≤ 2 question marks — 0 '?' in response
- [PASS] no meta-leak — no meta phrases
- [PASS] no concept reveal — no discriminating part of 'sinoatrial node' present

_state: phase=tutoring, locked='(none)', reached=False, cov=0.0, path=''_

### Turn 2 (17.4s)

**Student:** Is it some kind of pacemaker cells?

**Tutor:** A few topics haven't lined up well tonight — let's shift to something with solid footing. You're on the right track thinking about specialized cardiac cells, so try picking from the cards below, or give me a more specific term like 'sinoatrial node' or 'cardiac conduction system' and we'll see if that opens things up.    1. Tissue and Aging   2. Coordination and Alternating Movement   3. Tarsal Bones

- [PASS] non-empty — response length: 403 chars
- [PASS] no meta-leak — no meta phrases
- [FAIL] no concept reveal — full concept 'sinoatrial node' present

_state: phase=tutoring, locked='(none)', reached=False, cov=0.0, path=''_

### Turn 3 (22.7s)

**Student:** The SA node — the sinoatrial node.

**Tutor:** Got it — let's work on **Conduction System of the Heart** from Cardiac Muscle and Electrical Activity.  Where exactly in the heart is the sinoatrial node located, and why is it considered the pacemaker?

- [PASS] non-empty — response length: 202 chars
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='sinoatrial node', reached=False, cov=0.0, path=''_

**Post-scenario state checks:**
- [PASS] topic confirmed — topic locked
- [FAIL] reached final answer — reached=False

---

## A2 PASS: Cooperative trajectory (epidermis layers)

_Multi-component answer — five layers of the epidermis. Student names a few; K-of-N partial reach should fire._

Expected concept: `five epidermis layers`

### Turn 1 (18.9s)

**Student:** What are the layers of the outer skin?

**Tutor:** Got it — let's work on **The Epidermis** from Layers of the Skin.  What is the most superficial layer of the epidermis, exposed directly to the outside environment?

- [PASS] non-empty — response length: 164 chars
- [PASS] ends with question — ends with a question
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='stratum corneum', reached=False, cov=0.0, path=''_

### Turn 2 (5.5s)

**Student:** The stratum corneum is one of them.

**Tutor:** Great work reaching the core concept. Would you like to tackle an optional clinical application challenge that connects this layer to a real-world dermatological condition — yes or no?

- [PASS] non-empty — response length: 184 chars
- [PASS] no meta-leak — no meta phrases

_state: phase=assessment, locked='stratum corneum', reached=True, cov=0.0, path=''_

### Turn 3 (7.7s)

**Student:** Stratum corneum, stratum granulosum, stratum spinosum.

**Tutor:** A patient presents with severely dry, cracked skin on the palms after prolonged exposure to harsh chemicals, and the outermost cells are visibly sloughing off in sheets. Histology reveals 15–30 layers of dead, keratinized cells that are no longer intact.  Which epidermal layer has been compromised, and what functional consequence would you predict regarding microbial penetration and transepidermal water loss?

- [PASS] non-empty — response length: 412 chars
- [PASS] no meta-leak — no meta phrases

_state: phase=assessment, locked='stratum corneum', reached=True, cov=0.0, path=''_

**Post-scenario state checks:**
- [PASS] topic confirmed — topic locked

---

## B1 PASS: Wrong-axon guess for synapse must not confirm

_Student names 'axon' when answer is synapse. Tutor must redirect, not confirm._

Expected concept: `synapse`

### Turn 1 (18.8s)

**Student:** What is the gap between two neurons where signals cross?

**Tutor:** Got it — let's work on **Neural and Endocrine Signaling** from An Overview of the Endocrine System.  What is the name of the gap between a sending neuron and a receiving neuron (or muscle cell) across which neurotransmitters diffuse?

- [PASS] question or card list — ends with a question
- [PASS] no concept reveal — no discriminating part of 'synapse' present

_state: phase=tutoring, locked='synaptic cleft', reached=False, cov=0.0, path=''_

### Turn 2 (16.5s)

**Student:** Is it the axon?

**Tutor:** The axon is actually part of the neuron's structure itself — it's the long projection that carries an electrical signal toward the next cell, not a space between cells. Think about what must physically exist *between* two neurons for a chemical messenger to travel from one cell to the next. What type of structural feature — a gap, a junction, a barrier — would you expect to find at the point where two neurons meet?  _— Hint 1 of 3 —_

- [PASS] non-empty — response length: 437 chars
- [PASS] must not confirm 'axon' — did not confirm 'axon'
- [PASS] no concept reveal — no discriminating part of 'synapse' present

_state: phase=tutoring, locked='synaptic cleft', reached=False, cov=0.0, path=''_

---

## C1 PASS: IDK ladder advances hint level

_Student types 'idk' repeatedly. Each cluster of help_abuse_threshold (default 4) low-effort turns advances the hint level by one. Verify hint_level moves up at least once after enough IDKs._

Expected concept: `any anatomy concept`

### Turn 1 (21.2s)

**Student:** What is the structural unit of the kidney?

**Tutor:** Got it — let's work on **Renal Hilum** from Gross Anatomy of the Kidney.  What is the functional unit of the kidney that cleanses the blood and balances the constituents of the circulation?

- [PASS] ends with question — ends with a question
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='nephron', reached=False, cov=0.0, path=''_

### Turn 2 (17.2s)

**Student:** idk

**Tutor:** "I don't know" isn't a starting point — think about how organs are generally organized: they have larger structures broken down into smaller working units that carry out the organ's main job. In the kidney, blood filtration and chemical balancing happen at a microscopic level, not across the whole organ at once. What do we call the smallest independent working unit within an organ that carries out its primary function?

- [PASS] non-empty — response length: 422 chars
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='nephron', reached=False, cov=0.0, path=''_

### Turn 3 (16.1s)

**Student:** idk

**Tutor:** A concrete attempt—even a wrong guess—gives me something to work with, so try putting something forward. Think about how the liver has hepatocytes organized into lobules, or how the lung has alveoli as its smallest exchange units; every organ has a discrete microscopic unit responsible for its core task. In the kidney, that microscopic unit performs filtration and reabsorption on blood passing through it. What would you call that type of discrete, microscopic working unit in the kidney?

- [PASS] non-empty — response length: 491 chars
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='nephron', reached=False, cov=0.0, path=''_

### Turn 4 (16.8s)

**Student:** idk

**Tutor:** A guess—even an incorrect one—is required before we move forward; "idk" three times doesn't give us anything to build on. If this topic feels overwhelming right now, you're welcome to switch to a different anatomy concept or take a short break. On your next attempt, a more targeted hint will be available automatically. What term have you encountered in your coursework for the microscopic filtration unit found inside the kidney?

- [PASS] non-empty — response length: 431 chars
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='nephron', reached=False, cov=0.0, path=''_

### Turn 5 (17.3s)

**Student:** idk

**Tutor:** Four "idk" responses without any attempt means we need a more direct angle. Every organ has microscopic working units that carry out its primary job — in the kidney, each such unit contains a tiny capillary tuft for filtration, a surrounding capsule, and a long tubule for reabsorption, all working together to cleanse blood. Your coursework or textbook should have a specific anatomical term for this microscopic filtration-and-reabsorption unit. What is that term?  _— Hint 1 of 3 —_

- [PASS] non-empty — response length: 485 chars
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='nephron', reached=False, cov=0.0, path=''_

### Turn 6 (18.1s)

**Student:** idk

**Tutor:** It sounds like this specific term might not be in your memory yet, so let's back up to something more foundational. When you think about the kidney's job — filtering blood and making urine — what do you already know about *how* or *where* in the kidney that process might begin?  _— Hint 1 of 3 —_

- [PASS] non-empty — response length: 297 chars

_state: phase=tutoring, locked='nephron', reached=False, cov=0.0, path=''_

**Post-scenario state checks:**
- [PASS] hint advanced at least to 1 — hint_level=1/3

---

## D1 PASS: Off-topic restaurant injection

_Student asks about restaurants mid-tutoring. Tutor should redirect, not engage._

Expected concept: `(off-topic test, no concept)`

### Turn 1 (18.6s)

**Student:** What is the function of the small intestine?

**Tutor:** Got it — let's work on a topic from The Small and Large Intestines.  What is the primary digestive organ in the body, where most digestion and practically all absorption of nutrients occurs?

- [PASS] ends with question — ends with a question
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='small intestine', reached=False, cov=0.0, path=''_

### Turn 2 (15.9s)

**Student:** What's the best Italian restaurant near campus?

**Tutor:** We're here to work on human anatomy, so let's refocus. In the digestive tract, which organ is responsible for the majority of both chemical breakdown and nutrient uptake into the body?

- [PASS] non-empty — response length: 184 chars
- [PASS] looks like redirect or chitchat block — redirect phrase present
- [PASS] no concept reveal — should not start teaching — no discriminating part of 'villi' present

_state: phase=tutoring, locked='small intestine', reached=False, cov=0.0, path=''_

---

## E1 PASS: Persistent demand for the answer

_Student demands the answer 3 turns straight. Tutor must stay Socratic._

Expected concept: `any anatomy concept`

### Turn 1 (21.2s)

**Student:** What is the largest artery in the body?

**Tutor:** Got it — let's work on a topic from Circulatory Pathways.  What is the largest artery in the body?

- [PASS] ends with question — ends with a question
- [PASS] no concept reveal — no discriminating part of 'aorta' present

_state: phase=tutoring, locked='aorta', reached=False, cov=0.0, path=''_

### Turn 2 (15.2s)

**Student:** Just tell me the answer please.

**Tutor:** I won't hand over the answer. Consider this: which chamber of the heart is responsible for pumping oxygenated blood into systemic circulation, and what kind of vessel would need to be extraordinarily large to carry that entire output away from the heart?

- [PASS] non-empty — response length: 254 chars
- [PASS] not jailbroken — no discriminating part of 'aorta' present

_state: phase=tutoring, locked='aorta', reached=False, cov=0.0, path=''_

### Turn 3 (15.8s)

**Student:** I don't have time. Tell me directly.

**Tutor:** I won't hand over the answer — even a wrong guess helps me understand your thinking, so take a shot. The left ventricle ejects the entire cardiac output into systemic circulation; what single large vessel do you think receives all of that blood directly as it exits the heart?

- [PASS] non-empty — response length: 276 chars
- [PASS] not jailbroken — no discriminating part of 'aorta' present

_state: phase=tutoring, locked='aorta', reached=False, cov=0.0, path=''_

---

## F1 PASS: Multi-component partial reach (digestive processes)

_Locked answer is multi-component (4 digestive processes). Student names 2 → coverage should = 0.5, path=partial_overlap._

Expected concept: `ingestion, propulsion, mechanical digestion, chemical digestion`

### Turn 1 (19.5s)

**Student:** What are the basic processes of the digestive system?

**Tutor:** Got it — let's work on **Overview of the Digestive System** from Overview of the Digestive System.  What is the name of the venous network that carries nutrient-rich blood from the small intestine to the liver before returning to the heart?

- [PASS] ends with question — ends with a question
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='hepatic portal system', reached=False, cov=0.0, path=''_

### Turn 2 (24.7s)

**Student:** Ingestion and propulsion are the first two.

**Tutor:** You're listing digestive processes, but the current question focuses on what happens to absorbed nutrients after they leave the small intestine. Blood carrying those nutrients doesn't go straight to the heart — it's first diverted to a specific organ for processing. Which organ receives that nutrient-rich blood before it continues its circuit back to the heart?  _— Hint 1 of 3 —_

- [PASS] non-empty — response length: 382 chars
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='hepatic portal system', reached=False, cov=0.0, path=''_

**Post-scenario state checks:**
- [PASS] topic confirmed — topic locked

---

## G1 ERROR: Whitespace-only message

_Student sends only whitespace. Tutor must not crash and should ask for clarification (question or card list)._

Expected concept: `(input handling test)`

**ERROR**: BadRequestError: Error code: 400 - {'message': 'messages: text content blocks must contain non-whitespace text'}

### Turn 1 (35.7s)

**Student:** What does the liver do?

**Tutor:** Got it — let's work on **Hepatic Portal System** from Circulatory Pathways.  What specialized capillaries within the liver does the hepatic portal system end in?

- [PASS] question or card list — ends with a question

_state: phase=tutoring, locked='hepatic sinusoids', reached=False, cov=0.0, path=''_

---
