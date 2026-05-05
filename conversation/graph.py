"""
conversation/graph.py
─────────────────────
Assembles the V2 LangGraph StateGraph and compiles it into a runnable.

Graph shape:
  START → rapport_node → dean_node ⟷ (loops on student input)
                                    ↓ (when answer reached, hints exhausted, or turn limit)
                         assessment_node → memory_update_node → END

V2 stack (single source of truth post-D1):
  rapport_node       — conversation.lifecycle_v2 (TeacherV2 mode="rapport")
  dean_node          — conversation.nodes_v2.dean_node_v2 (preflight + dean_v2
                       + retry orchestrator + verifier quartet)
  assessment_node    — conversation.assessment_v2.assessment_node_v2 (opt-in +
                       clinical phase via DeanV2 + TeacherV2)
  memory_update_node — conversation.lifecycle_v2 (close-LLM + mem0 flush +
                       SQLite session-end + L21 mastery upsert)

Edges (also in conversation/lifecycle_v2):
  after_rapport      — rapport → dean / assessment / memory_update
  after_dean         — dean → assessment / memory_update / END
  after_assessment   — assessment → memory_update / END

Note: the graph does NOT loop internally. Each graph.invoke() handles
exactly ONE student turn and ends at END (or after assessment when
assessment_turn < 2). The frontend calls invoke() again on each student
message. The LangGraph MemorySaver checkpointer preserves state between
calls via thread_id.

Usage:
    from conversation.graph import build_graph
    graph = build_graph(retriever, memory_manager)
    config = {"configurable": {"thread_id": thread_id}}
    state = graph.invoke(state, config=config)

D1 note (V1 → V2 consolidation): the previous graph.py instantiated V1
DeanAgent + TeacherAgent and routed dean_node / assessment_node via the
SOKRATIC_USE_V2_FLOW feature flag. The flag is gone — V2 owns the
per-turn graph. BUT the V1 DeanAgent is STILL load-bearing for the
bootstrap path: `topic_lock_v2._render_starter_cards`,
`_render_anchor_pick`, the prelock refuse/fail handlers, and the
topic-lock anchor calls (`_lock_anchors_call`,
`_build_topic_ack_message`, `_prelock_refuse_call`,
`_prelock_anchor_fail_call`, `_retrieve_on_topic_lock`) all dispatch
through the `dean` partial-kwarg. Setting it to None silently breaks
the LLM-generated bootstrap responses → student sees templated
"could not find a strong textbook match" fallbacks instead of the
LLM-crafted contextual replies. Until the D1-bootstrap migration ports
those 4 dean methods into V2 namespace, V1 DeanAgent is instantiated
and passed in here. The TeacherAgent is similarly retained (legacy
fallback paths the bootstrap may still hit).
"""

from functools import partial
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

from conversation.state import TutorState
from conversation.lifecycle_v2 import (
    rapport_node,
    memory_update_node,
    after_rapport,
    after_dean,
    after_assessment,
)
from conversation.nodes_v2 import dean_node_v2
from conversation.assessment_v2 import assessment_node_v2

# V1 agents — retained until the D1-bootstrap migration ports the 4
# legacy dean methods used by topic_lock_v2 (and the teacher callback
# registry; the latter has already moved to conversation/streaming.py).
# These instances are passed via partial() into the V2 nodes so the
# bootstrap path's LLM-driven refuse/anchor/ack calls still work.
from conversation.dean import DeanAgent
from conversation.teacher import TeacherAgent


def build_graph(retriever, memory_manager):
    """Build and compile the V2 LangGraph StateGraph.

    Args:
        retriever:       Retriever or MockRetriever instance
        memory_manager:  MemoryManager instance (loads/flushes mem0)

    Returns:
        Compiled LangGraph runnable (with MemorySaver checkpointer).
    """
    # Compatibility: real MemoryManager exposes `.persistent`, while the
    # current stubbed manager may not. Dean accepts either and currently
    # does not depend on persistence-specific methods.
    memory_client = getattr(memory_manager, "persistent", memory_manager)
    dean = DeanAgent(retriever, memory_client)
    teacher = TeacherAgent()

    graph = StateGraph(TutorState)

    # rapport_node: V2 (TeacherV2 mode="rapport"). `teacher` partial
    # kwarg is currently unused by the V2 path but retained as the
    # signature still accepts it.
    graph.add_node(
        "rapport_node",
        partial(rapport_node, teacher=teacher, memory_manager=memory_manager),
    )

    # dean_node: V2 per-turn loop — preflight → dean_v2.plan() → retry
    # orchestrator → verifier quartet. `dean` is V1 DeanAgent — required
    # by topic_lock_v2's bootstrap helpers (_lock_anchors_call,
    # _retrieve_on_topic_lock, _build_topic_ack_message,
    # _prelock_refuse_call, _prelock_anchor_fail_call). Without a real
    # DeanAgent instance, those calls silently fail and the bootstrap
    # path emits templated fallbacks instead of LLM-crafted replies.
    graph.add_node(
        "dean_node",
        partial(dean_node_v2, dean=dean, teacher=teacher, retriever=retriever),
    )

    # assessment_node: V2 — opt-in + clinical phase via DeanV2 + TeacherV2.
    graph.add_node(
        "assessment_node",
        partial(assessment_node_v2, dean=dean, teacher=teacher, retriever=retriever),
    )

    # memory_update_node: V2 — close-LLM (TeacherV2 mode="close") +
    # mem0 flush + SQLite session-end + L21 mastery upsert.
    graph.add_node(
        "memory_update_node",
        partial(memory_update_node, dean=dean, memory_manager=memory_manager),
    )

    graph.add_edge(START, "rapport_node")
    graph.add_conditional_edges("rapport_node", after_rapport)
    graph.add_conditional_edges("dean_node", after_dean)
    graph.add_conditional_edges("assessment_node", after_assessment)
    graph.add_edge("memory_update_node", END)

    return graph.compile(checkpointer=MemorySaver())
