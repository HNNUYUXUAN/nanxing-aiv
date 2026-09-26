"""Independent analytic and enumerative checks for A--E model boundaries."""

from itertools import product
from pathlib import Path
import json

import numpy as np
import pytest

from aiv.models import (
    candidate_metric_intervals, ctq_on_verified_edges, decision_gate,
    identification_gate, normalized_weight_vertices,
    observational_nonidentification_witness, score_outer_bounds,
    stable_order, summarize_candidate_batch,
)


def test_same_observed_law_different_intervention_effect():
    witness = observational_nonidentification_witness()
    laws = []
    for world in witness["worlds"]:
        law = {}
        for row in world["rows"]:
            key = (row["treatment"], row["observed_outcome"])
            law[key] = law.get(key, 0) + row["probability"]
        laws.append(law)
    assert laws[0] == laws[1] == {(0, 0): 0.5, (1, 1): 0.5}
    assert [w["average_treatment_effect"] for w in witness["worlds"]] == [1, 0]
    assert identification_gate({})["status"] == "not_identified_from_available_evidence"
    assert identification_gate({"independent_learning_outcome": True})["missing_or_unsubstantiated"]


def test_verified_edges_and_split_boundary():
    levels = [2, 3, 4, 5]
    edges = [(0, 1), (1, 2), (2, 3)]
    assert ctq_on_verified_edges(levels, edges) == pytest.approx(.6)
    # Keeping all edges and edge-count weighting preserves the result.
    per_edge = [.5 + (levels[j] - levels[i]) / 10 for i, j in edges]
    assert ctq_on_verified_edges(levels, edges) == pytest.approx(sum(per_edge) / 3)
    # Removing the crossing edge is a change of estimand, not "split invariance".
    assert ctq_on_verified_edges([1, 6, 1], [(0, 1), (1, 2)]) == pytest.approx(.5)
    assert ctq_on_verified_edges([1, 6, 1], [(0, 1)]) == pytest.approx(1)
    assert ctq_on_verified_edges(levels, []) is None
    with pytest.raises(ValueError):
        ctq_on_verified_edges(levels, [(0, 1), (0, 1)])


def test_label_error_outer_bound_contains_every_one_change_scenario():
    original = [2, 4, 5]
    edges = [(0, 1), (1, 2)]
    outer = candidate_metric_intervals(original, tool_count=1,
                                       verified_edges=edges, max_changed_labels=1)
    for i, replacement in product(range(3), range(1, 7)):
        scenario = original.copy(); scenario[i] = replacement
        exact = candidate_metric_intervals(scenario, tool_count=1,
                                           verified_edges=edges, max_changed_labels=0)
        for name, (lo, hi) in outer["outer_intervals"].items():
            value = exact["point_conditional_on_candidates"][name]
            assert lo - 1e-12 <= value <= hi + 1e-12, (name, scenario, value)
    assert outer["outer_intervals"]["MAB"] == pytest.approx(
        [outer["point_conditional_on_candidates"]["MAB"]] * 2)


def test_missing_tool_and_edge_remain_unknown_not_zero():
    result = candidate_metric_intervals([2, 3], tool_count=None, verified_edges=[])
    assert result["point_conditional_on_candidates"]["CTQ"] is None
    assert result["point_conditional_on_candidates"]["MAB"] is None
    assert result["outer_intervals"]["CTQ"] == [0, 1]
    assert result["outer_intervals"]["MAB"] == [0, 1]
    verified_no_tools = candidate_metric_intervals([2], tool_count=0)
    assert verified_no_tools["point_conditional_on_candidates"]["MAB"] == 0
    with pytest.raises(ValueError):
        candidate_metric_intervals([1, 2], tool_count=None, max_changed_labels=3)


def test_weight_extrema_cover_dense_internal_grid_and_change_is_nontrivial():
    intervals = {"ABL": [.2, .3], "HOT": [.7, .8], "CTQ": [.1, .9],
                 "DHI": [.4, .5], "MAB": [0, 1]}
    exact = score_outer_bounds(intervals, "higher_order", relative_change=.1)
    base = np.array([.1, .5, .2, .15, .05])
    low = np.array([v[0] for v in intervals.values()])
    high = np.array([v[1] for v in intervals.values()])
    assert normalized_weight_vertices(base).shape == (32, 5)
    # Independent brute-force grid includes all corners and interior multipliers.
    for multipliers in product((.9, 1.0, 1.1), repeat=5):
        raw = base * np.array(multipliers)
        w = raw / raw.sum()
        assert exact["lower"] - 1e-12 <= 100 * np.dot(w, low)
        assert 100 * np.dot(w, high) <= exact["upper"] + 1e-12
    assert exact["upper"] > exact["lower"]
    assert stable_order({"lower": 70, "upper": 80}, {"lower": 20, "upper": 60}).startswith("first_")
    assert stable_order({"lower": 40, "upper": 60}, {"lower": 50, "upper": 70}) == "not_robustly_distinguishable"


def test_real_candidate_summary_is_not_ground_truth():
    path = Path(__file__).resolve().parents[1] / (
        "runtime/research/fast-strong-384-v3-20260926T121958Z/r3/annotations.json")
    if not path.exists():
        pytest.skip("Private 384-run fixture is not present in portable checkout")
    summary = summarize_candidate_batch(json.loads(path.read_text(encoding="utf-8")))
    assert summary["completed_real_turns"] == 384
    assert summary["candidate_task_turns"] == 187
    assert summary["task_status_counts"]["abstained"] == 167
    assert summary["candidate_level_counts"] == {
        "1": 72, "2": 94, "3": 3, "4": 3, "5": 0, "6": 15}
    assert summary["source_kind"] == "model_candidate_labels_no_human_truth"
    assert summary["by_term"]["25f"]["candidate_turns"] == 90
    assert summary["by_term"]["26s"]["candidate_turns"] == 97


def test_e_stage_requires_calibrated_complete_indicators_for_ranking():
    assert not decision_gate(candidate_coverage=.5, human_calibration=True,
                             identified_learning_effect=False,
                             missing_core_metric=False)["individual_aiv_ranking"]
    assert not decision_gate(candidate_coverage=1, human_calibration=False,
                             identified_learning_effect=False,
                             missing_core_metric=False)["individual_aiv_ranking"]
