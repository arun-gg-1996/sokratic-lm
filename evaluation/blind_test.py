"""
evaluation/blind_test.py
-------------------------
Blind test evaluation for the multimodal vision pipeline.
Required by the project spec: "The bot must correctly identify anatomical
structures in a blind test set of diagrams."

Setup:
  - Hold out ~10 diagrams from data/diagrams/ as the blind test set
  - Strip visible labels from images before testing (or use unlabeled versions)
  - Ground truth: the structures listed in each diagram's metadata JSON

Flow:
  1. Load blind test set from data/eval/blind_test/
     Each entry: {filename, ground_truth_structures: [str]}
  2. Send each image to the vision model (cfg.models.vision)
     Prompt: "Identify all anatomical structures visible in this image.
              Return a JSON list of structure names only."
  3. Compare identified structures against ground truth
  4. Compute: exact match, partial match (any overlap), precision, recall
  5. Save results to data/artifacts/blind_test_results.json

Directory structure expected:
  data/eval/blind_test/
    ├── images/          # unlabeled versions of held-out diagrams
    └── ground_truth.json   # [{filename, structures: [str]}]

Run:
  python -m evaluation.blind_test

Target: structure identification accuracy >= 0.75 (≥ 75% of ground truth
structures correctly identified across the blind test set).
"""

import json
from pathlib import Path
from config import cfg

BLIND_TEST_DIR = Path("data/eval/blind_test")
RESULTS_PATH = Path("data/artifacts/blind_test_results.json")
ACCURACY_THRESHOLD = 0.75

VISION_PROMPT = """
Look at this anatomy diagram carefully.
List every anatomical structure you can identify in the image.
Return ONLY a JSON array of structure names, nothing else.
Example: ["Deltoid", "Supraspinatus", "Biceps brachii"]
"""


def load_ground_truth() -> list[dict]:
    """Load ground truth from data/eval/blind_test/ground_truth.json."""
    path = BLIND_TEST_DIR / "ground_truth.json"
    assert path.exists(), f"Ground truth not found at {path}"
    with open(path) as f:
        return json.load(f)


def identify_structures(image_path: Path, client) -> list[str]:
    """
    Send image to vision model and return list of identified structure names.

    Args:
        image_path: Path to the unlabeled diagram image.
        client:     Anthropic client instance.

    Returns:
        List of structure name strings.
    """
    # TODO: read image bytes, base64-encode
    # TODO: call cfg.models.vision with VISION_PROMPT + image
    # TODO: parse JSON array from response
    # TODO: return list of structure names (lowercased + stripped for comparison)
    raise NotImplementedError


def score(predicted: list[str], ground_truth: list[str]) -> dict:
    """
    Compute precision, recall, and F1 for one image.
    Comparison is case-insensitive.
    """
    pred_set = {s.lower().strip() for s in predicted}
    gt_set = {s.lower().strip() for s in ground_truth}

    tp = len(pred_set & gt_set)
    precision = tp / len(pred_set) if pred_set else 0.0
    recall = tp / len(gt_set) if gt_set else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0

    return {"precision": precision, "recall": recall, "f1": f1, "tp": tp,
            "predicted": list(pred_set), "ground_truth": list(gt_set)}


def run():
    """
    Main entry point. Run blind test and save results.
    """
    ground_truth = load_ground_truth()
    results = []
    recall_scores = []

    # TODO: initialize Anthropic client
    # TODO: for each entry in ground_truth:
    #         image_path = BLIND_TEST_DIR / "images" / entry["filename"]
    #         predicted = identify_structures(image_path, client)
    #         entry_score = score(predicted, entry["structures"])
    #         results.append({**entry, **entry_score})
    #         recall_scores.append(entry_score["recall"])
    #         print(f"{entry['filename']}: recall={entry_score['recall']:.2f}")

    # TODO: compute mean recall across all images
    # TODO: print summary — pass/fail against ACCURACY_THRESHOLD
    # TODO: save full results to RESULTS_PATH

    raise NotImplementedError


if __name__ == "__main__":
    run()
