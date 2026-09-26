"""Offline notebook inputs and traceable, publication-ready figure exports."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time

import matplotlib.pyplot as plt
from matplotlib import font_manager
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "build/notebooks"
COLORS = {"25f": "#176B77", "26s": "#BC632F", "synthetic": "#566C9C"}


def atomic_write_text(path, content, encoding="utf-8"):
    """Replace a complete export; bound retries for transient Windows locks."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent,
                prefix="." + path.name + ".", suffix=".tmp", encoding=encoding,
                newline="\n", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        for attempt in range(4):
            try:
                os.replace(temporary, path)
                return
            except OSError as error:
                transient = isinstance(error, PermissionError) or error.errno in (13, 22)
                if not transient or attempt == 3:
                    error.add_note(f"Atomic export failed after {attempt + 1} attempts: {path}")
                    raise
                time.sleep(.1 * 2**attempt)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def atomic_save_figure(fig, path, fmt):
    """Render beside the target, then replace it without exposing partial files."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(4):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("wb", dir=path.parent,
                    prefix="." + path.stem + ".", suffix="." + fmt,
                    delete=False) as stream:
                temporary = Path(stream.name)
            fig.savefig(temporary, format=fmt, bbox_inches="tight", facecolor="white")
            os.replace(temporary, path)
            return
        except Exception as error:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    error.add_note(f"Temporary figure cleanup failed: {temporary}: {cleanup_error}")
            transient = isinstance(error, OSError) and (
                isinstance(error, PermissionError) or error.errno in (13, 22))
            if not transient or attempt == 3:
                error.add_note(f"Atomic figure export failed after {attempt + 1} attempts: {path}")
                raise
            time.sleep(.1 * 2**attempt)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def configure_style():
    # An inherited MPLBACKEND=Agg is appropriate for tests/CLI, but a live
    # Notebook kernel must register its display hook to retain inline figures.
    from IPython import get_ipython
    shell = get_ipython()
    if shell is not None and getattr(shell, "kernel", None) is not None:
        shell.run_line_magic("matplotlib", "inline")
        from matplotlib_inline.backend_inline import set_matplotlib_formats
        set_matplotlib_formats("png")
    installed = {f.name for f in font_manager.fontManager.ttflist}
    fonts = [f for f in ["Microsoft YaHei", "Noto Sans CJK SC", "SimHei", "DejaVu Sans"] if f in installed]
    plt.rcParams.update({
        "figure.figsize": (8.8, 4.6), "figure.dpi": 120, "savefig.dpi": 220,
        "font.family": "sans-serif", "font.sans-serif": fonts,
        "font.size": 11, "axes.titlesize": 14, "axes.titlepad": 16,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.unicode_minus": False, "axes.labelcolor": "#273949",
        "text.color": "#273949", "xtick.color": "#273949", "ytick.color": "#273949",
        "figure.facecolor": "white", "axes.facecolor": "white",
        "pdf.fonttype": 42, "ps.fonttype": 42, "svg.fonttype": "path",
    })


def provenance(paths):
    return [{"path": p, "sha256": sha256(checked_local_path(p))} for p in paths]


def save_figure(fig, figure_id, caption, *, kind, sources, notebook):
    if not re.fullmatch(r"[a-z0-9-]+", figure_id):
        raise ValueError("Use a stable ASCII figure id")
    directory = OUT / "figures"
    directory.mkdir(parents=True, exist_ok=True)
    if fig.axes and not any(ax.get_title() for ax in fig.axes):
        fig.axes[0].set_title(caption)
        fig.tight_layout()
    paths = {}
    for fmt in ("pdf", "svg", "png"):
        path = directory / f"{figure_id}.{fmt}"
        atomic_save_figure(fig, path, fmt)
        paths[fmt] = str(path.relative_to(OUT)).replace("\\", "/")
    import nbformat
    notebook_source = nbformat.read(ROOT / "notebooks" / notebook, as_version=4)
    source_code = "\n\n".join(c.source for c in notebook_source.cells if c.cell_type == "code")
    meta = {"id": figure_id, "caption": caption, "kind": kind, "notebook": notebook,
            "notebook_code_sha256": hashlib.sha256(source_code.encode()).hexdigest(),
            "files": paths, "sources": provenance(list(dict.fromkeys(sources + ["aiv/notebook_materials.py"])))}
    atomic_write_text(directory / f"{figure_id}.json", json.dumps(meta, ensure_ascii=False, indent=2))
    return paths


def export_table(frame, table_id):
    directory = OUT / "tables"
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write_text(directory / f"{table_id}.csv", frame.to_csv(index=False), encoding="utf-8-sig")


def research_inputs():
    """Load local evidence once; return aggregates, never student text or identifiers."""
    records_path = ROOT / "runtime/research/records.json"
    audit_path = ROOT / "runtime/research/data-audit.json"
    if not records_path.exists() or not audit_path.exists():
        return None
    records, audit = read_json(records_path), read_json(audit_path)
    frame = pd.DataFrame(records)
    if frame.empty or not frame.id.is_unique:
        raise ValueError("Research record ids must be nonempty and unique")
    for source, group in frame.groupby("source_file"):
        hashes = set(group.source_sha256)
        if hashes != {sha256(checked_local_path(source))}:
            raise ValueError(f"Source hash mismatch: {source}")
    frame["date"] = pd.to_datetime(frame.timestamp, errors="raise")
    terms, availability, months = [], [], []
    for term, group in frame.groupby("term", sort=True):
        stats = audit[term]
        if len(group) != stats["selected_rows"]:
            raise ValueError(f"Audit denominator mismatch: {term}")
        terms.append({"term": term, "raw_rows": stats["raw_rows"], "text_records": len(group),
                      "students_in_term": group.student.nunique(),
                      "first_observed": str(group.date.min().date()), "last_observed": str(group.date.max().date())})
        for label, field in [("提问文本", "question"), ("时间戳", "timestamp"), ("智能体类型", "agent"),
                             ("会话边界", "session")]:
            valid = group[field].notna() & group[field].astype(str).str.strip().ne("")
            availability.append({"term": term, "field": label, "available": int(valid.sum()),
                                 "denominator": len(group), "fraction": float(valid.mean())})
        counts = group.groupby(group.date.dt.to_period("M")).size()
        observed_months = pd.period_range(group.date.min(), group.date.max(), freq="M")
        for month, count in counts.reindex(observed_months, fill_value=0).items():
            months.append({"term": term, "month": str(month), "records": int(count)})
    return {"terms": pd.DataFrame(terms), "availability": pd.DataFrame(availability),
            "monthly": pd.DataFrame(months), "audit": audit,
            "unique_students_all_terms": int(frame.student.nunique()),
            "sources": provenance(["runtime/research/records.json", "runtime/research/data-audit.json"])}


def final_archive_summary():
    path = ROOT / "runtime/research/final-verification-export.json"
    if not path.exists():
        return None
    export = read_json(path)
    summary = export["summary"]
    if not summary["closed"] or summary["completed"] != summary["total"]:
        raise ValueError("Final human archive is not closed")
    cases = export["cases"]
    if len(cases) != summary["total"] or any(not c["decision"]["confirmed"] for c in cases):
        raise ValueError("Final archive does not reconcile")
    return {"round": summary["round_id"], "completed": summary["completed"], "total": summary["total"],
            "status": "已封存", "source": str(path.relative_to(ROOT)).replace("\\", "/"),
            "source_sha256": sha256(path),
            "closed_at_utc": datetime.fromtimestamp(summary["closed_at"], timezone.utc).isoformat(),
            "role": "AI 辅助最终裁定；用于规则记录"}


def pending_results(batch=None, materials=None):
    """Report the next evidence requirement for the explicitly loaded branch."""
    rows = [
        ("batch_coverage", "真实回合结算完成率", "等待冻结批次的结算完成数与计划数"),
        ("review_agreement", "两快独立标注与两强独立复核", "等待分阶段配对分母与冻结输出"),
        ("repeat_stability", "同模型重复稳定性", "等待可比较的重复调用"),
        ("runtime_cost", "调用账本与 token", "等待冻结运行账本"),
        ("real_distribution", "真实任务层级分布", "等待冻结标签；说明抽样范围"),
        ("real_calibration", "真实总体校准结果", "需要独立人工参考标签及适用的抽样设计"),
        ("real_ctq", "真实会话转移质量 CTQ", "需要经核验的相邻回合、会话边界与时间顺序"),
        ("real_aiv", "完整真实 AIV 与排名", "需要五项指标的完整可观测性及误差审计"),
        ("learning_gain", "独立学习增量", "目前缺少独立学习结果与识别设计"),
    ]
    result = pd.DataFrame(rows, columns=["id", "item", "dependency"])
    result["estimate"] = pd.Series([pd.NA] * len(result), dtype="Float64")
    result["denominator"] = pd.Series([pd.NA] * len(result), dtype="Int64")
    result["status"] = "待接入"
    result.loc[result.id.isin(["real_ctq", "real_aiv", "learning_gain"]), "status"] = "当前不可识别"
    if batch is not None:
        planned, completed = batch["planned_real"], batch["completed_real"]
        def describe(identifier, status, dependency):
            result.loc[result.id.eq(identifier), ["status", "dependency"]] = [status, dependency]
        describe("batch_coverage", "已接入冻结快照范围",
                 "计划与完成分母已核对；新增结果须另行冻结针定" if completed == planned else
                 f"冻结时仍有 {planned - completed} 个真实回合未完成；补齐后须接入新的冻结快照")
        result.loc[result.id.eq("batch_coverage"), "estimate"] = completed / planned if planned else pd.NA
        result.loc[result.id.eq("batch_coverage"), "denominator"] = planned
        paired = any(row.get("paired_labeled", 0) for row in batch.get("agreement", []))
        describe("review_agreement", "已接入分阶段配对范围" if paired else "已冻结；数字标签配对不足",
                 "分阶段配对分母见上表；准确率仍需独立参考标签" if paired else
                 "计算数字标签一致率仍需双方非空的成对输出；结构化弃权另列")
        comparable = any(row.get("comparable", 0) for row in batch.get("repeat", []))
        describe("repeat_stability", "已接入可比复跑" if comparable else "已冻结；可比复跑不足",
                 "有效配对分母见复跑表；独立调用与缓存复用须结合账本区分" if comparable else
                 "仍需同模型同题两次有效输出；失败、未创建及弃权不能作为相同数字标签")
        ledger = batch.get("ledger", [])
        unknown = sum(row.get("unknown_usage_attempts", 0) for row in ledger)
        describe("runtime_cost", "已接入已报告用量" if ledger else "已冻结；运行账本未接入",
                 f"仍有 {unknown} 次尝试用量未知；货币成本需另核计价" if unknown else
                 ("已报告用量见账本；货币成本需另核计价" if ledger else "仍需与快照对应的调用账本"))
        labeled = batch.get("candidate_labeled_real", 0)
        describe("real_distribution", "仅已完成子集可描述" if completed < planned else
                 ("已接入冻结候选范围" if labeled else "暂无数字候选标签"),
                 "候选与弃权按冻结分母分列；模型标签的准确率仍需独立参考标签")
    if materials is not None:
        availability = materials["summary"].get("score_availability", {})
        total = availability.get("student_term_rows", 0)
        for identifier, key in (("real_ctq", "CTQ_available"), ("real_aiv", "full_AIV_available")):
            available = availability.get(key, 0)
            result.loc[result.id.eq(identifier), "denominator"] = total
            if available:
                result.loc[result.id.eq(identifier), "status"] = f"仅报告可用子集：{available}/{total} 行"
                result.loc[result.id.eq(identifier), "dependency"] += "；可用指标不等同独立学习增量"
    return result


def checked_local_path(value):
    # Frozen inputs retain Windows separators; normalize only while resolving
    # the local file so the source bytes and their pinned hashes stay intact.
    path = (ROOT / os.fspath(value).replace("\\", "/")).resolve()
    if not path.is_relative_to(ROOT.resolve()):
        raise ValueError("Input must be inside this workspace")
    return path


def load_frozen_batch(config=None):
    """Explicit opt-in to completed, hashed files; never discover a running batch."""
    config = config or ROOT / "notebooks/research-inputs.json"
    entry = read_json(config)["agent_batch"]
    if entry is None:
        return None
    if entry.get("status") != "complete":
        raise ValueError("Agent batch must be explicitly marked complete")
    data = {}
    for name in ("summary", "annotations"):
        path = checked_local_path(entry[f"{name}_path"])
        if sha256(path) != entry[f"{name}_sha256"]:
            raise ValueError(f"Frozen {name} hash mismatch")
        data[name] = read_json(path)
    summary, rows = data["summary"], data["annotations"]
    if not rows or any(r.get("complete") is not True for r in rows):
        raise ValueError("Refusing unfinished annotation results")
    ids = [r["record"]["id"] for r in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate annotation ids")
    for scope in ("real", "synthetic"):
        selected = [r for r in rows if (r["record"]["term"] == "synthetic") == (scope == "synthetic")]
        observed = summary["scopes"][scope]
        if observed["records"] != len(selected) or observed["completed"] != len(selected):
            raise ValueError(f"Batch summary denominator mismatch: {scope}")
        counts = dict(Counter(str(r["final"]) for r in selected))
        if counts != observed["final_label_counts"]:
            raise ValueError(f"Batch label counts mismatch: {scope}")
    return summary  # Only aggregate fields reach notebook displays.


def turn_manifest_summary(path=None):
    """Validate the private turn package and expose counts, never its text."""
    path = Path(path or ROOT / "runtime/research/turns-v1/manifest.json")
    if not path.exists():
        return None
    manifest = read_json(path)
    if manifest.get("schema_version") != "csv-qa-turns-v1":
        raise ValueError("Unsupported turn-manifest schema")
    for name, expected in manifest["files"].items():
        if name not in {"turns.jsonl", "student_turns.jsonl", "record_audit.jsonl",
                        "docx_source_inventory.json"}:
            raise ValueError("Unexpected turn-manifest input")
        if sha256(path.parent / name) != expected:
            raise ValueError(f"Turn source hash mismatch: {name}")
    counts = manifest["counts"]
    if (counts["selected_records"] != counts["session_candidates"]
            or counts["student_turns"] + counts["assistant_turns"] != counts["all_messages"]
            or counts["model_eligible_student_turns"] > counts["student_turns"]
            or sum(t["student_turns"] for t in counts["by_term"].values()) != counts["student_turns"]
            or sum(t["model_eligible_student_turns"] for t in counts["by_term"].values())
            != counts["model_eligible_student_turns"]):
        raise ValueError("Turn manifest denominators do not reconcile")
    checks = manifest["checks"]
    if (checks["turn_id_unique"] is not True
            or checks["source_text_spans_valid"] is not True
            or checks["first_question_exact_matches"] != counts["selected_records"]
            or checks["docx_source_hashes_verified"] != checks["docx_maps"]):
        raise ValueError("Turn manifest source checks failed")
    return {"counts": counts, "checks": checks,
            "source": str(path.relative_to(ROOT)).replace("\\", "/"),
            "sha256": sha256(path), "source_scope": manifest["source_scope"]}


def _paired_agreement(rows, stage, field):
    structured = same_structured = labeled = same_labeled = 0
    for row in rows:
        entries = row[stage]
        if len(entries) != 2 or any(e.get("judgment") is None for e in entries):
            continue
        values = [e["judgment"].get(field) for e in entries]
        structured += 1
        same_structured += values[0] == values[1]
        if all(value in range(1, 7) for value in values):
            labeled += 1
            same_labeled += values[0] == values[1]
    return {"paired_structured": structured, "same_including_abstain": same_structured,
            "paired_labeled": labeled, "same_labeled": same_labeled,
            "labeled_agreement": same_labeled / labeled if labeled else None}


def _repeat_agreement(rows, field):
    counts = Counter()
    for row in rows:
        original = {e["model"]: e for e in row["independent"]}
        for repeated in row["repeat"]:
            first = original.get(repeated["model"])
            if not first or first.get("judgment") is None or repeated.get("judgment") is None:
                continue
            model = repeated["model"]
            counts[(model, "comparable")] += 1
            counts[(model, "equal")] += (first["judgment"].get(field)
                                            == repeated["judgment"].get(field))
    return [{"model": model, "dimension": field, "comparable": counts[(model, "comparable")],
             "equal": counts[(model, "equal")],
             "stability": counts[(model, "equal")] / counts[(model, "comparable")]}
            for model in sorted({model for model, _ in counts})]


def load_turn_snapshot(config=None):
    """Read a fixed, hashed full-turn snapshot into safe aggregate tables.

    A partial *frozen* snapshot is useful evidence of completed scope; unfinished
    turns remain in the denominator and never silently become abstentions.
    """
    config = Path(config or ROOT / "notebooks/research-inputs.json")
    entry = read_json(config).get("turn_snapshot")
    if entry is None:
        return None
    manifest_path = checked_local_path(entry["manifest_path"])
    if manifest_path.name != "snapshot-manifest.json" or manifest_path.parent.name != "frozen":
        raise ValueError("Only an explicit frozen turn snapshot is accepted")
    if sha256(manifest_path) != entry["manifest_sha256"]:
        raise ValueError("Frozen snapshot manifest hash mismatch")
    manifest = read_json(manifest_path)
    if manifest.get("schema_version") != 1 or manifest.get("status") not in {
            "completed", "partial", "manual_snapshot"}:
        raise ValueError("Unsupported frozen snapshot status")
    if set(manifest["files"]) != {"annotations.json", "summary.json"}:
        raise ValueError("Frozen snapshot has unexpected files")
    for name, expected in manifest["files"].items():
        if sha256(manifest_path.parent / name) != expected:
            raise ValueError(f"Frozen turn {name} hash mismatch")
    rows = read_json(manifest_path.parent / "annotations.json")
    summary = read_json(manifest_path.parent / "summary.json")
    if summary.get("schema_version") != 4 or summary["experiment"] != manifest["experiment"]:
        raise ValueError("Frozen turn summary identity mismatch")
    ids = [row["record"]["id"] for row in rows]
    if len(ids) != len(set(ids)) or len(rows) != summary["n"]:
        raise ValueError("Frozen turn ids or row count do not reconcile")
    real = [row for row in rows if row["record"]["term"] != "synthetic"]
    controls = [row for row in rows if row["record"]["term"] == "synthetic"]
    real_done = [row for row in real if row["complete"]]
    controls_done = [row for row in controls if row["complete"]]
    if (len(real) != manifest["real_planned"]
            or len(controls) != manifest["synthetic_controls"]
            or len(real_done) != manifest["real_completed"]
            or len(real_done) + len(controls_done) != manifest["records_completed"]
            or len(real_done) + len(controls_done) != summary["completed_records"]
            or sum(row["final"] is not None for row in real_done)
            != manifest["real_candidate_labeled"]
            or sum(row["review_selected"] for row in real)
            != manifest["review_selected"]
            or manifest["review_selected"] != summary["review_real_selected"]):
        raise ValueError("Frozen turn denominators do not reconcile")
    for stage in ("independent", "review", "repeat"):
        entries = [e for row in rows for e in row[stage]]
        observed = summary["stages"][stage]
        if (len(entries) != observed["requested"]
                or sum(e["judgment"] is not None for e in entries) != observed["valid"]
                or sum(e["status"] == "failed" for e in entries) != observed["failed"]):
            raise ValueError(f"Frozen turn stage mismatch: {stage}")
    by_term = []
    for term in sorted({row["record"]["term"] for row in real}):
        selected = [row for row in real if row["record"]["term"] == term]
        done = [row for row in selected if row["complete"]]
        by_term.append({"term": term, "planned": len(selected), "completed": len(done),
                        "candidate_labeled": sum(row["final"] is not None for row in done),
                        "unknown_or_abstained": sum(row["final"] is None for row in done),
                        "unfinished": len(selected) - len(done)})
    agreement = []
    for stage in ("independent", "review"):
        for dimension, field in (("task", "level"), ("contribution", "contribution_level")):
            agreement.append({"stage": stage, "dimension": dimension,
                              **_paired_agreement(real, stage, field)})
    repeat = [_repeat_agreement(real, field)
              for field in ("level", "contribution_level")]
    label_counts = dict(Counter(str(row["final"]) for row in real_done))
    return {"experiment": manifest["experiment"], "revision": summary["revision"],
            "status": manifest["status"], "frozen_at": manifest["frozen_at"],
            "source": str(manifest_path.relative_to(ROOT)).replace("\\", "/"),
            "source_sha256": sha256(manifest_path),
            "planned_real": len(real), "completed_real": len(real_done),
            "planned_controls": len(controls), "completed_controls": len(controls_done),
            "candidate_labeled_real": manifest["real_candidate_labeled"],
            "by_term": by_term, "label_counts": label_counts,
            "review_random_preselected": manifest["random_review_preselected"],
            "review_selected_real": manifest["review_selected"],
            "stages": summary["stages"], "agreement": agreement,
            "repeat": [item for group in repeat for item in group],
            "ledger": summary["pool"].get("ledger", []),
            "paused_models": summary.get("paused_models", [])}


def load_turn_plan(config=None):
    """Read only the pinned plan's public denominators, never running outputs."""
    config = Path(config or ROOT / "notebooks/research-inputs.json")
    entry = read_json(config).get("turn_plan")
    if entry is None:
        return None
    path = checked_local_path(entry["path"])
    if sha256(path) != entry["sha256"]:
        raise ValueError("Pinned turn plan hash mismatch")
    plan = read_json(path)
    if plan.get("revision") != "t3" or sum(plan["term_counts"].values()) != plan["real"]:
        raise ValueError("Expected a reconciled t3 plan")
    return {"experiment": plan["experiment"], "revision": plan["revision"],
            "planned_real": plan["real"], "planned_controls": plan["controls"],
            "term_counts": plan["term_counts"],
            "random_review_preselected": len(plan["audit_ids"]),
            "repeat_records": len(plan["repeat_ids"]),
            "repeat_expected_tasks": len(plan["repeat_ids"]) * 2,
            "source": path.relative_to(ROOT).as_posix(), "source_sha256": sha256(path)}


def load_full_turn_materials(config=None):
    """Validate the derived t3 package and return only safe aggregate JSONs."""
    config = Path(config or ROOT / "notebooks/research-inputs.json")
    entry = read_json(config).get("turn_materials")
    if entry is None:
        return None
    snapshot = load_turn_snapshot(config)
    if snapshot is None:
        raise ValueError("Derived materials require a pinned frozen snapshot")
    path = checked_local_path(entry["manifest_path"])
    if path.name != "manifest.json" or sha256(path) != entry["manifest_sha256"]:
        raise ValueError("Derived material manifest hash mismatch")
    manifest = read_json(path)
    source = manifest["source"]
    if (source["revision"] != "t3" or source["experiment"] != snapshot["experiment"]
            or source["manifest_sha256"] != snapshot["source_sha256"]):
        raise ValueError("Derived materials do not match the pinned t3 snapshot")
    safe_files = {"summary.json", "review-strata.json", "repeatability.json", "data-quality.json"}
    if not safe_files <= set(manifest["files"]):
        raise ValueError("Derived materials are incomplete")
    for name, expected in manifest["files"].items():
        candidate = (path.parent / name.replace("\\", "/")).resolve()
        if not candidate.is_relative_to(path.parent) or sha256(candidate) != expected:
            raise ValueError("Derived material file hash mismatch")
    output = {}
    for name in safe_files:
        value = read_json(path.parent / name)
        if value.get("source") != source:
            raise ValueError("Derived material source mismatch")
        output[name.removesuffix(".json").replace("-", "_")] = value
    counts = output["summary"]["denominators"]
    for field in ("planned_real", "completed_real", "candidate_labeled_real"):
        if counts[field] != snapshot[field]:
            raise ValueError("Derived material denominator mismatch")
    if counts["unfinished_real"] + counts["completed_real"] != counts["planned_real"]:
        raise ValueError("Derived unfinished denominator mismatch")
    output["source"] = path.relative_to(ROOT).as_posix()
    output["source_sha256"] = sha256(path)
    return output


def offline_monthly(monthly):
    import plotly.express as px
    directory = OUT / "interactive"
    directory.mkdir(parents=True, exist_ok=True)
    plot = px.bar(monthly, x="month", y="records", color="term", color_discrete_map=COLORS,
                  labels={"month": "自然月", "records": "文本记录数（条）", "term": "学期"},
                  title="真实文本记录的月度分布 · 点击图例筛选学期")
    plot.update_layout(template="plotly_white", font_family="Microsoft YaHei, sans-serif",
                       yaxis_rangemode="tozero", margin=dict(b=150))
    plot.update_xaxes(type="category", categoryorder="array", categoryarray=sorted(monthly.month.unique()), tickangle=0)
    plot.add_annotation(text="来源：records.json；每条记录的首个文本问题。首末月为部分观察月，学期窗口不同。",
                        xref="paper", yref="paper", x=0, y=-0.20, showarrow=False, xanchor="left", font_size=12)
    html = plot.to_html(include_plotlyjs=True, full_html=True,
                        config={"displaylogo": False, "responsive": True})
    atomic_write_text(directory / "real-monthly.html", html)
