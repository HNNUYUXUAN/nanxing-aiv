"""Independent offline acceptance of frozen t3 reports; emits aggregates only.

Counts, Cohen kappa, student metrics and quantiles use Python/stdlib arithmetic.
NumPy is used only for the documented PCG64 resampling indices, so the seeded
bootstrap can be checked exactly without calling any reporting implementation.
"""

import argparse
from collections import Counter, defaultdict
from contextlib import closing
import csv
import hashlib
import json
import math
from pathlib import Path
import random
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {"eligible_turns": 3515, "student_terms": 401, "random_review": 352,
            "controls": 12, "repeat_records": 40}
STATES = ("agreed", "abstained", "disagreement", "uncertain", "technical_failure")
WEIGHTS = {"balanced": (.2, .2, .2, .2, .2), "higher_order": (.1, .5, .2, .15, .05),
           "process": (.1, .2, .4, .25, .05)}
METRICS = ("ABL", "HOT", "CTQ", "DHI", "MAB")


def read(path):
    return json.loads(Path(path).read_text("utf-8"))


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def close(actual, expected):
    if expected is None:
        return actual is None
    if type(expected) is bool:
        return type(actual) is bool and actual == expected
    if isinstance(expected, (float, int)):
        return isinstance(actual, (float, int)) and math.isfinite(actual) and math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-10)
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and close(actual[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(close(a, b) for a, b in zip(actual, expected))
    return actual == expected


def cohen(pairs, levels=6):
    """Integer count formula, independently of NumPy matrix operations."""
    left, right, diagonal, n = Counter(), Counter(), 0, 0
    for (a, b), count in pairs.items():
        x, y = ((a - 1) // 2, (b - 1) // 2) if levels == 3 else (a, b)
        left[x] += count
        right[y] += count
        diagonal += count * (x == y)
        n += count
    expected = sum(count * right[level] for level, count in left.items())
    return (n * diagonal - expected) / (n * n - expected) if n * n != expected else None


def percentile(values, probability):
    values = sorted(values)
    position = (len(values) - 1) * probability
    lo = int(position)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (position - lo) * (values[hi] - values[lo])


def pair_reference(rows, stage, models, field, draws, seed):
    groups, pairs = {}, Counter()
    valid = equal = both_null = one_null = 0
    for row in rows:
        entries = {entry["model"]: entry.get("judgment") for entry in row[stage]}
        a, b = (entries.get(model) for model in models)
        if a is None or b is None:
            continue
        valid += 1
        x, y = a[field], b[field]
        equal += x == y
        if x is None and y is None:
            both_null += 1
        elif x is None or y is None:
            one_null += 1
        else:
            key = row["record"].get("student", row["record"]["id"])
            groups.setdefault(key, Counter())[(x, y)] += 1
            pairs[(x, y)] += 1
    matrices = {6: [[0] * 6 for _ in range(6)], 3: [[0] * 3 for _ in range(3)]}
    for (a, b), count in pairs.items():
        matrices[6][a - 1][b - 1] += count
        matrices[3][(a - 1) // 2][(b - 1) // 2] += count
    simulations = {6: [], 3: []}
    if len(groups) >= 2:
        import numpy as np
        generator = np.random.default_rng(seed)
        clusters = list(groups.values())
        for _ in range(draws):
            sample = Counter()
            multiplicities = Counter(int(i) for i in generator.integers(len(clusters), size=len(clusters)))
            for index, multiple in multiplicities.items():
                for pair, count in clusters[index].items():
                    sample[pair] += count * multiple
            for levels in (6, 3):
                value = cohen(sample, levels)
                if value is not None:
                    simulations[levels].append(value)
    result = {"planned_pairs": len(rows), "both_valid": valid, "both_nonnull": sum(pairs.values()),
              "both_abstained": both_null, "one_abstained": one_null, "equal": equal,
              "raw_agreement_including_abstention": equal / valid if valid else None,
              "student_clusters_dual_nonnull": len(groups), "bootstrap_draws": draws, "seed": seed}
    for levels in (6, 3):
        values = simulations[levels]
        result.update({f"kappa{levels}": cohen(pairs, levels), f"matrix{levels}": matrices[levels],
                       f"kappa{levels}_valid_bootstrap_draws": len(values),
                       f"kappa{levels}_cluster_interval95": [percentile(values, .025), percentile(values, .975)] if values else None})
    return result


def task_reference(rows, stage, models):
    entries = [e for row in rows for e in row[stage] if e["model"] in models]
    return {"expected": len(rows) * len(models), "created": len(entries),
            "done": sum(e["status"] == "done" for e in entries),
            "failed": sum(e["status"] == "failed" for e in entries),
            "pending": sum(e["status"] not in ("done", "failed") for e in entries),
            "not_created": len(rows) * len(models) - len(entries),
            "valid": sum(e.get("judgment") is not None for e in entries)}


def student_reference(rows, turns):
    groups = defaultdict(list)
    for row in rows:
        if row["record"]["term"] != "synthetic":
            groups[row["record"]["student"], row["record"]["term"]].append(row)
    result = {}
    for key, group in sorted(groups.items()):
        done = [r for r in group if r["complete"]]
        selected = [r for r in done if r["final"] is not None]
        levels = [r["final"] for r in selected]
        tools = [turns[r["record"]["id"]].get("agent") for r in selected]
        normalized = [str(t).strip() if t is not None else "" for t in tools]
        known = {t for t in normalized if t and t.casefold() != "unknown"}
        tool_complete = bool(tools) and all(t and t.casefold() != "unknown" for t in normalized)
        counts = Counter(r["dimensions"]["task"]["final"]["status"] for r in done)
        metrics = dict.fromkeys((*METRICS, "ABL_raw", "MAB_raw"))
        if levels:
            average = sum(levels) / len(levels)
            distribution = Counter(levels)
            metrics.update(ABL_raw=average, ABL=(average - 1) / 5,
                           HOT=sum(level >= 4 for level in levels) / len(levels),
                           DHI=1 - sum(abs(distribution[i + 1] / len(levels) - ideal)
                                       for i, ideal in enumerate((.1, .15, .2, .25, .2, .1))) / 2)
            if tool_complete:
                metrics.update(MAB_raw=len(known), MAB=min(1, math.log1p(len(known)) / math.log(5)))
        expected = {"eligible_turns": len(group), "completed_turns": len(done),
                    "unfinished_turns": len(group) - len(done), "candidate_turns": len(selected),
                    "candidate_coverage": len(selected) / len(group),
                    "completed_candidate_coverage": len(selected) / len(done) if done else None,
                    **{f"task_{state}": counts[state] for state in STATES},
                    "observed_tools_in_candidate_subset": len(known),
                    "candidate_tool_evidence_complete": tool_complete, "verified_session_edges": 0, **metrics}
        for name, weights in WEIGHTS.items():
            expected[f"AIV_{name}"] = expected[f"rank_{name}"] = None
            low = 100 * sum(w * metrics[metric] for w, metric in zip(weights, METRICS) if metrics[metric] is not None)
            high = low + 100 * sum(w for w, metric in zip(weights, METRICS) if metrics[metric] is None)
            expected[f"{name}_formula_lower"] = low if levels else None
            expected[f"{name}_formula_upper"] = high if levels else None
        result[key] = expected
    return result


def cache_reference(path, plan, rows):
    """Read-only, transaction-consistent lineage check independent of the runner."""
    path = Path(path)
    if not path.exists():
        return {"passed": False, "reason": "private_pool_database_missing"}
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        tasks = {r["id"]: dict(r) for r in db.execute("SELECT id,payload,status,result,attempts FROM tasks WHERE json_extract(payload,'$.experiment')=?", (plan["experiment"],))}
        table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='exact_task_reuse'").fetchone()
        links = [dict(r) for r in db.execute("SELECT * FROM exact_task_reuse WHERE experiment=?", (plan["experiment"],))] if table else []
        attempts = db.execute("SELECT COUNT(*),SUM(status='done') FROM attempts WHERE experiment=?", (plan["experiment"],)).fetchone()
        attempt_rows = [dict(row) for row in db.execute("SELECT * FROM attempts WHERE experiment=? ORDER BY rowid", (plan["experiment"],))]
    violations, cross_student = Counter(), 0
    students = {r["record"]["id"]: r["record"].get("student") for r in rows}

    def condition(task):
        payload = json.loads(task["payload"])
        parts = payload.pop("replicate").split("|")
        if len(parts) < 4 or parts[-2] not in ("independent", "review") or parts[1].startswith("v3-control-"):
            return None
        payload["revision"], payload["stage"] = parts[0], parts[-2]
        return canonical_hash(payload), parts[-2], "|".join(parts[1:-2])

    for link in links:
        target, source = tasks.get(link["task_id"]), tasks.get(link["source_task_id"])
        if not target or not source or target["status"] != "done" or source["status"] != "done":
            violations["missing_or_unfinished_link_task"] += 1
            continue
        a, b = condition(target), condition(source)
        if (not a or not b or a[0] != b[0] or a[0] != link["condition_sha256"] or a[1] != link["stage"]
            or a[2] != link["target_record_id"] or b[2] != link["source_record_id"]
            or target["result"] != source["result"] or target["attempts"] != 0 or source["attempts"] <= 0
            or hashlib.sha256(target["result"].encode()).hexdigest() != link["result_sha256"]):
            violations["condition_result_or_attempt_mismatch"] += 1
        if students.get(link["target_record_id"]) != students.get(link["source_record_id"]):
            cross_student += 1
    linked = {link["task_id"] for link in links}
    for row in rows:
        for stage in ("independent", "review", "repeat"):
            for entry in row[stage]:
                task = tasks.get(entry.get("task"))
                if not task or task["status"] != entry["status"]:
                    violations["frozen_entry_database_mismatch"] += 1
                elif stage == "repeat" and (entry["task"] in linked or entry["status"] == "done" and task["attempts"] <= 0):
                    violations["repeat_reused_or_without_call"] += 1
    return {"passed": not violations, "violations": dict(violations), "logical_reused_tasks": len(links),
            "actual_api_attempts": attempts[0], "successful_api_attempts": attempts[1] or 0,
            "ledger_projection_sha256": canonical_hash({"tasks": sorted(tasks.values(), key=lambda r: r["id"]),
                                                        "links": sorted(links, key=lambda r: r["task_id"]), "attempts": attempt_rows}),
            "cross_student_cache_links": cross_student,
            "interpretation": "Reuse preserves source locations but does not create independent API observations."}


def preflight(plan_path, root, expected):
    plan = read(plan_path)
    sample = read(plan_path.with_name("sample.json"))
    turn_path = root / "runtime/research/turns-v1/student_turns.jsonl"
    all_turns = [json.loads(line) for line in turn_path.read_text("utf-8").splitlines() if line.strip()]
    eligible = [t for t in all_turns if t.get("model_eligible") is True]
    turns = {t["turn_id"]: t for t in eligible}
    real = [r for r in sample if r["term"] != "synthetic"]
    counts = {"eligible_turns": len(eligible), "student_terms": len({(t["student_id"], t["term"]) for t in eligible}),
              "random_review": len(plan["audit_ids"]), "controls": len(sample) - len(real), "repeat_records": len(plan["repeat_ids"])}
    checks = {"explicit_t3_plan": plan_path.parent.name == plan.get("revision") == "t3" and plan.get("version") == "full-turns-v1",
              "study_denominators": counts == expected,
              "source_turn_hash": file_hash(turn_path) == plan["turn_file_sha256"],
              "source_manifest_hash": file_hash(turn_path.with_name("manifest.json")) == plan["turn_manifest_sha256"],
              "sample_hash": canonical_hash(sample) == plan["sample_sha256"],
              "rubric_hash": canonical_hash(plan["rubric"]) == plan["rubric_sha256"],
              "unique_source_and_sample": len(turns) == len(eligible) and len({r["id"] for r in sample}) == len(sample),
              "eligible_source_join": set(turns) == {r["id"] for r in real},
              "plan_sample_denominators": plan["real"] == len(real) and plan["controls"] == len(sample) - len(real)}
    checks["source_record_identity"] = all(
        r["question"] == turns[r["id"]]["text"] and r["student"] == turns[r["id"]]["student_id"]
        and r["term"] == turns[r["id"]]["term"] and r["source"] == turns[r["id"]]["source"]
        and all(r.get(key) == turns[r["id"]].get(key) for key in ("turn_index", "session_candidate_id", "parse_status", "record_timestamp", "turn_timestamp"))
        for r in real if r["id"] in turns)
    checks["source_manifest_turn_pin"] = read(turn_path.with_name("manifest.json"))["files"]["student_turns.jsonl"] == file_hash(turn_path)
    checks["fixed_membership_unique"] = (len(set(plan["audit_ids"])) == len(plan["audit_ids"])
        and set(plan["audit_ids"]) <= set(turns) and len(set(plan["repeat_ids"])) == len(plan["repeat_ids"])
        and set(plan["repeat_ids"]) <= set(turns))
    # Validate the frozen stratum denominator, independently of reported selection counts.
    checks["random_strata"] = all(
        sum(r["term"] == term for r in real) == spec["frame"]
        and sum(turns[i]["term"] == term for i in plan["audit_ids"]) == spec["selected"]
        and math.isclose(spec["inclusion_probability"], spec["selected"] / spec["frame"])
        for term, spec in plan["audit_strata"].items())
    target = min(len(real), max(40, math.ceil(len(real) / 10)))
    term_sizes = Counter(r["term"] for r in real)
    quotas = {term: math.floor(target * n / len(real)) for term, n in term_sizes.items()}
    order = sorted(term_sizes, key=lambda term: (-(target * term_sizes[term] / len(real) - quotas[term]), term))
    for term in order[:target - sum(quotas.values())]:
        quotas[term] += 1
    selected = []
    for term in sorted(term_sizes):
        frame = sorted(r["id"] for r in real if r["term"] == term)
        selected += random.Random(f"{plan['seed']}|full-turns-v1|audit|{term}").sample(frame, quotas[term])
    checks["prespecified_random_draw"] = sorted(selected) == plan["audit_ids"]
    repeated = [r["id"] for r in sorted(real, key=lambda r: canonical_hash([plan["seed"], "full-turns-v1", "repeat", r["id"]]))[:min(40, len(real))]]
    checks["prespecified_repeat_draw"] = repeated == plan["repeat_ids"]
    files = {str(t["source"].get("file", "")) for t in eligible}
    original_rows, source_hashes = {}, {}
    for relative in files:
        # Preserve the serialized source identity/hash; normalize only host lookup.
        file = (root / relative.replace("\\", "/")).resolve()
        if not file.is_relative_to(root / "data") or file.suffix != ".csv":
            checks["original_csv_hash_and_spans"] = False
            break
        source_hashes[relative] = file_hash(file)
        with file.open(encoding="utf-8-sig", newline="") as stream:
            original_rows[relative] = list(csv.DictReader(stream))
    else:
        valid_spans = True
        for turn in eligible:
            span = turn["source"]
            number, start, end = span.get("row"), span.get("char_start"), span.get("char_end")
            table = original_rows[span["file"]]
            if (not isinstance(number, int) or not 2 <= number <= len(table) + 1
                or not isinstance(start, int) or not isinstance(end, int)):
                valid_spans = False
                break
            text = table[number - 2].get(span.get("field", "问答记录"), "")
            if source_hashes[span["file"]] != span.get("sha256") or not 0 <= start <= end <= len(text) or text[start:end] != turn["text"]:
                valid_spans = False
                break
        checks["original_csv_hash_and_spans"] = valid_spans
    return plan, sample, turns, counts, checks


def verify(plan_path, materials, *, root=ROOT, expected=None, pool=None, cache_receipt=None):
    root, plan_path, materials = Path(root).resolve(), Path(plan_path).resolve(), Path(materials).resolve()
    if not plan_path.is_relative_to(root / "runtime/research") or plan_path.name != "plan.json":
        raise ValueError("Plan must be a private registered plan.json")
    plan, sample, turns, counts, checks = preflight(plan_path, root, expected or EXPECTED)
    result = {"schema_version": "independent-full-turn-audit-v1", "experiment": plan["experiment"],
              "checks": checks, "static_denominators": counts, "planned_repeat_tasks": 2 * counts["repeat_records"],
              "verifier_sha256": file_hash(Path(__file__)),
              "method": "Independent Python count/marginal kappa and manual quantile arithmetic; NumPy supplies PCG64 indices only.",
              "interpretation": ["Conditional agreement is not accuracy or causal learning gain.",
                  "Student-cluster intervals do not remove cross-student identical-text/cache dependence.",
                  "Logical cached tasks are not independent API attempts; repeat calls require separate lineage validation."],
              "contains_student_identifiers_or_text": False}
    frozen = plan_path.parent / "frozen"
    if not all(checks.values()):
        result.update(status="failed_static_preflight", passed=False)
        return result
    if not (frozen / "snapshot-manifest.json").exists():
        result.update(status="awaiting_frozen_snapshot", passed=None)
        return result
    snapshot = read(frozen / "snapshot-manifest.json")
    checks["snapshot_hashes"] = set(snapshot["files"]) == {"annotations.json", "summary.json"} and all(file_hash(frozen / n) == h for n, h in snapshot["files"].items())
    checks["snapshot_time"] = snapshot["status"] == "completed" or snapshot["frozen_at"] >= plan["stop_claiming_at"]
    rows, source_summary = read(frozen / "annotations.json"), read(frozen / "summary.json")
    checks["snapshot_source_order"] = len(rows) == len(sample) and all(row["record"] == item for row, item in zip(rows, sample))
    checks["snapshot_identity"] = snapshot["experiment"] == plan["experiment"] == source_summary["experiment"] and source_summary["revision"] == "t3" and snapshot["sample_sha256"] == plan["sample_sha256"]
    checks["no_inflight_tasks"] = not source_summary.get("pool", {}).get("tasks", {}).get("running", 0) and all(e["status"] != "running" for row in rows for stage in ("independent", "review", "repeat") for e in row[stage])
    if not all(checks.values()):
        result.update(status="failed_frozen_preflight", passed=False)
        return result
    if not (materials / "manifest.json").exists():
        result.update(status="awaiting_frozen_materials", passed=None)
        return result
    manifest = read(materials / "manifest.json")
    required = {"summary.json", "review-strata.json", "repeatability.json", "data-quality.json", "student-indicators.csv"}
    checks["material_file_hashes"] = set(manifest["files"]) == required and all(file_hash(materials / n) == h for n, h in manifest["files"].items())
    checks["material_code_hashes"] = all(file_hash(ROOT / n.replace("\\", "/")) == h for n, h in manifest["code_hashes"].items())
    reports = {name: read(materials / name) for name in required if name.endswith(".json")}
    pin = file_hash(frozen / "snapshot-manifest.json")
    checks["material_source_pins"] = all(report["source"] == manifest["source"] for report in reports.values()) and close(manifest["source"], {
        "manifest_sha256": pin, "annotations_sha256": file_hash(frozen / "annotations.json"),
        "plan_sha256": file_hash(plan_path), "experiment": plan["experiment"], "revision": "t3",
        "snapshot_status": snapshot["status"], "frozen_at": snapshot["frozen_at"], "turn_file_sha256": plan["turn_file_sha256"]})
    result["source_manifest_sha256"] = pin
    result["materials_manifest_sha256"] = file_hash(materials / "manifest.json")
    real = [r for r in rows if r["record"]["term"] != "synthetic"]
    controls = [r for r in rows if r["record"]["term"] == "synthetic"]
    done = [r for r in real if r["complete"]]
    fast = [m["id"] for m in plan["models"] if m["role"] == "fast"]
    strong = [m["id"] for m in plan["models"] if m["role"] == "review"]
    repeats = [r for r in real if r["record"]["id"] in set(plan["repeat_ids"])]
    cohorts = {name: [r for r in real if r["review_selection"] == name] for name in ("random_audit", "high_risk")}
    cohorts["control"] = controls
    students = student_reference(real, turns)
    denominators = {"planned_real": len(real), "completed_real": len(done), "unfinished_real": len(real) - len(done),
        "candidate_labeled_real": sum(r["final"] is not None for r in done), "unknown_or_abstained_real": sum(r["final"] is None for r in done),
        "planned_controls": len(controls), "completed_controls": sum(r["complete"] for r in controls),
        "student_term_rows": len(students), "random_review_preselected": len(plan["audit_ids"]),
        "high_risk_review_selected": len(cohorts["high_risk"]), "repeat_preselected": len(repeats), "repeat_expected_tasks": 2 * len(repeats)}
    summary = reports["summary.json"]
    checks["summary_denominators"] = close(summary["denominators"], denominators)
    result["recomputed_denominators"] = denominators
    checks["incomplete_labels_excluded"] = sum(s["candidate_turns"] for s in students.values()) == denominators["candidate_labeled_real"]
    checks["snapshot_denominators"] = snapshot["real_completed"] == len(done) and snapshot["records_completed"] == sum(r["complete"] for r in rows) == source_summary["completed_records"] and snapshot["real_candidate_labeled"] == denominators["candidate_labeled_real"]
    checks["fixed_random_membership"] = {r["record"]["id"] for r in cohorts["random_audit"]} == set(plan["audit_ids"])
    checks["independent_tasks"] = close(summary["independent_tasks"], task_reference(real, "independent", fast))
    by_term = []
    for term in sorted({r["record"]["term"] for r in real}):
        group = [r for r in real if r["record"]["term"] == term]
        finished = [r for r in group if r["complete"]]
        by_term.append({"term": term, "planned": len(group), "completed": len(finished), "unfinished": len(group) - len(finished), "candidate_labeled": sum(r["final"] is not None for r in finished), "unknown_or_abstained": sum(r["final"] is None for r in finished)})
    checks["term_denominators"] = close(summary["by_term"], by_term)
    draws, seed = manifest["bootstrap_draws"], manifest["seed"]
    for dimension, field in (("task", "level"), ("contribution", "contribution_level")):
        tally = Counter(r["dimensions"][dimension]["final"]["status"] for r in done)
        checks[f"{dimension}_final_status"] = close(summary[f"final_{dimension}_status_counts"], {state: tally[state] for state in STATES})
        checks[f"fast_{dimension}_kappa_and_cluster_bootstrap"] = close(summary["independent_agreement"][dimension], pair_reference(real, "independent", fast, field, draws, seed))
        for name, cohort in cohorts.items():
            report = reports["review-strata.json"]["strata"][name]
            checks[f"{name}_{dimension}_kappa_and_cluster_bootstrap"] = close(report["agreement"][dimension], pair_reference(cohort, "review", strong, field, draws, seed))
    for name, cohort in cohorts.items():
        report = reports["review-strata.json"]["strata"][name]
        checks[f"{name}_review_denominators"] = close(report, {"planned_records": len(cohort), "completed_records": sum(r["complete"] for r in cohort), "review_tasks": task_reference(cohort, "review", strong), "by_term": dict(Counter(r["record"]["term"] for r in cohort))})
    repeat_report = reports["repeatability.json"]
    checks["repeat_all_planned_tasks"] = close(repeat_report, {"planned_records": len(repeats), "expected_tasks": 2 * len(repeats), "tasks": task_reference(repeats, "repeat", fast)})
    for index, model in enumerate(fast):
        ref = {"tasks": task_reference(repeats, "repeat", [model])}
        for dimension, field in (("task", "level"), ("contribution", "contribution_level")):
            pairs = [(next((e.get("judgment") for e in row["independent"] if e["model"] == model), None), next((e.get("judgment") for e in row["repeat"] if e["model"] == model), None)) for row in repeats]
            valid = [(a[field], b[field]) for a, b in pairs if a is not None and b is not None]
            equal = sum(a == b for a, b in valid)
            ref[dimension] = {"planned_pairs": len(repeats), "paired_valid": len(valid), "equal": equal, "stability": equal / len(valid) if valid else None}
        checks[f"repeat_model_{index + 1}"] = close(repeat_report["by_model"][model], ref)
    with (materials / "student-indicators.csv").open(encoding="utf-8-sig", newline="") as stream:
        csv_rows = list(csv.DictReader(stream))
    keyed = {(row["student"], row["term"]): row for row in csv_rows}
    checks["student_csv_membership"] = len(csv_rows) == len(keyed) == len(students) and set(keyed) == set(students)
    mismatches = Counter()
    for key, expected_row in students.items():
        actual = keyed.get(key, {})
        for field, expected_value in expected_row.items():
            raw = actual.get(field)
            try:
                value = None if raw == "" else raw == "True" if isinstance(expected_value, bool) else float(raw)
            except (TypeError, ValueError):
                value = "invalid"
            if not close(value, expected_value):
                mismatches[field] += 1
    checks["student_csv_all_core_fields"] = not mismatches
    result["student_csv_mismatch_counts"] = dict(mismatches)
    availability = {"student_term_rows": len(students), "labeled_student_term_rows": sum(s["candidate_turns"] > 0 for s in students.values()),
                    "full_AIV_available": 0, "full_AIV_rank_available": 0, "CTQ_available": 0,
                    "MAB_available": sum(s["MAB"] is not None for s in students.values())}
    checks["missing_metrics_preserved"] = close(summary["score_availability"], availability)
    # Hash-pinned API provenance is a separate gate, never inferred from agreement.
    integrity = frozen / "integrity-audit.json"
    if integrity.exists():
        audit = read(integrity)
        checks["api_and_cache_integrity_gate"] = audit.get("passed") is True and audit.get("checks", {}).get("exact_input_cache_lineage") is True and audit.get("records") == len(rows)
        result["exact_input_reused_tasks"] = audit.get("exact_input_reused_tasks")
    else:
        checks["api_and_cache_integrity_gate"] = False
        result["required_followup"] = "Run frozen validate_full_turns.py to verify API/cache lineage."
    receipt_path = Path(cache_receipt) if cache_receipt is not None else frozen / "cache-lineage-audit.json"
    input_pins = {"snapshot_manifest_sha256": pin, "annotations_sha256": file_hash(frozen / "annotations.json"),
                  "summary_sha256": file_hash(frozen / "summary.json"), "plan_sha256": file_hash(plan_path),
                  "turn_file_sha256": plan["turn_file_sha256"], "sample_sha256": plan["sample_sha256"],
                  "integrity_audit_sha256": file_hash(integrity) if integrity.exists() else None}
    if pool is not None:
        cache_result = cache_reference(pool, plan, rows)
        receipt = {"schema_version": "frozen-cache-lineage-audit-v1", "experiment": plan["experiment"],
                   "source_pins": input_pins, "verification": cache_result,
                   "passed": cache_result["passed"] and checks["api_and_cache_integrity_gate"],
                   "verifier_sha256": file_hash(Path(__file__)),
                   "scope": "Read-only transaction over the specified experiment; no credentials, student text or IDs in this receipt."}
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False) + "\n", "utf-8", newline="\n")
        result["cache_verification_mode"] = "read_only_database_and_pinned_receipt"
    else:
        receipt = read(receipt_path) if receipt_path.exists() else {}
        result["cache_verification_mode"] = "offline_pinned_receipt"
    checks["cache_receipt_snapshot_pins"] = (receipt.get("schema_version") == "frozen-cache-lineage-audit-v1"
        and receipt.get("experiment") == plan["experiment"] and receipt.get("source_pins") == input_pins
        and receipt.get("verifier_sha256") == file_hash(Path(__file__)))
    result["cache_lineage"] = receipt.get("verification", {"passed": False, "reason": "pinned_cache_receipt_missing"})
    checks["independent_cache_lineage"] = receipt.get("passed") is True and checks["cache_receipt_snapshot_pins"] and result["cache_lineage"].get("passed") is True
    if receipt_path.exists():
        result["cache_receipt_sha256"] = file_hash(receipt_path)
    if not receipt:
        result["required_followup"] = "In the authorized workspace, rerun with --pool to create the frozen cache-lineage receipt, then include it with the offline package."
    # Never include identifiers/text in diagnostics, including CSV mismatch keys.
    for name, report in reports.items():
        serialized = json.dumps(report, ensure_ascii=False)
        checks[name.replace(".json", "") + "_aggregate_privacy"] = all(
            str(row["record"]["student"]) not in serialized and row["record"]["question"] not in serialized
            for row in real if len(str(row["record"]["student"])) >= 6 and len(row["record"]["question"]) >= 6)
    result.update(status="passed" if all(checks.values()) else "failed", passed=all(checks.values()))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--materials", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pool", type=Path, help="Optional read-only private ledger; omitted in offline packages")
    parser.add_argument("--cache-receipt", type=Path, help="Default: selected plan's frozen/cache-lineage-audit.json")
    args = parser.parse_args()
    try:
        result = verify(args.plan, args.materials, pool=args.pool, cache_receipt=args.cache_receipt)
    except (OSError, ValueError, KeyError, TypeError) as error:
        result = {"schema_version": "independent-full-turn-audit-v1", "passed": False,
                  "status": "invalid_input", "error_category": type(error).__name__,
                  "contains_student_identifiers_or_text": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", "utf-8", newline="\n")
    print(json.dumps({"status": result["status"], "passed": result["passed"],
                      "failed_checks": [name for name, passed in result.get("checks", {}).items() if not passed]}, ensure_ascii=False))
    raise SystemExit(0 if result["passed"] else 2 if result["passed"] is None else 1)
