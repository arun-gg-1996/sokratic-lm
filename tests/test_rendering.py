from conversation.rendering import render_history


def test_append_only_contract():
    messages = [
        {"role": "student", "content": "hi"},
        {"role": "tutor", "content": "hello"},
        {"role": "student", "content": "what is the FCR?"},
    ]
    for n in range(1, len(messages)):
        assert render_history(messages[: n + 1]).startswith(render_history(messages[:n]))


def test_ignores_extra_metadata():
    # Durable guarantee for Step 2 audit: render output is based only on role/content.
    base = [{"role": "student", "content": "hi"}]
    with_extra = [
        {
            "role": "student",
            "content": "hi",
            "phase": "tutoring",
            "timestamp": "2026-04-18T14:00:00Z",
            "hint_level": 1,
        }
    ]
    assert render_history(base) == render_history(with_extra)


def test_ignores_metadata_across_multiple_messages():
    base = [
        {"role": "student", "content": "topic: shoulder"},
        {"role": "tutor", "content": "Which nerve supplies the deltoid?"},
    ]
    with_meta = [
        {
            "role": "student",
            "content": "topic: shoulder",
            "phase": "topic_lock",
            "wrapper": "dean._setup_call",
            "hint_level": 1,
            "foo": {"bar": 1},
        },
        {
            "role": "tutor",
            "content": "Which nerve supplies the deltoid?",
            "timestamp": "2026-04-18T16:01:00Z",
            "clinical_turn_count": 0,
            "retrieval_query": "shoulder nerve",
        },
    ]
    assert render_history(base) == render_history(with_meta)


def test_handles_summarizer_system_message():
    messages = [
        {"role": "system", "content": "[Session summary]: Student explored FCR anatomy."},
        {"role": "student", "content": "what about extension?"},
    ]
    rendered = render_history(messages)
    assert "System: [Session summary]:" in rendered
    assert "Student: what about extension?" in rendered
