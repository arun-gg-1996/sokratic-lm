"""
Domain ontology adapters — pluggable entity-linking + synonym expansion
for TOC-grounded matching and BM25 query expansion.

Motivation
----------
The OT anatomy corpus needs UMLS (via scispacy) to turn student free-text
("deltoid", "cn vii", "wrist drop") into canonical concepts so the TOC
matcher can bridge the vocabulary gap between student phrasing and
textbook section titles. Physics (or any non-biomedical domain) has no
UMLS equivalent, so we abstract the lookup behind a stable interface and
let each domain plug in its own ontology adapter (or NoopAdapter when
there is no ontology).

Interface contract
------------------
- link_entities(text): extract canonical entity mentions from free text.
  Returns a list of EntityMention dicts with {span, canonical, cui, type}.
- get_synonyms(canonical): return known surface forms for a canonical
  concept — used for BM25 expansion at retrieval time.

Availability
------------
The UMLS linker has a heavy first-run cost: ~1 GB knowledge base download.
To keep start-up cheap, we load the scispacy pipeline lazily on the first
call to `link_entities` / `get_synonyms`. If scispacy or the model is
missing, the adapter degrades to NoopAdapter semantics (empty results)
rather than raising.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EntityMention:
    """A single entity link result from an ontology adapter."""

    span: str              # exact surface form matched in the input text
    canonical: str         # canonical concept name (e.g. "Deltoid muscle")
    cui: str = ""          # ontology identifier (UMLS CUI, etc.) — "" if none
    type: str = ""         # semantic type (e.g. "Body Part") — "" if none
    score: float = 1.0     # linker confidence in [0, 1]
    metadata: dict = field(default_factory=dict)


@runtime_checkable
class DomainOntologyAdapter(Protocol):
    """
    Pluggable per-domain ontology adapter.

    Implementations must be safe to construct eagerly (no heavy downloads
    inside __init__ unless explicitly opt-in), and should degrade to empty
    results rather than raising when the underlying resource is unavailable.
    """

    name: str

    def link_entities(self, text: str) -> list[EntityMention]:
        """
        Extract canonical entity mentions from `text`.
        Return [] when text is empty or nothing matches.
        """
        ...

    def get_synonyms(self, canonical: str) -> list[str]:
        """
        Return known surface forms / synonyms for `canonical`.
        Returns [] when the canonical is unknown.
        Callers MUST dedupe case-insensitively if they union with other sources.
        """
        ...


class NoopAdapter:
    """
    Zero-cost adapter used when a domain has no ontology plugin (e.g.
    physics). Always returns empty results; never raises.
    """

    name = "noop"

    def link_entities(self, text: str) -> list[EntityMention]:
        return []

    def get_synonyms(self, canonical: str) -> list[str]:
        return []


# Module-level cache of the fully-loaded scispacy pipeline. UMLSAdapter is
# cheap to re-construct, but loading the pipeline is ~15 seconds + ~1 GB
# of KB data — keep exactly one copy per process.
_UMLS_PIPELINE = None
_UMLS_LINKER = None
_UMLS_LOAD_FAILED = False


def _load_umls_pipeline(*, threshold: float = 0.80, resolve_abbreviations: bool = True):
    """
    Lazily load the scispacy NER model + UMLS EntityLinker.

    Returns (nlp, linker) on success, (None, None) on failure. Caches the
    result so subsequent calls are O(1). Failure is sticky for the life of
    the process — we don't keep retrying a missing dependency.
    """
    global _UMLS_PIPELINE, _UMLS_LINKER, _UMLS_LOAD_FAILED
    if _UMLS_PIPELINE is not None:
        return _UMLS_PIPELINE, _UMLS_LINKER
    if _UMLS_LOAD_FAILED:
        return None, None
    try:
        import spacy  # type: ignore
        from scispacy.linking import EntityLinker  # type: ignore  # noqa: F401 — registers the pipeline component
    except ImportError as e:
        logger.warning("scispacy not installed — UMLSAdapter degrading to noop: %s", e)
        _UMLS_LOAD_FAILED = True
        return None, None
    try:
        nlp = spacy.load("en_core_sci_sm")
    except OSError as e:
        logger.warning("en_core_sci_sm model not available — UMLSAdapter degrading to noop: %s", e)
        _UMLS_LOAD_FAILED = True
        return None, None
    try:
        nlp.add_pipe(
            "scispacy_linker",
            config={
                "resolve_abbreviations": resolve_abbreviations,
                "linker_name": "umls",
                "threshold": threshold,
            },
        )
    except Exception as e:
        # First-run downloads the ~1 GB UMLS KB; downloads are network-
        # dependent. Any failure here degrades to noop until process restart.
        logger.warning("UMLS EntityLinker failed to load — UMLSAdapter degrading to noop: %s", e)
        _UMLS_LOAD_FAILED = True
        return None, None
    linker = nlp.get_pipe("scispacy_linker")
    _UMLS_PIPELINE = nlp
    _UMLS_LINKER = linker
    return nlp, linker


class UMLSAdapter:
    """
    UMLS entity linker via scispacy.

    First use triggers a lazy load of `en_core_sci_sm` and the UMLS
    EntityLinker pipeline component (the linker auto-downloads its ~1 GB
    knowledge base on first use). If any dependency is missing, the
    adapter degrades to empty results for the remaining process lifetime.

    Tunables via __init__ (keep defaults in sync with `cfg.domain.ontology`
    when that wiring lands in P4):
        threshold: minimum cosine the linker must hit before returning a
            candidate. 0.80 is scispacy's recommended default — it keeps
            false-positive CUIs (e.g. linking "the" to a concept) out.
        top_k: number of UMLS candidates to keep per surface form.
    """

    name = "umls"

    def __init__(
        self,
        *,
        eager: bool = False,
        threshold: float = 0.80,
        top_k: int = 1,
        resolve_abbreviations: bool = True,
    ) -> None:
        self._threshold = float(threshold)
        self._top_k = max(1, int(top_k))
        self._resolve_abbreviations = bool(resolve_abbreviations)
        if eager:
            _load_umls_pipeline(
                threshold=self._threshold,
                resolve_abbreviations=self._resolve_abbreviations,
            )

    # ---- protocol methods -------------------------------------------------

    def link_entities(self, text: str) -> list[EntityMention]:
        if not text:
            return []
        nlp, linker = _load_umls_pipeline(
            threshold=self._threshold,
            resolve_abbreviations=self._resolve_abbreviations,
        )
        if nlp is None or linker is None:
            return []
        doc = nlp(text)
        mentions: list[EntityMention] = []
        for ent in doc.ents:
            kb_ents = getattr(ent._, "kb_ents", None) or []
            if not kb_ents:
                # Recognized but not linked — still useful as a surface-form
                # signal for downstream fuzzy matching. Emit span-as-canonical.
                mentions.append(EntityMention(span=ent.text, canonical=ent.text, score=0.0))
                continue
            for cui, score in kb_ents[: self._top_k]:
                entity = linker.kb.cui_to_entity.get(cui)
                if entity is None:
                    continue
                mentions.append(
                    EntityMention(
                        span=ent.text,
                        canonical=str(entity.canonical_name),
                        cui=str(cui),
                        type=",".join(entity.types) if getattr(entity, "types", None) else "",
                        score=float(score),
                    )
                )
        return mentions

    def get_synonyms(self, canonical: str) -> list[str]:
        if not canonical:
            return []
        nlp, linker = _load_umls_pipeline(
            threshold=self._threshold,
            resolve_abbreviations=self._resolve_abbreviations,
        )
        if nlp is None or linker is None:
            return []
        # The KB is keyed by CUI, but callers typically have the canonical
        # string. Do a two-step lookup: link the canonical string to its
        # own CUI, then return that CUI's aliases. Cached per call-site
        # rather than globally — this path is only hit at index-build time
        # (not per tutoring turn), so the cost is acceptable.
        doc = nlp(canonical)
        out: list[str] = []
        seen: set[str] = set()
        for ent in doc.ents:
            kb_ents = getattr(ent._, "kb_ents", None) or []
            for cui, _score in kb_ents[:1]:
                entity = linker.kb.cui_to_entity.get(cui)
                if entity is None:
                    continue
                for alias in getattr(entity, "aliases", []) or []:
                    norm = str(alias).strip()
                    key = norm.lower()
                    if norm and key not in seen:
                        seen.add(key)
                        out.append(norm)
                canon = str(getattr(entity, "canonical_name", "") or "")
                if canon and canon.lower() not in seen:
                    seen.add(canon.lower())
                    out.append(canon)
        return out


def get_ontology_adapter(domain_name: str | None = None) -> DomainOntologyAdapter:
    """
    Factory: return the adapter configured for `domain_name`.

    Current mapping (extend as new domains land):
      - "anatomy" / "ot" / "biomed"  -> UMLSAdapter
      - anything else                -> NoopAdapter

    Construction is cheap (the scispacy pipeline loads lazily). Callers
    should still call once per process and reuse the returned instance.
    """
    key = (domain_name or "").strip().lower()
    # "openstax_anatomy" added 2026-04-28 — it's the v1-rebuild retrieval_domain
    # value that supersedes "ot". "openstax" / "anatomy" substrings also catch
    # any future textbook-id naming we use in the OpenStax A&P family.
    if key in {"anatomy", "ot", "biomed", "biomedical", "openstax_anatomy"}:
        return UMLSAdapter()
    if "anatomy" in key or "biomed" in key:
        return UMLSAdapter()
    return NoopAdapter()
