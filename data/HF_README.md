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

- Generated: 2026-04-22T20:34:09+00:00
- Git commit: `d1e43f9a18f06ec1bedfb4baa5c237d21ed69a51`
- Tag: `v0-messy-metadata`

## Files

| Path | Size | SHA-256 (first 12) |
|------|------|---------------------|
| `data/processed/propositions_ot.jsonl` | 53.1 MB | `bdea4f9050f4` |
| `data/processed/chunks_ot.jsonl` | 6.4 MB | `9da5430d2f03` |
| `data/processed/raw_elements_ot.jsonl` | 3.8 MB | `bc02fb4d7c46` |
| `data/processed/raw_sections_ot.jsonl` | 2.5 MB | `235d7fad6c86` |
| `data/indexes/bm25_ot.pkl` | 56.9 MB | `323d685b6dc6` |
| `data/textbook_structure.json` | 0.1 MB | `bfd9b1df4d9f` |
| `data/topic_index.json` | 0.1 MB | `14342238e3ce` |
