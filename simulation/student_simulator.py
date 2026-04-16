"""
simulation/student_simulator.py
--------------------------------
Generates student responses for simulation using a StudentProfile.

For each turn, given the current state (topic, hint_level, last tutor message),
it calls the profile's response_strategy to produce a realistic student reply.

The simulator uses GPT-4o to make responses more natural — the profile strategy
defines the *behavior* (correct/wrong/vague), and GPT-4o adds realistic
anatomy student language around it.
"""

from simulation.profiles import StudentProfile, PROFILES
from conversation.state import TutorState


class StudentSimulator:
    def __init__(self, profile: StudentProfile):
        self.profile = profile
        # TODO: initialize OpenAI client for naturalizing responses

    def respond(self, state: TutorState) -> str:
        """
        Generate a student response for the current turn.

        Args:
            state: Current TutorState (used for topic, hint_level, last tutor message)

        Returns:
            A realistic student response string.
        """
        # TODO: call self.profile.response_strategy(topic, hint_level, target_answer)
        #       Note: student_simulator does NOT have access to locked_answer.
        #       Use a placeholder based on correct_answer_prob to decide if student "gets it".
        # TODO: use GPT-4o to rephrase into natural student language matching engagement_level
        raise NotImplementedError
