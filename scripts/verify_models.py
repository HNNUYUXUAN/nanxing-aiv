"""Produce a privacy-preserving, offline A--E model verification receipt.

Example:
  .venv/Scripts/python.exe scripts/verify_models.py \
      --annotations runtime/research/.../frozen/annotations.json \
      --output results/model-verification/frozen.json

The receipt contains counts and analytic examples only, never source text or
student identifiers.  It does not submit API requests.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aiv.metrics import NAMES, WEIGHTS  # noqa: E402
from aiv.models import (  # noqa: E402
    candidate_metric_intervals, decision_gate, identification_gate,
    observational_nonidentification_witness, score_outer_bounds,
    summarize_candidate_batch,
)


def exact_candidate_score(points: dict, scheme: str) -> float | None:
    """Independent direct dot product; missing metrics keep the point null."""
    if any(points[name] is None for name in NAMES):
        return None
    return 100 * sum(float(WEIGHTS[scheme][i]) * points[name]
                     for i, name in enumerate(NAMES))


def analytic_controls() -> dict:
    labels = [2, 3, 4, 5]
    edges = [(0, 1), (1, 2), (2, 3)]
    base = candidate_metric_intervals(labels, tool_count=1, verified_edges=edges)
    stack = candidate_metric_intervals(labels, tool_count=4, verified_edges=edges)
    one_change = candidate_metric_intervals(
        labels, tool_count=1, verified_edges=edges, max_changed_labels=1)
    unknown = candidate_metric_intervals(labels, tool_count=None, verified_edges=[])
    base_score = exact_candidate_score(base["point_conditional_on_candidates"], "balanced")
    stack_score = exact_candidate_score(stack["point_conditional_on_candidates"], "balanced")
    if base_score is None or stack_score is None:
        raise AssertionError("Complete analytic controls became incomplete")
    difference = stack_score - base_score
    # Independent closed form: MAB alone changes from ln 2 / ln 5 to 1.
    expected = 20 * (1 - __import__("math").log(2) / __import__("math").log(5))
    if abs(difference - expected) > 1e-10:
        raise AssertionError("Tool stacking calculation disagrees with closed form")
    return {
        "known_truth_synthetic_control": {
            "labels": labels,
            "verified_edges": len(edges),
            "baseline_balanced_score": base_score,
            "four_tool_balanced_score": stack_score,
            "tool_stacking_change": difference,
            "independent_closed_form_change": expected,
            "interpretation": "fixed labels, extra tool identities, design demonstration only",
        },
        "one_label_replacement_sensitivity": {
            "metric_outer_intervals": one_change["outer_intervals"],
            "balanced_score_outer_interval": score_outer_bounds(
                one_change["outer_intervals"], "balanced"),
            "scope": "at_most_one_of_four_candidate_labels_arbitrary; weight_factors_0.9_to_1.1",
        },
        "missing_evidence_control": {
            "candidate_point_score": exact_candidate_score(
                unknown["point_conditional_on_candidates"], "balanced"),
            "outer_score_interval": score_outer_bounds(
                unknown["outer_intervals"], "balanced"),
            "reason": "session edges and tool evidence unverified",
        },
    }


def build_receipt(annotation_path: Path) -> dict:
    data = annotation_path.read_bytes()
    annotations = json.loads(data)
    if not isinstance(annotations, list):
        raise ValueError("Expected a list of graph annotations")
    summary = summarize_candidate_batch(annotations)
    # Independent count, separate from the summary helper, catches accidental
    # denominator expansion to synthetic/incomplete records.
    independent_denominator = sum(
        row.get("complete") is True and row.get("record", {}).get("term") != "synthetic"
        for row in annotations)
    if summary["completed_real_turns"] != independent_denominator:
        raise AssertionError("Real completed denominator mismatch")
    candidate = summary["candidate_task_turns"]
    if candidate + sum(v for k, v in summary["task_status_counts"].items()
                       if k != "agreed") != independent_denominator:
        raise AssertionError("Task status accounting mismatch")
    return {
        "schema_version": "aiv-model-verification-v1",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "input": {
            "path": str(annotation_path.resolve()),
            "sha256": hashlib.sha256(data).hexdigest(),
            "rows_in_file": len(annotations),
            "privacy": "aggregate counts only; no student IDs or question text",
        },
        "A_candidate_measurement": summary,
        "B_identification": {
            "real_data_gate": identification_gate({}),
            "analytic_counterexample": observational_nonidentification_witness(),
            "interpretation": "A model label is not an independent learning outcome",
        },
        "C_D_analytic_verification": analytic_controls(),
        "E_decision": decision_gate(
            candidate_coverage=summary["candidate_coverage"] or 0,
            human_calibration=False, identified_learning_effect=False,
            missing_core_metric=True),
        "limits": [
            "Candidate label counts are conditional on the completed input list.",
            "No independent blinded reference labels enter this receipt.",
            "No causal AI effect, complete real AIV, accuracy, or confidence interval is estimated.",
            "Weight/label ranges are deterministic assumption sensitivity, not sampling intervals.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--annotations", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    receipt = build_receipt(args.annotations)
    rendered = json.dumps(receipt, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(json.dumps({
        "input_sha256": receipt["input"]["sha256"],
        "completed_real_turns": receipt["A_candidate_measurement"]["completed_real_turns"],
        "candidate_task_turns": receipt["A_candidate_measurement"]["candidate_task_turns"],
        "causal_status": receipt["B_identification"]["real_data_gate"]["status"],
        "candidate_score_missing_when_core_evidence_missing": (
            receipt["C_D_analytic_verification"]["missing_evidence_control"]["candidate_point_score"] is None),
        "output": str(args.output.resolve()) if args.output else None,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
