"""Pure, source-scoped product reports; never read credentials or run models."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import csv
import hashlib
import html
import io
import itertools
import json
import math
import re
from urllib.parse import quote
import zipfile

import numpy as np

from .metrics import IDEAL, NAMES, WEIGHTS, composite, metrics


SCHEMA = "workbench-product-report-v1"
DIMENSIONS = {"task": "level", "contribution": "contribution_level"}
GROUPS = {"independent": "independent", "random_audit": "review", "high_risk": "review", "repeat": "repeat"}
STATES = ("agreed", "abstained", "disagreement", "uncertain", "technical_failure", "unfinished")
LIMITS = [
    "层级为模型候选，不是独立人工真值；模型一致性不等于准确率。",
    "任务要求与学生已展示贡献分别报告；未观察到贡献不代表没有能力。",
    "现有日志不足以识别撤去 AI 后的学习增量或因果效应。",
    "会话候选不等于已核验相邻边；缺失指标保留空值，不补零或自动重分配权重。",
    "不同学期、课程和工具机会尚未建立共同量尺，不作真实学生总分排名。",
]


def _clean(value):
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, np.generic):
        value = value.item()
    return None if isinstance(value, float) and not math.isfinite(value) else value


def _mode(context):
    value = context.get("mode") or context.get("job", {}).get("mode")
    if value == "cache_replay" or not value and context.get("dataset", {}).get("kind") == "synthetic":
        return "synthetic"
    return value or "live"


def _rows(context):
    return [r for r in context.get("rows", [])
            if _mode(context) == "synthetic" or r.get("record", {}).get("term") != "synthetic"]


def _complete(row):
    return bool(row.get("complete", row.get("status") == "done"))


def _level(value):
    return int(value) if isinstance(value, (int, np.integer)) and not isinstance(value, bool) and 1 <= value <= 6 else None


def _final(row, dimension):
    if not _complete(row):
        return {"label": None, "status": "unfinished"}
    final = ((row.get("dimensions") or {}).get(dimension) or {}).get("final") or {}
    label = _level(final.get("label"))
    if not final and dimension == "task":
        label = _level(row.get("final"))
    status = final.get("status") or ("agreed" if label is not None else "abstained")
    # A disputed, failed or uncertain terminal output is not an accepted candidate.
    if status != "agreed":
        label = None
    return {"label": label, "status": status, "source": final.get("source")}


def _metadata(context):
    dataset, job, plan = (context.get(k) or {} for k in ("dataset", "job", "plan"))
    extra = context.get("metadata") or {}
    result = {"schema": SCHEMA, "mode": _mode(context), "dataset_id": dataset.get("id"),
              "dataset_name": dataset.get("name"), "job_id": job.get("id"),
              "experiment": plan.get("experiment"), "revision": plan.get("revision"),
              "version": plan.get("version"), "sample_sha256": plan.get("sample_sha256"),
              "rubric_sha256": plan.get("rubric_sha256"),
              "source_scope": plan.get("source_manifest_scope"),
              "source": "synthetic_teaching" if _mode(context) == "synthetic" else "model_candidate_records"}
    for key in ("snapshot_manifest_sha256", "materials_manifest_sha256", "frozen_at", "captured_at", "source_scope"):
        result[key] = extra.get(key, context.get(key, result.get(key)))
    for key in ("selected_role", "selection", "selection_sha256", "application_url"):
        if key in extra:
            result[key] = extra[key]
    return result


def _planned(context, rows):
    value = context.get("plan", {}).get("real")
    if _mode(context) != "synthetic" and isinstance(value, int):
        return max(value, len(rows))
    return len(rows)


def _judgment(entry):
    value = entry.get("judgment") if entry else None
    return value if isinstance(value, dict) and entry.get("status") in (None, "done") else None


def _entry(row, stage, model):
    return next((e for e in row.get(stage, []) if e.get("model") == model), {})


def _models(context, rows, stage):
    role = "review" if stage == "review" else "fast"
    planned = [m.get("id") for m in context.get("plan", {}).get("models", []) if m.get("role") == role]
    observed = [e.get("model") for r in rows for e in r.get(stage, [])]
    return list(dict.fromkeys(m for m in planned + observed if m))


def _kappa(matrix):
    n = int(matrix.sum())
    if not n:
        return None
    chance = float(np.dot(matrix.sum(axis=0), matrix.sum(axis=1)) / n ** 2)
    return float((np.trace(matrix) / n - chance) / (1 - chance)) if chance < 1 - 1e-12 else None


def _pair(rows, model_a, model_b, stage_a, stage_b, field, expected):
    matrix = np.zeros((6, 6), dtype=int)
    valid = both_null = one_null = equal_all = 0
    for row in rows:
        a, b = (_judgment(_entry(row, s, m)) for s, m in ((stage_a, model_a), (stage_b, model_b)))
        if a is None or b is None or field not in a or field not in b:
            continue
        # Only null or valid six-level values belong to the declared outcome space.
        if any(j[field] is not None and _level(j[field]) is None for j in (a, b)):
            continue
        x, y = a[field], b[field]
        valid += 1
        equal_all += x == y
        if x is None and y is None:
            both_null += 1
        elif x is None or y is None:
            one_null += 1
        else:
            matrix[x - 1, y - 1] += 1
    n = int(matrix.sum())
    three = matrix.reshape(3, 2, 3, 2).sum(axis=(1, 3))
    repeat = stage_a != stage_b
    return dict(a=f"{model_a} · 基础" if repeat else model_a,
                b=f"{model_b} · 复跑" if repeat else model_b,
                model=model_a if repeat else None, planned_pairs=expected,
                both_valid=valid, both_nonnull=n, n=n, both_abstained=both_null,
                one_abstained=one_null, unavailable_pairs=max(0, expected - valid),
                agreement=float(np.trace(matrix) / n) if n else None,
                agreement_including_abstention=equal_all / valid if valid else None,
                kappa6=_kappa(matrix), kappa3=_kappa(three), matrix6=matrix.tolist(), matrix3=three.tolist())


def compare(context, dimension="task", group="independent"):
    if dimension not in DIMENSIONS or group not in GROUPS:
        raise ValueError("未知的比较维度或请求集合")
    all_rows, plan = _rows(context), context.get("plan") or {}
    field, stage = DIMENSIONS[dimension], GROUPS[group]
    if group in ("random_audit", "repeat"):
        ids = set(plan.get("audit_ids" if group == "random_audit" else "repeat_ids", []))
        rows = [r for r in all_rows if r.get("record", {}).get("id") in ids
                or (r.get("review_selection") == group if group == "random_audit" else bool(r.get("repeat")))]
        # Explicit selection IDs preserve tasks which have not yet been created.
        control_ids = {r.get("record", {}).get("id") for r in context.get("rows", [])
                       if _mode(context) != "synthetic" and r.get("record", {}).get("term") == "synthetic"}
        expected = len((ids - control_ids) | {r.get("record", {}).get("id") for r in rows})
    elif group == "high_risk":
        rows = [r for r in all_rows if r.get("review_selection") == "high_risk"]
        expected = len(rows)
    else:
        rows, expected = all_rows, _planned(context, all_rows)
    models = _models(context, rows, stage)
    stats = []
    for model in models:
        entries = [_entry(r, stage, model) for r in rows]
        judgments = [j for e in entries if (j := _judgment(e)) is not None and field in j
                     and (j[field] is None or _level(j[field]) is not None)]
        levels = [j[field] for j in judgments]
        failed = sum(e.get("status") == "failed" for e in entries)
        terminal = sum(e.get("status") in ("done", "failed") or bool(e) and e.get("status") is None for e in entries)
        stats.append(dict(model=model, family=next((e.get("family") for e in entries if e.get("family")), model),
                          expected=expected, created=sum(bool(e) for e in entries), answered=len(judgments),
                          labelled=sum(v is not None for v in levels), abstained=levels.count(None), failed=failed,
                          pending=max(0, expected - terminal), invalid=max(0, terminal - failed - len(judgments)),
                          distribution=[levels.count(i) for i in range(1, 7)]))
    pairs = ([_pair(rows, m, m, "independent", "repeat", field, expected) for m in models]
             if group == "repeat" else
             [_pair(rows, a, b, stage, stage, field, expected) for a, b in itertools.combinations(models, 2)])
    finals = [_final(r, dimension) for r in rows]
    labels = [v["label"] for v in finals if v["label"] is not None]
    states = dict(Counter(v["status"] for v in finals))
    states["unfinished"] = states.get("unfinished", 0) + max(0, expected - len(rows))
    denominators = dict(planned_records=expected, observed_records=len(rows),
                        completed_records=sum(_complete(r) for r in rows),
                        expected_requests=expected * len(models), created_requests=sum(s["created"] for s in stats),
                        valid_outputs=sum(s["answered"] for s in stats), failed_requests=sum(s["failed"] for s in stats),
                        pending_requests=sum(s["pending"] for s in stats))
    result = dict(dimension=dimension, group=group, metadata=_metadata(context), total=expected,
                  denominator=expected, labelled=len(labels), models=stats, pairs=pairs,
                  final_distribution=[labels.count(i) for i in range(1, 7)], states=states,
                  denominators=denominators,
                  missing=["没有双非空有效配对时，一致率与 κ 保留空值。", "κ 只使用双方非空标签；共同弃权单独报告。"],
                  scope="所选请求集合的条件一致性，不是准确率；随机与高风险复核不混算，复跑不增加独立学生样本。",
                  weights={}, students=[], sensitivity=[])
    if _mode(context) == "synthetic":
        result["weights"] = {k: v.tolist() for k, v in WEIGHTS.items()}
        result["students"] = _student_rows(context, all_rows)
        result["sensitivity"] = _synthetic_sensitivity(result["students"])
    return _clean(result)


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def _student_rows(context, rows):
    grouped = defaultdict(list)
    for row in rows:
        r = row.get("record", {})
        grouped[(str(r.get("student") or "未分配"), str(r.get("term") or "未提供"))].append(row)
    official = {(str(r.get("student")), str(r.get("term"))): r for r in context.get("student_indicators", [])}
    output = []
    for (student, term), items in sorted(grouped.items()):
        candidates = [r for r in items if _final(r, "task")["label"] is not None]
        labels = [_final(r, "task")["label"] for r in candidates]
        tools = [r.get("record", {}).get("agent") for r in candidates]
        values = metrics(labels, tools, [None] * len(labels))
        values["CTQ"] = None
        lookup = {r.get("record", {}).get("id"): r for r in items}
        deltas = []
        seen_edges = set()
        for edge in context.get("verified_edges", []):
            a, b = lookup.get(edge.get("from")), lookup.get(edge.get("to"))
            pair = (edge.get("from"), edge.get("to"))
            if edge.get("verified") is not True or not a or not b or a is b or pair in seen_edges:
                continue
            seen_edges.add(pair)
            x, y = _final(a, "task")["label"], _final(b, "task")["label"]
            if x is not None and y is not None:
                deltas.append(y - x)
        frozen_t3 = bool(official) or (_mode(context) == "frozen" and
                                     (context.get("plan", {}).get("revision") == "t3" or context.get("dataset", {}).get("id") == "t3"))
        if not frozen_t3 and deltas:
            values["CTQ"] = 0.5 + float(np.mean(deltas)) / 10
        elif _mode(context) == "synthetic":
            # Synthetic records carry complete constructed sequences; unknown or
            # unlabelled rows break adjacency rather than being filtered away.
            ordered = sorted(items, key=lambda r: (str(r.get("record", {}).get("timestamp", "")),
                                                   r.get("record", {}).get("turn_index", 0)))
            for a, b in zip(ordered, ordered[1:]):
                ar, br = a["record"], b["record"]
                session_a = ar.get("session") or ar.get("session_candidate_id")
                session_b = br.get("session") or br.get("session_candidate_id")
                x, y = _final(a, "task")["label"], _final(b, "task")["label"]
                if session_a and session_a == session_b and x is not None and y is not None:
                    deltas.append(y - x)
            values["CTQ"] = 0.5 + float(np.mean(deltas)) / 10 if deltas else None
        source = official.get((student, term))
        if source:
            for name in (*NAMES, "ABL_raw", "MAB_raw"):
                values[name] = _number(source.get(name))
            values["CTQ"] = None
        scores = {name: composite(values, name) if not frozen_t3 else None for name in WEIGHTS}
        n, completed = len(items), sum(_complete(r) for r in items)
        contributions = sum(_final(r, "contribution")["label"] is not None for r in items)
        missing = [name for name in NAMES if values[name] is None]
        tips = []
        if not labels:
            tips.append("当前没有任务候选标签，先核对原文与弃权或失败原因。")
        if contributions < n:
            tips.append("部分回合没有可见贡献候选；补充学生独立解释与修改理由，不能据此断言能力不足。")
        if "CTQ" in missing:
            tips.append("相邻回合尚未核验，过程连续性与完整 AIV 暂不可判断。")
        if "MAB" in missing:
            tips.append("候选集合的工具身份不完整，补齐来源后再描述工具广度。")
        output.append(dict(student=student, term=term, n=n, completed=completed,
                           task_candidates=len(labels), contribution_candidates=contributions,
                           coverage=len(labels) / n if n else None, **values, **scores,
                           AIV=scores["balanced"], rank=None, missing=missing, tips=tips,
                           missing_reason=source.get("missing_reason") if source else ";".join(missing),
                           metric_source="official_student_indicators" if source else "selected_annotation_rows"))
    return output


def _synthetic_sensitivity(students):
    from .analysis import weight_sensitivity
    import pandas as pd
    if not students:
        return []
    return weight_sensitivity(pd.DataFrame(students)).to_dict("records")


def _link(row):
    r = row.get("record", {})
    return {"id": r.get("id"), "student": r.get("student"), "term": r.get("term"),
            "href": "#trace?item=" + quote(str(r.get("id") or ""), safe="")}


def _evidence(row):
    r, task, contribution = row.get("record", {}), _final(row, "task"), _final(row, "contribution")
    evidence = dict(task="", contribution="")
    text = r.get("question") or ""
    for stage in ("review", "independent"):
        for entry in row.get(stage, []):
            j = _judgment(entry)
            if not j:
                continue
            for dimension, field in (("task", "evidence"), ("contribution", "contribution_evidence")):
                snippet = j.get(field)
                candidate = task if dimension == "task" else contribution
                if (candidate["label"] is not None and j.get(DIMENSIONS[dimension]) == candidate["label"]
                        and not evidence[dimension] and isinstance(snippet, str) and snippet and snippet in text):
                    evidence[dimension] = snippet
    source = r.get("source") if isinstance(r.get("source"), dict) else {}
    return dict(_link(row), question=text, task=task["label"], contribution=contribution["label"],
                task_status=task["status"], contribution_status=contribution["status"],
                task_evidence=evidence["task"], contribution_evidence=evidence["contribution"],
                source={k: source.get(k) for k in ("file", "row", "sha256", "char_start", "char_end")})


def _confirmations(context, rows):
    ids = {r.get("record", {}).get("id") for r in rows}
    return [dict(c) for c in context.get("confirmations", [])
            if (c.get("turn_id") or c.get("item_id") or c.get("item") or c.get("record_id")) in ids]


def _comparability(context):
    evidence = context.get("comparability") or {}
    fields = [("common_tasks", "共同锚定任务", "记录共同题目、任务难度和评分量尺"),
              ("baseline", "处理前基础与学生构成", "补采干预前独立基础测量与群体构成"),
              ("tool_opportunity", "课程与工具开放机会", "核对课程内容、可用工具及开放时间"),
              ("session_edges", "会话与时间顺序", "核验会话相邻边、AI 介入及修改时间点"),
              ("independent_outcomes", "无 AI 独立结局与可比对照", "安排无 AI 前后测、迁移题及可比对照")]
    return [dict(key=key, label=label, status="已核验" if evidence.get(key, {}).get("verified") is True else "待补采",
                 reason=evidence.get(key, {}).get("reason") or "当前批次未提供已核验依据。", next_step=step)
            for key, label, step in fields]


def _action(text, rows, condition, reconsider):
    return dict(text=text, evidence=[_link(r) for r in rows[:3]], condition=condition, reconsider=reconsider)


def report(context, role="teacher", student="", term=""):
    if role not in ("student", "teacher", "administrator"):
        raise ValueError("未知报告角色")
    all_rows = _rows(context)
    terms = sorted({str(r.get("record", {}).get("term") or "未提供") for r in all_rows})
    available = [r for r in all_rows if not term or str(r.get("record", {}).get("term")) == term]
    students = sorted({str(r.get("record", {}).get("student") or "未分配") for r in available})
    selected_student = student or (students[0] if role == "student" and students and not context.get("report_all_students") else "")
    rows = [r for r in available if not selected_student or str(r.get("record", {}).get("student") or "未分配") == selected_student]
    student_rows = _student_rows(context, rows)
    total = _planned(context, rows) if not term and not selected_student else len(rows)
    completed = sum(_complete(r) for r in rows)
    dimensions, distributions = {}, {}
    for dimension in DIMENSIONS:
        finals = [_final(r, dimension) for r in rows]
        levels = [f["label"] for f in finals if f["label"] is not None]
        states = Counter(f["status"] for f in finals)
        states["unfinished"] += max(0, total - len(rows))
        dimensions[dimension] = {"candidate": len(levels), "coverage": len(levels) / total if total else None,
                                 "states": {s: states[s] for s in STATES}}
        distributions[dimension] = [levels.count(k) for k in range(1, 7)]
    summary = dict(total=total, completed=completed, unfinished=max(0, total - completed),
                   task_candidates=dimensions["task"]["candidate"],
                   contribution_candidates=dimensions["contribution"]["candidate"],
                   student_terms=len(student_rows), coverage=dimensions["task"]["coverage"],
                   contribution_coverage=dimensions["contribution"]["coverage"],
                   CTQ_available=sum(s["CTQ"] is not None for s in student_rows),
                   MAB_available=sum(s["MAB"] is not None for s in student_rows),
                   AIV_available=sum(s["AIV"] is not None for s in student_rows), rank_available=0)
    concern = [r for r in rows if any(_final(r, d)["status"] != "agreed" for d in DIMENSIONS)]
    concern_ids = {id(r) for r in concern}
    examples = concern + [r for r in rows if id(r) not in concern_ids]
    if role == "student":
        no_contribution = [r for r in rows if _complete(r) and _final(r, "contribution")["label"] is None]
        different = [r for r in rows if _final(r, "task")["label"] is not None
                     and _final(r, "contribution")["label"] is not None
                     and _final(r, "task")["label"] != _final(r, "contribution")["label"]]
        actions = [_action("先独立解释一个概念或写出解题理由，再对照 AI 建议，记录采纳或拒绝的依据。",
                           no_contribution or examples,
                           f"当前 {completed} 个已结算回合中，{len(no_contribution)} 个没有贡献候选；这不代表没有能力。",
                           "获得独立作答与修订证据后，重新核对贡献判断。")]
        if different:
            actions.append(_action("对照请求 AI 完成的操作与自己已经展示的操作，分别说明两者的证据。", different,
                                   f"{len(different)} 个回合的任务与贡献候选层级不同；二者测量对象不同。",
                                   "原文或判断规则变化时重新核对，不把两层级之差当能力缺口。"))
    elif role == "teacher":
        actions = [_action("抽查证据不足、模型分歧和技术失败的回合，结合学生修改过程核对。", examples,
                           f"所选范围 {total} 个回合中形成 {summary['task_candidates']} 个任务候选；{len(concern)} 个回合至少一维需核查或尚未结算。",
                           "原文无法定位或候选判断被撤回时，撤回对应个体诊断。"),
                   _action("安排共同锚定任务和无 AI 迁移题，再评估学习变化。", rows,
                           "日志分布只能支持当前范围的过程诊断。", "无可比任务或独立结局时，保留学习增量不可判断。")]
    else:
        actions = [_action("优先补齐共同任务、处理前基础、工具机会与独立结局，再考虑跨群体评价。", rows,
                           f"本批 {summary['student_terms']} 个学生×学期单位，{summary['CTQ_available']} 个可计算 CTQ；当前分组描述不自动满足可比条件。",
                           "任何可比性条件失效时，停止效果比较并保留分组描述。")]
    meta = _metadata(context)
    stamp = meta.get("captured_at") or meta.get("frozen_at") or context.get("job", {}).get("updated") or context.get("job", {}).get("created")
    generated = datetime.fromtimestamp(stamp, timezone.utc).isoformat() if isinstance(stamp, (int, float)) else str(stamp or "未提供时间")
    name = context.get("dataset", {}).get("name") or context.get("job", {}).get("dataset_name") or "当前批次"
    return _clean(dict(role=role, ready=bool(rows), message="当前范围还没有可查看的回合。" if not rows else "",
                       dataset=name, generated=generated, metadata=meta,
                       scope={"student": selected_student, "term": term,
                              "description": "所选批次内的模型候选过程证据；不是学生能力或因果增量。"},
                       options={"students": students, "terms": terms}, summary=summary, dimensions=dimensions,
                       distributions=distributions,
                       headline=f"所选范围 {total} 个回合，已结算 {completed} 个；任务候选 {summary['task_candidates']} 个，贡献候选 {summary['contribution_candidates']} 个。",
                       rows=student_rows, attention=student_rows[:10], students=student_rows,
                       evidence=[_evidence(r) for r in examples[:10]], actions=actions,
                       comparability=_comparability(context), confirmations=_confirmations(context, rows),
                       limits=(["本批为合成教学材料，不代表真实学生。"] if _mode(context) == "synthetic" else []) + LIMITS))


def results_csv(context):
    """Private student-by-term measures without question text; blanks retain missingness."""
    records = _student_rows(context, _rows(context))
    keys = ["student", "term", "n", "completed", "task_candidates", "contribution_candidates", "coverage",
            "ABL_raw", "ABL", "HOT", "CTQ", "DHI", "MAB_raw", "MAB", "AIV", "balanced", "higher_order", "process", "rank",
            "missing_reason", "metric_source", "snapshot_manifest_sha256", "sample_sha256"]
    meta = _metadata(context)
    values = [{**row, **{k: meta.get(k) for k in ("snapshot_manifest_sha256", "sample_sha256")}} for row in records]
    return _csv(values, keys)


def _csv(rows, keys):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=keys, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        safe = dict(row)
        # Spreadsheet software must not interpret imported identifiers as formulas.
        for key, value in safe.items():
            if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
                safe[key] = "'" + value
        writer.writerow(safe)
    return "\ufeff" + stream.getvalue()


def _turn_results_csv(context):
    rows = list(_rows(context))
    known = {r.get("record", {}).get("id") for r in rows}
    rows.extend({"record": r, "complete": False} for r in context.get("dataset", {}).get("records", [])
                if r.get("id") not in known and (_mode(context) == "synthetic" or r.get("term") != "synthetic"))
    keys = ["id", "student", "term", "complete", "task_status", "task_label", "contribution_status", "contribution_label",
            "independent_valid", "independent_failed", "review_valid", "review_failed", "repeat_valid", "repeat_failed",
            "source_file", "source_row", "source_sha256"]
    output = []
    for row in rows:
        record = row.get("record", {})
        source = record.get("source") if isinstance(record.get("source"), dict) else {}
        item = {**{k: record.get(k) for k in ("id", "student", "term")}, "complete": _complete(row),
                **{f"source_{k}": source.get(k) for k in ("file", "row", "sha256")}}
        for dimension in DIMENSIONS:
            value = _final(row, dimension)
            item[f"{dimension}_status"], item[f"{dimension}_label"] = value["status"], value["label"]
        for stage in ("independent", "review", "repeat"):
            item[f"{stage}_valid"] = sum(_judgment(e) is not None for e in row.get(stage, []))
            item[f"{stage}_failed"] = sum(e.get("status") == "failed" for e in row.get(stage, []))
        output.append(item)
    return _csv(output, keys)


def _json_bytes(value):
    return (json.dumps(_clean(value), ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _export_confirmation(item):
    keys = ("id", "job", "turn_id", "item_id", "item", "record_id", "dimension", "decision", "operator", "created", "created_at", "timestamp",
            "source_sha256", "model_output_sha256", "experiment", "revision", "note")
    value = {k: item[k] for k in keys if k in item}
    if item.get("note"):
        value["note_sha256"] = hashlib.sha256(str(item["note"]).encode("utf-8")).hexdigest()
    return value


def _without_text(value):
    if isinstance(value, list):
        return [_without_text(v) for v in value]
    if isinstance(value, dict):
        return {k: ([_export_confirmation(c) for c in v] if k == "confirmations" else _without_text(v))
                for k, v in value.items() if k not in ("question", "task_evidence", "contribution_evidence", "raw_qa", "quote")}
    return value


def _report_html(value):
    esc = lambda v: html.escape(str(v if v is not None else "不可判断"))
    role = {"student": "学生", "teacher": "教师", "administrator": "管理者"}[value["role"]]
    links = lambda items: "、".join(f'<a href="{esc(e.get("href", "#trace"))}">{esc(e.get("id"))}</a>' for e in items)
    actions = "".join(f"<li><b>{esc(a['text'])}</b><p>依据：{esc(a['condition'])}</p><p>关联回合：{links(a['evidence'])}</p><p>复核条件：{esc(a['reconsider'])}</p></li>" for a in value["actions"])
    limits = "".join(f"<li>{esc(x)}</li>" for x in value["limits"])
    summary = value["summary"]
    overview = "".join(f"<tr><th>{label}</th><td>{esc(summary.get(key))}</td></tr>" for key, label in
                       (("total", "范围内回合"), ("completed", "已结算"), ("unfinished", "待结算"),
                        ("task_candidates", "任务候选"), ("contribution_candidates", "贡献候选"),
                        ("student_terms", "学生×学期单位"), ("CTQ_available", "CTQ 可用单位"),
                        ("AIV_available", "完整 AIV 可用单位"), ("rank_available", "排名可用单位")))
    states = "".join(f"<tr><td>{esc(state)}</td><td>{value['dimensions']['task']['states'].get(state, 0)}</td><td>{value['dimensions']['contribution']['states'].get(state, 0)}</td></tr>" for state in STATES)
    distributions = "".join(f"<tr><td>L{i+1}</td><td>{value['distributions']['task'][i]}</td><td>{value['distributions']['contribution'][i]}</td></tr>" for i in range(6))
    students = "".join(f"<tr><td>{esc(r['student'])}</td><td>{esc(r['term'])}</td><td>{r['n']}</td><td>{r['task_candidates']}</td><td>{r['contribution_candidates']}</td><td>{esc(r['CTQ'])}</td><td>{esc(r['AIV'])}</td></tr>" for r in value["rows"])
    comparability = "".join(f"<tr><td>{esc(c['label'])}</td><td>{esc(c['status'])}</td><td>{esc(c['reason'])}</td><td>{esc(c['next_step'])}</td></tr>" for c in value["comparability"])
    confirmations = "".join(f"<tr><td>{esc(c.get('turn_id') or c.get('item_id'))}</td><td>{esc(c.get('dimension'))}</td><td>{esc(c.get('decision'))}</td><td>{esc(c.get('operator'))}</td><td>{esc(c.get('note', ''))}</td></tr>" for c in value["confirmations"])
    evidence = "".join(f"<li>{links([e])} · {esc(e['student'])} · {esc(e['term'])}；任务 {esc(e['task'])} / 贡献 {esc(e['contribution'])}；来源 {esc(e['source'].get('file'))} 第 {esc(e['source'].get('row'))} 行</li>" for e in value["evidence"])
    coverage = lambda v: "不可判断" if v is None else f"{v:.1%}"
    return (f'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>{role}报告</title>'
            '<style>body{font-family:system-ui,"Microsoft YaHei",sans-serif;max-width:950px;margin:32px auto;padding:16px;line-height:1.7;color:#15312d}p,small,td,a{overflow-wrap:anywhere}table{border-collapse:collapse;width:100%;margin:12px 0;font-size:13px}th,td{border:1px solid #ccd6d1;padding:6px;text-align:left;vertical-align:top}tr{break-inside:avoid}a{color:#176c5e}h2{break-after:avoid}@media print{body{margin:0;padding:0}thead{display:table-header-group}}</style>'
            f'<h1>{role}报告 · {esc(value["dataset"])}</h1><p>{esc(value["headline"])}</p>'
            f'<p>{esc(value["scope"]["description"])} 学生范围：{esc(value["scope"]["student"] or "全部")}；学期：{esc(value["scope"]["term"] or "全部")}。</p>'
            f'<h2>覆盖与缺测</h2><table>{overview}</table><p>任务候选覆盖 {coverage(summary["coverage"])}；贡献候选覆盖 {coverage(summary["contribution_coverage"])}。候选覆盖以全部范围内回合为分母。</p>'
            f'<h2>双维状态与待核事项</h2><table><thead><tr><th>状态</th><th>任务</th><th>贡献</th></tr></thead><tbody>{states}</tbody></table>'
            f'<h2>候选层级分布</h2><table><thead><tr><th>层级</th><th>任务候选</th><th>贡献候选</th></tr></thead><tbody>{distributions}</tbody></table>'
            f'<h2>学生×学期覆盖</h2><table><thead><tr><th>学生</th><th>学期</th><th>回合</th><th>任务候选</th><th>贡献候选</th><th>CTQ</th><th>AIV</th></tr></thead><tbody>{students}</tbody></table>'
            f'<h2>建议与复核条件</h2><ol>{actions}</ol><h2>可比性与补采</h2><table><thead><tr><th>条件</th><th>状态</th><th>依据</th><th>下一步</th></tr></thead><tbody>{comparability}</tbody></table>'
            f'<h2>返回原文核查</h2><p>链接需要在内部工作台登录；报告包不包含原始提问字段。</p><ul>{evidence}</ul>'
            f'<h2>独立人工确认记录</h2><p>以下是体验者的确认与备注，不改写模型输出，也不自动成为独立真值。</p><table><thead><tr><th>回合</th><th>维度</th><th>决定</th><th>确认人</th><th>备注</th></tr></thead><tbody>{confirmations}</tbody></table>'
            f'<h2>使用边界</h2><ul>{limits}</ul><small>样本 SHA-256：{esc(value["metadata"].get("sample_sha256"))}'
            f'<br>批次与版本：{esc(value["metadata"].get("experiment"))} / {esc(value["metadata"].get("revision"))}；模式：{esc(value["metadata"].get("mode"))}'
            f'<br>冻结清单 SHA-256：{esc(value["metadata"].get("snapshot_manifest_sha256"))}'
            f'<br>选择范围 SHA-256：{esc(value["metadata"].get("selection_sha256"))}</small></html>').encode("utf-8")


def _usage_summary(usage):
    # The caller may supply a richer accounting object; exports retain counters,
    # not credentials, model response bodies, arbitrary notes or account names.
    allowed = {"requests", "attempts", "calls", "successes", "retries", "done", "failed", "pending", "cached", "cache_hits", "input_tokens", "output_tokens",
               "total_tokens", "prompt_tokens", "completion_tokens", "cost", "cost_usd", "known_cost", "estimated_cost", "duration_seconds",
               "cached_tokens", "held_tokens", "held_cost", "unknown_usage_attempts"}
    numeric = lambda obj, keys: {k: v for k, v in obj.items() if k in keys and
                                (v is None or isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v))}
    safe_code = lambda value: isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_.:/-]{1,160}", value))
    usage = usage or {}
    result = numeric(usage, allowed)
    for key in ("source", "experiment"):
        if safe_code(usage.get(key)):
            result[key] = usage[key]
    if "models" in usage:
        result["models"] = [{**{k: row[k] for k in ("provider", "model") if safe_code(row.get(k))},
                             **numeric(row, allowed)} for row in usage["models"] if isinstance(row, dict)]
    if "states" in usage:
        result["states"] = [{**{k: row.get(k) for k in ("status", "error_kind")
                                 if row.get(k) is None or safe_code(row.get(k))}, **numeric(row, {"count"})}
                            for row in usage["states"] if isinstance(row, dict)]
    summary = usage.get("summary")
    if isinstance(summary, dict):
        selected = numeric(summary, {"schema_version", "updated_at", "n", "completed_records", "basic_complete_records",
                                     "final_available", "review_real_selected", "review_random_selected", "review_high_risk_selected", "concurrency_limit"})
        for key in ("experiment", "revision", "version"):
            if safe_code(summary.get(key)):
                selected[key] = summary[key]
        selected["scope"] = "原冻结运行摘要，可能包含工程控制；真实请求分母见 request_sets。"
        selected["stages"] = {stage: numeric((summary.get("stages") or {}).get(stage, {}),
                                             {"requested", "expected", "valid", "failed", "pending", "not_created"})
                              for stage in ("independent", "review", "repeat")}
        pool = summary.get("pool") or {}
        safe_pool = {"tasks": numeric(pool.get("tasks") or {}, {"done", "failed", "queued", "running", "cancelled"})}
        safe_pool["pending_reasons"] = [{"reason": r.get("reason"), **numeric(r, {"count"})}
                                        for r in pool.get("pending_reasons", [])
                                        if isinstance(r, dict) and (r.get("reason") is None or safe_code(r.get("reason")))]
        # Aggregate by currency, deliberately discarding account/alias identifiers.
        currencies = defaultdict(list)
        for row in pool.get("ledger", []):
            if isinstance(row, dict):
                currency = row.get("currency") if safe_code(row.get("currency")) else "unspecified"
                currencies[currency].append(numeric(row, allowed))
        safe_pool["ledger"] = []
        for currency, rows in sorted(currencies.items()):
            item = {"currency": currency}
            for key in set().union(*(r.keys() for r in rows)):
                values = [r[key] for r in rows if r.get(key) is not None]
                item[key] = sum(values) if values else None
            safe_pool["ledger"].append(item)
        selected["pool"] = safe_pool
        result["summary"] = selected
    return result


def _export_context(context, role, student, term):
    selected = report(context, role, student, term)["scope"]
    student, term = selected["student"], selected["term"]
    within = lambda r: (not student or str(r.get("student") or "未分配") == student) and (not term or str(r.get("term")) == term)
    rows = [r for r in _rows(context) if within(r.get("record", {}))]
    records = [r for r in context.get("dataset", {}).get("records", []) if within(r)
               and (_mode(context) == "synthetic" or r.get("term") != "synthetic")]
    ids = {r.get("record", {}).get("id") for r in rows}
    rows += [{"record": r, "complete": False} for r in records if r.get("id") not in ids]
    ids = {r.get("record", {}).get("id") for r in rows}
    plan = dict(context.get("plan") or {})
    if student or term:
        plan["real"] = len(rows)
        for key in ("audit_ids", "repeat_ids", "engineering_ids"):
            plan[key] = [i for i in plan.get(key, []) if i in ids]
    selection = {"student": student, "term": term}
    selection_sha = hashlib.sha256(_json_bytes({"ids": sorted(str(i) for i in ids), "selection": selection})).hexdigest()
    result = {**context, "dataset": {**context.get("dataset", {}), "records": records}, "plan": plan,
              "rows": rows, "report_all_students": True,
              "metadata": {**context.get("metadata", {}), "selected_role": role,
                           "selection": selection, "selection_sha256": selection_sha}}
    return result, selection


def _online_links(value, context):
    if isinstance(value, list):
        return [_online_links(v, context) for v in value]
    if not isinstance(value, dict):
        return value
    result = {k: _online_links(v, context) for k, v in value.items()}
    if "href" in result and "id" in result:
        base = str(context.get("metadata", {}).get("application_url") or "")
        if not base.startswith(("http://", "https://")):
            base = ""
        fragment = "#trace?job=" + quote(str(context.get("job", {}).get("id") or ""), safe="") + "&item=" + quote(str(result["id"]), safe="")
        result["href"] = base.rstrip("/") + "/" + fragment if base else fragment
    return result


def _selection_usage(context):
    grouped = defaultdict(list)
    for row in _rows(context):
        for stage in ("independent", "review", "repeat"):
            for entry in row.get(stage, []):
                grouped[entry.get("model", "unknown")].append(entry)
    models = []
    for model, entries in sorted(grouped.items()):
        attempts = [e.get("attempts") for e in entries]
        known = [n for n in attempts if isinstance(n, int) and not isinstance(n, bool) and n >= 0]
        models.append({"model": model, "requests": len(entries), "recorded_attempts": sum(known),
                       "requests_without_attempt_count": len(entries) - len(known),
                       "failed_requests": sum(e.get("status") == "failed" for e in entries)})
    return {"source": "selected_annotation_attempts", "models": models, "input_tokens": None,
            "output_tokens": None, "cost": None,
            "note": "仅为所选回合记录的请求与尝试计数；整批账本用量无法按当前聚合精确归属到本范围，token和费用保留缺失。"}


def _export_readme():
    weights = "\n".join(f"- `{name}`：`{json.dumps(values.tolist())}`" for name, values in WEIGHTS.items())
    return f"""# 工作台报告包

本包按导出时所选批次、学生和学期生成。三视角、CSV、确认记录及比较分母使用同一范围；
`manifest.json.metadata.selected_role` 记录导出时视角，`selection` 记录实际学生/学期条件。
学生视角未明确选择学生时，整包使用界面默认的第一名学生。

## 文件、版本与使用边界

- `reports/`：学生、教师、管理者各一份 JSON 和可打印 HTML。
- `results.csv`：学生×学期指标；`turn-results.csv`：逐回合双维状态、候选标签、请求计数及来源坐标。
- `confirmations.json`：独立人工确认，保留完整备注与 `note_sha256`；确认不改写模型结果，也不自动成为独立真值。
- `job-usage.json`：任务用量和四类请求集合分母。筛选范围后，无法精确归属的 token/费用保留 null，不能用整批用量代替。
- `manifest.json`：各文件 SHA-256、报告版本与来源摘要；清单本身不自我哈希。

报告结构版本为 `{SCHEMA}`，公式实现对应 `aiv/metrics.py` 与 `aiv/product_reports.py`。
模型流程版本见 metadata.version，运行修订见 revision，批次见 experiment，量规摘要见 rubric_sha256。
这些字段不是 Git 提交号；代码提交应另从部署记录核对，不能用 t3/t1 修订名代替代码版本。

原始提问字段和模型引文不在本包。报告的原文链接需登录内部工作台；相对 `#trace?job=...&item=...`
链接应拼到该工作台首页 URL 后访问。确认备注可能由体验者主动引用原文。CSV 含学生标识，仅供内部使用。
JSON null 与 CSV 空白均表示缺测，不表示零。真实数据的模型一致性不等于准确率，也不支持因果增量或学生排名。

## 统计分母与公式

真实模式排除工程控制题（term=synthetic）；合成教学模式保留构造样本。设所选学生×学期内全部回合数为 N。
某维度仅在 complete=true、终态 agreed 且标签为整数 1..6 时计作候选；任务和贡献分别判定。
任务候选数为 m、贡献候选数为 c；任务覆盖=m/N，贡献覆盖=c/N。N=0 时覆盖为 null。
`results.csv` 的 ABL/HOT/DHI/MAB 使用任务候选子集，不是贡献标签，也不把未运行/弃权/分歧补成 L1。
设任务候选标签 L_i、各级比例 p_k=count(L_i=k)/m，m=0 时下列指标均为空：

- ABL_raw = sum(L_i)/m；ABL = (ABL_raw-1)/5。
- HOT = count(L_i>=4)/m。
- DHI = 1 - sum(abs(p_k-q_k))/2，固定 q={json.dumps(IDEAL.tolist())}，按 L1..L6 排列；此处使用对称距离。
- MAB_raw=d 为任务候选子集中已知工具身份的去重数；必须每条候选的工具身份均已知，MAB=min(1, ln(1+d)/ln(5))。
  空值/unknown/nan/<na>/nat 不算已知工具，缺一则 MAB_raw 与 MAB 均为空。
- CTQ = 0.5 + mean(L_to-L_from)/10，只使用同一学生×学期内双方有候选、明确 verified=true 的不重复已核验边。
  会话候选 ID 本身不证明真实相邻关系；正式 t3 的 CTQ 始终为空。合成材料仅按构造的连续同会话边演示。
- AIV = 100 * sum(w_j * x_j)，x 按 ABL,HOT,CTQ,DHI,MAB 排列。任一正权重指标缺失即为空，不重分配权重。
  正式 t3 的完整 AIV/rank 保留空值；真实模式不产生排名。

固定权重如下：

{weights}

正式 t3 的学生指标优先使用固定来源清单中的 `student_indicators.csv`，以 student+term 连接。
`metric_source=official_student_indicators` 标识这种来源；live/synthetic 的 `selected_annotation_rows` 表示由本批回合计算。
候选覆盖仍按导出范围实际回合计算，不能把官方指标来源误读为所有回合都有候选。

基础独立判断、随机复核、高风险复核与配对复跑分开统计；复跑配对为同一模型原始输出与复跑输出。
`job-usage.json.request_sets` 含 planned_records、observed_records、completed_records、expected_requests、created_requests、
valid_outputs、failed_requests、pending_requests。expected_requests=计划范围回合数×该阶段计划模型数，包含尚未创建请求。
valid_outputs 包含合法弃权（该维度字段明确为 null）；字段缺失/非法值不是合法弃权。高风险分母是已选高风险集合，
并不代表未来可能进入复核的全部回合。调用尝试、请求和学生回合是不同计数单位，控制题仅出现在整批用量，不进入真实报告分母。

比较接口 `/api/wb/jobs/{{job_id}}/compare?dimension=task|contribution&group=independent|random_audit|high_risk|repeat`
返回配对 matrix6/matrix3。比较接口当前针对整批所选请求集合，未提供学生/学期筛选；需复核子范围时，
在受控环境使用相同 `_export_context` 后调用 `compare`。对矩阵 C，n=sum(C) 为双方有效非空标签配对数，
agreement=trace(C)/n，p_e=sum(row_sum_k*col_sum_k)/n²，kappa=(agreement-p_e)/(1-p_e)。n=0 或 p_e 接近1时 κ 为空。
三级矩阵把 L1–L2、L3–L4、L5–L6 合并。共同弃权另记，含弃权一致率以双方合法输出数为分母。
本包未携带逐模型配对矩阵，不能仅凭 final_distribution 或最终候选复算 κ。

## 哈希复核与可复算范围

文件完整性：manifest.files[name] = SHA256(该文件原始字节)。不要先重新保存 CSV/JSON 再算文件哈希。
snapshot_manifest_sha256、materials_manifest_sha256、source_sha256 也都是其对应来源文件的字节摘要。
sample_sha256 与 rubric_sha256 是来源 JSON 对象的规范化摘要：
`SHA256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8'))`。
sample_sha256 对完整原始样本计算，可能包括控制题；它不会因本次范围筛选而改变。

selection_sha256 使用另一种明确格式：从原始上下文取所选真实回合 ID（合成模式取构造回合 ID），去重，转为字符串后排序，
构造 `{{"ids": ids, "selection": {{"student": student, "term": term}}}}`，再按
`SHA256((json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)+'\\n').encode('utf-8'))` 计算。
student/term 必须采用 metadata.selection 的实际值。CSV 对 =、+、-、@、制表符或回车开头的标识添加单引号防公式执行；
因此一般标识可直接比对，特殊标识应回到受控 JSON 上下文复核 selection_sha256，不能盲目剥离单引号。
note_sha256 是完整备注字符串 UTF-8 字节摘要，无 JSON 包装或末尾换行。

将以下代码保存为 verify_export.py，运行 `python verify_export.py 报告包.zip`。仅使用 Python 标准库，
从 ZIP 读取而不解压；核验所有文件、确认备注、三视角范围以及逐回合覆盖/分布和学生×学期数量。
失败会终止并给出断言，不输出学生原文。哈希证明内部完整性；核实来源真实性仍需可信的冻结清单与受控原始文件。

```python
import csv, hashlib, io, json, math, sys, zipfile

with zipfile.ZipFile(sys.argv[1]) as archive:
    manifest = json.loads(archive.read("manifest.json"))
    expected = set(manifest["files"]) | {{"manifest.json"}}
    assert set(archive.namelist()) == expected, "file membership mismatch"
    assert len(archive.namelist()) == len(expected), "duplicate ZIP member"
    for name, digest in manifest["files"].items():
        assert hashlib.sha256(archive.read(name)).hexdigest() == digest, "file hash mismatch: " + name
    turns = list(csv.DictReader(io.StringIO(archive.read("turn-results.csv").decode("utf-8-sig"))))
    table = list(csv.DictReader(io.StringIO(archive.read("results.csv").decode("utf-8-sig"))))
    for item in json.loads(archive.read("confirmations.json")):
        if "note_sha256" in item:
            assert hashlib.sha256(item["note"].encode("utf-8")).hexdigest() == item["note_sha256"], "note hash mismatch"
    for role in ("student", "teacher", "administrator"):
        report = json.loads(archive.read("reports/" + role + ".json"))
        for key in ("selection", "selection_sha256", "selected_role"):
            assert report["metadata"][key] == manifest["metadata"][key], "scope mismatch"
        summary = report["summary"]
        assert summary["total"] == len(turns), "turn denominator mismatch"
        assert summary["student_terms"] == len(table), "student-term denominator mismatch"
        for dimension in ("task", "contribution"):
            labels = [int(row[dimension + "_label"]) for row in turns if row[dimension + "_label"]]
            assert summary[dimension + "_candidates"] == len(labels), "candidate count mismatch"
            assert report["distributions"][dimension] == [labels.count(i) for i in range(1, 7)], "distribution mismatch"
            coverage = report["dimensions"][dimension]["coverage"]
            assert (coverage is None if not turns else math.isclose(coverage, len(labels) / len(turns))), "coverage mismatch"
    print("Verified file hashes, scope, notes, coverage and distributions.")
```

`turn-results.csv` 足以按 student+term 复算覆盖、ABL、HOT、DHI；对来源官方指标的差异，应检查 source/版本而非自动覆盖。
MAB 需要原始工具身份，CTQ 需要已核验边，κ 需要相应比较矩阵；这些证据不完整包含在此包，须沿原文链接或受控来源复核。
所有候选指标均为条件描述；一致性、覆盖或哈希通过不等于教育效果通过。
""".encode("utf-8")


def export_bundle(context, usage=None, *, role="teacher", student="", term=""):
    context, selection = _export_context(context, role, student, term)
    files = {}
    for view in ("student", "teacher", "administrator"):
        value = _online_links(_without_text(report(context, view, **selection)), context)
        files[f"reports/{view}.json"] = _json_bytes(value)
        files[f"reports/{view}.html"] = _report_html(value)
    files["results.csv"] = results_csv(context).encode("utf-8")
    files["turn-results.csv"] = _turn_results_csv(context).encode("utf-8")
    files["confirmations.json"] = _json_bytes([_export_confirmation(c) for c in _confirmations(context, _rows(context))])
    job = context.get("job") or {}
    files["job-usage.json"] = _json_bytes({"job": {k: job.get(k) for k in ("id", "dataset", "mode", "state", "created", "updated")},
                                           "usage": _selection_usage(context) if any(selection.values()) else _usage_summary(usage),
                                           "usage_scope": "selected_records" if any(selection.values()) else "whole_job_including_controls",
                                           "metadata": _metadata(context),
                                           "request_sets": {g: compare(context, "task", g)["denominators"] for g in GROUPS}})
    files["README.md"] = _export_readme()
    files["manifest.json"] = _json_bytes({"schema": SCHEMA, "metadata": _metadata(context), "contains_original_question_fields": False,
                                         "includes_confirmation_notes": True,
                                         "contains_student_identifiers": True, "confirmation_semantics": "separate_operator_annotations",
                                         "files": {k: hashlib.sha256(v).hexdigest() for k, v in files.items()}})
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, value)
    return stream.getvalue()
