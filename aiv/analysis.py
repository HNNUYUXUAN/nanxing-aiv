"""Common demo/notebook calculations; simulated outcomes are explicitly marked."""

import numpy as np
import pandas as pd
from .metrics import metrics, composite, WEIGHTS, perturb_weights, _known_identifier


def student_scores(records):
    """Score complete teaching sequences; sampled source rows need explicit edges.

    Retain unlabeled rows as adjacency breaks. Session keys and the completeness
    of the supplied sequence are caller prerequisites, not source verification.
    """
    frame = pd.DataFrame(records)
    rows = []
    for student, group in frame.groupby("student", sort=True):
        ordered = group.assign(_aiv_time=pd.to_datetime(group.timestamp, errors="coerce", utc=True))
        ordered = ordered.sort_values("_aiv_time", kind="stable")
        valid = ordered[ordered.label.notna()]
        if valid.empty:
            continue
        # Never construct transitions on the candidate-filtered sequence.
        values = metrics(
            valid.label.tolist(), valid.agent.tolist(), [None] * len(valid)
        )
        deltas = []
        if ordered._aiv_time.notna().all():
            unambiguous = ~ordered._aiv_time.duplicated(keep=False)
            labels = ordered.label.tolist()
            sessions = [_known_identifier(value) for value in ordered.session]
            terms = ([_known_identifier(value) for value in ordered.term]
                     if "term" in ordered else None)
            for index in range(len(ordered) - 1):
                if (pd.notna(labels[index]) and pd.notna(labels[index + 1])
                        and unambiguous.iloc[index] and unambiguous.iloc[index + 1]
                        and sessions[index] is not None and sessions[index] == sessions[index + 1]
                        and (terms is None or terms[index] is not None and terms[index] == terms[index + 1])):
                    deltas.append(float(labels[index + 1]) - float(labels[index]))
        values["CTQ"] = float(0.5 + np.mean(deltas) / 10) if deltas else None
        rows.append(
            {
                "student": student,
                "n": len(valid),
                **values,
                **{name: composite(values, name) for name in WEIGHTS},
            }
        )
    return pd.DataFrame(rows)


def weight_sensitivity(table, seed=26, draws=200):
    from .metrics import NAMES

    values = table[list(NAMES)].dropna()
    if values.empty:
        return pd.DataFrame()
    w = perturb_weights(WEIGHTS["balanced"], np.random.default_rng(seed), draws)
    scores = values.to_numpy() @ w.T * 100
    ranks = pd.DataFrame(scores).rank(ascending=False, method="average").to_numpy()
    return pd.DataFrame(
        {
            "student": table.loc[values.index, "student"].to_numpy(),
            "score_low": np.quantile(scores, 0.025, axis=1),
            "score_high": np.quantile(scores, 0.975, axis=1),
            "rank_low": np.quantile(ranks, 0.025, axis=1),
            "rank_high": np.quantile(ranks, 0.975, axis=1),
        }
    )


def causal_simulation(seed=26, n=1000):
    import statsmodels.api as sm

    rng = np.random.default_rng(seed)
    ability = rng.normal(size=n)
    treatment = rng.binomial(1, 1 / (1 + np.exp(-ability)))
    outcome = 0.4 * treatment + 0.8 * ability + rng.normal(size=n)
    rows = []
    for name, x in [
        ("naive", treatment[:, None]),
        ("adjusted", np.column_stack([treatment, ability])),
    ]:
        fit = sm.OLS(outcome, sm.add_constant(x)).fit(cov_type="HC3")
        rows.append(
            {
                "model": name,
                "estimate": float(fit.params[1]),
                "lower": float(fit.conf_int()[1, 0]),
                "upper": float(fit.conf_int()[1, 1]),
                "p": float(fit.pvalues[1]),
                "true_effect": 0.4,
                "source": "synthetic_known_truth",
            }
        )
    return pd.DataFrame(rows)


def redteam():
    base = [2, 3, 4, 5]
    agents = ["a"] * 4
    scenarios = [
        ("基线", base, agents, ["s"] * 4),
        ("重复整段", base * 3, agents * 3, ["s"] * 12),
        ("逐轮复制", [x for x in base for _ in range(3)], agents * 3, ["s"] * 12),
        ("工具堆叠", base, ["a", "b", "c", "d"], ["s"] * 4),
        ("分段操纵", base, agents, ["a", "b", "c", "d"]),
        ("真实改善对照", [3, 4, 5, 6], agents, ["s"] * 4),
    ]
    rows = []
    for name, l, a, s in scenarios:
        values = metrics(l, a, s)
        rows.append(
            {
                "scenario": name,
                **values,
                "score": composite(values),
                "guarded_score": composite(values, [0.25, 0.25, 0.25, 0.25, 0]),
            }
        )
    table = pd.DataFrame(rows)
    table["delta"] = table.score - table.score.iloc[0]
    table["guarded_delta"] = table.guarded_score - table.guarded_score.iloc[0]
    return table


def educational_report(records, role="student"):
    scores = student_scores(records)
    return {
        "role": role,
        "source": "synthetic"
        if all(r.get("term") == "synthetic" for r in records)
        else "private_observational",
        "records": len(records),
        "students": len(scores),
        "judgment": "现有日志可描述任务要求与过程证据；独立学习成效增量不可判断。",
        "action": {
            "student": "先独立解释一个概念，再请求反例，最后写下你采纳或拒绝建议的理由。",
            "teacher": "安排共同锚定任务和无AI迁移题，对模型分歧样本实施盲审。",
            "administrator": "补采处理前基础、工具可用范围、会话边界和独立后测；避免直接跨课程排名。",
        }[role],
        "uncertainty": "模型概率未校准；合成样本只用于教学，真实数据缺失字段不以零代替。",
    }
