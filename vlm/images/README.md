# VLM test image set

Curated for the L77 image-driven topic entry demo. Eight images sorted
by expected behavior on the OpenStax Anatomy corpus + L9 topic mapper.

Naming convention: `<NN>_[HERO_]<bucket>_<topic>_<flag>.<ext>`

  - `<NN>`     — stable ordinal (01-08)
  - `HERO`     — flagged for the demo video (3 of 8)
  - `<bucket>` — strong / medium / weak  (predicted lock outcome)
  - `<topic>`  — short topic phrase
  - `<flag>`   — optional: `DUTCH` / `LATIN` if labels are non-English

---

## strong/ — clean topic-locks for the happy-path demo

Both should produce `route_decision = lock_immediately` from the VLM
endpoint, then a strong-verdict lock through L9.

| File | What it shows | Expected lock |
|---|---|---|
| `01_strong_inhalation_breathing.jpg` | Inhalation diagram (lungs expanding, ribcage up + out, diaphragm down) | **Process of Breathing** (32+ chunks) |
| `02_strong_heart_atrial_septal_defects_DUTCH.jpg` | Atrial septal defects: sinus venosus, ostium primum, ostium secundum, sinus coronarius (Dutch labels) | **Heart: Heart Defects** (44 chunks) |

## medium/ — plausible locks, may surface borderline cards (L10 confirm-and-lock)

These should produce `route_decision = show_top_matches` or
`lock_immediately` with confidence 0.7-0.85. Useful for demoing the
confirm-and-lock UX.

| File | What it shows | Likely lock |
|---|---|---|
| `03_medium_median_nerve_hand_DUTCH.jpg` | Median nerve cutaneous innervation + carpal tunnel anatomy | Brachial plexus / Spinal Nerves subsection |
| `04_medium_nociceptive_pathways_pelvis.png` | Pain pathways via sympathetic, parasympathetic, somatic nerves | Sympathetic Division of the Autonomic Nervous System |

## weak/ — likely refuse or wrong-topic — useful for testing fallbacks

These exercise the L77 `route_decision = refuse` path or the L9 `none`
verdict → starter-cards flow. Good for showing the system gracefully
handles content the corpus doesn't cover.

| File | What it shows | Why weak |
|---|---|---|
| `05_weak_pelvic_vessels_obturator_LATIN.jpg` | Pelvic vasculature + obturator nerve (Latin labels) | OpenStax doesn't have a dedicated pelvic-vasculature subsection at this granularity |
| `06_weak_auscultation_valves_simple.png` | Heart valve auscultation points overlaid on rib cage | Clinical exam technique, not core anatomy |
| `07_weak_auscultation_thorax_overlay.png` | Same content as 06 with thorax overlay | Same |
| `08_weak_female_pelvis_cystocele.jpg` | Female pelvis with cystocele (pathology) | Pathology, not in core anatomy corpus |

---

## Demo recommendation

For the demo video, lead with:

1. **#1 (inhalation)** — clean strong-lock → image-grounded first turn:
   *"Looking at your image, what happens to the diaphragm during inhalation?"*
2. **#2 (heart defects)** — strong-lock with Dutch labels (proves
   multilingual VLM extraction):
   *"Your image shows four atrial defects — name the one between the
   sinus venosus and the right atrium."*
3. **#6 (auscultation)** — graceful refuse / starter-cards path
   (proves L77's fallback works when content is out of scope):
   *"I couldn't map this image to a textbook subsection. Here are some
   anatomy topics we cover…"*

That's 3 distinct flows — happy path, multilingual happy path,
graceful fallback — covering the L77 contract end to end.
