# Haiku classifier validation — 2026-05-01T21-03-51

## hint_leak

- Accuracy: **29/30** (96.7%)
- Latency p50: 1.62s, p95: 2.29s

### Per-class precision / recall

| class | TP | FP | FN | precision | recall |
|---|---|---|---|---|---|
| clean | 12 | 1 | 0 | 92.31% | 100.00% |
| leak | 17 | 0 | 1 | 100.00% | 94.44% |

### Misses

| label | expected | got | rationale |
|---|---|---|---|
| synonym_using_everyday_language | leak | clean | The draft asks the student to explain contraction in everyday language, which is |

---

## sycophancy

- Accuracy: **27/27** (100.0%)
- Latency p50: 1.53s, p95: 1.83s

### Per-class precision / recall

| class | TP | FP | FN | precision | recall |
|---|---|---|---|---|---|
| sycophantic | 15 | 0 | 0 | 100.00% | 100.00% |
| clean | 12 | 0 | 0 | 100.00% | 100.00% |

### Misses

_(none — all cases correct)_

---

## off_domain

- Accuracy: **27/27** (100.0%)
- Latency p50: 1.28s, p95: 1.53s

### Per-class precision / recall

| class | TP | FP | FN | precision | recall |
|---|---|---|---|---|---|
| clean | 12 | 0 | 0 | 100.00% | 100.00% |
| off_domain | 15 | 0 | 0 | 100.00% | 100.00% |

### Misses

_(none — all cases correct)_

---
