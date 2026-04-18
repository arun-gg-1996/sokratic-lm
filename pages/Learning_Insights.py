import streamlit as st


def main():
    st.set_page_config(page_title="Learning Insights", layout="wide")
    st.title("Learning Insights")
    st.caption("Weak-topic tracking for focused review.")

    state = st.session_state.get("state", {}) or {}
    weak_topics = state.get("weak_topics", []) or []

    if st.button("Back to Tutor"):
        if hasattr(st, "switch_page"):
            st.switch_page("ui/app.py")
        st.stop()

    st.divider()
    if not weak_topics:
        st.info("No weak topics detected yet.")
        return

    for wt in weak_topics:
        topic = wt.get("topic", "topic")
        fails = wt.get("failure_count", 0)
        st.markdown(f"- **{topic}** — {fails} struggle attempts")


if __name__ == "__main__":
    main()
