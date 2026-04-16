"""
ui/app.py
----------
Streamlit demo interface for Socratic-OT.

Layout:
  Sidebar:
    - Domain selector (OT / Physics)
    - Student profile selector (demo mode)
    - Debug toggle: show Dean intervention log
    - Read-only: current turn count, hint level progress bar

  Main area:
    - Chat window (tutor ↔ student)
    - Image upload (multimodal path)
    - Topic selector (shown during Rapport phase)

  Info panel (right column or expander):
    - Weak topics this session
    - Weak topics from past sessions (from mem0)
    - Concepts covered count
    - Post-session EULER score (shown after session ends)

How Streamlit talks to LangGraph:
  - graph.invoke(state) for blocking calls (used in simulation)
  - graph.stream(state) for streaming tokens to the chat window
  - MCP tool calls happen entirely inside the graph — Streamlit never calls tools directly
  - State is kept in st.session_state between Streamlit reruns

Usage:
    streamlit run ui/app.py
"""

import streamlit as st
from config import cfg


def main():
    st.set_page_config(page_title="Socratic-OT", layout="wide")

    # --- Sidebar ---
    with st.sidebar:
        st.title("Socratic-OT")
        domain = st.selectbox("Domain", ["OT (Anatomy)", "Physics"])
        profile_id = st.selectbox("Student Profile (demo)", ["S1", "S2", "S3", "S4", "S5", "S6"])
        show_dean_log = st.toggle("Show Dean intervention log", value=False)
        st.divider()
        # TODO: display turn count and hint level progress bar from st.session_state

    # --- Initialize session state ---
    if "graph" not in st.session_state:
        # TODO: build graph and memory_manager, store in st.session_state
        # TODO: initialize TutorState in st.session_state
        pass

    # --- Main chat area ---
    # TODO: render chat messages from st.session_state.state["messages"]
    # TODO: handle topic selector during Rapport phase (st.selectbox from textbook_structure.json)

    # --- Image upload (multimodal path) ---
    # IMPORTANT: image processing happens HERE in Streamlit, before graph.invoke().
    # The graph itself never calls image_processor — Streamlit does it as a pre-step.
    #
    # Flow:
    #   uploaded_file = st.file_uploader("Upload anatomy diagram", type=["png","jpg","jpeg"])
    #   if uploaded_file:
    #       image_bytes = uploaded_file.read()
    #       result = process_image(image_bytes, retriever)
    #       if result["low_confidence"]:
    #           # Teacher will ask student to describe the image
    #           st.session_state.state["is_multimodal"] = True
    #           st.session_state.state["image_structures"] = []
    #       else:
    #           st.session_state.state["is_multimodal"] = True
    #           st.session_state.state["image_structures"] = result["image_structures"]
    #           st.session_state.state["retrieved_chunks"] = result["retrieved_chunks"]
    #       # Then fall through to graph.invoke() as normal — no special graph path needed

    # TODO: chat input → update state with student message → graph.stream(state)
    # TODO: stream tokens to st.write_stream or st.chat_message

    # --- Diagram visual aid ---
    # When retrieved_chunks contains a chunk with element_type="diagram",
    # display the actual image file alongside the tutor response.
    # This gives the student a visual reference during tutoring.
    #
    # if st.session_state.state["retrieved_chunks"]:
    #     diagram_chunks = [c for c in st.session_state.state["retrieved_chunks"]
    #                       if c.get("element_type") == "diagram"]
    #     for chunk in diagram_chunks:
    #         img_path = Path("data/diagrams") / chunk["image_filename"]
    #         if img_path.exists():
    #             st.image(str(img_path), caption=chunk.get("section_title", ""), use_column_width=True)

    # --- Info panel ---
    # TODO: show weak_topics from state (this session + from mem0)
    # TODO: show EULER score after session ends

    # --- Dean log (debug) ---
    if show_dean_log:
        # TODO: load and display dean_interventions from data/artifacts/
        pass


if __name__ == "__main__":
    main()
