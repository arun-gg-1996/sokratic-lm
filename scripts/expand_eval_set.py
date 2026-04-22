"""
scripts/expand_eval_set.py
---------------------------
Expand the RAG eval set from 100 → ~300 queries by adding four missing
categories that are underrepresented in rag_qa.jsonl:

  1. Conversational paraphrases of existing queries (50)
  2. Misspelled variants of existing queries (30, deterministic typos)
  3. Abbreviations / informal clinical shorthand (30, LLM-generated with ground truth)
  4. OOD negatives (50, no ground truth — expected empty retrieval)

Output: data/eval/rag_qa_expanded.jsonl (contains original 100 + new ~160)
"""
import json
import random
import re
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

ROOT = Path(__file__).parent.parent
SRC = ROOT / "data/eval/rag_qa.jsonl"
OUT = ROOT / "data/eval/rag_qa_expanded.jsonl"


def load_jsonl(path):
    return [json.loads(l) for l in open(path)]


def write_jsonl(path, rows):
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


# --- (2) Deterministic misspellings ------------------------------------------
_TYPO_RULES = [
    ("axillary",     "axillery"),
    ("supraspinatus","supraspinatous"),
    ("musculocutaneous","musculocuetaneous"),
    ("vertebrae",    "vertabrae"),
    ("humerus",      "humeris"),
    ("deltoid",      "deltiod"),
    ("biceps",       "bicepts"),
    ("brachial",     "bracheal"),
    ("sternocleidomastoid","sternoclydomastoid"),
    ("trapezius",    "trapezious"),
    ("rotator",      "rotater"),
    ("cranial",      "cranial"),  # no typo; skip via empty tuple below
    ("diaphragm",    "diaphram"),
    ("lumbar",       "lumbaar"),
    ("cerebellum",   "cerebellem"),
]


def add_typo(text: str) -> str | None:
    """Return a typo'd version of text if one of the rules applies, else None."""
    for correct, wrong in _TYPO_RULES:
        if correct == wrong:
            continue
        pat = re.compile(rf"\b{re.escape(correct)}\b", re.IGNORECASE)
        if pat.search(text):
            return pat.sub(wrong, text, count=1)
    return None


# --- (1) Conversational paraphrases (LLM-generated) --------------------------
def conversational_paraphrase(queries_batch: list[dict], client) -> list[dict]:
    """
    For each query, ask Claude Haiku to rewrite it as a conversational student
    question. Preserves ground truth (same source_chunk_id + expected_answer).
    """
    new_rows = []
    for q in queries_batch:
        prompt = (
            "Rewrite this textbook question as a casual, natural question a student "
            "might type into a tutoring chatbot. Keep the same intent and expected answer.\n"
            "Use informal phrasing, hedging ('I think...', 'what's the...'), or incomplete sentences.\n"
            "Do not add meta-commentary. Output only the rewritten question on one line.\n\n"
            f"Original: {q['question']}"
        )
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=120,
                messages=[{"role": "user", "content": prompt}],
            )
            new_q = resp.content[0].text.strip().strip('"')
            if new_q and len(new_q) > 5:
                new_rows.append({
                    **q,
                    "question": new_q,
                    "question_style": "conversational",
                    "derived_from_chunk_id": q.get("source_chunk_id"),
                })
        except Exception as e:
            print(f"  skip conversational paraphrase: {e}")
    return new_rows


# --- (3) Abbreviations / informal shorthand with ground truth ---------------
# These are hand-curated because ground-truth mapping requires domain knowledge.
ABBREV_QUERIES = [
    # Cranial nerves — these map to whatever chunks actually discuss them.
    ("CN VII function", "facial", "synonym"),
    ("what does CN X do", "vagus", "synonym"),
    ("CN V branches", "trigeminal", "synonym"),
    ("CN II and vision", "optic", "synonym"),
    ("CN III muscle control", "oculomotor", "synonym"),
    ("CN IX role", "glossopharyngeal", "synonym"),
    ("CN XI motor", "accessory", "synonym"),
    # Clinical shorthand
    ("wrist drop cause", "radial", "clinical_shorthand"),
    ("what is foot drop", "peroneal", "clinical_shorthand"),
    ("claw hand nerve", "ulnar", "clinical_shorthand"),
    ("dead arm after shoulder dislocation", "axillary", "clinical_shorthand"),
    ("saturday night palsy explained", "radial", "clinical_shorthand"),
    ("tennis elbow anatomy", "epicondyl", "clinical_shorthand"),
    ("frozen shoulder", "capsulitis", "clinical_shorthand"),
    ("carpal tunnel pain", "median", "clinical_shorthand"),
    # Ligament abbreviations
    ("ACL injury", "cruciate", "abbreviation"),
    ("PCL function", "cruciate", "abbreviation"),
    ("MCL role", "collateral", "abbreviation"),
    # Cardiac
    ("LAD supply area", "coronary", "abbreviation"),
    ("what is the RCA", "coronary", "abbreviation"),
    # OT test names
    ("empty can test muscle", "supraspinatus", "ot_test_name"),
    ("jobe test", "supraspinatus", "ot_test_name"),
    ("phalen test purpose", "median", "ot_test_name"),
    ("tinel sign meaning", "nerve", "ot_test_name"),
    # Conversational medical questions
    ("why does my wrist feel numb typing", "median", "conversational_clinical"),
    ("shoulder pain at night what muscle", "rotator", "conversational_clinical"),
    ("numbness in pinky", "ulnar", "conversational_clinical"),
    ("pain going down arm after fall", "plexus", "conversational_clinical"),
    ("thumb weakness after cut on wrist", "median", "conversational_clinical"),
    ("face drooping one side", "facial", "conversational_clinical"),
]


def build_abbrev_rows():
    """
    For abbreviation/informal queries we don't need a specific chunk_id —
    we check ground truth by whether the expected KEYWORD appears in any
    retrieved chunk. This is weaker than chunk-level ground truth but
    appropriate for these intentionally-ambiguous queries.
    """
    rows = []
    for q, kw, style in ABBREV_QUERIES:
        rows.append({
            "question": q,
            "expected_keyword": kw,
            "question_style": style,
            "question_type": "keyword_match",
            "source_chunk_id": None,  # keyword-match ground truth
        })
    return rows


# --- (4) OOD negatives -------------------------------------------------------
OOD_QUERIES = [
    "best pizza in buffalo",
    "what is the capital of France",
    "how to fix a leaky faucet",
    "who won the 2024 super bowl",
    "python programming tutorial",
    "weather tomorrow in manhattan",
    "when was napoleon born",
    "recipes for chicken parmesan",
    "how to invest in stocks",
    "basketball rules",
    "learn spanish online",
    "best laptop for students",
    "airline baggage policy",
    "how to file taxes",
    "stock price of apple",
    "solve x squared plus 5x equals 6",
    "meaning of life",
    "favorite movie recommendation",
    "how to train a puppy",
    "recipe for chocolate chip cookies",
    "translate hello to french",
    "what is machine learning",
    "how to meditate",
    "soccer world cup champions",
    "stock market prediction",
    "best restaurants in tokyo",
    "when did rome fall",
    "history of jazz music",
    "how to write a cover letter",
    "tallest mountain in europe",
    "difference between react and vue",
    "how to change car oil",
    "recipe for pad thai",
    "what are dark matter theories",
    "how to start a garden",
    "best hiking trails in colorado",
    "who wrote hamlet",
    "largest ocean",
    "how to knit a scarf",
    "explain blockchain",
    "top programming languages 2026",
    "history of the internet",
    "best books about space",
    "how to make sourdough bread",
    "what does npm install do",
    "ancient egyptian pyramids purpose",
    "how airplanes fly",
    "constitutional amendment rights",
    "how is coffee decaffeinated",
    "meaning of phrase break a leg",
]


def build_ood_rows():
    return [
        {
            "question": q,
            "expected_answer": "",
            "expected_keyword": "",
            "question_style": "ood",
            "question_type": "ood_negative",
            "source_chunk_id": None,
        }
        for q in OOD_QUERIES
    ]


def main():
    import anthropic
    client = anthropic.Anthropic()

    existing = load_jsonl(SRC)
    print(f"Existing queries: {len(existing)}")

    # Category 1: Conversational paraphrases of 50 existing queries
    random.seed(17)
    sample_50 = random.sample(existing, 50)
    print("Generating 50 conversational paraphrases...")
    conversational = conversational_paraphrase(sample_50, client)
    print(f"  → {len(conversational)}")

    # Category 2: Deterministic misspellings of existing queries
    random.seed(23)
    misspelled = []
    for q in existing:
        tq = add_typo(q["question"])
        if tq:
            misspelled.append({
                **q,
                "question": tq,
                "question_style": "misspelling",
                "derived_from_chunk_id": q.get("source_chunk_id"),
            })
        if len(misspelled) >= 30:
            break
    print(f"Generated {len(misspelled)} misspelling variants")

    # Category 3: Abbreviations / clinical shorthand
    abbrev = build_abbrev_rows()
    print(f"Curated {len(abbrev)} abbreviation queries (ground truth: keyword)")

    # Category 4: OOD negatives
    ood = build_ood_rows()
    print(f"Curated {len(ood)} OOD negative queries")

    # Keep the original 100 but tag them
    originals = [{**q, "question_style": q.get("question_style", "original")} for q in existing]

    all_rows = originals + conversational + misspelled + abbrev + ood
    print(f"\nTotal expanded eval: {len(all_rows)} queries")
    print(f"  original:       {len(originals)}")
    print(f"  conversational: {len(conversational)}")
    print(f"  misspelling:    {len(misspelled)}")
    print(f"  abbreviation/shorthand: {len(abbrev)}")
    print(f"  ood_negative:   {len(ood)}")

    write_jsonl(OUT, all_rows)
    print(f"\nWrote: {OUT}")


if __name__ == "__main__":
    main()
