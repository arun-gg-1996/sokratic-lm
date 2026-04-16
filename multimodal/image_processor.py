"""
multimodal/image_processor.py
------------------------------
Handles the multimodal path when a student uploads an anatomy image.

Flow:
  1. Send image to GPT-4o Vision → identify anatomical structures
  2. For each structure with confidence > 0.5: run retrieval
  3. Also search data/diagrams/ metadata JSONs (pre-indexed in knowledge base)
  4. Combine chunk results
  5. Return populated retrieved_chunks and image_structures for TutorState

Edge cases:
  - Non-anatomy image: all retrieval scores below out_of_scope_threshold → return empty
  - Low overall confidence: return signal for Teacher to ask student to describe the image
  - Unlabeled diagram: proceed with identified structures as starting hints
"""

from config import cfg


def process_image(image_bytes: bytes, retriever) -> dict:
    """
    Identify structures in an anatomy image and retrieve relevant textbook chunks.

    Args:
        image_bytes: Raw image bytes from Streamlit file uploader.
        retriever:   Retriever instance.

    Returns:
        {
          "image_structures": list[str],      # identified structure names
          "retrieved_chunks": list[dict],     # from retrieval on those structures
          "low_confidence": bool              # True if overall confidence < 0.5
        }
    """
    # TODO: base64-encode image
    # TODO: call GPT-4o Vision → parse into [{name, type, confidence}]
    # TODO: filter structures with confidence >= cfg.thresholds.image_structure_confidence
    # TODO: for each passing structure: call retriever.retrieve(structure_name)
    #       each call returns top-5 chunks already cross-encoder ranked
    # TODO: merge all chunk lists across all structure queries
    # TODO: deduplicate by chunk text — keep highest score per unique chunk
    # TODO: sort merged list by score descending, take top cfg.retrieval.top_chunks_final
    # TODO: return result dict
    raise NotImplementedError


def _identify_structures(image_bytes: bytes) -> list[dict]:
    """
    Call GPT-4o Vision and return identified structures.

    Returns:
        List of {name: str, type: str, confidence: float}
    """
    # TODO: implement GPT-4o Vision call
    raise NotImplementedError
