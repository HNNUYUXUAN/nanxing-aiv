import copy
import csv
import hashlib
import io
import json
import re
import sys
import zipfile

import pytest

from aiv.product_reports import compare, export_bundle, report, results_csv


def entry(model, level, contribution=None, *, status="done"):
    return {"model": model, "status": status,
            "judgment": {"level": level, "contribution_level": contribution,
                         "evidence": "原文证据", "contribution_evidence": "学生推理", "reason": "候选理由"}
            if status == "done" else None}


def row(key, student, task, contribution, *, term="25f", complete=True,
        task_status="agreed", contribution_status=None):
    return {"record": {"id": key, "student": student, "term": term,
                       "question": "私有原文标记：原文证据；学生推理", "agent": "tool-a",
                       "timestamp": "2026-01-01", "session_candidate_id": "same-unverified-session",
                       "source": {"file": "source.csv", "row": 2, "sha256": "source-hash", "char_start": 0, "char_end": 10}},
            "complete": complete, "final": task,
            "dimensions": {"task": {"final": {"label": task, "status": task_status}},
                           "contribution": {"final": {"label": contribution,
                                                       "status": contribution_status or ("agreed" if contribution else "abstained")}}},
            "independent": [entry("fast-a", task, contribution), entry("fast-b", task, contribution)],
            "review": [], "repeat": [], "review_selection": None}


@pytest.fixture
def context():
    first = row("one", "A", 2, 3)
    first["independent"] = [entry("fast-a", 1), entry("fast-b", 1)]
    first["review_selection"] = "random_audit"
    first["review"] = [entry("review-a", 2, 3), entry("review-b", 2, 3)]
    first["repeat"] = [entry("fast-a", 2), entry("fast-b", 1)]
    second = row("two", "A", None, None, task_status="disagreement")
    second["review_selection"] = "high_risk"
    second["independent"] = [entry("fast-a", 3, 2), entry("fast-b", 4)]
    second["review"] = [entry("review-a", 3, 2), entry("review-b", 4)]
    third = row("three", "B", None, None, term="26s", task_status="abstained")
    third["independent"] = [entry("fast-a", None), entry("fast-b", None)]
    fourth = row("four", "C", 5, 4, complete=False)
    fourth["independent"] = [entry("fast-a", None, status="failed"), entry("fast-b", None, status="running")]
    control = row("control", "CONTROL", 6, 6, term="synthetic")
    control["independent"] = [entry("fast-a", 6, 6), entry("fast-b", 1, 1)]
    return {"mode": "live", "dataset": {"id": "batch", "name": "新短测", "private": True},
            "job": {"id": "job-1", "mode": "live", "state": "paused", "created": 1000},
            "plan": {"real": 4, "experiment": "short-test", "revision": "t1", "sample_sha256": "sample-hash",
                     "models": [{"id": n, "role": role} for n, role in
                                (("fast-a", "fast"), ("fast-b", "fast"), ("review-a", "review"), ("review-b", "review"))],
                     "audit_ids": ["one"], "repeat_ids": ["one", "three"]},
            "rows": [first, second, third, fourth, control],
            "confirmations": [{"id": "confirmation-1", "turn_id": "one", "decision": "needs_review",
                               "dimension": "task", "operator": "内部体验", "note": "确认备注私有原文标记", "created_at": 1100},
                              {"id": "control-confirmation", "turn_id": "control", "decision": "accepted"}]}


def test_live_denominators_distinguish_terminal_pending_and_controls(context):
    data = compare(context)
    assert data["total"] == 4
    assert data["denominators"] == {"planned_records": 4, "observed_records": 4, "completed_records": 3,
                                     "expected_requests": 8, "created_requests": 8, "valid_outputs": 6,
                                     "failed_requests": 1, "pending_requests": 1}
    pair = data["pairs"][0]
    assert pair["both_valid"] == 3 and pair["n"] == 2 and pair["both_abstained"] == 1
    assert pair["agreement"] == 0.5 and pair["agreement_including_abstention"] == pytest.approx(2 / 3)
    assert pair["kappa6"] == pytest.approx(1 / 3)
    assert data["labelled"] == 1 and data["final_distribution"] == [0, 1, 0, 0, 0, 0]
    assert data["states"]["unfinished"] == 1


def test_review_groups_and_repeat_pair_baseline_with_same_model(context):
    random = compare(context, "contribution", "random_audit")
    assert random["total"] == 1 and random["pairs"][0]["n"] == 1
    assert random["pairs"][0]["agreement"] == 1
    assert random["pairs"][0]["kappa6"] is None  # single-category degenerate chance agreement
    risk = compare(context, "task", "high_risk")
    assert risk["total"] == 1 and risk["pairs"][0]["agreement"] == 0
    repeat = compare(context, "task", "repeat")
    assert repeat["total"] == 2 and repeat["denominators"]["expected_requests"] == 4
    assert repeat["denominators"]["pending_requests"] == 2
    assert [p["agreement"] for p in repeat["pairs"]] == [0, 1]
    assert all(p["planned_pairs"] == 2 and p["unavailable_pairs"] == 1 for p in repeat["pairs"])


def test_missing_requests_and_missing_dimension_never_become_abstentions(context):
    context["plan"]["real"] = 5
    context["rows"][0]["independent"][0]["judgment"].pop("contribution_level")
    data = compare(context, "contribution")
    assert data["denominators"]["expected_requests"] == 10
    assert data["denominators"]["pending_requests"] == 3
    assert data["pairs"][0]["both_valid"] == 2
    assert data["pairs"][0]["both_abstained"] == 1
    assert data["states"]["unfinished"] == 2


def test_reports_keep_dimensions_missingness_scopes_and_confirmation_separate(context):
    result = report(context)
    assert result["summary"]["total"] == 4 and result["summary"]["completed"] == 3
    assert result["summary"]["task_candidates"] == result["summary"]["contribution_candidates"] == 1
    assert result["summary"]["student_terms"] == 3
    assert all(s["CTQ"] is None and s["AIV"] is None and s["rank"] is None for s in result["rows"])
    assert result["confirmations"][0]["decision"] == "needs_review"
    assert len(result["confirmations"]) == 1 and result["dimensions"]["task"]["candidate"] == 1
    assert result["actions"][0]["evidence"][0]["href"].startswith("#trace?item=")
    assert result["metadata"]["sample_sha256"] == "sample-hash"
    selected = report(context, "student", "A", "25f")
    assert selected["scope"] == {"student": "A", "term": "25f", "description": result["scope"]["description"]}
    assert selected["summary"]["total"] == 2
    assert {r["student"] for r in selected["rows"]} == {"A"}
    assert report(context, "student", "does-not-exist")["ready"] is False
    for role in ("teacher", "administrator"):
        filtered = report(context, role, "A")
        assert filtered["scope"]["student"] == "A" and filtered["summary"]["total"] == 2
        assert {r["student"] for r in filtered["rows"]} == {"A"}
    admin = report(context, "administrator")
    assert len(admin["comparability"]) == 5 and all(c["status"] == "待补采" for c in admin["comparability"])


def test_official_student_metrics_preserve_null_and_never_revive_t3_ranking(context):
    context["mode"] = "frozen"
    context["plan"]["revision"] = "t3"
    context["student_indicators"] = [{"student": "A", "term": "25f", "ABL_raw": "2", "ABL": "0.2", "HOT": "0",
                                      "DHI": "0.15", "MAB": "", "CTQ": "0.9", "missing_reason": "official_missing"}]
    item = next(r for r in report(context)["rows"] if r["student"] == "A")
    assert item["ABL"] == 0.2 and item["HOT"] == 0 and item["DHI"] == 0.15
    assert item["MAB"] is item["CTQ"] is item["AIV"] is item["rank"] is None
    assert item["metric_source"] == "official_student_indicators"
    parsed = list(csv.DictReader(io.StringIO(results_csv(context).lstrip("\ufeff"))))
    assert len(parsed) == 3 and all(r["CTQ"] == r["AIV"] == r["rank"] == "" for r in parsed)


def test_tools_must_be_complete_and_verified_edges_are_explicit(context):
    context["rows"][1] = row("two", "A", 4, 4)
    context["rows"][1]["record"]["agent"] = None
    before = report(context, "student", "A")["rows"][0]
    assert before["MAB"] is None and before["CTQ"] is None and before["AIV"] is None
    context["verified_edges"] = [{"from": "one", "to": "two", "verified": True},
                                 {"from": "one", "to": "two", "verified": True}]
    after = report(context, "student", "A")["rows"][0]
    assert after["CTQ"] == 0.7 and after["AIV"] is None
    context["rows"][1]["record"]["agent"] = "tool-b"
    assert report(context, "student", "A")["rows"][0]["AIV"] is not None


def test_synthetic_complete_sequence_supports_teaching_weights(context):
    context["mode"] = "synthetic"
    context["rows"] = [row("s1", "S001", 2, 2, term="synthetic"), row("s2", "S001", 4, 4, term="synthetic")]
    context["rows"][1]["record"]["timestamp"] = "2026-01-02"
    context["rows"][0]["record"]["source"] = "synthetic_constructed"
    data = compare(context)
    assert set(data["weights"]) == {"balanced", "higher_order", "process"}
    assert data["students"][0]["balanced"] is not None
    assert len(data["sensitivity"]) == 1
    assert set(data["sensitivity"][0]) >= {"score_low", "score_high", "rank_low", "rank_high"}
    assert report(context)["evidence"][0]["source"]["file"] is None


def test_candidate_evidence_must_match_selected_level_and_original(context):
    assert {r["id"] for r in report(context, "student", "A")["evidence"]} == {"one", "two"}
    # Cite the judgment supporting the final level rather than the initial label.
    context["rows"] = context["rows"][:1]
    context["rows"][0]["independent"][0]["judgment"]["evidence"] = "私有原文标记"
    context["rows"][0]["review"][0]["judgment"]["evidence"] = "不存在的引文"
    evidence = report(context, "student", "A")["evidence"][0]
    assert evidence["task"] == 2 and evidence["task_evidence"] == "原文证据"
    assert evidence["contribution_evidence"] == "学生推理"


def test_export_is_deterministic_hashed_and_excludes_original_free_text_and_secrets(context):
    usage = {"input_tokens": 100, "output_tokens": 12, "requests": 5,
             "api_key": "never-export-this-secret", "response": "私有原文标记"}
    blob = export_bundle(context, usage)
    assert blob == export_bundle(context, usage)
    archive = zipfile.ZipFile(io.BytesIO(blob))
    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["contains_original_question_fields"] is False
    assert manifest["includes_confirmation_notes"] is True
    assert manifest["contains_student_identifiers"] is True
    assert set(manifest["files"]) == set(archive.namelist()) - {"manifest.json"}
    for filename, sha in manifest["files"].items():
        content = archive.read(filename)
        assert hashlib.sha256(content).hexdigest() == sha
        assert context["rows"][0]["record"]["question"].encode() not in content
        assert b"never-export-this-secret" not in content
    confirmations = json.loads(archive.read("confirmations.json"))
    assert len(confirmations) == 1 and "note_sha256" in confirmations[0]
    assert confirmations[0]["note"] == context["confirmations"][0]["note"]
    assert json.loads(archive.read("job-usage.json"))["usage"] == {"input_tokens": 100, "output_tokens": 12, "requests": 5}
    turns = list(csv.DictReader(io.StringIO(archive.read("turn-results.csv").decode("utf-8-sig"))))
    assert len(turns) == 4 and {r["id"] for r in turns} == {"one", "two", "three", "four"}
    assert next(r for r in turns if r["id"] == "four")["task_label"] == ""


def test_report_functions_are_pure_and_json_safe(context):
    original = copy.deepcopy(context)
    for value in (compare(context), report(context), report(context, "administrator")):
        json.dumps(value, allow_nan=False)
    results_csv(context)
    export_bundle(context)
    assert context == original
    with pytest.raises(ValueError):
        compare(context, "unknown")
    with pytest.raises(ValueError):
        report(context, "unknown")


def test_export_keeps_safe_nested_live_usage_and_frozen_request_counts(context):
    live = {"source": "persistent_pool_ledger", "experiment": "short-test", "cost": None,
            "models": [{"provider": "provider", "model": "fast-a", "calls": 3, "input_tokens": 100,
                        "output_tokens": None, "successes": 2, "account": "private-account", "api_key": "private-key"}],
            "states": [{"status": "failed", "error_kind": "timeout", "count": 1}]}
    archive = zipfile.ZipFile(io.BytesIO(export_bundle(context, live)))
    result = json.loads(archive.read("job-usage.json"))
    assert result["usage"]["models"][0]["calls"] == 3
    assert result["usage"]["models"][0]["output_tokens"] is None
    assert result["usage"]["states"][0]["count"] == 1 and result["usage"]["cost"] is None
    assert result["request_sets"]["independent"]["expected_requests"] == 8
    assert "private-account" not in archive.read("job-usage.json").decode()
    frozen = {"source": "frozen_summary", "cost": None, "summary": {"n": 5, "completed_records": 4,
              "stages": {"independent": {"requested": 10, "valid": 8, "failed": 1}},
              "pool": {"tasks": {"done": 8, "failed": 1, "running": 1},
                       "ledger": [{"account": "private-account", "currency": "USD", "attempts": 7,
                                   "known_cost": None, "input_tokens": 23, "unknown_usage_attempts": 3},
                                  {"account": "other-account", "currency": "USD", "attempts": 3,
                                   "known_cost": None, "input_tokens": 7, "unknown_usage_attempts": 1}]}}}
    archive = zipfile.ZipFile(io.BytesIO(export_bundle(context, frozen)))
    result = json.loads(archive.read("job-usage.json"))["usage"]["summary"]
    assert result["stages"]["independent"]["requested"] == 10
    assert result["pool"]["ledger"] == [{"currency": "USD", "attempts": 10, "known_cost": None,
                                          "input_tokens": 30, "unknown_usage_attempts": 4}]
    assert "private-account" not in archive.read("job-usage.json").decode()


def test_csv_identifier_is_not_a_spreadsheet_formula(context):
    context["rows"][0]["record"]["student"] = "=1+1"
    assert "'=1+1" in results_csv(context)


def test_scoped_export_uses_same_membership_in_all_views_tables_and_denominators(context):
    context["metadata"] = {"application_url": "http://127.0.0.1:8770/", "snapshot_manifest_sha256": "source-snapshot"}
    context["confirmations"][0]["note"] = "保留完整备注 <script>not code</script>"
    archive = zipfile.ZipFile(io.BytesIO(export_bundle(context, {"input_tokens": 999999}, role="administrator", student="A", term="25f")))
    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["metadata"]["selected_role"] == "administrator"
    assert manifest["metadata"]["selection"] == {"student": "A", "term": "25f"}
    assert len(manifest["metadata"]["selection_sha256"]) == 64
    for view in ("student", "teacher", "administrator"):
        value = json.loads(archive.read(f"reports/{view}.json"))
        assert value["summary"]["total"] == 2 and value["summary"]["student_terms"] == 1
        assert value["metadata"]["selected_role"] == "administrator"
        assert value["scope"]["student"] == "A" and value["scope"]["term"] == "25f"
        assert value["evidence"][0]["href"].startswith("http://127.0.0.1:8770/#trace?job=job-1&item=")
        content = archive.read(f"reports/{view}.html").decode()
        for section in ("覆盖与缺测", "双维状态与待核事项", "候选层级分布", "可比性与补采", "独立人工确认记录"):
            assert section in content
        assert "保留完整备注 &lt;script&gt;not code&lt;/script&gt;" in content
    table = list(csv.DictReader(io.StringIO(archive.read("results.csv").decode("utf-8-sig"))))
    assert len(table) == 1 and table[0]["student"] == "A"
    turns = list(csv.DictReader(io.StringIO(archive.read("turn-results.csv").decode("utf-8-sig"))))
    assert {r["id"] for r in turns} == {"one", "two"}
    accounting = json.loads(archive.read("job-usage.json"))
    assert accounting["request_sets"]["independent"]["expected_requests"] == 4
    assert accounting["request_sets"]["repeat"]["expected_requests"] == 2
    assert accounting["usage"]["input_tokens"] is None and accounting["usage_scope"] == "selected_records"
    assert b"999999" not in archive.read("job-usage.json")


def test_unfiltered_export_does_not_silently_limit_student_view_to_first_student(context):
    archive = zipfile.ZipFile(io.BytesIO(export_bundle(context)))
    for view in ("student", "teacher", "administrator"):
        value = json.loads(archive.read(f"reports/{view}.json"))
        assert value["summary"]["total"] == 4 and value["scope"]["student"] == ""
    selected = zipfile.ZipFile(io.BytesIO(export_bundle(context, role="student")))
    assert json.loads(selected.read("reports/teacher.json"))["summary"]["total"] == 2


@pytest.mark.parametrize("scope", [{}, {"role": "administrator", "student": "A", "term": "25f"}, {"student": "missing"}])
def test_export_readme_verifier_runs_for_actual_scope(context, scope, tmp_path, monkeypatch, capsys):
    context["plan"]["rubric_sha256"] = "rubric-version-digest"
    payload = export_bundle(context, **scope)
    archive = zipfile.ZipFile(io.BytesIO(payload))
    readme = archive.read("README.md").decode("utf-8")
    snippet = re.search(r"```python\n(.*?)\n```", readme, re.S).group(1)
    target = tmp_path / "report.zip"
    target.write_bytes(payload)
    monkeypatch.setattr(sys, "argv", ["verify_export.py", str(target)])
    exec(compile(snippet, "README.md:verify_export.py", "exec"), {})
    assert "Verified file hashes, scope, notes, coverage and distributions." in capsys.readouterr().out
    assert json.loads(archive.read("manifest.json"))["metadata"]["rubric_sha256"] == "rubric-version-digest"
    for definition in ("HOT = count(L_i>=4)/m", "DHI = 1 - sum(abs(p_k-q_k))/2", "ln(1+d)/ln(5)",
                       "kappa=(agreement-p_e)/(1-p_e)", "separators=(',', ':')", "indent=2, allow_nan=False",
                       "未携带逐模型配对矩阵", "不是 Git 提交号"):
        assert definition in readme


def test_export_readme_verifier_rejects_changed_file_and_rehashed_wrong_denominator(context, tmp_path, monkeypatch):
    source = zipfile.ZipFile(io.BytesIO(export_bundle(context)))
    files = {name: source.read(name) for name in source.namelist()}
    snippet = re.search(r"```python\n(.*?)\n```", files["README.md"].decode(), re.S).group(1)
    target = tmp_path / "report.zip"
    monkeypatch.setattr(sys, "argv", ["verify_export.py", str(target)])

    def write_archive():
        with zipfile.ZipFile(target, "w") as archive:
            for name, value in files.items():
                archive.writestr(name, value)

    files["results.csv"] += b"changed"
    write_archive()
    with pytest.raises(AssertionError, match="file hash mismatch: results.csv"):
        exec(compile(snippet, "README.md:verify_export.py", "exec"), {})
    files["results.csv"] = source.read("results.csv")
    report_data = json.loads(files["reports/teacher.json"])
    report_data["summary"]["total"] += 1
    files["reports/teacher.json"] = json.dumps(report_data).encode()
    manifest = json.loads(files["manifest.json"])
    manifest["files"]["reports/teacher.json"] = hashlib.sha256(files["reports/teacher.json"]).hexdigest()
    files["manifest.json"] = json.dumps(manifest).encode()
    write_archive()
    with pytest.raises(AssertionError, match="turn denominator mismatch"):
        exec(compile(snippet, "README.md:verify_export.py", "exec"), {})
