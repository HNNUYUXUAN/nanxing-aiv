"""Reproduce public aggregate plots and synthetic examples without model calls."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def verify_aggregates(summary):
    """Check denominators, bounds and ordering certificates in released tables."""
    d = summary["denominators"]
    states = ("agreed", "abstained", "disagreement", "uncertain", "technical_failure")
    for row in summary["descriptive"]:
        assert sum(row[k] for k in states) == row["turns"]
        assert row["candidate_turns"] == row["agreed"]
    for row in summary["flow"]:
        assert sum(row[k] for k in states) == row["turn_denominator"]
        assert row["turn_denominator"] == (d["random_review"] if row["stratum"] == "random_audit" else d["high_risk_review"])
    joint = summary["joint"]
    assert sum(joint[k] for k in ("task_above_contribution", "equal", "task_below_contribution")) == joint["paired_denominator"]
    for row in summary["missing_label_bounds"]:
        assert row["candidate_turns"] + row["unknown_turns"] == row["total_turns"]
        assert 1 <= row["abl_raw_lower"] <= row["abl_raw_upper"] <= 6
        assert 0 <= row["hot_lower"] <= row["hot_upper"] <= 1
    grouped = {}
    for row in summary["score_range_sensitivity"]:
        assert 0 <= row["width_minimum"] <= row["width_maximum"] + 1e-9 <= 100 + 1e-8
        assert 0 <= row["stably_distinguishable_pairs"] <= row["within_term_pairs"]
        key = row["term"], row["scheme"], row["weight_relative_change"]
        grouped.setdefault(key, []).append(row)
    for rows in grouped.values():
        rows.sort(key=lambda r: r["label_error_fraction"])
        for left, right in zip(rows, rows[1:]):
            assert left["width_median"] <= right["width_median"] + 1e-9
            assert left["stably_distinguishable_pairs"] >= right["stably_distinguishable_pairs"]
    return {"aggregate_denominators": True, "missing_bounds": True,
            "sensitivity_monotonicity": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorized-inputs", action="store_true", help="Recompute from the pinned private research inputs already present locally")
    parser.add_argument("--notebooks", action="store_true", help="Execute the ten companion notebooks in clean kernels")
    parser.add_argument("--notebook-workers", type=int, choices=range(1, 5), default=1,
                        help="Concurrent notebook kernels (default: 1 to limit font-rendering memory)")
    args = parser.parse_args()
    start = time.monotonic()
    if args.authorized_inputs:
        subprocess.run([sys.executable, "-X", "utf8", "-B", "scripts/build_final_analysis.py"], cwd=ROOT, check=True)
    source = ROOT / "results/final-analysis"
    summary = json.loads((source / "summary.json").read_text("utf-8"))
    checks = verify_aggregates(summary)
    from scripts.demo_evidence_chain import run_case
    from scripts.build_final_analysis import make_plots
    from scripts.build_figure_groups import build_groups
    import pandas as pd
    output = ROOT / "build/reproduction"
    output.mkdir(parents=True, exist_ok=True)
    tables = {p.name: pd.read_csv(p).to_dict("records") for p in source.glob("*.csv")}
    make_plots(summary, tables, output)
    build_groups(output, output)
    examples = [run_case("complete"), run_case("missing_evidence")]
    (output / "demo-examples.json").write_text(json.dumps(examples, ensure_ascii=False, indent=2, allow_nan=False), "utf-8")
    if args.notebooks:
        subprocess.run([sys.executable, "-X", "utf8", "-B", "scripts/execute_notebooks.py",
                        "--workers", str(args.notebook_workers)], cwd=ROOT, check=True)
    receipt = {"mode": "authorized_input_recalculation" if args.authorized_inputs else "public_aggregate_and_synthetic",
               "live_model_calls": False, "notebooks_executed": args.notebooks,
               "raw_records_recomputed": args.authorized_inputs,
               "notebook_workers": args.notebook_workers if args.notebooks else None,
               "source_summary_sha256": hashlib.sha256((source / "summary.json").read_bytes()).hexdigest(),
               "checks": checks, "examples_executed": len(examples), "panels_generated": 10,
               "plots_generated": 5, "figure_assembly": "independent_panels_then_latex",
               "seconds": round(time.monotonic() - start, 3)}
    (output / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == "__main__":
    main()
