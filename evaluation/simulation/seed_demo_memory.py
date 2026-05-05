"""
simulation/seed_demo_memory.py
-------------------------------
Seeds mem0 with realistic student history for demo purposes.

Instead of hardcoding fake data, this script runs 5 real simulated
conversations per profile through the actual system. The outcomes
(weak topics, mastered topics, failure counts) get stored in mem0
naturally via the memory_update_node.

Run this once before the demo to give each student profile a realistic
session history that the Rapport phase can surface.

Usage:
    python -m simulation.seed_demo_memory
"""

from simulation.profiles import PROFILES
from config import cfg


SEED_TOPICS = [
    "Chapter 11 > Muscle Tissue > Deltoid Innervation",
    "Chapter 11 > Muscle Tissue > Rotator Cuff Muscles",
    "Chapter 13 > Peripheral Nervous System > Brachial Plexus",
    "Chapter 11 > Muscle Tissue > Biceps Brachii",
    "Chapter 13 > Peripheral Nervous System > Axillary Nerve",
]


def seed_profile(profile_id: str, graph, memory_manager) -> None:
    """
    Run 5 seeding conversations for a single profile.

    Args:
        profile_id:     One of S1-S6
        graph:          Compiled LangGraph runnable
        memory_manager: MemoryManager instance
    """
    # TODO: for each topic in SEED_TOPICS:
    #   - run a full simulated conversation using StudentSimulator(PROFILES[profile_id])
    #   - memory_update_node will flush outcomes to mem0 automatically
    raise NotImplementedError


if __name__ == "__main__":
    # TODO: build graph and memory_manager
    # TODO: for each profile_id in PROFILES: seed_profile(profile_id, graph, memory_manager)
    print("Seeding demo memory for all 6 profiles...")
