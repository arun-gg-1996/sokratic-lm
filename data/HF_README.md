---
license: cc-by-4.0
tags:
- rag
- tutoring
- anatomy
---

# arun-ghontale/sokratic-anatomy-corpus

Processed corpus + retrieval indexes for the Sokratic AI Tutor thesis project.
Contents are regenerated artifacts from the OpenStax *Anatomy & Physiology 2e* textbook.
The source PDF is **not** included (grab it from OpenStax).

## Bootstrapping

```bash
python scripts/bootstrap_corpus.py
```

See `SETUP.md` in the code repo for the full flow.

- Generated: 2026-05-09T04:16:54+00:00
- Git commit: `067bdecba637d579139f94c1f0319e88cd1d025c`

## Files

| Path | Size | SHA-256 (first 12) |
|------|------|---------------------|
| `data/processed/chunks_openstax_anatomy.jsonl` | 10.8 MB | `cf26a0ddfc69` |
| `data/indexes/bm25_chunks_openstax_anatomy.pkl` | 14.8 MB | `918087a4450d` |
| `data/textbook_structure.json` | 0.1 MB | `bfd9b1df4d9f` |
| `data/topic_index.json` | 0.1 MB | `14342238e3ce` |
| `data/artifacts/raptor_subsection_summaries.jsonl` | 1.5 MB | `5d1f86aaa28f` |
| `data/artifacts/raptor_section_summaries.jsonl` | 0.3 MB | `888f4b28651f` |
| `data/artifacts/sokratic_seed_openstax_anatomy.sql` | 0.8 MB | `deb3a41d6b1f` |
| `data/curated_abbrevs_ot.json` | 0.0 MB | `a0aed54a76c5` |
| `data/processed/chunks_openstax_physics.jsonl` | 5.3 MB | `0b37dfe437b8` |
| `data/processed/propositions_openstax_physics.jsonl` | 29.1 MB | `5520f2fb7ab0` |
| `data/indexes/bm25_chunks_openstax_physics.pkl` | 7.1 MB | `41a98dbc2e9b` |
| `data/textbook_structure_openstax_physics.json` | 0.0 MB | `4888ec7e2087` |
| `data/topic_index_openstax_physics.json` | 0.1 MB | `2fbecf9e7d3b` |
| `data/artifacts/raptor_subsection_summaries_openstax_physics.jsonl` | 0.7 MB | `baa935d6837a` |
| `data/artifacts/raptor_section_summaries_openstax_physics.jsonl` | 0.2 MB | `6dc48f999475` |
| `data/curated_abbrevs_openstax_physics.json` | 0.0 MB | `fa2196f6c4c4` |
| `data/indexes/qdrant_sokratic_kb_chunks.snapshot` | 140.1 MB | `41cdd2b615c5` |
| `data/indexes/qdrant_sokratic_physics_kb.snapshot` | 295.3 MB | `74399ef97153` |
