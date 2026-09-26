"""Public teaching core: explicit definitions, empty-data abstention, fixed weights."""

import numpy as np

NAMES = ("ABL", "HOT", "CTQ", "DHI", "MAB")
WEIGHTS = {
    "balanced": np.array([0.20, 0.20, 0.20, 0.20, 0.20]),
    "higher_order": np.array([0.10, 0.50, 0.20, 0.15, 0.05]),
    "process": np.array([0.10, 0.20, 0.40, 0.25, 0.05]),
}
IDEAL = np.array([0.10, 0.15, 0.20, 0.25, 0.20, 0.10])


def levels_array(levels):
    a = np.asarray(levels, dtype=float)
    if (
        a.ndim != 1
        or np.any(~np.isfinite(a))
        or np.any((a < 1) | (a > 6) | (a != np.floor(a)))
    ):
        raise ValueError("Levels must be a one-dimensional sequence of integers 1..6")
    return a


def distribution(levels):
    a = levels_array(levels)
    return np.bincount(a.astype(int), minlength=7)[1:] / len(a) if len(a) else None


def _known_identifier(value):
    """Normalize an observed tool/session key; missing sentinels are unknown."""
    if value is None:
        return None
    text = str(value).strip()
    return None if text.casefold() in {"", "unknown", "nan", "<na>", "nat"} else text


def transition_quality(levels, sessions=None):
    a = levels_array(levels)
    if sessions is not None and (isinstance(sessions, str) or len(sessions) != len(a)):
        raise ValueError("Session length mismatch")
    if len(a) < 2:
        return None
    if sessions is not None:
        sessions = [_known_identifier(value) for value in sessions]
    mask = (
        np.ones(len(a) - 1, dtype=bool)
        if sessions is None
        else np.array(
            [
                x is not None and y is not None and x == y
                for x, y in zip(sessions[:-1], sessions[1:])
            ]
        )
    )
    if len(mask) != len(a) - 1:
        raise ValueError("Session length mismatch")
    edges = np.diff(a)[mask]
    if not len(edges):
        return None
    return float(0.5 + edges.mean() / 10)


def dhi(p, ideal=IDEAL, asymmetric=False):
    p = np.asarray(p, dtype=float)
    q = np.asarray(ideal, dtype=float)
    for a in (p, q):
        if (
            a.shape != (6,)
            or np.any(a < 0)
            or not np.all(np.isfinite(a))
            or not np.isclose(a.sum(), 1)
        ):
            raise ValueError("DHI needs two probability distributions of length six")
    if not asymmetric:
        return float(1 - np.abs(p - q).sum() / 2)

    # More probability below a cognitive cutoff receives twice the penalty.
    def loss(x):
        delta = np.cumsum(x - q)[:-1]
        return np.maximum(delta, 0).sum() * 2 + np.maximum(-delta, 0).sum()

    maximum = max(loss(vertex) for vertex in np.eye(6))
    return float(1 - loss(p) / maximum)


def metrics(levels, agents, sessions=None, ideal=IDEAL, asymmetric=False):
    a = levels_array(levels)
    if isinstance(agents, str) or len(agents) != len(a):
        raise ValueError("One tool identity per label is required")
    if sessions is not None and (isinstance(sessions, str) or len(sessions) != len(a)):
        raise ValueError("Session length mismatch")
    if not len(a):
        return {name: None for name in (*NAMES, "ABL_raw", "MAB_raw")}
    identities = [_known_identifier(value) for value in agents]
    tools_complete = all(value is not None for value in identities)
    mab = len(set(identities)) if tools_complete else None
    return {
        "ABL_raw": float(a.mean()),
        "ABL": float((a.mean() - 1) / 5),
        "HOT": float((a >= 4).mean()),
        "CTQ": transition_quality(a, sessions),
        "DHI": dhi(distribution(a), ideal, asymmetric),
        "MAB_raw": mab,
        "MAB": float(min(1, np.log1p(mab) / np.log(5))) if tools_complete else None,
    }


def composite(values, weights="balanced"):
    w = (
        WEIGHTS[weights]
        if isinstance(weights, str)
        else np.asarray(weights, dtype=float)
    )
    if (
        w.shape != (5,)
        or not np.all(np.isfinite(w))
        or np.any(w < 0)
        or not np.isclose(w.sum(), 1)
    ):
        raise ValueError("Five nonnegative normalized weights required")
    if any(values.get(name) is None and w[i] > 0 for i, name in enumerate(NAMES)):
        return None
    a = np.array([values.get(name) or 0 for name in NAMES], dtype=float)
    if np.any(~np.isfinite(a)) or np.any((a < 0) | (a > 1)):
        raise ValueError("Submetrics outside [0,1]")
    return float(100 * np.dot(a, w))


def perturb_weights(base, rng, size=200):
    w = np.asarray(base)
    draws = w * rng.uniform(0.9, 1.1, size=(size, 5))
    return draws / draws.sum(axis=1, keepdims=True)


def label_uncertainty(
    probabilities, agents, sessions=None, draws=300, seed=26, correlation=0
):
    p = np.asarray(probabilities, dtype=float)
    if (
        p.ndim != 2
        or p.shape[1] != 6
        or np.any(p < 0)
        or not np.all(np.isfinite(p))
        or not np.allclose(p.sum(axis=1), 1)
    ):
        raise ValueError("Expected n by six probability matrix")
    if not 0 <= correlation <= 1:
        raise ValueError("Dependence mixture must be in [0,1]")
    rng = np.random.default_rng(seed)
    scores = []
    for _ in range(draws):
        if correlation and rng.random() < correlation:
            u = rng.random()
            labels = [min(6, int(np.searchsorted(np.cumsum(row), u)) + 1) for row in p]
        else:
            labels = [rng.choice(np.arange(1, 7), p=row) for row in p]
        scores.append(composite(metrics(labels, agents, sessions)))
    if any(s is None for s in scores):
        return {"interval": None, "scores": [], "kind": "insufficient_data"}
    return {
        "interval": np.quantile(scores, [0.025, 0.5, 0.975]).tolist(),
        "scores": scores,
        "kind": "conditional_label_simulation_not_validated_confidence_interval",
        "dependence_mixture": correlation,
    }
