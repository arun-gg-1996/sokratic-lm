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

- Generated: 2026-05-08T21:50:29+00:00
- Git commit: `969e006c94e47e7351b2f53f7a562aa2dda503cc`

## Files

| Path | Size | SHA-256 (first 12) |
|------|------|---------------------|
| `data/processed/chunks_openstax_anatomy.jsonl` | 10.8 MB | `5ec279d91de8` |
| `data/indexes/bm25_chunks_openstax_anatomy.pkl` | 16.8 MB | `3d236a56dd2b` |
| `data/textbook_structure.json` | 0.1 MB | `bfd9b1df4d9f` |
| `data/topic_index.json` | 0.1 MB | `14342238e3ce` |
| `data/artifacts/raptor_subsection_summaries.jsonl` | 1.5 MB | `5d1f86aaa28f` |
| `data/artifacts/raptor_section_summaries.jsonl` | 0.3 MB | `888f4b28651f` |
| `data/artifacts/sokratic_seed_openstax_anatomy.sql` | 0.8 MB | `deb3a41d6b1f` |
| `data/indexes/qdrant_sokratic_kb_chunks.snapshot` | 140.1 MB | `41cdd2b615c5` |
