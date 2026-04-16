"""
evaluation/euler.py
--------------------
Offline EULER evaluation. Run on saved conversation logs, NOT live.

Scores each tutor response on 4 criteria (0.0 to 1.0 each) using GPT-4o as judge:
  1. question_present  — did the response contain at least one question?
  2. relevance         — is it relevant to the student's last message?
  3. helpful           — does it advance understanding without giving the answer?
  4. no_reveal         — is the locked answer absent from the response?

Target: average EULER score > 0.75 across a full conversation.

Results saved to data/artifacts/euler_scores/{run_id}.json.

Usage:
    python -m evaluation.euler --conv_id <uuid>
    python -m evaluation.euler --all      # score all saved conversations
"""

from config import cfg


def score_turn(
    tutor_response: str,
    student_message: str,
    locked_answer: str,
) -> dict:
    """
    Score a single tutor response on all 4 EULER criteria.

    Args:
        tutor_response:  The tutor's response text.
        student_message: The student's preceding message.
        locked_answer:   The correct answer for this topic (used for no_reveal check).

    Returns:
        {
          "question_present": float,
          "relevance": float,
          "helpful": float,
          "no_reveal": float,
          "average": float
        }
    """
    # TODO: build GPT-4o judge prompt with all 4 criteria
    # TODO: call GPT-4o and parse scores
    # TODO: compute average
    raise NotImplementedError


def score_conversation(conv_path: str) -> dict:
    """
    Load a saved conversation from data/artifacts/conversations/ and score every turn.

    Args:
        conv_path: Path to the conversation JSON file.

    Returns:
        {
          "conv_id": str,
          "per_turn_scores": list[dict],
          "conversation_average": float
        }
    """
    # TODO: load conversation JSON
    # TODO: for each tutor turn, call score_turn
    # TODO: aggregate and save to data/artifacts/euler_scores/
    raise NotImplementedError


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--conv_id", type=str, help="Score a single conversation by ID")
    parser.add_argument("--all", action="store_true", help="Score all saved conversations")
    args = parser.parse_args()

    if args.conv_id:
        result = score_conversation(f"{cfg.paths.artifacts}/conversations/{args.conv_id}.json")
        print(f"Average EULER score: {result['conversation_average']:.3f}")
    elif args.all:
        # TODO: glob all conversation JSONs and score each
        pass
