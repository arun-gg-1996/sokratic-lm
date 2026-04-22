from pathlib import Path
import yaml


def _load_prompts() -> dict:
    cfg_path = Path(__file__).resolve().parents[1] / "config.yaml"
    with cfg_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return raw["prompts"]


def _reconstruct(base: str, delta: str) -> str:
    b = base or ""
    d = delta or ""
    if b and d:
        # Support both styles:
        # 1) token-level factoring (e.g., "You are " + "the Dean ...")
        # 2) paragraph factoring (base + blank line + delta)
        if b.endswith((" ", "\n", "\t")) or d.startswith((" ", "\n", "\t")):
            return f"{b}{d}".strip()
        return f"{b}\n\n{d}".strip()
    return (b or d).strip()


def test_prompt_parity_teacher_and_dean_wrappers():
    prompts = _load_prompts()

    teacher_base = prompts.get("teacher_base", "")
    dean_base = prompts.get("dean_base", "")

    teacher_pairs = [
        ("teacher_topic_engagement", "teacher_topic_engagement_delta"),
        ("teacher_rapport", "teacher_rapport_delta"),
        ("teacher_socratic_static", "teacher_socratic_delta"),
        ("teacher_clinical_opt_in_static", "teacher_clinical_opt_in_delta"),
        ("teacher_clinical_static", "teacher_clinical_delta"),
    ]
    dean_pairs = [
        ("dean_setup_classify_static", "dean_setup_delta"),
        ("dean_quality_check_tutoring_static", "dean_quality_check_tutoring_delta"),
        ("dean_quality_check_assessment_static", "dean_quality_check_assessment_delta"),
        ("dean_clinical_turn_static", "dean_clinical_turn_delta"),
        ("dean_assessment_static", "dean_assessment_delta"),
        ("dean_memory_summary", "dean_memory_summary_delta"),
    ]

    for static_key, delta_key in teacher_pairs:
        original = (prompts.get(static_key, "") or "").strip()
        reconstructed = _reconstruct(teacher_base, prompts.get(delta_key, ""))
        assert original == reconstructed, f"Prompt parity broken for {static_key}"

    for static_key, delta_key in dean_pairs:
        original = (prompts.get(static_key, "") or "").strip()
        reconstructed = _reconstruct(dean_base, prompts.get(delta_key, ""))
        assert original == reconstructed, f"Prompt parity broken for {static_key}"
