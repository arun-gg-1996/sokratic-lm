"""
ingestion/sources/openstax_physics/parse.py — physics PDF parsing.

OpenStax University Physics V1 shares the same house style as A&P 2e (font
sizes, heading conventions, sidebar markers, back-matter layout). The author
note on `openstax_anatomy/parse.py:5-6` explicitly calls this out:

    "Same thresholds work for OpenStax University Physics V1 because both
     books share the OpenStax house style."

So instead of duplicating ~480 lines of font-size heuristics + section-
detection state machine, we re-export the anatomy parser verbatim. If
physics ever needs divergent parsing (different heading patterns, etc.),
swap the re-export for a copy + tweak — no upstream caller changes.
"""
from __future__ import annotations

# Re-export the anatomy parser surface. The pipeline source-loader
# (ingestion/core/pipeline.py:load_source) calls `parse_pdf` from this
# module by attribute name, so the re-export is sufficient.
from ingestion.sources.openstax_anatomy.parse import parse_pdf  # noqa: F401
