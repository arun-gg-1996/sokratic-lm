# Proposition Quality Report (50 Base Chunks)

## Scope
- Source chunks: first 50 base chunks only (`is_overlap=false`, excluding `paragraph_overlap`)
- Propositions evaluated: **474**

## Hallucination Grounding Check
Method: Extract proper nouns (excluding sentence-start capitalization artifacts) and numbers from each proposition, then verify each term appears in `parent_chunk_text`.
- Total propositions: **474**
- Fully grounded: **471**
- Flagged for possible hallucination: **3**
- Hallucination rate: **0.63%**

### Flagged Propositions
1. Missing terms: `Earth`
   - Proposition: The surface of Earth and its atmosphere provide a range of temperature and pressure necessary for human survival.
   - Parent chunk: `7a6bde4b-ee46-4a0f-97d8-e86091736bc1` | Ch1 | 1.4 Requirements for Human Life
2. Missing terms: `Fahrenheit`
   - Proposition: Physicians lower a cardiac arrest patient's body temperature to approximately 91 degrees Fahrenheit as part of controlled hypothermia treatment.
   - Parent chunk: `d0e40ea9-cdf7-4c40-8a3d-3d17faee5579` | Ch1 | 1.4 Requirements for Human Life — Controlled Hypothermia
3. Missing terms: `Fahrenheit`
   - Proposition: Lowering the body temperature to approximately 91 degrees Fahrenheit slows the patient's metabolic rate.
   - Parent chunk: `d0e40ea9-cdf7-4c40-8a3d-3d17faee5579` | Ch1 | 1.4 Requirements for Human Life — Controlled Hypothermia

## Atomicity Check
- Propositions with 3+ commas: **5**
- Propositions with " and " >= 2: **12**
- Non-atomic union count: **16**
- Non-atomic rate: **3.38%**

### Examples: 3+ commas
- Some biological structures can be seen, manipulated, measured, and weighed without specialized tools.
- Regional anatomy helps in understanding how muscles, nerves, blood vessels, and other structures work together within a particular body region.
- Biological sex is determined by chromosomes, hormones, organs, and other physical characteristics.

### Examples: " and " >= 2
- Human physiology is the scientific study of the chemistry and physics of the body's structures and the ways they work together to support the functions of life.
- Molecular and ionic interactions occur at a smaller level of analysis than the arrangement and function of nerves and muscles.
- The words "female" and "male" can be used to describe two different concepts: gender identity and biological sex.

## Targets vs Observed
- Target hallucination rate: 0%
- Observed hallucination rate: 0.63%
- Target non-atomic rate: < 2%
- Observed non-atomic rate: 3.38%
