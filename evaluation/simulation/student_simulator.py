"""
simulation/student_simulator.py
--------------------------------
Generates student responses for simulation using a StudentProfile.

Two-step process:
  1. Profile strategy decides the BEHAVIOR (correct/wrong/vague) based on
     correct_answer_prob and hint_level.
  2. Claude naturalizes the raw behavior string into realistic student language
     matching the profile's engagement_level.

Note: StudentSimulator HAS access to locked_answer (unlike Teacher).
      It uses profile.correct_answer_prob[hint_level - 1] to probabilistically
      decide if the student "gets it right" this turn, then passes the appropriate
      strategy output to Claude for naturalization.

Uses cfg.models.teacher (claude-sonnet-4-5) for naturalization.
"""

import random
import anthropic
from evaluation.simulation.profiles import StudentProfile, PROFILES
from conversation.state import TutorState
from config import cfg


class StudentSimulator:
    def __init__(self, profile: StudentProfile):
        """
        Args:
            profile: StudentProfile instance (from simulation/profiles.py)
        """
        self.profile = profile
        self.client = anthropic.Anthropic()
        self.model = cfg.models.teacher  # reuse same model

    def respond(self, state: TutorState) -> str:
        """
        Generate a student response for the current turn.

        Args:
            state: Current TutorState.
                   Uses: hint_level, locked_answer, messages (for last tutor message),
                         pending_user_choice (mimic UI button clicks)

        Returns:
            A realistic student response string matching this profile's behavior.

        Flow:
          0. If state.pending_user_choice is set, mimic UI buttons: pick the
             expected option (yes for opt_in/confirm_topic; first option for
             topic cards). Real users click these buttons; the simulator
             mirrors that so the conversation doesn't drift back into
             topic-mapper hell when it's actually mid-confirmation.
          1. Get hint_level from state (clamp to 1-3)
          2. Determine if student "should" answer correctly this turn:
               roll = random.random()
               correct = roll < profile.correct_answer_prob[hint_level - 1]
          3. If correct: target = state["locked_answer"]
             If not correct and error_patterns exist: target = random.choice(error_patterns)
             If not correct and no error_patterns: target = ""
          4. raw = profile.response_strategy(topic, hint_level, target)
          5. Naturalize raw with Claude
          6. Return naturalized response
        """
        # L73 + Track 4.7d UX: mimic UI button clicks when a pending choice
        # is open. Without this, the simulator typed substantive answers
        # into the opt-in / confirm-topic prompts, which the v2 flow
        # interpreted as topic-switch attempts and re-fired the L9 mapper.
        # That manifested as the "wrong-topic-lock + opt-in infinite loop"
        # bug observed during the 2026-05-03 sanity check.
        pending = state.get("pending_user_choice") or {}
        if isinstance(pending, dict):
            kind = pending.get("kind", "")
            options = pending.get("options") or []
            if kind in {"opt_in", "confirm_topic"} and options:
                # Disengaged students decline; everyone else takes the
                # bonus/confirms. Mimics how the buttons would actually
                # be clicked by these personas.
                if self.profile.engagement_level < 0.3:
                    # Disengaged → decline
                    return "No"
                return "Yes"
            if kind == "topic" and options:
                # Pick the first card — same default as the e2e harness
                end_value = pending.get("end_session_value")
                # Skip the "Give up / End session" sentinel even if present
                for opt in options:
                    if end_value and opt.lower() in {end_value.lower(), "give up", "end session"}:
                        continue
                    return str(opt)
                return str(options[0])

        hint_level = max(1, min(3, state.get("hint_level", 1)))

        # Get topic from first student message or locked_answer context
        topic = ""
        for msg in state.get("messages", []):
            if msg.get("role") == "student":
                topic = msg.get("content", "")[:80]
                break

        locked_answer = state.get("locked_answer", "")

        # Probabilistically decide if student answers correctly
        prob = self.profile.correct_answer_prob[hint_level - 1]
        correct = random.random() < prob

        if correct:
            target = locked_answer
        elif self.profile.error_patterns:
            target = random.choice(self.profile.error_patterns)
        else:
            target = ""

        # Get raw behavior string from profile strategy
        raw = self.profile.response_strategy(topic, hint_level, target)

        # Get last tutor message for naturalization context
        last_tutor_msg = ""
        for msg in reversed(state.get("messages", [])):
            if msg.get("role") == "tutor":
                last_tutor_msg = msg.get("content", "")
                break

        return self._naturalize(raw, last_tutor_msg, self.profile.engagement_level)

    def _naturalize(self, raw_behavior: str, last_tutor_msg: str, engagement_level: float) -> str:
        """
        Use Claude to rephrase raw strategy output into natural student language.

        Args:
            raw_behavior:    Output from profile.response_strategy()
            last_tutor_msg:  Last message from the tutor (for context)
            engagement_level: 0.0-1.0 — low = short/vague, high = detailed/engaged

        Returns:
            Natural student response string.
        """
        if engagement_level < 0.3:
            style = "very short, vague, and minimal (1 short sentence or less)"
        elif engagement_level < 0.6:
            style = "brief and casual (1-2 sentences)"
        elif engagement_level < 0.8:
            style = "moderate detail, somewhat engaged (2-3 sentences)"
        else:
            style = "detailed and engaged (2-4 sentences)"

        system = (
            f"You are a student in a tutoring session. "
            f"Write a response that is {style}. "
            f"Be realistic and natural — this is a real student responding to a tutor. "
            f"Do not add any meta-commentary. Just write the student's response directly."
        )

        user_msg = (
            f"The tutor said: {last_tutor_msg}\n\n"
            f"Your intended response behavior is: {raw_behavior}\n\n"
            f"Rephrase this as a natural student message matching the style described."
        )

        resp = self.client.messages.create(
            model=self.model,
            max_tokens=150,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )

        return resp.content[0].text.strip()
