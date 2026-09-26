"""Operator workbench: import → quality check → resumable replay jobs → anomalies → compare → report → export.

No model API is called here. Jobs replay the published synthetic judge cache; records without a cached
judgment are marked "not_run" so the page never presents an unrun model call as a result.
Public mode only sees synthetic/uploaded datasets; private competition data needs the workbench token.
"""

from pathlib import Path
import csv
import hashlib
import hmac
import io
import json
import math
import os
import re
import time
import uuid
import zipfile

import numpy as np
import pandas as pd
from dotenv import dotenv_values
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .analysis import student_scores, weight_sensitivity
from .data import synthetic_records, question_only, load_competition
from .metrics import metrics, composite, label_uncertainty, WEIGHTS, NAMES

ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "runtime/workbench"
CACHE = ROOT / "examples/synthetic-judge-cache.json"
BATCH = 24
LEVELS = ["记忆", "理解", "应用", "分析", "评价", "创造"]
router = APIRouter(prefix="/api/wb")

# Accepted CSV headers (Chinese competition export or English aliases) → internal field.
COLUMNS = {
    "student": ("学号", "student", "student_id"),
    "question": ("问答记录", "question", "提问"),
    "timestamp": ("问题建立时间", "timestamp", "time"),
    "agent": ("智能体类型", "agent"),
    "session": ("会话", "session", "session_id"),
    "label": ("标签", "label"),
}


def clean(value):
    """Make numpy/pandas values JSON-safe; NaN/inf become None (shown as 不可判断)."""
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def token_ok(token):
    """Workbench unlock: WORKBENCH_TOKEN if configured, otherwise the existing C-role review token."""
    settings = {**dotenv_values(ROOT / ".env"), **os.environ}
    expected = settings.get("WORKBENCH_TOKEN") or settings.get("REVIEW_TOKEN_C")
    return bool(expected and token and hmac.compare_digest(token, expected))


def require(token):
    if not token_ok(token):
        raise HTTPException(401, "工作台口令无效")


def save(kind, key, value):
    path = STORE / kind / f"{key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean(value), ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load(kind, key, token=None):
    if not re.fullmatch(r"[\w-]{1,64}", key):
        raise HTTPException(404, "没有找到该对象")
    path = STORE / kind / f"{key}.json"
    if not path.exists():
        if kind == "datasets" and key in SAMPLES:
            return build_sample(key)
        raise HTTPException(404, "没有找到该对象")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("private"):
        require(token)
    return value


# ---------- 1. 导入 ----------

def dirty_csv():
    """Deliberately flawed synthetic export used to demonstrate quality checks and abnormal input."""
    good = synthetic_records()[:40]
    rows = [["学号", "问答记录", "问题建立时间", "智能体类型"]]
    for r in good:
        rows.append([r["student"], f"Q：{r['question']}\nA：（合成回答略）", r["timestamp"], r["agent"]])
    rows += [
        ["S001", "Q：请列举线性规划的基本要素。\nA：略", "2026-13-45", "探知侠"],  # invalid date
        [rows[1][0], rows[1][1], rows[1][2], rows[1][3]],  # exact duplicate
        ["S002", "Q：https://example.com/screenshot.png\nA：略", "2026-01-03", "数模全才"],  # link only
        ["S003", "（图片）", "2026-01-04", "逆行侠"],  # no text question
        ["", "Q：请解释对偶问题的经济含义。\nA：略", "2026-01-05", "探知侠"],  # missing student id
        ["S004", "Q：请检查我的约束。\nA：略", "2026-01-06", ""],  # missing agent
    ]
    out = io.StringIO()
    csv.writer(out).writerows(rows)
    return out.getvalue()


SAMPLES = {
    "demo-synthetic": dict(name="合成教学样本（24 名学生 × 8 条）", kind="synthetic",
                           note="构造标签已知，全部提问均有三模型缓存，可完整走通流程。"),
    "demo-dirty": dict(name="含问题的导出样例（46 行）", kind="upload",
                       note="故意混入非法日期、重复行、纯链接、无文字、缺学号和缺智能体字段，用于演示质量检查。"),
}


def build_sample(key):
    if key == "demo-synthetic":
        records = synthetic_records()
        quality = quality_report(records, dict(raw_rows=len(records)), synthetic=True)
        return dataset(key, SAMPLES[key]["name"], "synthetic", records, quality, private=False)
    parsed = parse_csv(dirty_csv(), SAMPLES[key]["name"])
    return dataset(key, SAMPLES[key]["name"], "upload", *parsed, private=False)


def dataset(key, name, kind, records, quality, private):
    value = dict(id=key, name=name, kind=kind, private=private, created=time.time(),
                 records=records, quality=quality)
    save("datasets", key, value)
    return value


def pick(header, field):
    for alias in COLUMNS[field]:
        if alias in header:
            return alias
    return None


def parse_csv(text, name):
    """Parse an uploaded CSV into records plus row-level problems; never raises on bad rows."""
    if not text.strip():
        raise HTTPException(422, "文件为空")
    try:
        frame = pd.read_csv(io.StringIO(text.lstrip("﻿")), dtype=str).fillna("")
    except (pd.errors.ParserError, pd.errors.EmptyDataError, UnicodeDecodeError) as error:
        raise HTTPException(422, f"无法解析 CSV：{error}") from None
    header = list(frame.columns)
    cols = {f: pick(header, f) for f in COLUMNS}
    missing = [COLUMNS[f][0] for f in ("student", "question", "timestamp") if not cols[f]]
    if missing:
        raise HTTPException(422, f"缺少必需列：{'、'.join(missing)}。识别到的列：{'、'.join(header) or '无'}")
    problems = {k: [] for k in ("invalid_date", "duplicate", "no_text", "missing_student", "missing_agent", "bad_label")}
    seen, records = set(), []
    source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    for i, row in frame.iterrows():
        line = int(i) + 2
        signature = hashlib.sha256(json.dumps(row.to_dict(), ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if signature in seen:
            problems["duplicate"].append(dict(row=line, reason="与前面某行完全相同"))
            continue
        seen.add(signature)
        raw = row[cols["question"]].strip()
        question = question_only(raw) if re.match(r"^Q[:：]", raw) else raw
        if not question or re.fullmatch(r"https?://\S+", question) or question in ("（图片）", "[图片]"):
            problems["no_text"].append(dict(row=line, reason="没有可标注的文字提问（空、纯链接或图片）"))
            continue
        stamp = pd.to_datetime(row[cols["timestamp"]], errors="coerce")
        if pd.isna(stamp):
            problems["invalid_date"].append(dict(row=line, reason=f"时间无法解析：{row[cols['timestamp']][:30]}"))
            continue
        sid = row[cols["student"]].strip()
        if not sid:
            problems["missing_student"].append(dict(row=line, reason="学号为空，无法归属到学生"))
            continue
        agent = row[cols["agent"]].strip() if cols["agent"] else ""
        if cols["agent"] and not agent:
            problems["missing_agent"].append(dict(row=line, reason="智能体类型为空（保留该行，MAB 计为缺失）"))
        label = None
        if cols["label"] and row[cols["label"]].strip():
            try:
                label = int(row[cols["label"]])
                assert 1 <= label <= 6
            except (ValueError, AssertionError):
                problems["bad_label"].append(dict(row=line, reason="标签不是 1–6 的整数，已忽略"))
                label = None
        records.append(dict(
            id=signature[:20],
            student=sid if re.fullmatch(r"S\d{3}", sid) else hashlib.sha256(("aiv-upload-v1|" + sid).encode()).hexdigest()[:12],
            term="upload", timestamp=str(stamp.date()), question=question,
            agent=agent or None, session=(row[cols["session"]].strip() or None) if cols["session"] else None,
            label=label, label_status="constructed_truth" if label else "pending",
            source_file=name, source_row=line, source_sha256=source_hash,
        ))
    stats = dict(raw_rows=len(frame))
    return records, quality_report(records, stats, problems=problems, columns=header)


CHECKS = [
    ("invalid_date", "时间无法解析", "error"),
    ("duplicate", "完全重复行", "warn"),
    ("no_text", "无文字提问", "warn"),
    ("missing_student", "缺少学号", "error"),
    ("missing_agent", "缺少智能体类型", "warn"),
    ("bad_label", "非法标签值", "warn"),
    ("outside_window", "超出学期时间窗", "warn"),
    ("roster_unmatched", "不在春季名单", "warn"),
]


def quality_report(records, stats, problems=None, columns=None, synthetic=False, boundaries=None):
    problems = problems or {}
    counts = {**{k: len(v) for k, v in problems.items()}, **{k: v for k, v in stats.items() if k in dict((c[0], 1) for c in CHECKS)}}
    has_agent = any(r.get("agent") for r in records)
    has_session = any(r.get("session") for r in records)
    has_label = any(r.get("label") for r in records)
    checks = [dict(key=k, label=label, severity=sev, count=int(counts.get(k, 0)), examples=problems.get(k, [])[:8])
              for k, label, sev in CHECKS if k in counts]
    identifiable = {
        "ABL": dict(ok=True, why="只需认知层级"),
        "HOT": dict(ok=True, why="只需认知层级"),
        "DHI": dict(ok=True, why="只需认知层级分布"),
        "CTQ": dict(ok=has_session, why="需要会话编号；不能用 CSV 相邻行代替" if not has_session else "有会话编号"),
        "MAB": dict(ok=has_agent, why="需要智能体类型字段" if not has_agent else "有智能体类型"),
    }
    return dict(
        raw_rows=int(stats.get("raw_rows", len(records))), usable=len(records),
        students=len({r["student"] for r in records}), columns=columns,
        checks=checks, fields=dict(agent=has_agent, session=has_session, label=has_label),
        identifiable=identifiable, synthetic=synthetic,
        boundaries=boundaries or ([] if synthetic else ["没有独立前后测：只能描述过程，不能判断学习增量。"]),
    )


class ImportBody(BaseModel):
    name: str = Field(max_length=120)
    text: str = Field(max_length=20_000_000)


@router.get("/samples")
def samples(x_workbench_token: str | None = Header(default=None)):
    items = [dict(id=k, **v) for k, v in SAMPLES.items()]
    unlocked = token_ok(x_workbench_token)
    items.append(dict(id="competition", name="竞赛真实记录（秋季 + 春季）", kind="private", locked=not unlocked,
                      note="需要工作台口令；只在本机读取 data/ 下的 qa.csv，学号经哈希处理。"))
    return dict(samples=items, unlocked=unlocked)


@router.post("/import/sample/{key}")
def import_sample(key: str, x_workbench_token: str | None = Header(default=None)):
    if key in SAMPLES:
        return summary(build_sample(key))
    if key != "competition":
        raise HTTPException(404, "没有这个样例")
    require(x_workbench_token)
    try:
        records, audit = load_competition(ROOT)
    except (OSError, KeyError, ValueError) as error:
        raise HTTPException(503, f"竞赛数据不可读：{error}") from None
    stats = dict(raw_rows=sum(audit[t]["raw_rows"] for t in ("25f", "26s")))
    for key_ in ("invalid_dates", "outside_window", "roster_unmatched", "duplicate_rows", "no_text_question"):
        target = {"invalid_dates": "invalid_date", "duplicate_rows": "duplicate", "no_text_question": "no_text"}.get(key_, key_)
        stats[target] = sum(audit[t][key_] for t in ("25f", "26s"))
    quality = quality_report(records, stats, boundaries=audit["boundaries"])
    return summary(dataset("competition", "竞赛真实记录", "private", records, quality, private=True))


@router.post("/import/upload")
def import_upload(body: ImportBody):
    records, quality = parse_csv(body.text, body.name)
    key = "up-" + uuid.uuid4().hex[:10]
    return summary(dataset(key, body.name, "upload", records, quality, private=False))


def summary(value):
    return {k: v for k, v in value.items() if k != "records"}


@router.get("/datasets/{key}")
def get_dataset(key: str, x_workbench_token: str | None = Header(default=None)):
    return summary(load("datasets", key, x_workbench_token))


# ---------- 3. 任务（缓存回放，可续跑） ----------

def judge_cache():
    return {x["record"]["question"]: x for x in json.loads(CACHE.read_text(encoding="utf-8"))}


def level(entry):
    return (entry.get("judgment") or {}).get("level") if entry else None


def replay(record, cached):
    """Turn one record into a job item; uncached records are explicitly not run (API not connected)."""
    base = dict(id=record["id"], student=record["student"], timestamp=record["timestamp"],
                question=record["question"], constructed=record.get("label"))
    if not cached:
        return dict(base, status="not_run", stage="independent", independent=[], peer=[], arbitration=None,
                    final=None, flags=["not_run"])
    def view(e):
        j = e.get("judgment") or {}
        return dict(model=e["model"], family=e["family"], level=j.get("level"), evidence=j.get("evidence", ""),
                    reason=j.get("reason", ""), error=e.get("error"), uncertain=j.get("uncertain", False))
    independent = [view(e) for e in cached["independent"]]
    peer = [view(e) for e in cached["peer"]]
    arb = view(cached["arbitration"]) if cached.get("arbitration") else None
    flags = []
    ind_levels = [x["level"] for x in independent]
    if None in ind_levels or any(x["error"] for x in independent):
        flags.append("abstain")
    if len({x for x in ind_levels if x}) > 1:
        flags.append("disagree")
    if arb:
        flags.append("arbitrated")
    final = cached.get("final")
    if record.get("label") and final and final != record["label"]:
        flags.append("vs_constructed")
    return dict(base, status="done", stage="arbitration" if arb else "peer", independent=independent,
                peer=peer, arbitration=arb, final=final, flags=flags)


class JobBody(BaseModel):
    dataset: str = Field(max_length=64)


@router.post("/jobs")
def create_job(body: JobBody, x_workbench_token: str | None = Header(default=None)):
    data = load("datasets", body.dataset, x_workbench_token)
    key = "job-" + uuid.uuid4().hex[:10]
    job = dict(id=key, dataset=data["id"], dataset_name=data["name"], private=data["private"],
               mode="cache_replay", created=time.time(), updated=time.time(), cursor=0,
               total=len(data["records"]), state="ready", items=[], log=[dict(t=time.time(), msg="任务已创建（缓存回放，不调用模型 API）")])
    save("jobs", key, job)
    return status(job)


def status(job):
    items = job["items"]
    count = lambda f: sum(f in i["flags"] for i in items)
    return dict(
        {k: v for k, v in job.items() if k not in ("items",)},
        done=sum(i["status"] == "done" for i in items), not_run=count("not_run"),
        disagree=count("disagree"), arbitrated=count("arbitrated"), abstain=count("abstain"),
        stages=dict(
            independent=sum(i["status"] == "done" for i in items),
            peer=sum(bool(i["peer"]) for i in items),
            arbitration=count("arbitrated"),
        ),
    )


@router.get("/jobs")
def list_jobs(x_workbench_token: str | None = Header(default=None)):
    unlocked = token_ok(x_workbench_token)
    jobs = []
    for path in sorted((STORE / "jobs").glob("job-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
        job = json.loads(path.read_text(encoding="utf-8"))
        if job.get("private") and not unlocked:
            continue
        jobs.append(status(job))
    return dict(jobs=jobs)


@router.get("/jobs/{key}")
def get_job(key: str, x_workbench_token: str | None = Header(default=None)):
    return status(load("jobs", key, x_workbench_token))


@router.post("/jobs/{key}/step")
def step_job(key: str, x_workbench_token: str | None = Header(default=None)):
    """Process the next batch from the saved cursor; safe to call again after a pause or restart."""
    job = load("jobs", key, x_workbench_token)
    if job["cursor"] >= job["total"]:
        job["state"] = "finished"
        return status(job)
    records = load("datasets", job["dataset"], x_workbench_token)["records"]
    cache = judge_cache()
    batch = records[job["cursor"]: job["cursor"] + BATCH]
    job["items"] += [replay(r, cache.get(r["question"])) for r in batch]
    job["cursor"] += len(batch)
    job["state"] = "finished" if job["cursor"] >= job["total"] else "running"
    job["updated"] = time.time()
    missed = sum(not cache.get(r["question"]) for r in batch)
    job["log"].append(dict(t=time.time(), msg=f"处理第 {job['cursor'] - len(batch) + 1}–{job['cursor']} 条；"
                                            f"{len(batch) - missed} 条回放缓存，{missed} 条无缓存未运行"))
    save("jobs", key, job)
    return status(job)


@router.post("/jobs/{key}/pause")
def pause_job(key: str, x_workbench_token: str | None = Header(default=None)):
    job = load("jobs", key, x_workbench_token)
    if job["state"] == "running":
        job["state"] = "paused"
        job["log"].append(dict(t=time.time(), msg=f"已暂停于第 {job['cursor']} 条，可随时继续"))
        save("jobs", key, job)
    return status(job)


# ---------- 4. 异常追溯 ----------

FLAG_NAMES = dict(disagree="模型独立判断不一致", arbitrated="进入仲裁", abstain="弃权或输出无效",
                  vs_constructed="最终标签与构造标签不同", not_run="未运行（无缓存，API 未接入）")


@router.get("/jobs/{key}/anomalies")
def anomalies(key: str, x_workbench_token: str | None = Header(default=None)):
    job = load("jobs", key, x_workbench_token)
    groups = {}
    for item in job["items"]:
        for flag in item["flags"]:
            groups.setdefault(flag, []).append(item)
    items = [i for i in job["items"] if i["flags"]]
    return dict(flags=FLAG_NAMES, counts={k: len(v) for k, v in groups.items()},
                items=items[:300], truncated=max(0, len(items) - 300), processed=len(job["items"]), total=job["total"])


@router.get("/jobs/{key}/items/{item_id}")
def job_item(key: str, item_id: str, x_workbench_token: str | None = Header(default=None)):
    """One processed record, flagged or not; lets the report link its evidence back to the trace view."""
    job = load("jobs", key, x_workbench_token)
    for item in job["items"]:
        if item["id"] == item_id:
            return item
    raise HTTPException(404, "该记录尚未处理或不存在")


# ---------- 5. 模型与指标比较 ----------

def labelled(job, records):
    final = {i["id"]: i["final"] for i in job["items"]}
    return [dict(r, label=final.get(r["id"])) for r in records if final.get(r["id"])]


@router.get("/jobs/{key}/compare")
def compare(key: str, x_workbench_token: str | None = Header(default=None)):
    job = load("jobs", key, x_workbench_token)
    done = [i for i in job["items"] if i["status"] == "done"]
    models = []
    if done:
        names = [x["model"] for x in done[0]["independent"]]
        for m, name in enumerate(names):
            lv = [i["independent"][m]["level"] for i in done]
            con = [(l, i["constructed"]) for l, i in zip(lv, done) if i["constructed"] and l]
            models.append(dict(model=name, family=done[0]["independent"][m]["family"],
                               answered=sum(l is not None for l in lv), distribution=[lv.count(k) for k in range(1, 7)],
                               vs_constructed=(sum(a == b for a, b in con) / len(con)) if con else None,
                               peer_changed=sum(i["peer"] and i["peer"][m]["level"] != i["independent"][m]["level"] for i in done)))
        pairs = []
        for a in range(len(names)):
            for b in range(a + 1, len(names)):
                both = [(i["independent"][a]["level"], i["independent"][b]["level"]) for i in done
                        if i["independent"][a]["level"] and i["independent"][b]["level"]]
                pairs.append(dict(a=names[a], b=names[b], n=len(both),
                                  agreement=(sum(x == y for x, y in both) / len(both)) if both else None))
    else:
        pairs = []
    records = load("datasets", job["dataset"], x_workbench_token)["records"]
    rows = labelled(job, records)
    table = student_scores(rows) if rows else pd.DataFrame()
    sens = weight_sensitivity(table) if len(table) else pd.DataFrame()
    finals = [i["final"] for i in done if i["final"]]
    con = [(i["final"], i["constructed"]) for i in done if i["final"] and i["constructed"]]
    return clean(dict(
        processed=len(job["items"]), total=job["total"], labelled=len(rows),
        models=models, pairs=pairs,
        final_distribution=[finals.count(k) for k in range(1, 7)],
        final_vs_constructed=(sum(a == b for a, b in con) / len(con)) if con else None,
        students=table.to_dict("records") if len(table) else [],
        sensitivity=sens.to_dict("records") if len(sens) else [],
        weights={k: v.tolist() for k, v in WEIGHTS.items()},
    ))


class MetricBody(BaseModel):
    student: str
    weights: list[float]
    uncertainty: float = 0.15
    asymmetric: bool = False


@router.post("/jobs/{key}/metrics")
def job_metrics(key: str, body: MetricBody, x_workbench_token: str | None = Header(default=None)):
    job = load("jobs", key, x_workbench_token)
    rows = sorted((r for r in labelled(job, load("datasets", job["dataset"], x_workbench_token)["records"])
                   if r["student"] == body.student), key=lambda r: r["timestamp"])
    if not rows:
        raise HTTPException(422, "该学生还没有可用标签")
    if not 0 <= body.uncertainty <= 0.5:
        raise HTTPException(422, "标签扰动概率须在 0–50% 之间")
    levels = [r["label"] for r in rows]
    agents = [r["agent"] for r in rows]
    sessions = [r["session"] for r in rows]
    try:
        values = metrics(levels, agents, sessions, asymmetric=body.asymmetric)
        score = composite(values, body.weights)
    except ValueError:
        raise HTTPException(422, "权重须为 5 个非负数且和为 1") from None
    missing = [n for n, w in zip(NAMES, body.weights) if w > 0 and values.get(n) is None]
    interval = None
    if score is not None:
        rng = np.random.default_rng(26)
        probs = np.full((len(rows), 6), body.uncertainty / 5)
        for i, lv in enumerate(levels):
            probs[i, lv - 1] = 1 - body.uncertainty
        samples = [composite(metrics([rng.choice(range(1, 7), p=p) for p in probs], agents, sessions,
                                     asymmetric=body.asymmetric), body.weights) for _ in range(200)]
        interval = np.quantile(samples, [0.025, 0.5, 0.975]).tolist()
    return clean(dict(n=len(rows), metrics=values, score=score, interval=interval, missing=missing))


# ---------- 6. 教师报告 ----------

def teacher_report(job, records):
    rows = labelled(job, records)
    processed, total = len(job["items"]), job["total"]
    not_run = sum("not_run" in i["flags"] for i in job["items"])
    base = dict(dataset=job["dataset_name"], job=job["id"], generated=time.strftime("%Y-%m-%d %H:%M"),
                processed=processed, total=total, labelled=len(rows), not_run=not_run,
                synthetic=all(r.get("term") == "synthetic" for r in records))
    if not rows:
        return dict(base, ready=False, message="还没有可用标签：请先完成任务；无缓存的记录需要接入模型 API 后才能标注。")
    table = student_scores(rows)
    dist = [sum(r["label"] == k for r in rows) for k in range(1, 7)]
    hot = float(np.mean([r["label"] >= 4 for r in rows]))
    low = float(np.mean([r["label"] <= 2 for r in rows]))
    by_student = {}
    for r in rows:
        by_student.setdefault(r["student"], []).append(r)
    students = []
    for _, s in table.iterrows():
        tips = []
        if s["HOT"] < 0.25:
            tips.append("高阶提问偏少：布置“比较两种做法并说明理由”类任务")
        if s["ABL"] < 0.4:
            tips.append("多停留在记忆/理解：先让其用自己的话解释，再请 AI 给反例")
        if s["CTQ"] is not None and not pd.isna(s["CTQ"]) and s["CTQ"] < 0.5:
            tips.append("同一会话内层级下降：检查是否在追问中放弃了自己的思路")
        # Supporting evidence = this student's lowest-level question, so the claim can be checked against source text.
        weakest = min(by_student[s["student"]], key=lambda r: (r["label"], r["timestamp"]))
        students.append(dict(student=s["student"], n=int(s["n"]), ABL=s["ABL"], HOT=s["HOT"],
                             balanced=s["balanced"], tips=tips or ["保持：可尝试让其向同伴讲解一次推理过程"],
                             evidence=dict(id=weakest["id"], question=weakest["question"], label=weakest["label"],
                                           timestamp=weakest["timestamp"])))
    students.sort(key=lambda x: (x["balanced"] is None, x["balanced"] if x["balanced"] is not None else 0))
    low_group = [s for s in students if s["ABL"] is not None and s["ABL"] < 0.4]
    disagree = sum("disagree" in i["flags"] for i in job["items"])
    return dict(
        base, ready=True, students_count=len(table), distribution=dist, hot=hot, low=low,
        headline=f"本批 {len(rows)} 条已标注提问中，高阶（分析及以上）占 {hot:.0%}，记忆/理解占 {low:.0%}。",
        finding=dict(count=len(low_group), total=len(table),
                     text=f"{len(low_group)} 名学生的提问平均停留在记忆/理解层级",
                     example=low_group[0] if low_group else (students[0] if students else None)),
        disagree=disagree,
        attention=students[:5], students=students,
        actions=[
            "对“需关注”名单中的学生，下次课先布置一道需要解释或比较的任务，再允许使用 AI。",
            f"对模型意见不一致的 {disagree} 条提问安排人工抽查，而不是直接采用多数结果。" if disagree else
            "抽查一部分模型意见一致的提问，确认一致的判断也是对的。",
            "期末前补一次不使用 AI 的迁移题，才能判断学习是否真的提升。",
        ],
        limits=[
            "标签来自模型判断的缓存回放，尚未经过人工正式校准。" + ("本批为合成教学数据，不代表真实学生。" if base["synthetic"] else ""),
            "现有日志只能描述提问过程，不能说明 AI 让成绩提高了多少。",
            f"{not_run} 条记录因未接入模型 API 尚未标注，未计入上述比例。" if not_run else "本批所有记录均已完成标注。",
        ],
    )


@router.get("/jobs/{key}/report")
def report(key: str, x_workbench_token: str | None = Header(default=None)):
    job = load("jobs", key, x_workbench_token)
    return clean(teacher_report(job, load("datasets", job["dataset"], x_workbench_token)["records"]))


def report_markdown(r):
    if not r["ready"]:
        return f"# 教师报告\n\n{r['message']}\n"
    lines = [f"# 教师报告 · {r['dataset']}", "", f"生成时间：{r['generated']}　任务：{r['job']}", "", r["headline"], "",
             "## 认知层级分布", "", "| 层级 | 条数 |", "|---|---|"]
    lines += [f"| L{k + 1} {LEVELS[k]} | {n} |" for k, n in enumerate(r["distribution"])]
    lines += ["", "## 需关注的学生", ""]
    lines += [f"- {s['student']}（{s['n']} 条，均衡 AIV {s['balanced']:.1f}）：{'；'.join(s['tips'])}" if s["balanced"] is not None
              else f"- {s['student']}（{s['n']} 条，AIV 不可判断）：{'；'.join(s['tips'])}" for s in r["attention"]]
    example = (r.get("finding") or {}).get("example")
    if example and example.get("evidence", {}).get("question"):
        e = example["evidence"]
        lines += ["", f"证据示例（{example['student']}，{e['timestamp']}，L{e['label']}）：“{e['question']}”"]
    lines += ["", "## 建议行动", ""] + [f"{i + 1}. {a}" for i, a in enumerate(r["actions"])]
    lines += ["", "## 使用边界", ""] + [f"- {x}" for x in r["limits"]]
    return "\n".join(lines) + "\n"


# ---------- 7. 导出复现材料 ----------

REPRODUCE = """# 复现说明

本包由认知证据实验室工作台导出。任务模式：{mode}（缓存回放，未调用模型 API）。

1. 在项目根目录按 docs/协作环境安装指南.md 建立环境。
2. 启动服务：`.venv/Scripts/python.exe -m uvicorn aiv.server:app --host 127.0.0.1 --port 8765`
3. 在「导入资料」选择数据集 `{dataset}`，新建任务并运行至完成。
4. 对照本包 labels.csv、metrics.csv、sensitivity.csv；指标定义见 docs/指标定义与性质.md。
5. 用 manifest.json 中的 SHA-256 核验各文件未被改动。

权重方案：{weights}
"""


def frame_csv(rows):
    out = io.StringIO()
    pd.DataFrame(rows).to_csv(out, index=False)
    return out.getvalue().encode("utf-8-sig")


@router.get("/jobs/{key}/export")
def export(key: str, x_workbench_token: str | None = Header(default=None)):
    job = load("jobs", key, x_workbench_token)
    data = load("datasets", job["dataset"], x_workbench_token)
    private = data["private"]
    # Private exports keep provenance (file, row, hash) but never the student's question text.
    labels = [dict(id=i["id"], student=i["student"], timestamp=i["timestamp"],
                   **({} if private else dict(question=i["question"])),
                   **{f"model_{k + 1}": x["level"] for k, x in enumerate(i["independent"])},
                   final=i["final"], constructed=i["constructed"], status=i["status"], flags="|".join(i["flags"]))
              for i in job["items"]]
    cmp = compare(key, x_workbench_token)
    rep = teacher_report(job, data["records"])
    if private:
        for s in rep.get("students", []):
            s.get("evidence", {}).pop("question", None)
    files = {
        "labels.csv": frame_csv(labels),
        "metrics.csv": frame_csv(cmp["students"]),
        "sensitivity.csv": frame_csv(cmp["sensitivity"]),
        "quality.json": json.dumps(clean(data["quality"]), ensure_ascii=False, indent=2).encode("utf-8"),
        "teacher-report.md": report_markdown(clean(rep)).encode("utf-8"),
        "job-log.json": json.dumps(clean(job["log"]), ensure_ascii=False, indent=2).encode("utf-8"),
        "REPRODUCE.md": REPRODUCE.format(mode=job["mode"], dataset=job["dataset"],
                                         weights=json.dumps(cmp["weights"], ensure_ascii=False)).encode("utf-8"),
    }
    manifest = dict(job=job["id"], dataset=job["dataset"], private=private, mode=job["mode"],
                    exported=time.strftime("%Y-%m-%dT%H:%M:%S"), processed=len(job["items"]), total=job["total"],
                    api_called=False, question_text_included=not private,
                    files={k: hashlib.sha256(v).hexdigest() for k, v in files.items()})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return Response(buffer.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{job["id"]}-reproduce.zip"',
                             "Cache-Control": "no-store"})


# ---------- 人工核验（只读封存） ----------

@router.get("/review-summary")
def review_summary():
    """Read-only aggregate counts; never calls next()/advance(), so no timer or assignment changes."""
    import sqlite3
    path = ROOT / "runtime/research/review.sqlite3"
    if not path.exists():
        return dict(available=False, rounds=[])
    try:
        # mode=ro: opening the sealed ledger must not run ReviewStore's schema migration or touch clocks.
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT round_id,mode,COUNT(*) assigned,SUM(submitted IS NOT NULL) completed "
                "FROM assignments GROUP BY round_id,mode ORDER BY MIN(rowid)").fetchall()
        return dict(available=True, rounds=[dict(r) for r in rows])
    except sqlite3.Error:
        return dict(available=False, rounds=[])
