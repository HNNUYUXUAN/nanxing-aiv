import numpy as np
from aiv.metrics import label_uncertainty,transition_quality


def test_shared_uniform_keeps_deterministic_labels_and_unknown_sessions_abstain():
    p=np.eye(6)[[1,2,3,4]]
    independent=label_uncertainty(p,['a']*4,draws=30)
    shared=label_uncertainty(p,['a']*4,draws=30,correlation=1)
    assert independent['interval']==shared['interval']
    assert transition_quality([1,6],[None,None]) is None
