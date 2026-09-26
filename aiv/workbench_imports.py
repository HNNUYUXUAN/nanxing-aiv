"""Private upload normalization and isolated plans; never edits the research cohort."""

import csv
import hashlib
import io
import json
import re
import time
import uuid
from collections import Counter
from pathlib import Path

from .fast_strong_workflow import RUBRIC, controls, digest, save_json
from .full_turn_workflow import model_conditions, read_turns, repetition_sample, stratified_audit
from .turns import parse_qa_record

MAX_IMPORT_BYTES = 5 * 1024 * 1024
VERSION = "workbench-import-v1"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def import_directory(root, identifier):
    if not re.fullmatch(r"[a-f0-9]{16}", identifier):
        raise ValueError("导入 ID 无效")
    base = (Path(root) / "runtime/workbench/imports").resolve()
    target = (base / identifier).resolve()
    if not target.is_relative_to(base):
        raise ValueError("导入路径无效")
    return target


def read_source(suffix, content):
    if suffix == ".csv":
        reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff")), strict=True)
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError("CSV 须有不重复的表头")
        try:
            rows = list(reader)
        except csv.Error:
            raise ValueError("CSV 格式无效") from None
        if any(None in row or any(value is None for value in row.values()) for row in rows):
            raise ValueError("CSV 数据列数与表头不一致")
        return [(i + 2, row) for i, row in enumerate(rows)], reader.fieldnames
    rows = []
    for number, line in enumerate(content.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line.lstrip("\ufeff"))
        except json.JSONDecodeError:
            raise ValueError(f"JSONL 第 {number} 行解析失败") from None
        if not isinstance(row, dict):
            raise ValueError(f"JSONL 第 {number} 行应为对象")
        rows.append((number, row))
    return rows, sorted({key for _, row in rows for key in row})


def stage_import(root, filename, content):
    if not isinstance(filename, str) or not isinstance(content, str):
        raise ValueError("请提供文件名与文本内容")
    suffix = Path(filename).suffix.lower()
    if suffix not in (".csv", ".jsonl"):
        raise ValueError("仅支持 CSV 或 JSONL 文本")
    data = content.encode("utf-8")
    if not data or len(data) > MAX_IMPORT_BYTES:
        raise ValueError("文件须非空且不超过 5 MiB")
    numbered, fields = read_source(suffix, content)
    if not numbered:
        raise ValueError("文件没有数据行")
    aliases = {
        "text": ("text", "question", "提问", "问答记录"),
        "role": ("role", "speaker", "角色"),
        "id": ("turn_id", "id", "记录ID"),
        "student": ("student_id", "student", "学生ID", "学生编号"),
        "term": ("term", "学期"),
    }
    names = {key: next((name for name in options if name in fields), None)
             for key, options in aliases.items()}
    qa = names["text"] == "问答记录"
    identifier = uuid.uuid4().hex[:16]
    target = import_directory(root, identifier)
    relative = (target / ("source" + suffix)).relative_to(Path(root).resolve()).as_posix()
    source_sha = hashlib.sha256(data).hexdigest()
    issues, counts, seen, turns = [], Counter(), Counter(), []
    for number, row in numbered:
        values = {key: row.get(name, "") if name else "" for key, name in names.items()}
        values = {key: value if isinstance(value, str) else str(value) if isinstance(value, (int, float)) else ""
                  for key, value in values.items()}
        if not isinstance(row.get(names["text"]), str):
            values["text"] = ""
        rid, student, term, text = (values[k].strip() for k in ("id", "student", "term", "text"))
        seen[rid] += 1
        role = {"学生": "student", "user": "student", "AI": "assistant", "助理": "assistant"}.get(values["role"].strip(), values["role"].strip().lower())
        invalid = False
        for condition, key in ((not rid, "missing_id"), (not student, "missing_student"),
                               (term not in ("25f", "26s"), "invalid_term"),
                               (not text, "missing_text"),
                               (not qa and not role, "missing_role"),
                               (not qa and bool(role) and role not in ("student", "assistant"), "unknown_role")):
            if condition:
                counts[key] += 1
                invalid = True
        if invalid:
            continue
        student_hash = hashlib.sha256(f"{identifier}|{student}".encode()).hexdigest()[:20]
        record_id = hashlib.sha256(f"{identifier}|{rid}".encode()).hexdigest()[:24]
        if qa:
            parsed, audit = parse_qa_record({"id": record_id, "student": student_hash, "term": term,
                "timestamp": None, "source_file": relative, "source_row": number,
                "source_sha256": source_sha}, values["text"])
            if not audit["structural_alternating"] or not parsed:
                counts["invalid_qa"] += 1
            for turn in parsed:
                turn["source"].update(import_id=identifier, format=suffix[1:])
                turns.append(turn)
        else:
            raw = values["text"]
            start = len(raw) - len(raw.lstrip())
            turns.append({"turn_id": record_id, "student_id": student_hash, "term": term,
                "role": role, "text": text, "model_eligible": role == "student",
                "session_candidate_id": None, "turn_index": None, "record_timestamp": None,
                "turn_timestamp": None, "parse_status": "explicit_role_unverified",
                "source": {"file": relative, "row": number, "sha256": source_sha,
                    "record_id": record_id, "field": names["text"], "char_start": start,
                    "char_end": start + len(text), "import_id": identifier, "format": suffix[1:]}})
    counts["duplicate_ids"] = sum(n - 1 for key, n in seen.items() if key and n > 1)
    labels = {"missing_id": "缺稳定记录 ID", "missing_student": "缺学生 ID", "invalid_term": "学期须为 25f 或 26s",
              "missing_text": "空文本", "missing_role": "缺角色", "unknown_role": "未知角色",
              "duplicate_ids": "重复 ID", "invalid_qa": "Q/A 结构待核"}
    issues = [f"{labels[key]} {counts[key]} 行" for key in labels if counts[key]]
    eligible = sum(t["model_eligible"] for t in turns)
    if not eligible:
        issues.append("没有可运行的学生回合")
    manifest = {"schema_version": VERSION, "source_scope": "本次私有导入；角色/结构未经独立人工核验",
        "import_id": identifier, "source_sha256": source_sha,
        "counts": {"selected_records": len(numbered), "student_turns": sum(t["role"] == "student" for t in turns),
                   "assistant_turns": sum(t["role"] == "assistant" for t in turns), "model_eligible_student_turns": eligible,
                   "records_needing_review": sum(counts.values())},
        "checks": {"source_text_spans_valid": True, "ready_for_plan": not issues},
        "boundaries": ["学生身份仅用于本批次去标识分组", "未推断会话关系、逐轮时间或学习增量"]}
    metadata = {"id": identifier, "filename": Path(filename).name[:120], "created_at": time.time(),
        "sha256": source_sha, "bytes": len(data), "rows": len(numbered), "fields": fields[:80],
        **{key: counts[key] for key in labels}, "eligible_turns": eligible, "issues": issues,
        "status": "quality_checked" if not issues else "needs_review", "experiment_link": None,
        "source_suffix": suffix}
    target.mkdir(parents=True)
    (target / ("source" + suffix)).write_bytes(data)
    (target / "student_turns.jsonl").write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in turns), "utf-8")
    manifest["turn_file_sha256"] = sha(target / "student_turns.jsonl")
    save_json(target / "manifest.json", manifest)
    metadata["manifest_sha256"] = sha(target / "manifest.json")
    save_json(target / "metadata.json", metadata)
    return metadata


def validate_import(root, identifier):
    directory = import_directory(root, identifier)
    metadata = json.loads((directory / "metadata.json").read_text("utf-8"))
    suffix = metadata.get("source_suffix")
    if suffix not in (".csv", ".jsonl") or sha(directory / ("source" + suffix)) != metadata["sha256"]:
        raise ValueError("导入源文件哈希不一致")
    if sha(directory / "manifest.json") != metadata.get("manifest_sha256"):
        raise ValueError("导入清单哈希不一致")
    manifest = json.loads((directory / "manifest.json").read_text("utf-8"))
    if sha(directory / "student_turns.jsonl") != manifest["turn_file_sha256"]:
        raise ValueError("回合清单哈希不一致")
    if metadata["issues"] or not manifest["checks"]["ready_for_plan"]:
        raise ValueError("导入资料仍有质量问题，请修正后重新导入")
    return directory, metadata, manifest


def create_plan(root, identifier, *, duration_hours=2):
    root = Path(root).resolve()
    source, metadata, manifest = validate_import(root, identifier)
    experiment = "workbench-" + identifier
    directory = root / "runtime/research" / experiment / "t1"
    if (directory / "plan.json").exists():
        plan = json.loads((directory / "plan.json").read_text("utf-8"))
        return {"plan_id": experiment + ":t1", "experiment": experiment, "real": plan["real"], "created": False}
    records = read_turns(source / "student_turns.jsonl")
    audit, strata = stratified_audit(records)
    control_rows = controls()
    sample = records + control_rows
    now = time.time()
    plan = {"schema_version": 1, "version": VERSION, "revision": "t1", "experiment": experiment,
        "started_at": now, "stop_claiming_at": now + duration_hours * 3600, "seed": 26,
        "import_id": identifier, "real": len(records), "controls": len(control_rows),
        "models": model_conditions(), "rubric": RUBRIC, "rubric_sha256": digest(RUBRIC),
        "sample_sha256": digest(sample), "turn_file_sha256": manifest["turn_file_sha256"],
        "turn_manifest_sha256": sha(source / "manifest.json"), "source_manifest_scope": manifest["source_scope"],
        "source_quality": manifest, "audit_ids": audit, "audit_strata": strata,
        "repeat_ids": repetition_sample(records), "engineering_ids": [],
        "review_policy": "term_stratified_random_10pct_min40_plus_all_risks", "maximum_concurrency": 4,
        "risk_definition": ["fast_task_failure_or_disagreement_or_uncertainty", "contribution_disagreement", "source_role_or_parse_unverified"],
        "max_attempts_per_task_condition": 2, "timeout_seconds": 90,
        "authorization": "Created in authenticated workbench; execution requires explicit run enablement"}
    # mkdir is the cross-request creation guard; no existing batch can be replaced.
    directory.mkdir(parents=True, exist_ok=False)
    save_json(directory / "sample.json", sample)
    save_json(directory / "plan.json", plan)
    metadata["experiment_link"] = experiment + ":t1"
    save_json(source / "metadata.json", metadata)
    return {"plan_id": experiment + ":t1", "experiment": experiment, "real": len(records), "created": True}


def verify_source(root, record):
    source = record.get("source") or {}
    try:
        directory = import_directory(root, source.get("import_id", ""))
        metadata = json.loads((directory / "metadata.json").read_text("utf-8"))
        suffix = metadata.get("source_suffix")
        if suffix not in (".csv", ".jsonl"):
            raise ValueError("来源格式无效")
        path = directory / ("source" + suffix)
        relative = path.relative_to(Path(root).resolve()).as_posix()
        if source.get("file") != relative or sha(path) != source.get("sha256") or sha(path) != metadata["sha256"]:
            raise ValueError("源文件路径或哈希不一致")
        numbered, _ = read_source(suffix, path.read_text("utf-8"))
        row = next((row for number, row in numbered if number == source.get("row")), None)
        raw = row.get(source.get("field")) if row else None
        start, end = source.get("char_start"), source.get("char_end")
        if not isinstance(raw, str) or not isinstance(start, int) or not isinstance(end, int) or not 0 <= start <= end <= len(raw) or raw[start:end] != record.get("question"):
            raise ValueError("字符位置与提问不一致")
        return {"valid": True, "source_file": relative, "source_row": source["row"],
                "char_start": start, "char_end": end, "sha256": source["sha256"], "raw_qa": raw}
    except (OSError, ValueError, KeyError, TypeError):
        return {"valid": False, "reason": "导入来源的路径、哈希或字符位置未通过核验"}
