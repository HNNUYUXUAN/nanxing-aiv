import numpy as np
import pytest
from aiv.metrics import *
from aiv.calibration import residual_mean, allocate_review


def test_axioms_and_transition_partition():
    rng=np.random.default_rng(26)
    for _ in range(100):
        a=rng.integers(1,7,20);p=distribution(a)
        assert 0<=transition_quality(a)<=1
        assert 0<=dhi(p)<=1 and 0<=dhi(p,asymmetric=True)<=1
        edges=np.diff(a)
        # Split edges with their boundary retained; weight by actual edge count.
        assert np.isclose(transition_quality(a),.5+sum(edges)/10/len(edges))
    assert transition_quality([3,3,3])==.5
    assert transition_quality([1,6])==1 and transition_quality([6,1])==0
    assert dhi(IDEAL)==1 and dhi(IDEAL,asymmetric=True)==1
    vertex=np.array([1,0,0,0,0,0])
    for asymmetric in (False,True):
        values=[dhi((1-t)*IDEAL+t*vertex,asymmetric=asymmetric) for t in np.linspace(0,1,20)]
        assert np.all(np.diff(values)<=1e-12)


def test_composite_monotonic_and_missing():
    base={n:.3 for n in NAMES}
    for weights in WEIGHTS:
        for n in NAMES:
            assert composite({**base,n:.7},weights)>=composite(base,weights)
        assert composite({n:1 for n in NAMES},weights)==pytest.approx(100)
    assert composite({**base,'MAB':None}) is None
    assert transition_quality([1,6],['a','b']) is None
    assert metrics([],[])['ABL'] is None
    with pytest.raises(ValueError):metrics([0],[])


def test_residual_census_exact_and_allocation_budget():
    y=np.array([0,1,1,0,1,1]);f=np.array([.9,.8,.7,.6,.5,.4])
    result=residual_mean(f,np.arange(6),y)
    assert result['estimate']==pytest.approx(y.mean()) and result['standard_error']==0
    alloc=allocate_review([.6,.4],[.4,.3],[1,2],30)
    assert alloc['cost']<=30 and alloc['feasible']


def test_duplicate_agent_and_uncertainty_degenerate():
    assert metrics([3,4],['a','a'])['MAB_raw']==1
    p=np.eye(6)[[2,3,4]]
    s=label_uncertainty(p,['a']*3,draws=10)
    assert len(set(s['scores']))==1


@pytest.mark.parametrize("missing", [None, "", "  ", "Unknown", " UNKNOWN ", np.nan])
def test_partial_tool_evidence_keeps_mab_and_full_score_missing(missing):
    result = metrics([2, 3], ["tool-a", missing], ["s", "s"])
    assert result["MAB_raw"] is None and result["MAB"] is None
    assert result["ABL"] == pytest.approx(.3) and result["CTQ"] == pytest.approx(.6)
    assert composite(result) is None


def test_complete_tool_keys_are_trimmed_without_inflating_breadth():
    result = metrics([2, 3, 4], [" tool-a ", "tool-a", "tool-b"], ["s"] * 3)
    assert result["MAB_raw"] == 2
    assert result["MAB"] == pytest.approx(np.log(3) / np.log(5))


@pytest.mark.parametrize("levels,agents,sessions", [
    ([2, 3], ["a"], None), ([2], ["a", "b"], None),
    ([], ["a"], None), ([2], ["a"], []), ([], [], ["s"]),
])
def test_label_tool_and_session_evidence_must_align(levels, agents, sessions):
    with pytest.raises(ValueError):
        metrics(levels, agents, sessions)


@pytest.mark.parametrize("unknown", ["", " ", "Unknown", np.nan])
def test_unknown_session_identity_does_not_create_an_edge(unknown):
    assert transition_quality([1, 6], [unknown, unknown]) is None
