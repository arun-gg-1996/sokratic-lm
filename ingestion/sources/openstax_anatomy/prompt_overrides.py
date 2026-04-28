"""
ingestion/sources/openstax_anatomy/prompt_overrides.py
------------------------------------------------------
Domain-specific prompt additions for OpenStax Anatomy & Physiology.

The generic core/propositions.py prompt body is source-agnostic. This module
contributes any anatomy/physiology-specific instructions that get appended to
the cached system prompt.

Populated in B.5 if needed. For OpenStax A&P 2e the generic prompt is likely
sufficient; this stub exists for future textbooks.
"""
from __future__ import annotations


PROPOSITION_PROMPT_SUFFIX: str = ""
"""Optional source-specific suffix appended to the generic proposition prompt.
Empty for OpenStax A&P 2e (generic prompt suffices)."""


SUMMARY_PROMPT_SUFFIX: str = ""
"""Optional source-specific suffix for subsection summary generation.
Reserved for Phase D.0 if summaries are added."""
