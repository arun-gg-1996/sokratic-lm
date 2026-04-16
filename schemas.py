"""
schemas.py
----------
Canonical data schemas for every object that flows through the ingestion
and retrieval pipeline.

These are the contracts between:
  extract.py → chunk.py → propositions.py → index.py → retriever.py

Every field is documented with WHY it exists, not just what it is.
Fields marked [RETRIEVAL CRITICAL] are required for quality retrieval —
do not drop these during ingestion.

All dicts are validated against these TypedDicts before indexing.
Use validate_chunk(), validate_proposition(), validate_diagram() to check.
"""

from typing import TypedDict, Literal, Optional


# ---------------------------------------------------------------------------
# 1. CHUNK
# Produced by: ingestion/chunk.py
# Consumed by: ingestion/propositions.py, ingestion/index.py (via propositions)
# Saved to:    data/processed/chunks_ot.jsonl
# ---------------------------------------------------------------------------

class ChunkSchema(TypedDict):
    # --- Identity ---
    chunk_id: str               # UUID. Stable identifier — propositions reference this.

    # --- Content ---
    text: str                   # Full chunk text (600-900 chars target).
                                # [RETRIEVAL CRITICAL] Cross-encoder scores (query, text).

    # --- Provenance: where in the textbook this came from ---
    chapter_num: int            # e.g. 11. [RETRIEVAL CRITICAL] Enables chapter-level filtering.
    chapter_title: str          # e.g. "Chapter 11: Muscle Tissue"
    section_num: str            # e.g. "11.2". String because subsections can be "11.2.1".
    section_title: str          # e.g. "Naming Skeletal Muscles"
    subsection_title: str       # e.g. "Origin, Insertion, and Action". Empty string if none.
                                # [RETRIEVAL CRITICAL] Most specific level — key for anatomy queries.
    page: int                   # Page number in the PDF. Useful for human verification.

    # --- Type ---
    element_type: Literal["paragraph", "table", "figure_caption"]
                                # [RETRIEVAL CRITICAL] Table chunks are high-value for anatomy
                                # (origin/insertion/innervation tables). UI uses this to flag
                                # visually important content.

    # --- Domain ---
    domain: Literal["ot", "physics"]
                                # [RETRIEVAL CRITICAL] Qdrant filter key — ensures OT queries
                                # never surface Physics chunks and vice versa.


# ---------------------------------------------------------------------------
# 2. PROPOSITION
# Produced by: ingestion/propositions.py
# Consumed by: ingestion/index.py, retrieval/retriever.py
# Saved to:    data/processed/propositions_ot.jsonl
#              AND upserted to Qdrant sokratic_kb
# ---------------------------------------------------------------------------

class PropositionSchema(TypedDict):
    # --- Identity ---
    proposition_id: str         # UUID.

    # --- Content ---
    text: str                   # Single atomic fact sentence. Self-contained — no pronouns,
                                # no "it" or "this muscle". GPT-4o resolves all references.
                                # [RETRIEVAL CRITICAL] This is what gets embedded and matched
                                # against student queries. Quality here = retrieval quality.

    # --- Parent chunk link ---
    parent_chunk_id: str        # References ChunkSchema.chunk_id.
    parent_chunk_text: str      # Full text of the parent chunk.
                                # [RETRIEVAL CRITICAL] Stored directly in Qdrant payload so
                                # retriever can expand proposition→chunk without a file lookup.
                                # Cross-encoder scores (query, parent_chunk_text) pairs.

    # --- Provenance (inherited from parent chunk) ---
    chapter_num: int
    chapter_title: str
    section_num: str
    section_title: str
    subsection_title: str       # [RETRIEVAL CRITICAL] Enables subsection-level Qdrant filtering
                                # when topic selector picks a specific subsection.
    page: int
    element_type: Literal["paragraph", "table", "figure_caption", "diagram"]
    domain: Literal["ot", "physics"]

    # --- Diagram-only fields (empty string for textbook propositions) ---
    image_filename: str         # e.g. "shoulder_rotator_cuff.png". Empty for textbook chunks.
                                # [RETRIEVAL CRITICAL] UI uses this to display the diagram image
                                # when a diagram proposition is in retrieved_chunks.


# ---------------------------------------------------------------------------
# 3. DIAGRAM METADATA JSON
# Produced by: manual curation (scraping from AnatomyTOOL / MedPix)
# Consumed by: ingestion/index.py → index_diagrams()
# Saved to:    data/diagrams/<name>.json (one file per diagram)
# ---------------------------------------------------------------------------

class DiagramStructureEntry(TypedDict):
    name: str                   # Anatomical structure name. e.g. "Deltoid"
                                # [RETRIEVAL CRITICAL] index_diagrams() embeds this name
                                # when building the proposition sentence.
    structure_type: Literal["muscle", "nerve", "bone", "vessel", "joint", "ligament", "other"]
                                # [RETRIEVAL CRITICAL] Enables type-based queries.
                                # e.g. "what nerves are in the shoulder?" filters to nerve entries.
    origin: str                 # e.g. "Clavicle, acromion, scapular spine". Empty if not muscle.
    insertion: str              # e.g. "Deltoid tuberosity of humerus". Empty if not muscle.
    action: str                 # e.g. "Abduction of arm at shoulder"
    innervation: str            # e.g. "Axillary nerve (C5-C6)". Empty if bone/joint.
    clinical_note: str          # e.g. "Damaged in shoulder dislocation". Empty if none.
                                # [RETRIEVAL CRITICAL] OT students need clinical relevance.
                                # This surfaces in Socratic questions about patient scenarios.

class DiagramSchema(TypedDict):
    diagram_id: str             # UUID or slug. e.g. "shoulder_rotator_cuff"
    filename: str               # Image filename. e.g. "shoulder_rotator_cuff.png"
    title: str                  # Human-readable diagram title.
    source: Literal["AnatomyTOOL", "MedPix", "OpenStax", "other"]
    chapter_num: int            # Which textbook chapter this diagram relates to.
    chapter_title: str
    section_num: str
    section_title: str
    labels_visible: bool        # True = labels shown on image. False = unlabeled (blind test set).
    structures: list[DiagramStructureEntry]
                                # [RETRIEVAL CRITICAL] Each entry becomes one proposition in
                                # sokratic_kb. All structure fields are folded into the
                                # proposition text by index_diagrams().


# ---------------------------------------------------------------------------
# 4. QDRANT POINT PAYLOAD
# What gets stored per vector in sokratic_kb.
# This is the PropositionSchema fields that go into Qdrant payload.
# (Not a separate TypedDict — Qdrant payload IS the proposition dict.)
#
# Qdrant collection spec:
#   name:        sokratic_kb
#   vector_size: 3072          (text-embedding-3-large)
#   distance:    Cosine
#   on_disk:     False         (keep in RAM for < 200ms latency)
#
# Every PropositionSchema field is stored in payload EXCEPT nothing is dropped.
# Retriever gets back the full payload so it can:
#   - Return parent_chunk_text to cross-encoder without extra lookup
#   - Filter by domain, chapter_num, section_num, element_type
#   - Pass image_filename to UI for diagram display
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 5. RETRIEVED CHUNK (output of retriever.retrieve())
# What gets stored in TutorState["retrieved_chunks"]
# Produced by: retrieval/retriever.py
# Consumed by: conversation/dean.py, conversation/teacher.py, ui/app.py
# ---------------------------------------------------------------------------

class RetrievedChunkSchema(TypedDict):
    # Content
    chunk_id: str
    text: str                   # parent_chunk_text — what Teacher and cross-encoder see
    score: float                # cross-encoder score (higher = more relevant)

    # Provenance (for Dean to build guiding question chain + UI display)
    chapter_num: int
    chapter_title: str
    section_num: str
    section_title: str
    subsection_title: str
    page: int
    element_type: str           # "paragraph" | "table" | "figure_caption" | "diagram"
    domain: str

    # Diagram-only
    image_filename: str         # Empty string for textbook chunks. UI checks this to show image.


# ---------------------------------------------------------------------------
# Validation helpers — call these in ingestion scripts before indexing
# ---------------------------------------------------------------------------

def validate_chunk(chunk: dict) -> list[str]:
    """Return list of validation errors. Empty list = valid."""
    errors = []
    required = ChunkSchema.__annotations__.keys()
    for field in required:
        if field not in chunk:
            errors.append(f"Missing field: {field}")
    if chunk.get("text") and len(chunk["text"]) < 50:
        errors.append(f"Text too short ({len(chunk['text'])} chars): {chunk['text'][:80]}")
    if chunk.get("element_type") not in ("paragraph", "table", "figure_caption"):
        errors.append(f"Invalid element_type: {chunk.get('element_type')}")
    return errors


def validate_proposition(prop: dict) -> list[str]:
    """Return list of validation errors. Empty list = valid."""
    errors = []
    required = PropositionSchema.__annotations__.keys()
    for field in required:
        if field not in prop:
            errors.append(f"Missing field: {field}")
    if prop.get("text") and len(prop["text"]) < 10:
        errors.append(f"Proposition text suspiciously short: {prop['text']}")
    if not prop.get("parent_chunk_text"):
        errors.append("parent_chunk_text is empty — cross-encoder will have no context")
    return errors


def validate_diagram(diagram: dict) -> list[str]:
    """Return list of validation errors. Empty list = valid."""
    errors = []
    required = DiagramSchema.__annotations__.keys()
    for field in required:
        if field not in diagram:
            errors.append(f"Missing field: {field}")
    if not diagram.get("structures"):
        errors.append("No structures defined — diagram will produce 0 propositions")
    for i, s in enumerate(diagram.get("structures", [])):
        if not s.get("name"):
            errors.append(f"Structure {i} has no name")
        if not s.get("action") and not s.get("innervation"):
            errors.append(f"Structure '{s.get('name')}' has no action or innervation — low retrieval value")
    return errors
