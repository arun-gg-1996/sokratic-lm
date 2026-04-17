# Progress Journal — 2026-04-16_18-25-18 (Parse-PDF Architecture Shift)

**Author:** Nidhi Rajani  
**Session:** Extraction foundation switched to `parse_pdf.py` + section-first semantic chunking with overlap

---

## What changed

### 1) Extraction foundation switched
- Copied teammate parser into project:
  - `ingestion/parse_pdf.py`
- Replaced `ingestion/extract.py` to use `parse_pdf.parse_pdf(...)` as source of truth.
- `extract.py` now maps parse sections to ChunkSchema-compatible seeds and writes:
  - `data/processed/raw_sections_ot.jsonl`

### 2) Section stats (Step 2)
Run: `python -m ingestion.extract`
- Total sections found: **574** (L1=120, L2=454)
- Chapters covered: **28**
- Total words across sections: **372,865**
- Sections containing "axillary nerve": **2**
- Sections containing "deltoid": **7**

### 3) Section->ChunkSchema seed mapping (Step 3)
Mapped fields:
- `chunk_id = uuid4()`
- `text = section[text]`
- `chapter_num = section[chapter_num]`
- `chapter_title = section[chapter]`
- `section_num = section[section_num]`
- `section_title = section[section_title]`
- `subsection_title = section[parent_section] if level==2 else ""`
- `page = section[page_start]`
- `element_type = "paragraph"`
- `domain = "ot"`

Saved to: `data/processed/raw_sections_ot.jsonl`

### 4) New section-first chunking pipeline (Step 4 + Step 5)
Rewrote `ingestion/chunk.py`:
- semantic split **within each section independently** (`threshold=75`)
- token budget enforcement (`MAX_CHUNK_TOKENS=440`)
- sentence overlap chunks added across adjacent chunks in same chapter:
  - `element_type = paragraph_overlap`
  - `original_chunk_id` link to base chunk
  - `overlap_source_chunk_id`
  - `overlap_prefix`
- no cross-chapter overlap
- added postprocess pronoun-boundary merge for base chunks

### 5) Schema update
Updated `schemas.py`:
- `element_type` now supports `paragraph_overlap` in chunk/proposition literals
- validation allows `paragraph_overlap`

---

## Final validation results (All 9 tests)

Using current `data/processed/chunks_ot.jsonl`:
- Total chunks: **7,742**
- Overlap chunks: **3,857**

### Test summary
1. Token distribution: **PASS**
   - mean 152.64, p50 126, p90 305, p95 384, p99 440, max 442
   - `<15 tokens=0`, `>450=0`
2. Topic coherence (10 random): **PASS**
   - GOOD 9/10, BAD 1/10
3. Pronoun-start chunks: **PASS**
   - 43 / 7,742 = **0.56%**
4. Noise detection: **PASS**
   - noise chunks = 0
5. Chapter coverage: **PASS**
   - all 1..28 present
6. Critical content check: **PASS**
   - axillary nerve=4 (>3)
   - deltoid=24 (>5)
   - rotator cuff=14 (>2)
   - biceps=33 (>3)
7. Proposition preview (3 chunk types): **PASS**
   - generated for muscle, overlap, clinical chunk
8. Overlap chunk check: **PASS**
   - overlap_count=3857
   - valid_original_chunk_id=3857/3857
   - overlap_prefix_in_source=3857/3857
9. Section-level check: **PASS**
   - L1=120, L2=454
   - axillary nerve chunks=4
   - section titles containing axillary-nerve chunks: `13.4 The Peripheral Nervous System`, `A`, `B`

---

## Output artifacts
- `data/processed/raw_sections_ot.jsonl`
- `data/processed/chunks_ot.jsonl`
- `data/processed/chunks/raw_sections/all_sections_OT_anatomy.json`
- `data/processed/chunks/raw_sections/ch01_OT_anatomy.json` ... `ch28_OT_anatomy.json`

---

## Gate status
- All 9 requested tests: **PASS**
- `axillary nerve > 3`: **PASS (4)**
- `propositions.py`: **NOT RUN** (awaiting explicit approval)
