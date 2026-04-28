"""
ingestion/sources/openstax_anatomy/filters.py
---------------------------------------------
OpenStax-specific structural filters for the ingestion pipeline.

This module is the "knows-about-OpenStax" home for noise detection that the
generic core/chunker.py shouldn't bake in.

Populated in B.2:
  - Back-matter detection (alphabetical index, glossary, references) — drop these.
  - Sidebar tagging (Career Connection, Everyday Connection, Aging and the X System,
    Disorders of...) — tag with element_type="sidebar" rather than letting them
    leak into adjacent paragraph chunks.

For now this is an empty stub so the directory layout is in place.
"""
from __future__ import annotations


def is_back_matter(element: dict) -> bool:
    """Return True if an element looks like a back-matter index/glossary/references entry.

    Populated in B.2.
    """
    return False


def detect_sidebar(element: dict) -> str | None:
    """If element starts with a known sidebar marker, return the sidebar label
    (e.g., "Career Connection"). Otherwise None.

    Populated in B.2.
    """
    return None
