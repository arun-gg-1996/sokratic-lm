"""
evaluation/generate_rag_qa_edge_cases.py
----------------------------------------
Generate 50 edge-case Q&A pairs for retrieval stress testing:
  1) Rare topics (10)
  2) Cross-chapter dependencies (10)
  3) Informal language queries (10)
  4) Clinical OT scenarios (10)
  5) Single-occurrence terms (10)

Output:
  data/eval/rag_qa_edge_cases.jsonl
"""

from __future__ import annotations

import json
import random
import re
from collections import Counter
from pathlib import Path

from config import cfg

SEED = 42
OUT_PATH = Path("data/eval/rag_qa_edge_cases.jsonl")


def load_base_chunks(path: str) -> list[dict]:
    chunks = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            c = json.loads(line)
            if c.get("is_overlap") is not False:
                continue
            if c.get("element_type") == "paragraph_overlap":
                continue
            if int(c.get("chapter_num", 0)) == 28:
                # Skip index page chunks.
                continue
            text = (c.get("text") or "").strip()
            if len(text) < 80:
                continue
            chunks.append(c)
    return chunks


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [p.strip() for p in parts if len(p.strip()) >= 30]


def clean_sentence(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def pick_answer_sentence(chunk_text: str, terms: list[str]) -> str | None:
    sentences = split_sentences(chunk_text)
    if not sentences:
        return None

    low_terms = [t.lower() for t in terms if t]
    for s in sentences:
        low = s.lower()
        if any(t in low for t in low_terms):
            return clean_sentence(s)

    # fallback: first declarative sentence
    for s in sentences:
        if "?" not in s and "http://" not in s.lower() and "https://" not in s.lower():
            return clean_sentence(s)
    return clean_sentence(sentences[0])


def chunk_matches(chunk: dict, terms: list[str], chapter: int | None = None) -> bool:
    if chapter is not None and int(chunk.get("chapter_num", 0)) != chapter:
        return False
    text = chunk.get("text", "").lower()
    return any(t.lower() in text for t in terms)


def choose_chunk(chunks: list[dict], terms: list[str], chapter: int | None = None) -> dict:
    matches = [c for c in chunks if chunk_matches(c, terms, chapter)]
    if not matches:
        # fallback by chapter if term not found there
        if chapter is not None:
            chapter_chunks = [c for c in chunks if int(c.get("chapter_num", 0)) == chapter]
            if chapter_chunks:
                return random.choice(chapter_chunks)
        return random.choice(chunks)

    # prefer medium-sized chunks (better sentence quality)
    matches.sort(key=lambda c: abs(len(c.get("text", "")) - 550))
    return matches[0]


def make_record(
    *,
    question: str,
    question_type: str,
    chunk: dict,
    expected_answer: str,
    category: str,
    extra: dict | None = None,
) -> dict:
    rec = {
        "question": question,
        "expected_answer": expected_answer,
        "source_chunk_id": chunk["chunk_id"],
        "chapter_num": int(chunk.get("chapter_num", 0)),
        "chapter_title": chunk.get("chapter_title", ""),
        "section_title": chunk.get("section_title", ""),
        "question_type": question_type,
        "edge_case_category": category,
    }
    if extra:
        rec.update(extra)
    return rec


def generate_rare_topics(chunks: list[dict]) -> list[dict]:
    anatomy_terms = [
        "axillary nerve",
        "musculocutaneous nerve",
        "subscapularis",
        "teres minor",
        "coracobrachialis",
        "brachioradialis",
        "extensor carpi radialis",
        "flexor digitorum superficialis",
        "anterior interosseous",
        "posterior interosseous",
        "quadratus lumborum",
        "iliopsoas",
        "tensor fasciae latae",
        "obturator nerve",
        "femoral nerve",
        "saphenous nerve",
        "common peroneal nerve",
        "deep peroneal nerve",
        "superficial peroneal nerve",
        "sural nerve",
    ]

    rows: list[tuple[str, list[dict]]] = []
    for term in anatomy_terms:
        found = [c for c in chunks if term.lower() in c.get("text", "").lower()]
        if 1 <= len(found) <= 3:
            rows.append((term, found))

    rows.sort(key=lambda x: (len(x[1]), x[0]))
    out: list[dict] = []
    used_terms: set[str] = set()
    for term, found in rows:
        if len(out) >= 10:
            break
        if term in used_terms:
            continue
        chunk = found[0]
        answer = pick_answer_sentence(chunk.get("text", ""), [term]) or clean_sentence(chunk.get("text", "")[:200])
        question = f"What does this passage say about the {term}?"
        out.append(
            make_record(
                question=question,
                question_type="factual",
                chunk=chunk,
                expected_answer=answer,
                category="rare_topics",
                extra={"rare_term": term},
            )
        )
        used_terms.add(term)

    if len(out) != 10:
        raise RuntimeError(f"Rare topics generation produced {len(out)} records, expected 10.")
    return out


def generate_cross_chapter(chunks: list[dict]) -> list[dict]:
    spec = [
        {
            "question": "What happens at the sarcomere level when the deltoid muscle contracts?",
            "primary_chapter": 10,
            "secondary_chapter": 11,
            "terms": ["sarcomere", "contraction"],
        },
        {
            "question": "What spinal cord segments give rise to the nerve that controls deltoid?",
            "primary_chapter": 13,
            "secondary_chapter": 11,
            "terms": ["axillary nerve", "c5", "c6", "brachial plexus"],
        },
        {
            "question": "What is the motor pathway from the brain to the biceps brachii muscle?",
            "primary_chapter": 14,
            "secondary_chapter": 11,
            "terms": ["motor pathway", "upper motor neuron", "lower motor neuron"],
        },
        {
            "question": "What type of joint is the shoulder and which muscles act on it?",
            "primary_chapter": 9,
            "secondary_chapter": 11,
            "terms": ["shoulder joint", "ball-and-socket", "glenohumeral"],
        },
        {
            "question": "How does a neuromuscular junction work?",
            "primary_chapter": 12,
            "secondary_chapter": 13,
            "terms": ["neuromuscular junction", "synapse"],
        },
        {
            "question": "What blood vessels supply the deltoid muscle?",
            "primary_chapter": 20,
            "secondary_chapter": 11,
            "terms": ["axillary artery", "brachial artery", "anterior circumflex"],
        },
        {
            "question": "What metabolic process provides ATP for sustained muscle contraction?",
            "primary_chapter": 24,
            "secondary_chapter": 10,
            "terms": ["oxidative phosphorylation", "atp", "metabolism"],
        },
        {
            "question": "What is the difference between the somatic and autonomic nervous systems?",
            "primary_chapter": 14,
            "secondary_chapter": 15,
            "terms": ["somatic nervous system", "autonomic"],
        },
        {
            "question": "What reflex protects the knee joint from hyperextension?",
            "primary_chapter": 14,
            "secondary_chapter": 9,
            "terms": ["stretch reflex", "knee", "reflex"],
        },
        {
            "question": "Which nerve controls wrist extension and what happens if it is damaged?",
            "primary_chapter": 13,
            "secondary_chapter": 11,
            "terms": ["radial nerve", "ulnar nerve", "median nerve"],
        },
    ]

    out: list[dict] = []
    for item in spec:
        chunk = choose_chunk(
            chunks=chunks,
            terms=item["terms"],
            chapter=item["primary_chapter"],
        )
        answer = pick_answer_sentence(chunk.get("text", ""), item["terms"]) or clean_sentence(chunk.get("text", "")[:200])
        out.append(
            make_record(
                question=item["question"],
                question_type="cross_chapter",
                chunk=chunk,
                expected_answer=answer,
                category="cross_chapter_dependencies",
                extra={"secondary_chapter": item["secondary_chapter"]},
            )
        )
    return out


def generate_informal(chunks: list[dict]) -> list[dict]:
    spec = [
        ("that bump on your shoulder where the muscle attaches", ["deltoid tuberosity", "humerus"]),
        ("the muscle that lets you shrug your shoulders", ["trapezius"]),
        ("what makes your arm go numb when you hit your elbow", ["ulnar nerve"]),
        ("the muscle tear athletes get in their shoulder", ["rotator cuff", "supraspinatus"]),
        ("nerve that gets pinched in carpal tunnel", ["median nerve", "carpal tunnel"]),
        ("the joint that pops when you crack your knuckles", ["interphalangeal", "synovial fluid", "joint"]),
        ("muscle under your armpit", ["serratus anterior", "pectoralis minor", "axillary"]),
        ("what holds your bones together at a joint", ["ligament", "joint capsule"]),
        ("the nerve damage that causes foot drop", ["fibular nerve", "peroneal", "sciatic nerve"]),
        ("why your muscle shakes when you hold something heavy for too long", ["fatigue", "atp", "muscle contraction"]),
    ]

    out: list[dict] = []
    for question, terms in spec:
        chunk = choose_chunk(chunks=chunks, terms=terms, chapter=None)
        answer = pick_answer_sentence(chunk.get("text", ""), terms) or clean_sentence(chunk.get("text", "")[:200])
        out.append(
            make_record(
                question=question,
                question_type="factual",
                chunk=chunk,
                expected_answer=answer,
                category="informal_language",
                extra={"informal_bridge": True},
            )
        )
    return out


def generate_clinical_ot(chunks: list[dict]) -> list[dict]:
    spec = [
        (
            "A patient presents with inability to abduct the arm after anterior shoulder dislocation. Which nerve is most likely damaged?",
            ["axillary nerve", "abduct", "shoulder"],
        ),
        (
            "An OT patient has weakness in elbow flexion and forearm supination. Which nerve root level is affected?",
            ["c5", "c6", "forearm", "flexion"],
        ),
        (
            "A patient cannot extend their wrist after a humeral shaft fracture. Which nerve is compressed?",
            ["radial nerve", "wrist", "humerus"],
        ),
        (
            "After carpal tunnel release surgery, which movement should the OT prioritize rehabilitating first?",
            ["carpal tunnel", "thumb opposition", "thenar", "median nerve"],
        ),
        (
            "A patient has weakness in finger abduction and adduction. Which nerve is involved?",
            ["finger abduction", "ulnar nerve", "interossei"],
        ),
        (
            "An OT patient cannot shrug their shoulder after neck surgery. Which cranial nerve was damaged?",
            ["cranial nerve xi", "accessory nerve", "trapezius"],
        ),
        (
            "A patient has foot drop after knee replacement surgery. Which nerve was injured during the procedure?",
            ["fibular nerve", "sciatic nerve", "knee"],
        ),
        (
            "After a shoulder dislocation, a patient has numbness over the lateral deltoid region. Which nerve is affected?",
            ["axillary nerve", "deltoid", "cutaneous"],
        ),
        (
            "A patient presents with weakness in hip flexion and knee extension. Which nerve is compromised?",
            ["femoral nerve", "hip flexion", "knee extension"],
        ),
        (
            "An OT patient has a claw hand deformity. Which two nerves are most likely damaged?",
            ["ulnar nerve", "median nerve", "hand"],
        ),
    ]

    out: list[dict] = []
    for question, terms in spec:
        chunk = choose_chunk(chunks=chunks, terms=terms)
        answer = pick_answer_sentence(chunk.get("text", ""), terms) or clean_sentence(chunk.get("text", "")[:200])
        out.append(
            make_record(
                question=question,
                question_type="clinical",
                chunk=chunk,
                expected_answer=answer,
                category="clinical_ot",
            )
        )
    return out


def generate_single_occurrence(chunks: list[dict]) -> list[dict]:
    specific_terms = [
        "deltoid tuberosity",
        "olecranon fossa",
        "coracoid process",
        "anatomical snuffbox",
        "carpal tunnel",
        "pes anserinus",
        "iliotibial band",
        "calcaneal tendon",
        "plantar fascia",
        "rotator interval",
        "scapular notch",
        "cubital tunnel",
        "Guyon canal",
        "tarsal tunnel",
        "anterior talofibular",
        "calcaneofibular ligament",
        "spring ligament",
        "triangular fibrocartilage",
        "interosseous membrane",
        "quadrangular space",
    ]

    fallback_terms = [
        "saphenous nerve",
        "coracobrachialis",
        "extensor carpi radialis",
        "axillary nerve",
        "teres major",
        "bicipital groove",
        "intertubercular groove",
        "sciatic nerve",
        "tibial nerve",
        "fibular nerve",
        "ulnar nerve",
    ]

    def occurrences(term: str) -> list[dict]:
        low = term.lower()
        return [c for c in chunks if low in c.get("text", "").lower()]

    chosen_terms: list[str] = []
    for t in specific_terms:
        if len(occurrences(t)) == 1:
            chosen_terms.append(t)
    if len(chosen_terms) < 10:
        for t in fallback_terms:
            if t in chosen_terms:
                continue
            if len(occurrences(t)) == 1:
                chosen_terms.append(t)
            if len(chosen_terms) >= 10:
                break

    if len(chosen_terms) < 10:
        raise RuntimeError(f"Could not find 10 single-occurrence terms (found {len(chosen_terms)}).")

    chosen_terms = chosen_terms[:10]
    out: list[dict] = []
    for term in chosen_terms:
        chunk = occurrences(term)[0]
        answer = pick_answer_sentence(chunk.get("text", ""), [term]) or clean_sentence(chunk.get("text", "")[:200])
        question = f"What is the {term} and what is its clinical significance?"
        out.append(
            make_record(
                question=question,
                question_type="factual",
                chunk=chunk,
                expected_answer=answer,
                category="single_occurrence",
                extra={"rarity": "single_occurrence", "single_term": term},
            )
        )
    return out


def main() -> None:
    random.seed(SEED)
    chunks = load_base_chunks(cfg.domain_path("chunks"))

    records: list[dict] = []
    records.extend(generate_rare_topics(chunks))
    records.extend(generate_cross_chapter(chunks))
    records.extend(generate_informal(chunks))
    records.extend(generate_clinical_ot(chunks))
    records.extend(generate_single_occurrence(chunks))

    if len(records) != 50:
        raise RuntimeError(f"Expected 50 edge-case records, got {len(records)}.")

    # Quick sanity: grounding check by containment
    by_id = {c["chunk_id"]: c for c in chunks}
    bad_grounding = 0
    for r in records:
        src = by_id.get(r["source_chunk_id"], {})
        if " ".join(r["expected_answer"].split()).lower() not in " ".join(src.get("text", "").split()).lower():
            bad_grounding += 1
    if bad_grounding:
        raise RuntimeError(f"Grounding check failed for {bad_grounding} edge-case records.")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    counts = Counter(r["edge_case_category"] for r in records)
    print(f"Wrote {len(records)} edge-case records -> {OUT_PATH}")
    print(f"Category counts: {dict(counts)}")


if __name__ == "__main__":
    main()

