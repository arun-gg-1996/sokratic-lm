# Haiku classifier validation — 2026-05-01T21-02-26

## hint_leak

- Accuracy: **29/30** (96.7%)
- Latency p50: 1.62s, p95: 2.35s

### Per-class precision / recall

| class | TP | FP | FN | precision | recall |
|---|---|---|---|---|---|
| leak | 17 | 0 | 1 | 100.00% | 94.44% |
| clean | 12 | 1 | 0 | 92.31% | 100.00% |

### Misses

| label | expected | got | rationale |
|---|---|---|---|
| synonym_using_everyday_language | leak | clean | The draft asks the student to explain contraction in everyday language, which is |

---
