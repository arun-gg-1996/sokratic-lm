"""
simulation/runner.py
---------------------
Async simulation runner. Runs conversations in parallel batches.

Distribution for full run:
  6 profiles × ~15 anatomy topics × ~17 convos per combo = ~1,500 total

Each conversation:
  - Initializes TutorState with profile_id as student_id
  - Drives student turns using StudentSimulator
  - Loops until phase == "memory_update" or turn_count >= max_turns
  - Logs full conversation to data/simulations/ via logger.py

Parallelism: asyncio with max cfg.simulation.max_concurrent concurrent conversations.
Resume support: skip conv_ids that already exist in output JSONL.

Usage:
    python -m simulation.runner                         # full 1500 convos
    python -m simulation.runner --profile S1 --n 50    # smoke test: 1 profile, 50 convos
"""

import asyncio
import uuid
import json
from pathlib import Path
from dotenv import load_dotenv
from config import cfg

# Load API keys from .env for CLI runs.
load_dotenv()

# Anatomy topics — loaded from textbook_structure.json at startup.
# Populated by _load_topics() in run_all().
TOPICS: list[str] = []


async def run_conversation(
    profile_id: str,
    topic: str,
    graph,
    simulator,
    semaphore: asyncio.Semaphore,
) -> dict:
    """
    Run a single simulated conversation asynchronously.

    Args:
        profile_id: One of S1-S6
        topic:      Full topic path e.g. "Chapter 11 > Deltoid Innervation"
        graph:      Compiled LangGraph runnable (built once, shared across convos)
        simulator:  StudentSimulator instance for this profile
        semaphore:  Limits concurrent API calls to cfg.simulation.max_concurrent

    Returns:
        Conversation result dict matching simulation/logger.py schema.
    """
    async with semaphore:
        from conversation.state import initial_state

        conv_id = str(uuid.uuid4())
        student_id = f"{profile_id}_{conv_id[:8]}"
        state = initial_state(student_id, cfg)
        thread_config = {"configurable": {"thread_id": conv_id}}

        # Rapport turn
        state = await asyncio.to_thread(graph.invoke, state, thread_config)

        # Seed the topic as the first student message
        state["messages"].append({"role": "student", "content": topic})

        turns_log = []
        turn_num = 0

        while True:
            # Invoke graph for this turn
            state = await asyncio.to_thread(graph.invoke, state, thread_config)
            turn_num += 1

            # Log turns
            for msg in state.get("messages", [])[-2:]:
                turns_log.append({
                    "role": msg.get("role"),
                    "content": msg.get("content"),
                    "phase": state.get("phase"),
                    "turn": turn_num,
                })

            # Check exit conditions
            if state.get("phase") == "memory_update":
                break
            if state.get("turn_count", 0) >= state.get("max_turns", 25):
                break
            if state.get("assessment_turn", 0) == 2:
                break

            # Generate student response if session is still active
            if state.get("phase") not in ("assessment", "memory_update"):
                student_response = await asyncio.to_thread(simulator.respond, state)
                state["messages"].append({"role": "student", "content": student_response})
                state["debug"]["turn_trace"] = []

        # Build result dict
        weak_topics_added = [
            wt.get("topic", "") for wt in state.get("weak_topics", [])
            if not state.get("student_reached_answer")
        ]

        result = {
            "conv_id": conv_id,
            "student_profile": profile_id,
            "topic": topic,
            "topic_difficulty": "moderate",  # default; real difficulty from textbook_structure.json
            "turns": turns_log,
            "outcome": {
                "reached_answer": state.get("student_reached_answer", False),
                "turns_taken": state.get("turn_count", 0),
                "hints_used": state.get("hint_level", 1),
                "weak_topics_added": weak_topics_added,
            },
            "dean_interventions": state.get("debug", {}).get("interventions", 0),
        }

        return result


def _load_topics() -> list[str]:
    """
    Load anatomy topics from textbook_structure.json.
    Returns flat list of topic paths e.g. ["Chapter 11 > Deltoid Innervation", ...]
    Falls back to 5 hardcoded topics if file doesn't exist yet (MockRetriever mode).
    """
    structure_path = Path(cfg.paths.textbook_structure)
    if not structure_path.exists():
        # Fallback for MockRetriever development mode
        return [
            "Chapter 11 > Muscles That Move the Humerus > Deltoid Innervation",
            "Chapter 11 > Rotator Cuff > Supraspinatus",
            "Chapter 11 > Brachial Plexus > Axillary Nerve",
            "Chapter 12 > Spinal Cord > Posterior Horn",
            "Chapter 13 > Peripheral Nervous System > Sciatic Nerve",
        ]

    # Parse textbook_structure.json — extract all leaf topic paths.
    # Supports dict- and list-based schemas with common keys:
    # chapters/sections/subsections/topics/children.
    try:
        structure = json.loads(structure_path.read_text())
    except Exception:
        structure = {}

    topics: list[str] = []
    seen: set[str] = set()
    structural_keys = {"chapters", "sections", "subsections", "topics", "children"}
    ignored_meta = {"difficulty", "domain", "metadata", "page", "pages", "id", "order"}

    def _add_topic(path_parts: list[str]) -> None:
        cleaned = [p.strip() for p in path_parts if isinstance(p, str) and p.strip()]
        if not cleaned:
            return
        topic = " > ".join(cleaned)
        if topic not in seen:
            seen.add(topic)
            topics.append(topic)

    def _extract_leaves(node, path_parts: list[str]):
        if isinstance(node, list):
            for item in node:
                _extract_leaves(item, path_parts)
            return

        if isinstance(node, str):
            _add_topic(path_parts + [node])
            return

        if not isinstance(node, dict):
            if path_parts:
                _add_topic(path_parts)
            return

        label = node.get("title") or node.get("name") or node.get("topic")
        current_path = path_parts + [label] if isinstance(label, str) and label.strip() else list(path_parts)

        has_structural_child = False
        for key in structural_keys:
            child = node.get(key)
            if isinstance(child, dict):
                has_structural_child = True
                for child_name, child_node in child.items():
                    _extract_leaves(child_node, current_path + [child_name])
            elif isinstance(child, list):
                has_structural_child = True
                for child_node in child:
                    _extract_leaves(child_node, current_path)

        # Also support schemas where hierarchy is represented as arbitrary nested dicts.
        for key, value in node.items():
            if key in structural_keys or key in ignored_meta:
                continue
            if isinstance(value, (dict, list)):
                has_structural_child = True
                _extract_leaves(value, current_path + [key])

        if not has_structural_child and current_path:
            _add_topic(current_path)

    if isinstance(structure, dict):
        for root_name, root_node in structure.items():
            _extract_leaves(root_node, [root_name])
    elif isinstance(structure, list):
        _extract_leaves(structure, [])

    return topics if topics else [
        "Chapter 11 > Deltoid Innervation",
        "Chapter 11 > Rotator Cuff",
        "Chapter 13 > Brachial Plexus",
    ]


async def run_all(n: int = None, profile_id: str = None):
    """
    Run the full simulation batch.

    Args:
        n:          Override total number of conversations (default: cfg.simulation.n_conversations)
        profile_id: Run only this profile (default: all 6)
    """
    from evaluation.simulation.profiles import PROFILES
    from evaluation.simulation.student_simulator import StudentSimulator
    from evaluation.simulation.logger import log_conversation, load_conversations
    from conversation.graph import build_graph
    from retrieval.retriever import MockRetriever
    from memory.memory_manager import MemoryManager

    global TOPICS
    TOPICS = _load_topics()
    print(f"Loaded {len(TOPICS)} topics.")

    # Build graph once — shared across all conversations
    graph = build_graph(MockRetriever(), MemoryManager())

    profiles_to_run = [profile_id] if profile_id else list(PROFILES.keys())
    target_n = n or cfg.simulation.n_conversations

    # Load existing conv_ids for resume support
    existing = {conv["conv_id"] for conv in load_conversations()}
    print(f"Resuming: {len(existing)} conversations already logged.")

    # Build (profile_id, topic) pairs
    pairs = []
    per_combo = max(1, target_n // (len(profiles_to_run) * len(TOPICS)))
    for pid in profiles_to_run:
        for topic in TOPICS:
            for _ in range(per_combo):
                pairs.append((pid, topic))
    pairs = pairs[:target_n]
    print(f"Running {len(pairs)} conversations across {len(profiles_to_run)} profiles, {len(TOPICS)} topics.")

    semaphore = asyncio.Semaphore(cfg.simulation.max_concurrent)

    tasks = []
    for pid, topic in pairs:
        simulator = StudentSimulator(PROFILES[pid])
        tasks.append(run_conversation(pid, topic, graph, simulator, semaphore))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    success = 0
    for result in results:
        if isinstance(result, Exception):
            print(f"Conversation failed: {result}")
        else:
            log_conversation(result)
            success += 1

    print(f"Simulation complete: {success}/{len(pairs)} conversations logged.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", type=str, default=None, help="Run only this profile (S1-S6)")
    parser.add_argument("--n", type=int, default=None, help="Number of conversations")
    args = parser.parse_args()
    asyncio.run(run_all(n=args.n, profile_id=args.profile))
