"""
conversation/teacher.py
------------------------
Teacher agent — generates Socratic responses for the student.

Teacher receives from Dean:
  - retrieved_chunks  (raw textbook passages, answer is somewhere in them)
  - conversation history
  - hint_level and max_hints
  - dean_critique (empty string on first attempt, populated on retry)

Teacher does NOT have any tools and does NOT know the locked answer.
Teacher's only job: ask one question that guides the student toward
the concepts in the retrieved chunks, at the appropriate hint level.

Uses GPT-4o with the 'teacher_system' prompt from config.yaml.
The full assembled prompt is logged to data/artifacts/session_prompts/.
"""

from conversation.state import TutorState
from config import cfg


class TeacherAgent:
    def __init__(self):
        # TODO: initialize OpenAI client (no tools)
        raise NotImplementedError

    def draft_response(self, state: TutorState) -> str:
        """
        Generate a Socratic response given the current state.

        Called by Dean after it has set retrieved_chunks, hint_level,
        and optionally dean_critique in state.

        Returns:
            A draft response string (not yet approved by Dean).
        """
        # TODO: build prompt from cfg.prompts.teacher_system
        # TODO: call GPT-4o (no tools)
        # TODO: log prompt to artifacts
        raise NotImplementedError

    def get_full_prompt(self, state: TutorState) -> str:
        """
        Return the full assembled Teacher prompt with === STATIC === / === DYNAMIC === delimiters.
        """
        # TODO: assemble and return
        raise NotImplementedError
