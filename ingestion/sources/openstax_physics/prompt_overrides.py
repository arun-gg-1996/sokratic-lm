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

PROPOSITION_PROMPT_SUFFIX: str = ""
"""Physics-specific suffix appended to the generic proposition prompt.
Empty for the first ingestion pass; revisit after dual-task QA."""

SUMMARY_PROMPT_SUFFIX: str = ""
"""Physics-specific suffix for subsection summary generation."""
