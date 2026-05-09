"""
ingestion/sources/openstax_physics/filters.py — physics filters.

OpenStax house style is shared across A&P 2e and University Physics V1: same
back-matter labels (key terms, glossary, index, references, answer key,
appendix, license), same sidebar marker phrasing ("Career Connection" etc.),
same boilerplate headings. So we re-export the anatomy filters module
wholesale.

If a physics-only filter ever surfaces (e.g. equation captions that pollute
proposition extraction), swap the re-export for a copy + tweak.
"""
from __future__ import annotations

# Re-export everything from the anatomy filters. parse.py and the pipeline
# orchestrator import these names directly.
from ingestion.sources.openstax_anatomy.filters import (  # noqa: F401
    BACK_MATTER_HEADINGS,
    is_back_matter_text,
    is_back_matter_heading,
    strip_sidebar_markers,
    is_back_matter,
    detect_sidebar,
)
