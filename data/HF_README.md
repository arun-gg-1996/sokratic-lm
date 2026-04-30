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

- Generated: 2026-04-30T09:17:49+00:00
- Git commit: `3e0d73dace6b545ed2bea8872a03fde4c417d076`
- Tag: `v1-chunks-only`

## Files

| Path | Size | SHA-256 (first 12) |
|------|------|---------------------|
| `data/processed/chunks_openstax_anatomy.jsonl` | 10.8 MB | `5ec279d91de8` |
| `data/indexes/bm25_chunks_openstax_anatomy.pkl` | 16.8 MB | `3d236a56dd2b` |
| `data/textbook_structure.json` | 0.1 MB | `bfd9b1df4d9f` |
| `data/topic_index.json` | 0.1 MB | `14342238e3ce` |
| `data/indexes/qdrant_sokratic_kb_chunks.snapshot` | 360.6 MB | `0101ef9f8185` |
