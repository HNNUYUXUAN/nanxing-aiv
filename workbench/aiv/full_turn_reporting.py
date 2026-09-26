"""Offline, hash-pinned reporting for a frozen full-turn t3 experiment.

This adapter never imports the worker, opens the pool database, or submits a
model request. Student rows remain private; the JSON reports contain aggregates.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

import numpy as np

from .metrics import NAMES, WEIGHTS, composite, dhi, distribution

SCHEMA = "full-turn-materials-v1"
FINAL_STATES = ("agreed", "abstained", "disagreement", "uncertain", "technical_failure")
TERMINAL = {"done", "failed"}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def digest(value) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


@dataclass
class FrozenTurns:
    plan: dict
    rows: list[dict]
    turns: dict[str, dict]
    source: dict
    checks: dict


def load_frozen(plan_path: Path, manifest_sha256: str, *, root: Path) -> FrozenTurns:
    """Accept only the explicit frozen t3 snapshot and its exact source join."""
    root, plan_path = root.resolve(), plan_path.resolve()
    require(plan_path.is_relative_to(root / "runtime/research"), "Plan must stay in private research")
    require(plan_path.name == "plan.json" and plan_path.parent.name == "t3", "Expected t3 plan.json")
    directory = plan_path.parent
    frozen = directory / "frozen"
    manifest_path = frozen / "snapshot-manifest.json"
    require(len(manifest_sha256) == 64 and sha256(manifest_path) == manifest_sha256.lower(),
            "Frozen manifest hash mismatch")
    manifest, plan = read_json(manifest_path), read_json(plan_path)
    require(plan.get("version") == "full-turns-v1" and plan.get("revision") == "t3",
            "Unsupported experiment revision")
    require(manifest.get("schema_version") == 1 and manifest.get("status") in
            {"completed", "partial", "manual_snapshot"}, "Unsupported frozen snapshot")
    require(manifest["status"] == "completed" or manifest["frozen_at"] >= plan["stop_claiming_at"],
            "Partial snapshot precedes the agreed freeze time")
    require(set(manifest["files"]) == {"annotations.json", "summary.json"},
            "Unexpected snapshot file set")
    require(all(sha256(frozen / name) == expected for name, expected in manifest["files"].items()),
            "Frozen file hash mismatch")
    sample, rows = read_json(directory / "sample.json"), read_json(frozen / "annotations.json")
    summary = read_json(frozen / "summary.json")
    require(digest(sample) == plan["sample_sha256"] == manifest["sample_sha256"], "Sample hash mismatch")
    require(digest(plan["rubric"]) == plan["rubric_sha256"], "Rubric hash mismatch")
    require(manifest["experiment"] == plan["experiment"] == summary["experiment"]
            and summary["revision"] == "t3", "Snapshot identity mismatch")
    require(len(rows) == len(sample) == summary["n"]
            and all(r["record"] == s for r, s in zip(rows, sample)), "Snapshot source order mismatch")
    require(len({r["record"]["id"] for r in rows}) == len(rows), "Duplicate snapshot record")
    require(not summary.get("pool", {}).get("tasks", {}).get("running", 0),
            "Frozen summary contains in-flight tasks")
    turn_dir = root / "runtime/research/turns-v1"
    turn_file, turn_manifest_file = turn_dir / "student_turns.jsonl", turn_dir / "manifest.json"
    require(sha256(turn_file) == plan["turn_file_sha256"], "Student turn source hash mismatch")
    require(sha256(turn_manifest_file) == plan["turn_manifest_sha256"], "Turn manifest hash mismatch")
    turn_manifest = read_json(turn_manifest_file)
    require(turn_manifest["files"]["student_turns.jsonl"] == plan["turn_file_sha256"],
            "Turn manifest source identity mismatch")
    all_turns = [json.loads(line) for line in turn_file.read_text("utf-8").splitlines() if line.strip()]
    require(len({t["turn_id"] for t in all_turns}) == len(all_turns), "Duplicate source turn ID")
    turns = {t["turn_id"]: t for t in all_turns if t.get("model_eligible") is True}
    real = [r for r in rows if r["record"]["term"] != "synthetic"]
    controls = [r for r in rows if r["record"]["term"] == "synthetic"]
    require(set(turns) == {r["record"]["id"] for r in real}, "Eligible turn join is not one-to-one")
    for row in real:
        record, turn = row["record"], turns[row["record"]["id"]]
        require(record["question"] == turn["text"] and record["student"] == turn["student_id"]
                and record["term"] == turn["term"] and record["source"] == turn["source"]
                and record.get("turn_index") == turn.get("turn_index")
                and record.get("session_candidate_id") == turn.get("session_candidate_id")
                and record.get("parse_status") == turn.get("parse_status"), "Source turn join identity mismatch")
    fast = [m["id"] for m in plan["models"] if m["role"] == "fast"]
    reviewers = [m["id"] for m in plan["models"] if m["role"] == "review"]
    require(len(fast) == len(reviewers) == 2 and len(set(fast + reviewers)) == 4,
            "Expected two fast and two independent review models")
    random_ids, repeat_ids = set(plan["audit_ids"]), set(plan["repeat_ids"])
    require(len(random_ids) == len(plan["audit_ids"]) and random_ids <= set(turns)
            and len(repeat_ids) == len(plan["repeat_ids"]) and repeat_ids <= set(turns),
            "Invalid fixed random or repeat membership")
    require(len(real) == plan["real"] == manifest["real_planned"]
            and len(controls) == plan["controls"] == manifest["synthetic_controls"], "Planned denominators mismatch")
    for term, stratum in plan["audit_strata"].items():
        require(sum(r["record"]["term"] == term for r in real) == stratum["frame"]
                and sum(turns[i]["term"] == term for i in random_ids) == stratum["selected"],
                "Random review term denominator mismatch")
    for row in rows:
        rid = row["record"]["id"]
        expected_selection = ("control" if row["record"]["term"] == "synthetic" else
                              "random_audit" if rid in random_ids else
                              "high_risk" if row["review_selected"] else None)
        require(row["review_selection"] == expected_selection
                and bool(row["review_selected"]) == (expected_selection is not None),
                "Review selection mismatch")
        for stage, models in (("independent", fast), ("review", reviewers), ("repeat", fast)):
            entries = row[stage]
            require(len({e["model"] for e in entries}) == len(entries)
                    and all(e["model"] in models for e in entries), "Duplicate or unknown stage model")
            require(all(e["status"] in {"done", "failed", "queued"} for e in entries),
                    "Frozen task has an invalid or running status")
            require(all((e.get("judgment") is not None) == (e["status"] == "done") for e in entries),
                    "Task status and judgment validity disagree")
            required = stage == "independent" or (stage == "review" and row["review_selected"]) or (
                stage == "repeat" and rid in repeat_ids)
            require(required or not entries, "Unexpected unselected stage entries")
            if row["complete"] and required:
                require({e["model"] for e in entries} == set(models)
                        and all(e["status"] in TERMINAL for e in entries), "Complete row has missing required tasks")
        if row["complete"]:
            for dimension in ("task", "contribution"):
                final = row["dimensions"][dimension]["final"]
                require(final["status"] in FINAL_STATES
                        and (final["label"] is not None) == (final["status"] == "agreed"),
                        "Completed dimension status mismatch")
                require(final["label"] is None or type(final["label"]) is int and 1 <= final["label"] <= 6,
                        "Invalid candidate label")
            require(row["final"] == row["dimensions"]["task"]["final"]["label"], "Candidate task label mismatch")
    done = [r for r in real if r["complete"]]
    complete_controls = sum(r["complete"] for r in controls)
    require(len(done) == manifest["real_completed"]
            and len(done) + complete_controls == manifest["records_completed"] == summary["completed_records"]
            and sum(r["final"] is not None for r in done) == manifest["real_candidate_labeled"]
            and len(random_ids) == manifest["random_review_preselected"]
            and sum(r["review_selected"] for r in real) == manifest["review_selected"] == summary["review_real_selected"],
            "Completed or review denominators mismatch")
    require(manifest["status"] != "completed" or all(r["complete"] for r in rows),
            "Completed snapshot contains unfinished rows")
    for stage in ("independent", "review", "repeat"):
        entries = [e for r in rows for e in r[stage]]
        actual = {"requested": len(entries), "valid": sum(e["judgment"] is not None for e in entries),
                  "failed": sum(e["status"] == "failed" for e in entries)}
        require(all(summary["stages"][stage][k] == v for k, v in actual.items()), "Stage denominator mismatch")
    source = {"experiment": plan["experiment"], "revision": "t3", "snapshot_status": manifest["status"],
              "frozen_at": manifest["frozen_at"], "manifest_path": manifest_path.relative_to(root).as_posix(),
              "manifest_sha256": manifest_sha256.lower(), "annotations_sha256": sha256(frozen / "annotations.json"),
              "plan_sha256": sha256(plan_path), "turn_file_sha256": plan["turn_file_sha256"]}
    checks = {name: True for name in ("frozen_hash_pin", "snapshot_file_hashes", "sample_and_rubric_hashes",
              "source_turn_hash", "eligible_join_one_to_one", "unique_record_ids", "source_record_identity",
              "fixed_review_membership", "completed_denominators", "stage_denominators", "no_running_tasks")}
    return FrozenTurns(plan, rows, turns, source, checks)


def task_counts(rows: list[dict], stage: str, models: list[str]) -> dict:
    entries = [e for row in rows for e in row[stage] if e["model"] in models]
    statuses = Counter(e["status"] for e in entries)
    expected = len(rows) * len(models)
    require(len(entries) <= expected, "Stage task count exceeds expected denominator")
    return {"expected": expected, "created": len(entries), "done": statuses["done"],
            "failed": statuses["failed"], "pending": sum(v for k, v in statuses.items() if k not in TERMINAL),
            "not_created": expected - len(entries), "valid": sum(e["judgment"] is not None for e in entries)}


def kappa(matrix: np.ndarray) -> float | None:
    n = matrix.sum()
    if not n:
        return None
    chance = float(np.dot(matrix.sum(0), matrix.sum(1)) / n ** 2)
    return float((np.trace(matrix) / n - chance) / (1 - chance)) if chance < 1 - 1e-12 else None


def agreement(rows: list[dict], stage: str, models: list[str], field: str, *, draws=2000, seed=26) -> dict:
    grouped = defaultdict(lambda: np.zeros((6, 6), dtype=int))
    both_valid = equal = both_null = one_null = 0
    for row in rows:
        entries = {e["model"]: e for e in row[stage]}
        judgments = [entries.get(m, {}).get("judgment") for m in models]
        if any(j is None for j in judgments):
            continue
        both_valid += 1
        x, y = (j[field] for j in judgments)
        equal += x == y
        if x is None and y is None:
            both_null += 1
        elif x is None or y is None:
            one_null += 1
        else:
            cluster = row["record"].get("student", row["record"]["id"])
            grouped[cluster][x - 1, y - 1] += 1
    blocks = np.array(list(grouped.values()))
    matrix = blocks.sum(axis=0) if len(blocks) else np.zeros((6, 6), dtype=int)
    three = lambda m: m.reshape(3, 2, 3, 2).sum(axis=(1, 3))
    simulations = {6: [], 3: []}
    # One cluster cannot support a resampling interval.
    if len(blocks) >= 2:
        rng = np.random.default_rng(seed)
        for _ in range(draws):
            trial = blocks[rng.integers(len(blocks), size=len(blocks))].sum(axis=0)
            for level, value in ((6, kappa(trial)), (3, kappa(three(trial)))):
                if value is not None:
                    simulations[level].append(value)
    result = {"planned_pairs": len(rows), "both_valid": both_valid, "both_nonnull": int(matrix.sum()),
              "both_abstained": both_null, "one_abstained": one_null, "equal": equal,
              "raw_agreement_including_abstention": equal / both_valid if both_valid else None,
              "student_clusters_dual_nonnull": len(grouped), "kappa6": kappa(matrix), "kappa3": kappa(three(matrix)),
              "matrix6": matrix.tolist(), "matrix3": three(matrix).tolist(),
              "scope": "Conditional model agreement; not accuracy. Three tiers: L1-2/L3-4/L5-6. Student-cluster intervals do not remove cross-student dependence from identical text or exact-input reuse and are not independent-call accuracy intervals.",
              "bootstrap_draws": draws, "seed": seed}
    for level in (6, 3):
        sims = simulations[level]
        result[f"kappa{level}_cluster_interval95"] = np.quantile(sims, [.025, .975]).tolist() if sims else None
        result[f"kappa{level}_valid_bootstrap_draws"] = len(sims)
    return result


def student_indicators(data: FrozenTurns) -> list[dict]:
    grouped = defaultdict(list)
    for row in data.rows:
        if row["record"]["term"] != "synthetic":
            grouped[(row["record"]["student"], row["record"]["term"])].append(row)
    output = []
    for (student, term), rows in sorted(grouped.items()):
        done = [r for r in rows if r["complete"]]
        candidates = [r for r in done if r["final"] is not None]
        levels = [r["final"] for r in candidates]
        raw_tools = [data.turns[r["record"]["id"]].get("agent") for r in candidates]
        tools = [str(value).strip() if value is not None else "" for value in raw_tools]
        tools = [value if value and value.casefold() != "unknown" else None for value in tools]
        known = {value for value in tools if value is not None}
        tools_complete = bool(tools) and all(value is not None for value in tools)
        states = Counter(r["dimensions"]["task"]["final"]["status"] for r in done)
        values = dict.fromkeys((*NAMES, "ABL_raw", "MAB_raw"))
        if levels:
            mean = sum(levels) / len(levels)
            values.update(ABL_raw=mean, ABL=(mean - 1) / 5, HOT=sum(v >= 4 for v in levels) / len(levels),
                          DHI=dhi(distribution(levels)))
            if tools_complete:
                values.update(MAB_raw=len(known), MAB=float(min(1, np.log1p(len(known)) / np.log(5))))
        # t3 has no adjudicated session-edge input. Structural CSV adjacency is
        # insufficient, including when every selected label happens to be present.
        values["CTQ"] = None
        result = {"student": student, "term": term, "eligible_turns": len(rows), "completed_turns": len(done),
                  "unfinished_turns": len(rows) - len(done), "candidate_turns": len(candidates),
                  "candidate_coverage": len(candidates) / len(rows),
                  "completed_candidate_coverage": len(candidates) / len(done) if done else None,
                  **{f"task_{s}": states[s] for s in FINAL_STATES},
                  "observed_tools_in_candidate_subset": len(known), "candidate_tool_evidence_complete": tools_complete,
                  **values, "verified_session_edges": 0,
                  "snapshot_manifest_sha256": data.source["manifest_sha256"],
                  "scope": "completed_model_candidate_turns; not student ability or learning gain"}
        reasons = ["session_continuity_unverified"]
        if not levels:
            reasons.insert(0, "no_completed_candidate_label")
        elif not tools_complete:
            reasons.append("candidate_tool_identity_missing")
        for name, weights in WEIGHTS.items():
            result[f"AIV_{name}"] = composite(values, weights)
            result[f"rank_{name}"] = None
            if levels:
                low = 100 * sum(weights[i] * values[n] for i, n in enumerate(NAMES) if values[n] is not None)
                missing = 100 * sum(weights[i] for i, n in enumerate(NAMES) if values[n] is None)
                result[f"{name}_formula_lower"], result[f"{name}_formula_upper"] = float(low), float(low + missing)
            else:
                result[f"{name}_formula_lower"] = result[f"{name}_formula_upper"] = None
        result["missing_reason"] = ";".join(reasons)
        output.append(result)
    return output


def build_reports(data: FrozenTurns, *, draws=2000, seed=26) -> tuple[dict, list[dict]]:
    plan, source = data.plan, data.source
    real = [r for r in data.rows if r["record"]["term"] != "synthetic"]
    controls = [r for r in data.rows if r["record"]["term"] == "synthetic"]
    done = [r for r in real if r["complete"]]
    fast = [m["id"] for m in plan["models"] if m["role"] == "fast"]
    reviewers = [m["id"] for m in plan["models"] if m["role"] == "review"]
    students = student_indicators(data)
    cohorts = {name: [r for r in real if r["review_selection"] == name] for name in ("random_audit", "high_risk")}
    cohorts["control"] = controls
    repeats = [r for r in real if r["record"]["id"] in set(plan["repeat_ids"])]
    denominators = {"planned_real": len(real), "completed_real": len(done), "unfinished_real": len(real) - len(done),
                    "candidate_labeled_real": sum(r["final"] is not None for r in done),
                    "unknown_or_abstained_real": sum(r["final"] is None for r in done),
                    "planned_controls": len(controls), "completed_controls": sum(r["complete"] for r in controls),
                    "student_term_rows": len(students), "random_review_preselected": len(plan["audit_ids"]),
                    "high_risk_review_selected": len(cohorts["high_risk"]), "repeat_preselected": len(repeats),
                    "repeat_expected_tasks": 2 * len(repeats)}
    by_term = []
    for term in sorted({r["record"]["term"] for r in real}):
        term_rows = [r for r in real if r["record"]["term"] == term]
        complete = [r for r in term_rows if r["complete"]]
        by_term.append({"term": term, "planned": len(term_rows), "completed": len(complete),
                        "unfinished": len(term_rows) - len(complete), "candidate_labeled": sum(r["final"] is not None for r in complete),
                        "unknown_or_abstained": sum(r["final"] is None for r in complete)})
    availability = {"student_term_rows": len(students), "labeled_student_term_rows": sum(s["candidate_turns"] > 0 for s in students),
                    "full_AIV_available": sum(s["AIV_balanced"] is not None for s in students),
                    "full_AIV_rank_available": sum(s["rank_balanced"] is not None for s in students),
                    "CTQ_available": sum(s["CTQ"] is not None for s in students), "MAB_available": sum(s["MAB"] is not None for s in students)}
    summary = {"schema_version": SCHEMA, "source": source, "denominators": denominators, "by_term": by_term,
               "score_availability": availability, "independent_tasks": task_counts(real, "independent", fast),
               "independent_agreement": {dimension: agreement(real, "independent", fast, field, draws=draws, seed=seed)
                                         for dimension, field in (("task", "level"), ("contribution", "contribution_level"))},
               "independent_agreement_scope": "All real turns with both fast outputs valid, regardless of final row completion.",
               "scope": "Completed model-candidate text only; incomplete rows stay in the planned denominator.",
               "interpretation": "Formula ranges are missing-metric bounds conditional on candidate labels, not confidence intervals."}
    for dimension in ("task", "contribution"):
        status = Counter(r["dimensions"][dimension]["final"]["status"] for r in done)
        summary[f"final_{dimension}_status_counts"] = {s: status[s] for s in FINAL_STATES}
    strata = {}
    for name, cohort in cohorts.items():
        strata[name] = {"planned_records": len(cohort), "completed_records": sum(r["complete"] for r in cohort),
                        "review_tasks": task_counts(cohort, "review", reviewers),
                        "by_term": dict(Counter(r["record"]["term"] for r in cohort)),
                        "agreement": {dimension: agreement(cohort, "review", reviewers, field, draws=draws, seed=seed)
                                      for dimension, field in (("task", "level"), ("contribution", "contribution_level"))}}
        if name == "control":
            strata[name]["independent_tasks"] = task_counts(cohort, "independent", fast)
    repeat_models = {}
    for model in fast:
        item = {"tasks": task_counts(repeats, "repeat", [model])}
        for dimension, field in (("task", "level"), ("contribution", "contribution_level")):
            paired = equal = 0
            for row in repeats:
                values = [next((e.get("judgment") for e in row[stage] if e["model"] == model), None)
                          for stage in ("independent", "repeat")]
                if all(v is not None for v in values):
                    paired += 1
                    equal += values[0][field] == values[1][field]
            item[dimension] = {"planned_pairs": len(repeats), "paired_valid": paired, "equal": equal,
                               "stability": equal / paired if paired else None}
        repeat_models[model] = item
    checks = dict(data.checks)
    checks.update(student_rows_unique=len({(s["student"], s["term"]) for s in students}) == len(students),
                  student_eligible_reconciles=sum(s["eligible_turns"] for s in students) == len(real),
                  student_completed_reconciles=sum(s["completed_turns"] for s in students) == len(done),
                  student_candidates_reconcile=sum(s["candidate_turns"] for s in students) == denominators["candidate_labeled_real"],
                  missing_ctq_and_ranking_preserved=availability["CTQ_available"] == availability["full_AIV_rank_available"] == 0)
    require(all(checks.values()), "Material reconciliation failed")
    reports = {"summary.json": summary,
               "review-strata.json": {"schema_version": SCHEMA, "source": source, "strata": strata,
                                      "audit_strata": plan["audit_strata"],
                                      "scope": "Fixed random audit, selected high risk, and synthetic controls reported separately; not accuracy."},
               "repeatability.json": {"schema_version": SCHEMA, "source": source, "planned_records": len(repeats),
                                      "expected_tasks": 2 * len(repeats), "tasks": task_counts(repeats, "repeat", fast),
                                      "by_model": repeat_models, "scope": "Within-model repeat stability, including paired abstention; no cache reuse of repeat tasks."},
               "data-quality.json": {"schema_version": SCHEMA, "source": source, "passed": True, "checks": checks,
                                     "student_term_rows": len(students),
                                     "limitations": ["Q/A roles and session continuity are structurally inferred, not adjudicated.",
                                                     "Tool breadth is conditional on completed candidate turns and requires every tool identity.",
                                                     "API/cache lineage must also pass validate_full_turns.py; this offline adapter does not read its database."]}}
    return reports, students
