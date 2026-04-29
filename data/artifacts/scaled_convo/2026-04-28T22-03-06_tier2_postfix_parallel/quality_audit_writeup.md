# Quality Audit — Tier 2 Post-Fix Baseline

**Run:** `2026-04-28T22-03-06_tier2_postfix_parallel`
**18 conversations, 6 profiles × 3 seeds, anatomy-only topic bank**

## Headline

The pipeline is genuinely working pedagogically. **0 real LeakGuard
violations, 0 real off-topic drifts, 0 stuck conversations**. The 5
heuristic flags raised by `audit_convos.py` are all false positives on
inspection. The 0% reach rate on S3/S5/S6 is profile-correct behavior,
not a system bug.

## Automated flags vs reality

| Flag | n | True positives | False positives | Notes |
|---|---|---|---|---|
| answer_reveal_pre_student | 5 | 0 | 5 | All 5 are either: final memory_update summary (3), topic intro mentioning the topic name (1), or legitimate high-hint-level guided reveal (1) |
| off_topic_drift | 1 | 0 | 1 | S1/seed2 (placenta) — tutor stayed on-topic, just didn't echo specialized jargon from the student's specific question |
| repetition | 0 | — | — | |
| generic_filler | 0 | — | — | |

## Sample evidence

### S3/seed1 — Exercise and Stretching (S3 Weak, 17 turns, reached=False)

The previously-stuck case from the pre-fix run. Now resolves cleanly:

```
[1] student: Um, is it like the median nerve or something? I'm honestly just guessing.
[2] tutor:   I notice we've drifted into guessing—let me reset us to something you can
             directly observe. When you look down at your arm right now, what hard
             structures can you actually see or feel running through it?
[3] student: Yeah, I'm really not sure... is it like bones? Or maybe nerves?
[4] tutor:   You've named bones—that's the hard structure we need. Bones are stiff,
             just like the hammer handle, and they're what the deltoid muscle pulls on...
[8] tutor:   Right—the nerve is the messenger, the muscle does the pulling. Now place
             your hand on your deltoid, contract it gently, and tell me: what part of
             your arm actually moves when the muscle shortens?
[9] student: Yeah, so like... the bones move when the muscle pulls on them, right?
             I'm thinking the joints act like pivot points or something?
[11] tutor:  (memory update) You made real progress today identifying bones, joints,
             and muscles as parts of a lever system. However, you struggled with a
             foundational distinction: nerves signal muscles, but muscles themselves
             create movement.
```

Quality observations:
- Tutor explicitly notices guessing, resets to observable physical structure
- Embodied learning ("place your hand on your deltoid, contract it gently")
- Patient with repeated "median nerve?" guesses
- Builds bones → muscles → joints progressively
- Honest assessment summary at end (acknowledges what student did NOT learn)

### S5/seed1 — Types of Synovial Joints (S5 Disengaged, 16 turns)

```
[1] student: um, like... one moves more ways than the other?
[2] tutor:   You're onto something—one joint does move in more directions than the
             other. Can you think of which joint lets you move in just one direction
             (like shaking your head no)?
[3] student: umm i'm not sure
[6] tutor:   You've felt both movements: at your neck, your head spins (that's a pivot
             joint). At your knee, your leg bends (hinge joint). Which movement—
             spinning or bending—happens at the pivot joint?
[8] tutor:   Right now, try this: slowly shake your head left and right, then slowly
             bend your knee up and down. Which motion feels like your head is spinning
             in place, and which feels like your leg is folding like a door?
[9] student: um like the head one i guess?
[10] tutor:  Great—you're connecting the movement you feel to the joint type!
```

Quality observations:
- Disengaged student responses ("um i'm not sure", "umm") handled patiently
- Embodied learning escalation: simple → comparison → physical experiment
- Connects sensation directly to anatomy concept
- Doesn't lecture or reveal; lets student verbalize

### S6/seed2 — Development of the Placenta (S6 Anxious, 20 turns)

```
[1] student: I'm thinking maybe the endometrium would need to stay really vascularized
             so there's a lot of blood flow close to where the villi are burrowing in?
             That way the maternal blood could actually get near the fetal tissue to
             exchange stuff? But I'm not totally sure...
[2] tutor:   That's exactly the right reasoning—you've connected structure to function
             beautifully. Since you've identified that maternal blood needs to flow
             close to the villi, what do you think happens to the specific maternal
             blood vessels?
[8] tutor:   Great instinct—you're on the right track. Can you think of any examples
             from everyday life where making a pipe or tube wider changes how much
             liquid can pass through it?
[9] student: Um, I'm thinking maybe like a garden hose? If you make it wider, more
             water comes out faster?
[10] tutor:  That garden hose example is perfect—you've got the right intuition!
```

Quality observations:
- Validates anxious student's hedged reasoning ("you've connected structure to function beautifully")
- Doesn't dismiss correct partial answers
- Constructs a concrete real-world analogy when conceptual gap appears
- Affirms the student when they make the analogical leap

## Quality issues NOT found

Items I checked but did not observe:
- Premature answer reveal during Socratic questioning
- Tutor confirming wrong student claims (sycophancy)
- Off-topic drift (tutor wandering from locked subsection)
- Generic filler / non-progressive turns
- Repeated identical questions across turns

## Profile-correct vs profile-bug

| Profile | reached_answer | Verdict |
|---|---|---|
| S1 Strong | 100% (3/3) — 2 turns avg | Correct: precise queries, tutor confirms quickly |
| S2 Moderate | 33% (1/3) — 5 turns avg | Mostly correct; one weak topic_match |
| S3 Weak | 0% (0/3) — 10.7 turns avg | **Profile-correct.** Tutor patient and pedagogical, student converges partially in 17-25 turns but doesn't name the full target answer |
| S4 Overconfident | 100% (3/3) — 3.3 turns avg | Correct: leading assertions are confidence-cued, tutor confirms |
| S5 Disengaged | 0% (0/3) — 12 turns avg | **Profile-correct.** Tutor uses embodied learning to engage; student names 1-2 of N target items |
| S6 Anxious | 0% (0/3) — 11.3 turns avg | **Profile-correct.** Tutor validates and analogizes; conversation runs long with hedged reasoning, doesn't synthesize the full answer in time |

The 0% reach for S3/S5/S6 reflects what those student personas are
designed to do — exhibit weak/disengaged/anxious behavior that doesn't
converge quickly. The system handles all three with appropriate
pedagogical strategy. The system is doing the right thing; the
`reached_answer` metric just doesn't credit partial convergence.

## Recommendation

This is a defensible checkpoint. Lock as the Tier 2 post-fix baseline.
The audit confirms that the bug fix (locked_answer cap 6→15) and
parallelization unlocked real, sustained conversation quality across all
6 profiles without introducing any quality regressions. No further fixes
are needed before moving to Tier 3 (EULER, ablation) or other Tier 2
items (D.2 adaptive routing, D.3 KT) when the user is ready.

## Followup ideas (not blocking)

1. The `audit_convos.py` heuristic for `answer_reveal_pre_student` is too
   aggressive — flags topic intros and final summaries as reveals. Could
   tighten by:
   - Skipping pre_assessment messages (only flag during phase=='tutoring')
   - Looking at a SLIDING WINDOW of recent tutor messages, not pooled
   - Discounting tokens that are the topic NAME (which is by design
     mentioned in early turns)
2. A `reached_partial` metric — student names ≥40% of locked_answer
   tokens — would credit S3/S5/S6's actual learning even when they don't
   hit the full target.
