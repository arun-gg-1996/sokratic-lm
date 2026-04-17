"""
evaluation/generate_rag_qa.py
------------------------------
One-time script: generate a grounded RAG Q&A dataset from existing chunks.

Current generation plan (fixed-size, deterministic):
  - 100 total pairs
  - Chapter distribution:
      Ch11: 30, Ch13: 20, Ch14: 20, Ch9: 15, Cross-group: 15
  - Question type distribution:
      factual: 40, clinical: 35, cross_chapter: 25

Rules:
  - Every expected answer is copied from source chunk text (1-2 sentences).
  - No outside knowledge and no free-text hallucinated answers.
"""

import json
import random
import re
from pathlib import Path
from collections import Counter
from config import cfg

SEED = 42
N_PAIRS = 100

# Exact requested distribution
GROUP_PLAN: dict[str, dict] = {
    "ch11": {"chapter": 11, "total": 30, "factual": 13, "clinical": 13, "cross_chapter": 4},
    "ch13": {"chapter": 13, "total": 20, "factual": 9, "clinical": 9, "cross_chapter": 2},
    "ch14": {"chapter": 14, "total": 20, "factual": 9, "clinical": 9, "cross_chapter": 2},
    "ch9": {"chapter": 9, "total": 15, "factual": 9, "clinical": 4, "cross_chapter": 2},
    # cross group is drawn from chapters other than 9/11/13/14
    "cross": {"chapter": None, "total": 15, "factual": 0, "clinical": 0, "cross_chapter": 15},
}


def load_chunks(path: str) -> list[dict]:
    """
    Load base chunks only.
    """
    chunks = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            c = json.loads(line)
            if c.get("is_overlap") is not False:
                continue
            if c.get("element_type") == "paragraph_overlap":
                continue
            text = (c.get("text") or "").strip()
            if len(text) < 120:
                continue
            if not is_quality_chunk(text):
                continue
            chunks.append(c)
    return chunks


def split_sentences(text: str) -> list[str]:
    """
    Lightweight sentence splitter suitable for textbook prose.
    """
    text = re.sub(r"\s+", " ", text.strip())
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [p.strip() for p in parts if len(p.strip()) >= 35]


def is_quality_chunk(text: str) -> bool:
    """
    Filter out glossary/index/noise style chunks to keep eval questions meaningful.
    """
    low = text.lower()

    # Known noise markers
    noise_markers = [
        "interactive link",
        "http://",
        "https://",
        "review questions",
        "learning objectives",
    ]
    if any(m in low for m in noise_markers):
        return False

    # Index-like chunk: many "term 123" patterns
    term_num_pairs = re.findall(r"\b[a-z][a-z\-]{2,}\s+\d{1,4}\b", low)
    if len(term_num_pairs) >= 8:
        return False

    # Overly punctuation-heavy and list-like
    comma_count = text.count(",")
    semicolon_count = text.count(";")
    period_count = text.count(".")
    if (comma_count + semicolon_count) >= 18 and period_count <= 1:
        return False

    sentences = split_sentences(text)
    if len(sentences) < 1:
        return False

    return True


def _usable_answer_sentence(s: str) -> bool:
    s_strip = s.strip()
    if len(s_strip) < 45 or len(s_strip) > 260:
        return False
    if "http://" in s_strip.lower() or "https://" in s_strip.lower():
        return False
    if "interactive link" in s_strip.lower():
        return False
    if "figure " in s_strip.lower() or "table " in s_strip.lower():
        return False
    if "?" in s_strip:
        return False
    # Prefer declarative sentence starting with a letter.
    if not re.match(r"^[A-Z][A-Za-z0-9]", s_strip):
        return False
    return True


def _pick_clinical_sentence(sentences: list[str]) -> str | None:
    clinical_patterns = [
        r"\b(patient|clinical|exam|diagnos|injur|damage|paraly|lesion|sever|weakness|loss)\b",
        r"\b(cannot|unable|fails to|impaired)\b",
    ]
    for s in sentences:
        if not _usable_answer_sentence(s):
            continue
        low = s.lower()
        if any(re.search(p, low) for p in clinical_patterns):
            return s
    return None


def _pick_cross_sentence(sentences: list[str]) -> str | None:
    """
    Find a sentence with multi-system linkage cues.
    """
    buckets = {
        "muscle": r"\b(muscle|deltoid|biceps|triceps|rotator cuff|contraction)\b",
        "nerve": r"\b(nerve|axillary|brachial plexus|c5|c6|motor neuron)\b",
        "joint": r"\b(joint|abduct|movement|range of motion|glenohumeral)\b",
        "energy": r"\b(atp|metabolism|fatigue|energy)\b",
    }
    for s in sentences:
        if not _usable_answer_sentence(s):
            continue
        low = s.lower()
        hit = 0
        for pattern in buckets.values():
            if re.search(pattern, low):
                hit += 1
        if hit >= 2:
            return s
    return None


def _pick_factual_sentence(sentences: list[str]) -> str | None:
    factual_patterns = [
        r"\b(is|are|refers to|defined as)\b",
        r"\b(originates|inserts|innervated|abducts|adducts|flexes|extends)\b",
        r"\b(consists of|contains|includes)\b",
    ]
    for s in sentences:
        if not _usable_answer_sentence(s):
            continue
        low = s.lower()
        if any(re.search(p, low) for p in factual_patterns):
            return s
    for s in sentences:
        if _usable_answer_sentence(s):
            return s
    return None


def pick_answer(chunk_text: str, question_type: str) -> str | None:
    """
    Expected answer must be directly grounded in chunk text.
    Return 1-2 sentences (we use one authoritative sentence).
    """
    sentences = split_sentences(chunk_text)
    if not sentences:
        return None

    if question_type == "clinical":
        s = _pick_clinical_sentence(sentences)
        if s:
            return s
        # fallback to any factual sentence if explicitly clinical sentence absent
        s = _pick_factual_sentence(sentences)
        return s

    if question_type == "cross_chapter":
        s = _pick_cross_sentence(sentences)
        if s:
            return s
        s = _pick_factual_sentence(sentences)
        return s

    return _pick_factual_sentence(sentences)


def _extract_subject(sentence: str) -> str:
    sentence = sentence.strip()
    sentence = re.sub(r"^(In males|In females|Finally|However|Therefore|Thus|For example|In skeletal muscle tissue|In anatomical terminology),?\s+", "", sentence, flags=re.IGNORECASE)
    m = re.search(r"\bthe ([a-z][a-z0-9\-\s]{2,60}?)\s+(is|are|was|were|originates|inserts|innervated|abducts|adducts|flexes|extends)\b", sentence, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m2 = re.search(r"\b([A-Z][a-z]+(?:\s+[a-zA-Z\-]+){0,4})\b", sentence)
    if m2:
        return m2.group(1).strip()
    words = re.findall(r"[A-Za-z][A-Za-z\-]+", sentence)
    if not words:
        return "this concept"
    return " ".join(words[:4]).strip()


def make_question(answer_sentence: str, question_type: str) -> str:
    """
    Convert grounded answer sentence into a student-style question.
    """
    low = answer_sentence.lower()
    subject = _extract_subject(answer_sentence)

    if question_type == "clinical":
        if "innervat" in low:
            return f"In a clinical case, which nerve is described as innervating the {subject}?"
        if "lesion" in low or "sever" in low or "damage" in low:
            return f"If this structure is damaged, what does the textbook say happens to {subject}?"
        if answer_sentence.strip().lower().startswith("when "):
            stem = answer_sentence.strip().rstrip(".")
            return f"{stem}?"
        if any(k in low for k in ["abduct", "adduct", "flex", "extend", "movement"]):
            return f"For a patient with movement issues, what does this text say about {subject}?"
        if any(k in low for k in ["damage", "injur", "paraly", "lesion", "sever"]):
            return f"In an injury scenario, what outcome does this text report about {subject}?"
        return f"In a patient assessment, what does the textbook state about {subject}?"

    if question_type == "cross_chapter":
        if "atp" in low and "contraction" in low:
            return "How does ATP relate to muscle contraction according to this text?"
        if "brachial plexus" in low and "axillary" in low:
            return "What connection does this text make between the brachial plexus and the axillary pathway?"
        if any(k in low for k in ["nerve", "muscle"]) and any(k in low for k in ["joint", "movement", "abduct"]):
            return "How does this passage connect nerve-level and movement-level anatomy?"
        if "somatic" in low and "muscle" in low:
            return "How does this text connect the somatic nervous system to skeletal muscle function?"
        return f"How does this passage connect multiple concepts around {subject}?"

    # factual
    if low.startswith("there are "):
        m = re.search(r"there are\s+(?:\w+\s+)?([a-z][a-z0-9\-\s]{2,60}?)(?:,|\s+which|\s+that|\.)", low)
        if m:
            return f"How many {m.group(1).strip()} are described in this passage?"
        return "How many structures are described in this passage?"
    if low.startswith("there is "):
        m = re.search(r"there is\s+(?:an?|the)?\s*([a-z][a-z0-9\-\s]{2,60}?)(?:,|\s+that|\s+which|\.)", low)
        if m:
            return f"What structure is described as {m.group(1).strip()} in this passage?"
        return "What structure is described in this passage?"
    if low.startswith("in males, there is"):
        m = re.search(r"in males,\s+there is\s+(?:an?|the)?\s*([a-z][a-z0-9\-\s]{2,70}?)(?:\s+that|\s+which|,|\.)", low)
        if m:
            return f"In males, which structure is identified as {m.group(1).strip()}?"
        return "In males, which structure does this passage identify?"
    if "light with a wavelength of" in low and "is" in low:
        return "What does this text say about how wavelength relates to perceived light color?"
    if "innervat" in low:
        return f"Which nerve innervates the {subject}?"
    if "originates" in low:
        return f"Where does the {subject} originate?"
    if "inserts" in low:
        return f"Where does the {subject} insert?"
    m = re.search(r"\b[Tt]he ([A-Za-z][A-Za-z0-9\-\s]{2,80}?)\s+includes?\b", answer_sentence)
    if m:
        return f"Which structures are included in the {m.group(1).strip()}?"
    m = re.search(r"\b[Tt]he ([A-Za-z][A-Za-z0-9\-\s]{2,80}?)\s+is\b", answer_sentence)
    if m:
        return f"What is the {m.group(1).strip()}?"
    m = re.search(r"\b[Tt]he ([A-Za-z][A-Za-z0-9\-\s]{2,80}?)\s+are\b", answer_sentence)
    if m:
        return f"What are the {m.group(1).strip()}?"
    if "abduct" in low:
        return f"What does this text say about abduction related to {subject}?"
    if "brachial plexus" in low:
        return "What is the brachial plexus according to this text?"
    return f"What does this passage state about {subject}?"


def _cross_candidate(chunk: dict) -> bool:
    """
    Candidate chunk for cross-group: should carry multi-concept linkage
    and not come from the 4 fixed chapter groups.
    """
    ch = int(chunk.get("chapter_num", 0))
    if ch in {9, 11, 13, 14}:
        return False
    low = (chunk.get("text") or "").lower()
    buckets = 0
    if re.search(r"\b(muscle|contraction|deltoid|biceps|triceps)\b", low):
        buckets += 1
    if re.search(r"\b(nerve|brachial plexus|axillary|c5|c6)\b", low):
        buckets += 1
    if re.search(r"\b(joint|movement|abduct|range of motion)\b", low):
        buckets += 1
    if re.search(r"\b(atp|metabolism|fatigue|energy)\b", low):
        buckets += 1
    return buckets >= 2


def _sample_records(
    chunks: list[dict],
    chapter_num: int | None,
    count: int,
    question_type: str,
    used_chunk_ids: set[str],
    require_cross_candidate: bool = False,
) -> list[dict]:
    """
    Sample source chunks and build Q&A records for one bucket.
    """
    candidates = []
    for c in chunks:
        cid = c.get("chunk_id")
        if not cid or cid in used_chunk_ids:
            continue
        if chapter_num is not None and int(c.get("chapter_num", 0)) != chapter_num:
            continue
        if require_cross_candidate and not _cross_candidate(c):
            continue
        candidates.append(c)

    random.shuffle(candidates)
    out: list[dict] = []

    for c in candidates:
        if len(out) >= count:
            break
        answer = pick_answer(c.get("text", ""), question_type=question_type)
        if not answer:
            continue
        question = make_question(answer, question_type=question_type)
        rec = {
            "question": question,
            "expected_answer": answer,
            "source_chunk_id": c["chunk_id"],
            "chapter_num": int(c.get("chapter_num", 0)),
            "chapter_title": c.get("chapter_title", ""),
            "section_title": c.get("section_title", ""),
            "question_type": question_type,
        }
        out.append(rec)
        used_chunk_ids.add(c["chunk_id"])

    return out


def run() -> list[dict]:
    """
    Main entry point. Load chunks, sample, generate Q&A pairs, save to JSONL.
    """
    random.seed(SEED)

    chunks_path = cfg.paths.chunks_ot
    out_path = Path("data/eval/rag_qa.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    chunks = load_chunks(chunks_path)
    used_chunk_ids: set[str] = set()
    records: list[dict] = []

    # Build exact distribution by group and question type.
    for group_name, plan in GROUP_PLAN.items():
        chapter = plan["chapter"]
        # factual
        n = int(plan["factual"])
        if n:
            records.extend(
                _sample_records(
                    chunks=chunks,
                    chapter_num=chapter,
                    count=n,
                    question_type="factual",
                    used_chunk_ids=used_chunk_ids,
                    require_cross_candidate=False,
                )
            )
        # clinical
        n = int(plan["clinical"])
        if n:
            records.extend(
                _sample_records(
                    chunks=chunks,
                    chapter_num=chapter,
                    count=n,
                    question_type="clinical",
                    used_chunk_ids=used_chunk_ids,
                    require_cross_candidate=False,
                )
            )
        # cross_chapter
        n = int(plan["cross_chapter"])
        if n:
            records.extend(
                _sample_records(
                    chunks=chunks,
                    chapter_num=chapter,
                    count=n,
                    question_type="cross_chapter",
                    used_chunk_ids=used_chunk_ids,
                    require_cross_candidate=(group_name == "cross"),
                )
            )

    # Safety checks
    if len(records) != N_PAIRS:
        raise RuntimeError(f"Expected exactly {N_PAIRS} records, generated {len(records)}.")

    # Verify chapter distribution
    chapter_counter = Counter([r["chapter_num"] for r in records if r["chapter_num"] in {9, 11, 13, 14}])
    if chapter_counter[11] != 30 or chapter_counter[13] != 20 or chapter_counter[14] != 20 or chapter_counter[9] != 15:
        raise RuntimeError(f"Chapter distribution mismatch: {dict(chapter_counter)}")

    # Verify cross-group count = records not in target fixed chapter groups
    cross_group_count = sum(1 for r in records if r["chapter_num"] not in {9, 11, 13, 14})
    if cross_group_count != 15:
        raise RuntimeError(f"Cross-group count mismatch: {cross_group_count}")

    # Verify question type distribution
    type_counter = Counter([r["question_type"] for r in records])
    if type_counter["factual"] != 40 or type_counter["clinical"] != 35 or type_counter["cross_chapter"] != 25:
        raise RuntimeError(f"Type distribution mismatch: {dict(type_counter)}")

    # Write JSONL
    with out_path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"Wrote {len(records)} records -> {out_path}")
    print(f"Question type distribution: {dict(type_counter)}")
    print(f"Chapter distribution (9/11/13/14): {dict(chapter_counter)}")
    print(f"Cross-group records (chapters not 9/11/13/14): {cross_group_count}")
    return records


if __name__ == "__main__":
    run()
