import numpy as np
import pandas as pd
import pytest

from aiv.analysis import student_scores


def sequence(labels, *, sessions=None, timestamps=None, agents=None, terms=None):
    n = len(labels)
    return [{"student": "fixture", "label": label,
             "timestamp": (timestamps or [f"2026-01-{i+1:02d}" for i in range(n)])[index],
             "session": (sessions or ["verified"] * n)[index],
             "agent": (agents or ["tool-a"] * n)[index],
             "term": (terms or ["synthetic"] * n)[index]}
            for index, label in enumerate(labels)]


def test_abstention_between_two_candidates_does_not_create_an_edge_or_aiv():
    result = student_scores(sequence([1, None, 6])).iloc[0]
    assert result["n"] == 2 and result["ABL_raw"] == 3.5
    assert result["CTQ"] is None and result["balanced"] is None


def test_only_original_labeled_adjacencies_contribute_to_ctq():
    # The only usable original edge is 6 -> 1. Deleting None would add 1 -> 6.
    result = student_scores(sequence([1, None, 6, 1])).iloc[0]
    assert result["CTQ"] == 0
    assert result["n"] == 3


@pytest.mark.parametrize("kwargs", [
    {"sessions": ["a", "b"]},
    {"sessions": ["Unknown", "Unknown"]},
    {"timestamps": ["2026-01-01", "2026-01-01"]},
    {"timestamps": ["2026-01-01", None]},
    {"terms": ["25f", "26s"]},
])
def test_unknown_or_nonadjacent_sequence_boundaries_keep_ctq_missing(kwargs):
    result = student_scores(sequence([1, 6], **kwargs)).iloc[0]
    assert result["CTQ"] is None and result["balanced"] is None


def test_tied_time_records_cannot_create_arbitrarily_ordered_neighbor_edges():
    records = sequence([1, 2, 5, 6], timestamps=["2026-01-01", "2026-01-02", "2026-01-02", "2026-01-03"])
    assert student_scores(records).iloc[0]["CTQ"] is None


def test_complete_synthetic_sequence_retains_known_scores_when_input_is_unsorted():
    result = student_scores(list(reversed(sequence([2, 3, 4, 5])))).iloc[0]
    expected = np.array([.5, .5, .6, .8, np.log(2) / np.log(5)])
    assert np.array([result[name] for name in ("ABL", "HOT", "CTQ", "DHI", "MAB")]) == pytest.approx(expected)
    assert result["balanced"] == pytest.approx(100 * expected.mean())
    assert result["higher_order"] == pytest.approx(100 * expected @ np.array([.1, .5, .2, .15, .05]))
    assert result["process"] == pytest.approx(100 * expected @ np.array([.1, .2, .4, .25, .05]))


def test_partial_candidate_tool_evidence_blocks_aiv_but_not_other_metrics():
    result = student_scores(sequence([2, 3], agents=["tool-a", pd.NA])).iloc[0]
    assert result["MAB"] is None and result["MAB_raw"] is None
    assert result["balanced"] is None and result["CTQ"] == pytest.approx(.6)
