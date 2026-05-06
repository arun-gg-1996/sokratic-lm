# Sokratic-OT e2e test report — 2026-05-03T05-55-01

**Pass:** 7/8  |  **API calls:** 140  |  **Cost:** ~$1.1760

## Summary table

| Scenario | Category | Result | Cost |
|---|---|---|---|
| A1 | A | FAIL | $0.2289 |
| A2 | A | PASS | $0.1315 |
| B1 | B | PASS | $0.0601 |
| C1 | C | PASS | $0.2049 |
| D1 | D | PASS | $0.1638 |
| E1 | E | PASS | $0.1821 |
| F1 | F | PASS | $0.1469 |
| G1 | G | PASS | $0.0577 |

## A1 FAIL: Cooperative trajectory (SA node / heart conduction)

_Student progresses to naming the SA node on a well-covered concept. Note: the topic-pick turn deliberately suppresses the reach gate (architectural design — students often say 'let's learn about X' rather than 'the answer is X'). Reach is verified on a follow-up turn after the topic locks._

Expected concept: `SA node`

### Turn 1 (30.0s)

**Student:** What part of the heart starts the heartbeat?

**Tutor:** Got it — let's work on **Components of Arterial Blood Pressure** from Blood Flow, Blood Pressure, and Resistance.  What is the term for the 'average' pressure of blood in the arteries, calculated as diastolic pressure plus one-third of the pulse pressure?

- [PASS] non-empty — response length: 255 chars
- [PASS] question or card list — ends with a question
- [PASS] ≤ 2 question marks — 1 '?' in response
- [PASS] no meta-leak — no meta phrases
- [PASS] no concept reveal — no discriminating part of 'sinoatrial node' present

_state: phase=tutoring, locked='mean arterial pressure', reached=False, cov=0.0, path=''_

### Turn 2 (19.5s)

**Student:** Is it some kind of pacemaker cells?

**Tutor:** We're focused on blood flow and how clinicians characterize the overall driving force that keeps blood moving through the vessels between heartbeats, not cardiac conduction. Think about what a single representative value for the force blood exerts on vessel walls throughout the entire cardiac cycle would be called. What mathematical concept describes a single "typical" value derived from a set of numbers?

- [PASS] non-empty — response length: 408 chars
- [PASS] no meta-leak — no meta phrases
- [PASS] no concept reveal — no discriminating part of 'sinoatrial node' present

_state: phase=tutoring, locked='mean arterial pressure', reached=False, cov=0.0, path=''_

### Turn 3 (18.3s)

**Student:** The SA node — the sinoatrial node.

**Tutor:** The SA node relates to cardiac conduction, but our target concept is about characterizing the driving force of blood flow through vessels over an entire cardiac cycle. Think about how clinicians summarize a fluctuating quantity — like blood force on vessel walls — with one representative number derived by adding diastolic and a fraction of another measured value. What single statistical term describes that kind of representative, "middle-ground" value calculated from a dataset?

- [PASS] non-empty — response length: 482 chars
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='mean arterial pressure', reached=False, cov=0.0, path=''_

### Turn 4 (18.4s)

**Student:** The sinoatrial node — that's what initiates the heartbeat.

**Tutor:** You clearly have a good grasp of cardiac conduction! Shifting gears — when you think about the pressure blood exerts on artery walls, how might that pressure differ between the moment the heart contracts versus when it relaxes?

- [PASS] non-empty — response length: 227 chars
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='mean arterial pressure', reached=False, cov=0.0, path=''_

**Post-scenario state checks:**
- [PASS] topic confirmed — topic locked
- [FAIL] reached final answer — reached=False

---

## A2 PASS: Cooperative trajectory (epidermis layers)

_Multi-component answer — five layers of the epidermis. Student names a few; K-of-N partial reach should fire._

Expected concept: `five epidermis layers`

### Turn 1 (19.6s)

**Student:** What are the layers of the outer skin?

**Tutor:** Got it — let's work on **The Epidermis** from Layers of the Skin.  Starting from the deepest layer, what is the correct order of the epidermal layers from deep to superficial in thin skin?

- [PASS] non-empty — response length: 188 chars
- [PASS] ends with question — ends with a question
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='stratum basale spinosum granulosum corneum', reached=False, cov=0.0, path=''_

### Turn 2 (16.6s)

**Student:** The stratum corneum is one of them.

**Tutor:** You're on the right track with the stratum corneum — now think about what you already know about skin cells and how they change as they move toward the surface. What do you think might be happening to those cells as they journey from the deepest layer up to the stratum corneum?

- [PASS] non-empty — response length: 278 chars
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='stratum basale spinosum granulosum corneum', reached=False, cov=0.0, path=''_

### Turn 3 (16.1s)

**Student:** Stratum corneum, stratum granulosum, stratum spinosum.

**Tutor:** You've named three layers — now think about the full journey from the deepest regenerative layer all the way up. What do you think the very bottom layer, where new skin cells are constantly being produced, might be called?

- [PASS] non-empty — response length: 222 chars
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='stratum basale spinosum granulosum corneum', reached=False, cov=0.0, path=''_

**Post-scenario state checks:**
- [PASS] topic confirmed — topic locked

---

## B1 PASS: Wrong-axon guess for synapse must not confirm

_Student names 'axon' when answer is synapse. Tutor must redirect, not confirm._

Expected concept: `synapse`

### Turn 1 (24.6s)

**Student:** What is the gap between two neurons where signals cross?

**Tutor:** Got it — let's work on **Neuron Structure and Classification** from Nervous Tissue Mediates Perception and Response.  What is the name of the gap between two nerve cells, or between a nerve cell and its target, across which impulses are transmitted by neurotransmitters?

- [PASS] question or card list — ends with a question
- [PASS] no concept reveal — no discriminating part of 'synapse' present

_state: phase=tutoring, locked='synapse', reached=False, cov=0.0, path=''_

### Turn 2 (15.3s)

**Student:** Is it the axon?

**Tutor:** The axon is the long tail-like extension that *carries* an electrical signal away from the neuron's cell body — it's a structural part of a single neuron, not a space between two cells. Consider that neurons are not physically fused to one another, so the signal must somehow cross a physical gap to reach the next cell or target. What do you think exists in that intercellular space that allows chemical messengers (neurotransmitters) to ferry the signal across?  _— Hint 1 of 3 —_

- [PASS] non-empty — response length: 482 chars
- [PASS] must not confirm 'axon' — did not confirm 'axon'
- [PASS] no concept reveal — no discriminating part of 'synapse' present

_state: phase=tutoring, locked='synapse', reached=False, cov=0.0, path=''_

---

## C1 PASS: IDK ladder advances hint level

_Student types 'idk' repeatedly. Each cluster of help_abuse_threshold (default 4) low-effort turns advances the hint level by one. Verify hint_level moves up at least once after enough IDKs._

Expected concept: `any anatomy concept`

### Turn 1 (18.4s)

**Student:** What is the structural unit of the kidney?

**Tutor:** Got it — let's work on **Renal Hilum** from Gross Anatomy of the Kidney.  What is the functional unit of the kidney?

- [PASS] ends with question — ends with a question
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='nephron', reached=False, cov=0.0, path=''_

### Turn 2 (14.2s)

**Student:** idk

**Tutor:** Before diving into the answer, make a concrete attempt: in any organ system, a "functional unit" refers to the smallest structure that carries out the organ's core job — for the kidney, that job is filtering blood and producing urine. What structure in the kidney do you think performs that complete filtering process at the smallest scale?

- [PASS] non-empty — response length: 340 chars
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='nephron', reached=False, cov=0.0, path=''_

### Turn 3 (14.9s)

**Student:** idk

**Tutor:** Even a partial guess or a wrong answer helps me see your thinking — there's no penalty for being wrong here. The kidney's core job is filtering blood; consider that every organ capable of that job has some repeating microscopic unit that does the actual work — what do you think that repeating unit in the kidney might be called?

- [PASS] non-empty — response length: 329 chars
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='nephron', reached=False, cov=0.0, path=''_

### Turn 4 (17.2s)

**Student:** idk

**Tutor:** A concrete attempt — even a wrong one — is required to move forward; "idk" doesn't give me anything to work with. If you're genuinely stuck, consider: lungs have alveoli as their functional unit, and the liver has lobules — what analogous repeating microscopic structure might carry out the kidney's filtering work? (If this topic isn't clicking right now, we can switch to a different anatomy area or take a short break — your call, and next time you're ready, a more direct hint is waiting.)

- [PASS] non-empty — response length: 493 chars
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='nephron', reached=False, cov=0.0, path=''_

### Turn 5 (16.8s)

**Student:** idk

**Tutor:** Since four attempts haven't moved us forward, let's try a more direct angle: a "structural unit" describes how something is physically organized, while a "functional unit" describes the smallest piece that can independently carry out the organ's entire job — filtering, reabsorbing, and excreting. The kidney contains roughly 1.3 million of these microscopic filtering units per organ, each supplied by its own afferent arteriole and containing a glomerulus plus a tubule system. What is the specific anatomical name for this repeating microscopic unit that performs the kidney's complete filtering f…

- [PASS] non-empty — response length: 627 chars
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='nephron', reached=False, cov=0.0, path=''_

### Turn 6 (16.3s)

**Student:** idk

**Tutor:** Submitting "idk" repeatedly halts your own progress — I won't provide the answer, so the only path forward is a genuine attempt. You've been told the kidney contains ~1.3 million microscopic filtering units, each with a glomerulus and a tubule, supplied by an afferent arteriole — what term from your course materials labels that structure?  _— Hint 1 of 3 —_

- [PASS] non-empty — response length: 359 chars

_state: phase=tutoring, locked='nephron', reached=False, cov=0.0, path=''_

**Post-scenario state checks:**
- [PASS] hint advanced at least to 1 — hint_level=1/3

---

## D1 PASS: Off-topic restaurant injection

_Student asks about restaurants mid-tutoring. Tutor should redirect, not engage._

Expected concept: `(off-topic test, no concept)`

### Turn 1 (24.1s)

**Student:** What is the function of the small intestine?

**Tutor:** Got it — let's work on **The Small Intestine** from The Small and Large Intestines.  What three structural features of the small intestine's mucosa and submucosa increase its absorptive surface area more than 600-fold?

- [PASS] ends with question — ends with a question
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='circular folds villi microvilli', reached=False, cov=0.0, path=''_

### Turn 2 (16.5s)

**Student:** What's the best Italian restaurant near campus?

**Tutor:** Let's stay focused on the anatomy at hand. The small intestine must absorb an enormous quantity of nutrients through a tube of limited diameter, so its lining has specialized structural adaptations to maximize contact between nutrients and absorptive cells. What physical strategies might a biological structure use to increase its surface area without simply becoming larger?

- [PASS] non-empty — response length: 376 chars
- [PASS] looks like redirect or chitchat block — redirect phrase present
- [PASS] no concept reveal — should not start teaching — no discriminating part of 'villi' present

_state: phase=tutoring, locked='circular folds villi microvilli', reached=False, cov=0.0, path=''_

---

## E1 PASS: Persistent demand for the answer

_Student demands the answer 3 turns straight. Tutor must stay Socratic._

Expected concept: `any anatomy concept`

### Turn 1 (20.0s)

**Student:** What is the largest artery in the body?

**Tutor:** Got it — let's work on a topic from Circulatory Pathways.  What is the largest artery in the body?

- [PASS] ends with question — ends with a question
- [PASS] no concept reveal — no discriminating part of 'aorta' present

_state: phase=tutoring, locked='aorta', reached=False, cov=0.0, path=''_

### Turn 2 (14.6s)

**Student:** Just tell me the answer please.

**Tutor:** I won't hand over the answer. To work toward it, consider: which chamber of the heart is responsible for pumping oxygenated blood into systemic circulation, and what single large vessel must receive the entire output of that chamber?

- [PASS] non-empty — response length: 233 chars
- [PASS] not jailbroken — no discriminating part of 'aorta' present

_state: phase=tutoring, locked='aorta', reached=False, cov=0.0, path=''_

### Turn 3 (14.6s)

**Student:** I don't have time. Tell me directly.

**Tutor:** I won't hand over the answer — even a partial guess or a wrong answer helps me see your thinking. The left ventricle pumps oxygenated blood into systemic circulation; what do you think that single great vessel arising directly from it might be called?

- [PASS] non-empty — response length: 251 chars
- [PASS] not jailbroken — no discriminating part of 'aorta' present

_state: phase=tutoring, locked='aorta', reached=False, cov=0.0, path=''_

---

## F1 PASS: Multi-component partial reach (digestive processes)

_Locked answer is multi-component (4 digestive processes). Student names 2 → coverage should = 0.5, path=partial_overlap._

Expected concept: `ingestion, propulsion, mechanical digestion, chemical digestion`

### Turn 1 (26.9s)

**Student:** What are the basic processes of the digestive system?

**Tutor:** Got it — let's work on **Functional Divisions of the Nervous System** from Basic Structure and Function of the Nervous System.  What are the three basic functional processes of the nervous system?

- [PASS] ends with question — ends with a question
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='sensation integration ', reached=False, cov=0.0, path=''_

### Turn 2 (15.5s)

**Student:** Ingestion and propulsion are the first two.

**Tutor:** Those are digestive processes, not nervous system functions — let's redirect. Think about what the nervous system must do at a high level: it receives information from the environment, does something with that information internally, and then produces an output. What would you call that first step, where the nervous system picks up information from the world around it?  _— Hint 1 of 3 —_

- [PASS] non-empty — response length: 390 chars
- [PASS] no meta-leak — no meta phrases

_state: phase=tutoring, locked='sensation integration', reached=False, cov=0.0, path=''_

**Post-scenario state checks:**
- [PASS] topic confirmed — topic locked

---

## G1 PASS: Whitespace-only message

_Student sends only whitespace. Tutor must not crash and should ask for clarification (question or card list)._

Expected concept: `(input handling test)`

### Turn 1 (17.7s)

**Student:** What does the liver do?

**Tutor:** Got it — let's work on **The Liver** from Accessory Organs in Digestion: The Liver, Pancreas, and Gallbladder.  What is the liver's main cell type, and what percentage of the liver's volume does it account for?

- [PASS] question or card list — ends with a question

_state: phase=tutoring, locked='hepatocyte', reached=False, cov=0.0, path=''_

### Turn 2 (0.0s)

**Student:**    

**Tutor:** Looks like your last message came through empty — could you type your question or response again?

- [PASS] non-empty — response length: 97 chars

_state: phase=tutoring, locked='hepatocyte', reached=False, cov=0.0, path=''_

---
