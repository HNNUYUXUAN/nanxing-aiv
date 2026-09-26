"""Design-based residual correction; simulation and real labels stay distinct."""

import numpy as np
from scipy.stats import norm
from sklearn.metrics import cohen_kappa_score


def agreement(truth, predicted):
    a = np.asarray(truth)
    b = np.asarray(predicted)
    if a.shape != b.shape or a.ndim != 1 or len(a) < 2:
        return {"n": len(a), "accuracy": None, "kappa6": None, "kappa3": None}

    def safe_kappa(x, y):
        value = cohen_kappa_score(x, y)
        return float(value) if np.isfinite(value) else None

    return {
        "n": len(a),
        "accuracy": float(np.mean(a == b)),
        "kappa6": safe_kappa(a, b),
        "kappa3": safe_kappa((a - 1) // 2, (b - 1) // 2),
        "three_tier_mapping": "L1-2 / L3-4 / L5-6; distinct from HOT L4-6",
    }


def residual_mean(predictions, sampled_indices, true_values, strata=None):
    f = np.asarray(predictions, dtype=float)
    idx = np.asarray(sampled_indices, dtype=int)
    y = np.asarray(true_values, dtype=float)
    h = np.zeros(len(f), dtype=int) if strata is None else np.asarray(strata)
    if (
        len(idx) != len(y)
        or len(np.unique(idx)) != len(idx)
        or len(h) != len(f)
        or np.any(idx < 0)
        or np.any(idx >= len(f))
    ):
        raise ValueError("Invalid stratified sample")
    estimate = float(f.mean())
    variance = 0.0
    details = []
    for group in np.unique(h):
        N = int(np.sum(h == group))
        selected = h[idx] == group
        n = int(selected.sum())
        W = N / len(f)
        if n < min(2, N):
            return {
                "estimate": None,
                "interval": None,
                "reason": "each stratum needs two labels or a full census",
            }
        residual = y[selected] - f[idx[selected]]
        estimate += W * residual.mean()
        v = 0 if n == N else W**2 * (1 - n / N) * np.var(residual, ddof=1) / n
        variance += v
        details.append({"stratum": str(group), "N": N, "n": n, "weight": W})
    se = float(np.sqrt(variance))
    return {
        "estimate": float(estimate),
        "standard_error": se,
        "interval": [estimate - 1.96 * se, estimate + 1.96 * se],
        "strata": details,
        "assumptions": "fixed predictions; within-stratum simple random sampling; normal approximation; not clustered records",
    }


def wilson_interval(correct, total, alpha=0.05):
    if total == 0:
        return None
    z = norm.ppf(1 - alpha / 2)
    p = correct / total
    den = 1 + z * z / total
    center = (p + z * z / (2 * total)) / den
    radius = z * np.sqrt(p * (1 - p) / total + z * z / (4 * total**2)) / den
    return [float(center - radius), float(center + radius)]


def joint_error(truth, predictions):
    a = np.asarray(predictions)
    truth = np.asarray(truth)
    if a.ndim != 2 or a.shape[1] != len(truth):
        raise ValueError("models by cases required")
    return float(np.mean(np.all(a != truth[None, :], axis=0)))


def allocate_review(weights, residual_sd, costs, budget, minimum=2):
    w = np.asarray(weights, float)
    sd = np.asarray(residual_sd, float)
    c = np.asarray(costs, float)
    if np.any(c <= 0) or not (len(w) == len(sd) == len(c)):
        raise ValueError("Invalid strata")
    n = np.full(len(w), minimum, dtype=int)
    if np.dot(n, c) > budget:
        return {
            "feasible": False,
            "allocation": n.tolist(),
            "cost": float(np.dot(n, c)),
        }
    while True:
        gain = w * w * sd * sd / (n * (n + 1) * c)
        possible = np.dot(n, c) + c <= budget
        if not possible.any():
            break
        gain[~possible] = -1
        j = int(np.argmax(gain))
        n[j] += 1
    return {
        "feasible": True,
        "allocation": n.tolist(),
        "cost": float(np.dot(n, c)),
        "scope": "variance allocation under independent stratified sampling; does not guarantee kappa threshold",
    }
