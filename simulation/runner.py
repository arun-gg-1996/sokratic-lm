"""
simulation/runner.py
---------------------
Async simulation runner. Runs 1,500 conversations in parallel batches.

Distribution:
  6 profiles × ~15 anatomy topics × ~17 convos per combo = ~1,500 total

Each conversation:
  - Instantiates the LangGraph graph
  - Drives student turns using student_simulator.py
  - Logs the full conversation to data/simulations/ via logger.py
  - Dean + Teacher are both active (cfg.simulation.with_dean = True)

Parallelism: asyncio with max cfg.simulation.max_concurrent concurrent convos.

Resume support: skip conv_ids that already exist in the output directory.

Usage:
    python -m simulation.runner
    python -m simulation.runner --profile S1 --n 50   # smoke test
"""

import asyncio
from config import cfg


# Anatomy topics to simulate across (drawn from textbook_structure.json)
# These are populated at runtime from the structure file.
TOPICS: list[str] = []  # TODO: load from textbook_structure.json at startup


async def run_conversation(
    profile_id: str,
    topic: str,
    graph,
    semaphore: asyncio.Semaphore,
) -> dict:
    """
    Run a single simulated conversation asynchronously.

    Args:
        profile_id: One of S1-S6
        topic:      Full topic path e.g. "Chapter 11 > Deltoid Innervation"
        graph:      Compiled LangGraph runnable
        semaphore:  Limits concurrent API calls

    Returns:
        Conversation result dict (passed to logger).
    """
    async with semaphore:
        # TODO: initialize TutorState with profile_id and topic
        # TODO: loop: get student response from student_simulator, invoke graph, check phase
        # TODO: stop when phase == "memory_update" or turn_count > max_turns
        # TODO: return full conversation dict
        raise NotImplementedError


async def run_all(n: int = None, profile_id: str = None):
    """
    Run the full simulation batch.

    Args:
        n:          Override number of conversations (default: cfg.simulation.n_conversations)
        profile_id: Run only this profile (default: all 6)
    """
    # TODO: load TOPICS from textbook_structure.json
    # TODO: build (profile_id, topic) pairs for target n
    # TODO: build graph once (shared across all convos)
    # TODO: create semaphore(cfg.simulation.max_concurrent)
    # TODO: gather all run_conversation coroutines
    # TODO: pass results to logger.log_conversation
    raise NotImplementedError


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", type=str, default=None)
    parser.add_argument("--n", type=int, default=None)
    args = parser.parse_args()
    asyncio.run(run_all(n=args.n, profile_id=args.profile))
