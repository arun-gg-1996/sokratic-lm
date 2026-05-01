"""
test_graph_state.py — verify Phase 5 mode + study fields are defined.
"""
from graph.state import GraphState


def test_mode_field_present():
    keys = GraphState.__annotations__
    assert "mode" in keys, f"GraphState missing 'mode' field. Keys: {list(keys)}"
    assert keys["mode"] is str, f"mode should be str, got {keys['mode']}"


def test_study_fields_present():
    keys = GraphState.__annotations__
    assert "study_active_topic" in keys
    assert "study_topic_count" in keys
    assert keys["study_active_topic"] is str
    assert keys["study_topic_count"] is int


def test_mode_default_via_dict():
    state: GraphState = {"mode": "socratic"}  # type: ignore[typeddict-item]
    assert state["mode"] == "socratic"


if __name__ == "__main__":
    test_mode_field_present()
    test_study_fields_present()
    test_mode_default_via_dict()
    print("PASS: all 3 tests")
