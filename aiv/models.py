"""Auditable A--E model boundaries for candidate-labelled learning logs.

This module deliberately separates a *candidate-conditional* calculation from
an observed learning effect.  Bounds are sensitivity ranges over stated label
and weight assumptions, not sampling confidence intervals.
"""

from __future__ import annotations

from collections import Counter
from itertools import product
from math import log1p, log
from typing import Mapping, Sequence

import numpy as np

from .metrics import IDEAL, NAMES, WEIGHTS, dhi, levels_array


def observational_nonidentification_witness() -> dict:
    """Two binary worlds with identical observed (T,Y) and different ATEs.

    In both, U is a fair coin and T=U.  World 1 has Y(t)=t; world 2 has
    Y(t)=U.  Thus both show half (0,0), half (1,1) observationally, but
    their intervention effects differ.  This is a constructive counterexample
    to treating an observed association as the AI effect.
    """
    worlds = []
    for name, potential in (
        ("treatment_effect", ((0, 1), (0, 1))),
        ("selection_only", ((0, 0), (1, 1))),
    ):
        rows = []
        for u, (y0, y1) in enumerate(potential):
            treatment = u
            rows.append({"probability": 0.5, "treatment": treatment,
                         "observed_outcome": (y0, y1)[treatment],
                         "y0": y0, "y1": y1})
        worlds.append({"name": name, "rows": rows,
                       "average_treatment_effect": sum(
                           row["probability"] * (row["y1"] - row["y0"])
                           for row in rows)})
    return {"observed_distribution": [
                {"treatment": 0, "outcome": 0, "probability": 0.5},
                {"treatment": 1, "outcome": 1, "probability": 0.5}],
            "worlds": worlds,
            "conclusion": "observational_distribution_does_not_identify_ate"}


def identification_gate(evidence: Mapping[str, bool]) -> dict:
    """Report whether a real learning-effect analysis has its prerequisites.

    A complete gate says *conditionally estimable*, not that exchangeability
    or other assumptions have been proved by the supplied data.
    """
    required = (
        "well_defined_treatment", "independent_learning_outcome",
        "pre_treatment_baseline", "comparable_control",
        "treatment_precedes_outcome", "plausible_exchangeability",
        "common_support", "consistent_measurement", "interference_addressed",
    )
    missing = [key for key in required if evidence.get(key) is not True]
    return {
        "status": ("not_identified_from_available_evidence" if missing else
                   "conditionally_estimable_assumptions_not_proved"),
        "missing_or_unsubstantiated": missing,
        "estimand": "E[Y(1)-Y(0)] for a defined eligible population",
        "not_an_estimator": True,
    }


def ctq_on_verified_edges(levels: Sequence[int], edges: Sequence[tuple[int, int]]) -> float | None:
    """CTQ only on explicitly verified, ordered adjacent student-turn edges."""
    labels = levels_array(levels)
    if not edges:
        return None
    if len(set(map(tuple, edges))) != len(edges):
        raise ValueError("Repeated edge")
    for before, after in edges:
        if not (0 <= before < len(labels) and 0 <= after < len(labels)) or before == after:
            raise ValueError("Invalid edge indices")
    return float(0.5 + sum(labels[j] - labels[i] for i, j in edges) / (10 * len(edges)))


def candidate_metric_intervals(
    levels: Sequence[int], *, tool_count: int | None,
    verified_edges: Sequence[tuple[int, int]] = (), max_changed_labels: int = 0,
    ideal: Sequence[float] = IDEAL,
) -> dict:
    """Conservative outer bounds for a fixed candidate-labelled subset.

    Up to ``max_changed_labels`` candidate labels may be replaced by any
    L1--L6 value.  ABL/HOT/DHI change by at most s/n; an altered label may
    affect all edges incident to it.  All five metric intervals are coupled
    in reality, so their independent Cartesian product is an *outer* set.

    ``tool_count=None`` means missing evidence, whereas zero means verified
    no-tool use.  ``verified_edges`` must be supplied only after checking
    source order and session continuity; an empty sequence makes CTQ missing.
    """
    a = levels_array(levels)
    n = len(a)
    if n == 0:
        raise ValueError("A candidate-labelled subset needs at least one label")
    if not isinstance(max_changed_labels, int) or not 0 <= max_changed_labels <= n:
        raise ValueError("Invalid label error budget")
    if tool_count is not None and (not isinstance(tool_count, int) or tool_count < 0):
        raise ValueError("tool_count must be a nonnegative integer or None")
    p = np.bincount(a.astype(int), minlength=7)[1:] / n
    ideal = np.asarray(ideal, dtype=float)
    points = {
        "ABL": float((a.mean() - 1) / 5),
        "HOT": float((a >= 4).mean()),
        "CTQ": ctq_on_verified_edges(a, verified_edges),
        "DHI": dhi(p, ideal),
        "MAB": None if tool_count is None else float(min(1, log1p(tool_count) / log(5))),
    }
    common = max_changed_labels / n
    widths = {"ABL": common, "HOT": common, "DHI": common, "MAB": 0.0}
    if verified_edges:
        # The degree sum of the s highest-degree vertices bounds the number
        # of affected edges, even when selected vertices share an edge.
        degrees = Counter()
        for before, after in verified_edges:
            degrees[before] += 1
            degrees[after] += 1
        affected = min(len(verified_edges), sum(sorted(degrees.values(), reverse=True)[:max_changed_labels]))
        widths["CTQ"] = affected / len(verified_edges)
    else:
        widths["CTQ"] = 1.0
    intervals = {}
    for name in NAMES:
        value = points[name]
        intervals[name] = [0.0, 1.0] if value is None else [
            float(max(0, value - widths[name])), float(min(1, value + widths[name]))]
    return {
        "n_candidate_labels": n,
        "max_changed_labels": max_changed_labels,
        "n_verified_edges": len(verified_edges),
        "tool_count_status": "unknown" if tool_count is None else "verified",
        "point_conditional_on_candidates": points,
        "outer_intervals": intervals,
        "scope": "candidate_labelled_subset_only",
        "interpretation": "assumption_sensitivity_outer_bound_not_confidence_interval",
    }


def normalized_weight_vertices(base: Sequence[float], relative_change: float = 0.10) -> np.ndarray:
    """All vertices of a multiplicative weight box, re-normalized to sum 1."""
    w = np.asarray(base, dtype=float)
    if (w.shape != (5,) or np.any(~np.isfinite(w)) or np.any(w < 0)
            or not np.isclose(w.sum(), 1)):
        raise ValueError("Five nonnegative normalized weights required")
    if not 0 <= relative_change < 1:
        raise ValueError("relative_change must be in [0,1)")
    factors = np.array(list(product((1 - relative_change, 1 + relative_change), repeat=5)))
    raw = factors * w
    return raw / raw.sum(axis=1, keepdims=True)


def score_outer_bounds(metric_intervals: Mapping[str, Sequence[float]],
                       base_weights: str | Sequence[float] = "balanced",
                       relative_change: float = 0.10) -> dict:
    """Exact extrema of the rectangular metric/weight *outer* uncertainty set.

    With nonnegative weights the metric extrema occur at interval endpoints.
    A linear-fractional function of the independent weight multipliers is
    monotone in each multiplier with other multipliers fixed, hence its extrema
    occur at one of 2**5 box vertices.
    """
    low, high = [], []
    for name in NAMES:
        pair = metric_intervals[name]
        if len(pair) != 2 or not 0 <= pair[0] <= pair[1] <= 1:
            raise ValueError(f"Invalid interval for {name}")
        low.append(pair[0]); high.append(pair[1])
    base = WEIGHTS[base_weights] if isinstance(base_weights, str) else base_weights
    weights = normalized_weight_vertices(base, relative_change)
    lower = float(100 * min(weights @ np.asarray(low, dtype=float)))
    upper = float(100 * max(weights @ np.asarray(high, dtype=float)))
    return {"lower": lower, "upper": upper,
            "weight_scheme": base_weights if isinstance(base_weights, str) else "custom",
            "relative_weight_change": relative_change,
            "kind": "deterministic_outer_bound_not_confidence_interval"}


def stable_order(first: Mapping[str, float], second: Mapping[str, float]) -> str:
    """Certify a ranking only if conservative score ranges are disjoint."""
    for pair in (first, second):
        if not 0 <= pair["lower"] <= pair["upper"] <= 100:
            raise ValueError("Score bounds must lie in [0,100]")
    if first["lower"] > second["upper"]:
        return "first_robustly_higher_under_stated_assumptions"
    if second["lower"] > first["upper"]:
        return "second_robustly_higher_under_stated_assumptions"
    return "not_robustly_distinguishable"


def decision_gate(*, candidate_coverage: float, human_calibration: bool,
                  identified_learning_effect: bool, missing_core_metric: bool) -> dict:
    """Carry A/B/C evidence limits into an E-stage teaching recommendation."""
    if not 0 <= candidate_coverage <= 1:
        raise ValueError("candidate_coverage outside [0,1]")
    return {
        "allowed": "text_task_diagnostic_with_coverage_and_source_trace",
        "causal_learning_gain_claim": bool(identified_learning_effect),
        "individual_aiv_ranking": (
            candidate_coverage == 1 and not missing_core_metric and human_calibration),
        "conditions": {
            "candidate_coverage": candidate_coverage,
            "independent_human_calibration": human_calibration,
            "identified_learning_effect": identified_learning_effect,
            "core_metric_missing": missing_core_metric,
        },
        "next_evidence": [
            "independent common-scale learning outcome and comparable control",
            "blinded reference labels on a probability sample",
            "verified within-session turn edges and tool-use evidence",
        ],
    }


def summarize_candidate_batch(annotations: Sequence[Mapping]) -> dict:
    """A-stage descriptive receipt for the graph annotation schema.

    Its denominator is completed, non-synthetic input turns only.  The label
    count describes accepted *model candidates*, never reference truth.
    Raw questions, source text and student IDs are never copied to the output.
    """
    real = [row for row in annotations
            if row.get("complete") is True and row.get("record", {}).get("term") != "synthetic"]
    status_counts = Counter()
    level_counts = Counter()
    by_term: dict[str, dict] = {}
    by_student: dict[tuple[str, str], list[bool]] = {}
    for row in real:
        record = row["record"]
        term = str(record.get("term", "unknown"))
        final = row.get("dimensions", {}).get("task", {}).get("final", {})
        status = str(final.get("status", "missing"))
        label = final.get("label")
        accepted = status == "agreed" and isinstance(label, int) and 1 <= label <= 6
        status_counts[status] += 1
        if accepted:
            level_counts[label] += 1
        term_row = by_term.setdefault(term, {"completed_turns": 0, "candidate_turns": 0,
                                             "candidate_level_sum": 0,
                                             "candidate_hot_count": 0})
        term_row["completed_turns"] += 1
        if accepted:
            term_row["candidate_turns"] += 1
            term_row["candidate_level_sum"] += label
            term_row["candidate_hot_count"] += int(label >= 4)
        key = (term, str(record.get("student", "unknown")))
        by_student.setdefault(key, []).append(accepted)
    for term_row in by_term.values():
        n = term_row["candidate_turns"]
        total = term_row.pop("candidate_level_sum")
        hot = term_row.pop("candidate_hot_count")
        term_row["candidate_coverage"] = (
            n / term_row["completed_turns"] if term_row["completed_turns"] else None)
        term_row["candidate_abl_raw"] = total / n if n else None
        term_row["candidate_hot"] = hot / n if n else None
    n_real = len(real)
    n_candidate = sum(level_counts.values())
    student_coverages = [sum(values) / len(values) for values in by_student.values()]
    return {
        "completed_real_turns": n_real,
        "candidate_task_turns": n_candidate,
        "candidate_coverage": n_candidate / n_real if n_real else None,
        "task_status_counts": dict(sorted(status_counts.items())),
        "candidate_level_counts": {str(k): level_counts[k] for k in range(1, 7)},
        "by_term": dict(sorted(by_term.items())),
        "student_term_clusters": len(by_student),
        "equal_student_term_candidate_coverage": (
            float(np.mean(student_coverages)) if student_coverages else None),
        "source_kind": "model_candidate_labels_no_human_truth",
        "noncompleted_and_synthetic_excluded": len(annotations) - n_real,
    }
