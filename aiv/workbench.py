"""Private operations view over a frozen, manifest-driven annotation run.

This module never serves source text to the public demo. Imported files are
staged separately and cannot silently change a running experiment's sample.
"""

import csv
import hashlib
import html
import io
import json
import os
import subprocess
import sys
import time
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from dotenv import dotenv_values
from . import workbench_imports

ALLOWED_SOURCE = {"data/25f/csv/qa.csv", "data/26s/csv/qa.csv"}


def _json(path, default=None):
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _level(entry):
    judgment = entry.get("judgment") or {}
    return judgment.get("level")


class Workbench:
    def __init__(self, root, plan_id=None):
        self.root = Path(root).resolve()
        self.private = self.root / "runtime" / "workbench"
        self._lock = RLock()
        self.plan_id = plan_id
        self._context = None

    def plans(self):
        research = self.root / "runtime/research"
        paths = list(research.glob("full-turns-v1-*/t*/plan.json")) + list(research.glob("workbench-*/t1/plan.json"))
        return [{"plan_id": p.parent.parent.name + ":" + p.parent.name,
                 "experiment": p.parent.parent.name, "revision": p.parent.name}
                for p in sorted(paths, key=lambda p: p.stat().st_mtime_ns, reverse=True)
                if p.resolve().is_relative_to(research.resolve())]

    def select(self, plan_id=None):
        view = Workbench(self.root, plan_id)
        view._context = view._read_context()
        return view

    def run_enabled(self):
        settings = {**dotenv_values(self.root / ".env"), **os.environ}
        return settings.get("WORKBENCH_ALLOW_RUN", "1") == "1"

    def plan_path(self):
        if self._context is not None:
            return self._context["path"]
        if self.plan_id:
            if self.plan_id not in {p["plan_id"] for p in self.plans()}:
                raise ValueError("未登记的工作台批次")
            experiment, revision = self.plan_id.split(":")
            return self.root / "runtime/research" / experiment / revision / "plan.json"
        override = os.getenv("WORKBENCH_PLAN", "")
        if override:
            path = Path(override).resolve()
            if not path.is_relative_to((self.root / "runtime" / "research").resolve()):
                raise ValueError("工作台计划必须位于私有研究目录")
            return path if path.is_file() else None
        plans = list((self.root / "runtime" / "research").glob("full-turns-v1-*/t*/plan.json"))
        return max(plans, key=lambda p: p.stat().st_mtime_ns) if plans else None

    def _read_context(self):
        path = self.plan_path()
        if path is None:
            return {"path": None, "plan": {}, "rows": [], "summary": {}, "frozen": {}, "quality": {}}
        plan = _json(path, {})
        directory = path.parent
        frozen = _json(directory / "frozen/snapshot-manifest.json", {})
        if frozen and frozen.get("sample_sha256") != plan.get("sample_sha256"):
            raise ValueError("冻结快照与计划的样本哈希不一致")
        source = directory / "frozen" if frozen else directory
        raw = {}
        for name in ("annotations.json", "summary.json"):
            file = source / name
            data = file.read_bytes() if file.exists() else None
            if frozen and (not data or frozen.get("files", {}).get(name) != _sha(data)):
                raise ValueError("冻结快照文件缺失或哈希不一致")
            raw[name] = json.loads(data) if data else ([] if name == "annotations.json" else {})
        quality = plan.get("source_quality")
        if quality is None:
            manifest = self.root / "runtime/research/turns-v1/manifest.json"
            quality = _json(manifest, {})
            expected = plan.get("turn_manifest_sha256")
            if expected and (not manifest.exists() or _sha(manifest.read_bytes()) != expected):
                quality = {"counts": {}, "checks": {"plan_source_manifest_matches": False},
                           "boundaries": ["本机来源清单与所选批次哈希不一致，未展示其他批次的质量统计"],
                           "source_scope": plan.get("source_manifest_scope")}
        return {"path": path, "plan": plan, "rows": raw["annotations.json"],
                "summary": raw["summary.json"], "frozen": frozen, "quality": quality,
                "captured_at": time.time(), "outcome": _json(directory / "run-outcome.json", {})}

    def context(self):
        return self._context if self._context is not None else self._read_context()

    def run_state(self):
        context = self.context()
        plan_path = context["path"]
        if plan_path is None:
            return {"available": False, "message": "尚无全量测评计划"}
        plan = context["plan"]
        directory = plan_path.parent
        summary = context["summary"]
        outcome = context["outcome"]
        frozen = context["frozen"]
        pool = summary.get("pool") or {}
        tasks = pool.get("tasks") or {}
        stopped = time.time() >= plan.get("stop_claiming_at", 0) or (directory / "STOP").exists()
        last_update = summary.get("updated_at") or 0
        # A live worker refreshes summary; the subprocess lock remains the final guard.
        active = not frozen and bool(tasks.get("running")) and time.time() - last_update < 120
        real_rows = [r for r in context["rows"] if r.get("record", {}).get("term") != "synthetic"]
        return {
            "available": True, "experiment": plan.get("experiment"),
            "plan_id": plan_path.parent.parent.name + ":" + plan_path.parent.name,
            "captured_at": context["captured_at"],
            "revision": plan.get("revision"), "version": plan.get("version"),
            "planned_real": plan.get("real"), "controls": plan.get("controls"),
            "source_scope": plan.get("source_manifest_scope"),
            "sample_sha256": plan.get("sample_sha256"),
            "stop_claiming_at": plan.get("stop_claiming_at"),
            "stopped": stopped, "active": active,
            "run_enabled": self.run_enabled(),
            "status": frozen.get("status") or outcome.get("status") or ("running" if active else "waiting"),
            "completed_records": sum(bool(r.get("complete")) for r in context["rows"]),
            "basic_complete_records": summary.get("basic_complete_records", 0),
            "candidate_labels": sum(bool(r.get("complete")) and isinstance(r.get("final"), int) for r in context["rows"]),
            "real_completed": sum(bool(r.get("complete")) for r in real_rows),
            "real_candidate_labels": sum(bool(r.get("complete")) and isinstance(r.get("final"), int) for r in real_rows),
            "review_selected": summary.get("review_real_selected", 0),
            "review_random_selected": summary.get("review_random_selected", 0),
            "review_high_risk_selected": summary.get("review_high_risk_selected", 0),
            "stages": summary.get("stages", {}), "tasks": tasks,
            "concurrency": summary.get("concurrency_limit"),
            "updated_at": last_update, "frozen": bool(frozen),
            "snapshot": {k: frozen.get(k) for k in (
                "status", "frozen_at", "real_completed", "real_candidate_labeled", "sample_sha256"
            )} if frozen else None,
        }

    def source_quality(self):
        manifest = self.context()["quality"]
        return {"counts": manifest.get("counts", {}), "checks": manifest.get("checks", {}),
                "boundaries": manifest.get("boundaries", []),
                "source_scope": manifest.get("source_scope")}

    def rows(self):
        return self.context()["rows"]

    def model_comparison(self):
        by_model = {}
        for row in self.rows():
            if row.get("record", {}).get("term") == "synthetic":
                continue
            for stage in ("independent", "review", "repeat"):
                for entry in row.get(stage, []):
                    key = (stage, entry.get("model") or "unknown")
                    stats = by_model.setdefault(key, Counter())
                    stats["total"] += 1
                    status = entry.get("status") or "missing"
                    stats[status] += 1
                    if entry.get("judgment"):
                        stats["valid"] += 1
                        label = _level(entry)
                        stats["abstained" if label is None else f"L{label}"] += 1
        return [{"stage": stage, "model": model, **dict(counts)}
                for (stage, model), counts in sorted(by_model.items())]

    def cases(self, query="", issue="all", offset=0, limit=25):
        words = query.casefold().split()
        found = []
        for item in self.rows():
            record = item.get("record", {})
            if record.get("term") == "synthetic":
                continue
            task = ((item.get("dimensions") or {}).get("task") or {}).get("final") or {}
            flags = []
            if record.get("parse_status") != "structural_alternating_unverified":
                flags.append("source_parse")
            if not item.get("complete"):
                flags.append("unfinished")
            if task.get("status") in ("technical_failure", "disagreement", "uncertain"):
                flags.append(task["status"])
            if task.get("status") == "abstained":
                flags.append("abstained")
            if item.get("review_selected"):
                flags.append("reviewed")
            haystack = " ".join(str(record.get(k, "")) for k in
                                ("id", "term", "student", "question", "session_candidate_id")).casefold()
            if not all(word in haystack for word in words):
                continue
            if issue != "all" and issue not in flags:
                continue
            found.append({"id": record.get("id"), "term": record.get("term"),
                          "student": record.get("student"),
                          "turn_index": record.get("turn_index"),
                          "session_candidate_id": record.get("session_candidate_id"),
                          "preview": str(record.get("question", ""))[:180],
                          "label": task.get("label"), "status": task.get("status"),
                          "complete": bool(item.get("complete")), "flags": flags})
        return {"total": len(found), "offset": offset, "items": found[offset:offset + limit]}

    def detail(self, turn_id):
        item = next((r for r in self.rows() if r.get("record", {}).get("id") == turn_id), None)
        if item is None:
            raise KeyError(turn_id)
        record = item.get("record", {})
        source = record.get("source") or {}
        relative = str(source.get("file", "")).replace("\\", "/")
        verification = {"valid": False, "reason": "无原文来源映射"}
        if source.get("import_id"):
            verification = workbench_imports.verify_source(self.root, record)
        if relative in ALLOWED_SOURCE:
            path = (self.root / relative).resolve()
            if path.is_relative_to((self.root / "data").resolve()) and path.exists():
                digest = _sha(path.read_bytes())
                if digest == source.get("sha256"):
                    with path.open(encoding="utf-8-sig", newline="") as stream:
                        source_rows = list(csv.DictReader(stream))
                    row_number = source.get("row")
                    if isinstance(row_number, int) and 2 <= row_number <= len(source_rows) + 1:
                        raw = source_rows[row_number - 2].get("问答记录", "")
                        start, end = source.get("char_start"), source.get("char_end")
                        if isinstance(start, int) and isinstance(end, int) and 0 <= start <= end <= len(raw) and raw[start:end] == record.get("question"):
                            verification = {"valid": True, "source_file": relative,
                                            "source_row": row_number, "char_start": start,
                                            "char_end": end, "sha256": digest,
                                            "record_id": source.get("record_id"),
                                            "raw_qa": raw}
                        else:
                            verification = {"valid": False, "reason": "字符位置与提问不一致"}
                else:
                    verification = {"valid": False, "reason": "源文件哈希不一致"}
        return {"record": record, "independent": item.get("independent", []),
                "review": item.get("review", []), "repeat": item.get("repeat", []),
                "dimensions": item.get("dimensions"), "complete": item.get("complete"),
                "review_selection": item.get("review_selection"),
                "source_verification": verification}

    def teacher_report(self):
        context = self.context()
        real = [r for r in context["rows"] if r.get("record", {}).get("term") != "synthetic"]
        complete = [r for r in real if r.get("complete")]
        statuses = Counter((((r.get("dimensions") or {}).get("task") or {}).get("final") or {}).get("status", "missing")
                           for r in complete)
        levels = Counter(r.get("final") for r in complete if isinstance(r.get("final"), int))
        observations = []
        if statuses.get("abstained", 0) or statuses.get("disagreement", 0):
            observations.append("部分提问证据不足或判断分歧，建议教师结合学生修改过程与独立作答核对。")
        if levels.get(1, 0) + levels.get(2, 0) > sum(levels.get(i, 0) for i in (4, 5, 6)):
            observations.append("在已获候选标签的提问中，基础层级较多，可增加解释理由、方案比较与反思活动。")
        if not observations:
            observations.append("保持多种任务难度，优先采集独立作答和修改过程后再判断学习增量。")
        return {"title": "教师简报 · 认知证据",
                "scope": "已完成模型候选标注的学生提问；不代表学习成效或因果增量",
                "planned": context["plan"].get("real", len(real)), "completed": len(complete),
                "candidate_labeled": sum(levels.values()),
                "snapshot": "frozen" if context["frozen"] else "live",
                "sample_sha256": context["plan"].get("sample_sha256"),
                "frozen_at": context["frozen"].get("frozen_at"),
                "task_status": dict(statuses),
                "candidate_levels": {f"L{i}": levels.get(i, 0) for i in range(1, 7)},
                "observations": observations,
                "next_evidence": ["保留学生独立作答前后测", "记录 AI 介入及修改时间点", "抽样核查原文与贡献证据"],
                "source": context["plan"].get("experiment"), "revision": context["plan"].get("revision")}

    def list_imports(self):
        path = self.private / "imports"
        return sorted((_json(p, {}) for p in path.glob("*/metadata.json")),
                      key=lambda r: r.get("created_at", 0), reverse=True)[:30]

    def import_text(self, filename, content):
        return workbench_imports.stage_import(self.root, filename, content)

    def create_plan(self, import_id):
        with self._lock:
            return workbench_imports.create_plan(self.root, import_id)

    def resume(self):
        if not self.run_enabled():
            raise ValueError("当前部署未启用付费任务运行")
        state = self.run_state()
        if not state["available"]:
            raise ValueError("尚无可续跑的计划")
        if state["frozen"] or state["stopped"]:
            raise ValueError("该批次已冻结或超过领取任务截止时间")
        if state["active"]:
            raise ValueError("任务正在运行，无需重复启动")
        if state["status"] == "canary_failed":
            raise ValueError("控制题验证未通过，请先核查模型条件后建立新批次")
        plan = self.plan_path()
        if self.context()["plan"].get("version") == workbench_imports.VERSION:
            from scripts.run_workbench import assert_research_idle, load_plan
            load_plan(self.root, plan)
            assert_research_idle(self.root)
        log = plan.parent / f"workbench-resume-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.log"
        python = self.root / ".venv" / "Scripts" / "python.exe"
        if not python.exists():
            python = Path(sys.executable)
        command = [str(python), str(self.root / ("scripts/run_workbench.py" if self.context()["plan"].get("version") == workbench_imports.VERSION else "scripts/run_full_turns.py")),
                   "run", "--plan", str(plan)]
        with log.open("wb") as output:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            process = subprocess.Popen(command, cwd=self.root, stdout=output,
                                       stderr=subprocess.STDOUT, creationflags=flags,
                                       start_new_session=os.name != "nt")
        return {"started": True, "pid": process.pid, "experiment": state["experiment"]}

    def export_package(self):
        if self._context is None:
            return self.select(self.plan_id).export_package()
        state = self.run_state()
        quality = self.source_quality()
        report = self.teacher_report()
        comparison = self.model_comparison()
        payload = {"run-state.json": state, "source-quality.json": quality,
                   "teacher-report.json": report, "model-comparison.json": comparison}
        plan = self.plan_path()
        if plan:
            payload["plan-public-fields.json"] = {k: _json(plan, {}).get(k) for k in
                                                  ("version", "revision", "experiment", "seed", "real", "controls",
                                                   "sample_sha256", "turn_manifest_sha256", "rubric_sha256",
                                                   "review_policy", "risk_definition")}
        content = io.BytesIO()
        with zipfile.ZipFile(content, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            hashes = {}
            for name, item in payload.items():
                encoded = (json.dumps(item, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                archive.writestr(name, encoded)
                hashes[name] = _sha(encoded)
            for name, encoded in (("teacher-report.html", self.teacher_html().encode("utf-8")),
                                  ("results-aggregate.csv", self.results_csv().encode("utf-8-sig"))):
                archive.writestr(name, encoded)
                hashes[name] = _sha(encoded)
            archive.writestr("SHA256SUMS.json", json.dumps(hashes, indent=2) + "\n")
            archive.writestr("README.txt", "南行工作台核验摘要包。包含聚合状态、质量、模型比较和教师简报；不包含学生原文。完整复算须在授权环境中使用冻结快照与原始材料。\n")
        return content.getvalue()

    def results_csv(self):
        """Aggregate candidate results only: no student, record ID or source text."""
        context = self.context()
        counts = Counter()
        for row in context["rows"]:
            record = row.get("record", {})
            if record.get("term") == "synthetic":
                continue
            final = ((row.get("dimensions") or {}).get("task") or {}).get("final") or {}
            counts[(record.get("term", ""), bool(row.get("complete")),
                    final.get("status", "pending"), final.get("label"))] += 1
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(["experiment", "revision", "snapshot", "planned_real", "term", "settled", "task_status", "candidate_level", "turns"])
        plan = context["plan"]
        for (term, complete, status, level), n in sorted(counts.items(), key=lambda item: str(item[0])):
            writer.writerow([plan.get("experiment"), plan.get("revision"), "frozen" if context["frozen"] else "live",
                             plan.get("real", 0), term, int(complete), status, level if level is not None else "", n])
        return stream.getvalue()

    def teacher_html(self):
        report = self.teacher_report()
        escape = lambda value: html.escape(str(value if value is not None else "—"))
        observations = "".join(f"<li>{escape(item)}</li>" for item in report["observations"])
        evidence = "".join(f"<li>{escape(item)}</li>" for item in report["next_evidence"])
        levels = "".join(f"<tr><td>{escape(level)}</td><td>{count}</td></tr>" for level, count in report["candidate_levels"].items())
        return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>教师简报 · 南行</title>
<style>body{{font-family:system-ui,"Microsoft YaHei",sans-serif;max-width:780px;margin:40px auto;line-height:1.8;color:#182824;padding:0 24px}}h1{{font-size:28px}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccd6d1;padding:6px 12px;text-align:left}}.scope{{background:#eef5f1;padding:12px}}small{{overflow-wrap:anywhere}}@media print{{body{{margin:0;max-width:none}}}}</style>
<h1>{escape(report['title'])}</h1><p class="scope">{escape(report['scope'])}</p>
<p>批次：{escape(report['source'])} · {escape(report['revision'])} · {'冻结快照' if report['snapshot']=='frozen' else '进行中快照，尚非最终结果'}</p>
<p>计划 {report['planned']} 个真实学生提问；已结算 {report['completed']} 个，其中形成候选标签 {report['candidate_labeled']} 个。合成控制题另行统计。结算包括技术失败，不等于有效标注。</p>
<h2>候选层级分布</h2><table><thead><tr><th>层级</th><th>回合数</th></tr></thead><tbody>{levels}</tbody></table>
<h2>教学观察</h2><ul>{observations}</ul><h2>后续证据</h2><ol>{evidence}</ol>
<small>样本 SHA-256：{escape(report['sample_sha256'])}。可通过浏览器打印此页。</small></html>'''
