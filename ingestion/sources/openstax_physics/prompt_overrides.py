"""
ingestion/sources/openstax_physics/prompt_overrides.py
Physics-specific prompt additions for OpenStax University Physics V1.

The generic core/propositions.py prompt body is source-agnostic. This module
contributes physics-specific instructions appended to the cached system
prompt — primarily to keep proposition extraction from inventing or
mangling LaTeX-style equations and quantity symbols.

Empty stubs for now; populate as the dual-task phase reveals systematic
failure modes specific to physics text (e.g. equation-heavy chunks where
the LLM tries to "explain" a formula in prose and produces ungrounded
propositions).
"""
from __future__ import annotations

PROPOSITION_PROMPT_SUFFIX: str = """\
DOMAIN OVERRIDE — UNIVERSITY PHYSICS

The base prompt is phrased for an anatomy textbook, but THIS corpus is
OpenStax University Physics V1. Override the base prompt's domain
references as follows:

  - Wherever the base prompt says "anatomical, physiological,
    biochemical, or clinical fact", read this as "physical quantity,
    principle, equation, definition, or worked-example result".
  - Wherever the base prompt says "biology" / "disease" / "symptom" /
    "drug" / "dose", read this as "physics concept" / "phenomenon" /
    "measurement" / "formula" / "boundary condition".

What COUNTS as fact-bearing content in physics (PRESERVE these — do NOT
treat as noise):

  - Equations and formulas (verbatim — F = ma, KE = ½mv², λ = v/f, etc.).
  - Numerical values, units, vector components, constants
    (g = 9.81 m/s², 6.022×10²³, etc.).
  - Definitions of physical quantities (force, momentum, energy, work,
    impulse, torque, angular velocity, etc.).
  - Conservation laws, Newton's laws, conditions for equilibrium,
    boundary conditions.
  - Worked example setups, given values, and final answers.
  - Comparisons between regimes (low vs high speed, elastic vs
    inelastic collision, etc.).

What COUNTS as noise (still REMOVE — same as base prompt, just calibrated
for physics):

  - "Learning Objectives" / "By the end of this section" preambles.
  - "Watch this video", "INTERACTIVE LINK", URL navigation.
  - Bare "(Figure 5.2)" / "(see Figure 3.4)" decorative refs WITHOUT
    embedded content.
  - End-of-chapter "Conceptual Questions", "Problems", "Challenge
    Problems" headings + the question lists themselves (these are
    student exercises, not narrative content).

OUTPUT REQUIREMENT — even if the chunk looks unfamiliar (heavy
equations, OCR artifacts from math typesetting, mixed numerical
example), you MUST return well-formed JSON with the exact shape
{"cleaned_text": "...", "propositions": ["...", "..."]}. Empty
cleaned_text + empty propositions is acceptable when the chunk is
ALL noise; otherwise produce the structured output. Never refuse,
never explain in prose — JSON only.
"""
"""Physics-specific suffix appended to the generic proposition prompt.
Reframes the anatomy-coded base prompt for physics content + reinforces
JSON-only output (Haiku tends to refuse or commentate when it sees
equation-heavy text framed as a 'biology' task)."""

SUMMARY_PROMPT_SUFFIX: str = ""
"""Physics-specific suffix for subsection summary generation."""
