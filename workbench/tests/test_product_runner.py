"""Real scheduler/graph tests with local deterministic transport and private temp files."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import csv
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from aiv import product_runner as runner
from aiv.fast_strong_workflow import controls, digest
from aiv.full_turn_workflow import model_conditions
from aiv.pool import CallError, Pool
from aiv.pool_config import Account, Config, Credential


def fixture(tmp_path, count=2, name="example"):
    runtime = tmp_path / "product"
    directory = runtime / "jobs" / name
    source = runtime / "datasets" / (name + ".csv")
    source.parent.mkdir(parents=True, exist_ok=True)
    questions = [f"请解释第{i + 1}个概率概念。" for i in range(count)]
    with source.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["text"])
        writer.writeheader()
        writer.writerows({"text": q} for q in questions)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    records = [{"id": f"{name}-{i}", "student": f"fixture-{i % 3}", "term": "25f", "question": q,
                "parse_status": "explicit_role_unverified",
                "source": {"origin": "product", "file": source.relative_to(runtime).as_posix(),
                           "row": i + 2, "field": "text", "char_start": 0, "char_end": len(q),
                           "sha256": source_hash, "field_sha256": hashlib.sha256(q.encode()).hexdigest()}}
               for i, q in enumerate(questions)]
    settings = {"runtime_root": str(runtime), "research_root": str(tmp_path / "research"),
                "code_root": str(Path(__file__).resolve().parents[1]),
                "credentials_file": str(tmp_path / "unread.env")}
    plan = runner.create_plan(directory, records, name="fixture", source_digest=digest(records))
    credentials = [Credential(f"{p}_{i}", p, f"{p}_{i}_account", f"DUMMY-{p}-{i}", "https://example.test/v1",
                   tuple(m["id"] for m in model_conditions() if m["provider"] == p))
                   for p in runner.PROVIDERS for i in (1, 2)]
    accounts = {c.group: Account(c.group, c.provider, concurrency=2, call_limit=10**6, token_limit=10**12)
                for c in credentials}
    config = Config(credentials, accounts, str(runtime / "pool.sqlite3"), concurrency=4, max_attempts=2, timeout=90)
    return directory, settings, config, plan, records


def transport(_, task, __):
    question = json.loads(task["messages"][-1]["content"])["student_text"]
    refs = {r["question"]: r["reference"] for r in controls()}
    reference = refs.get(question, {"level": 2, "contribution_level": None})
    level, contribution = reference["level"], reference["contribution_level"]
    judgment = {"level": level, "probabilities": [int(i + 1 == level) for i in range(6)] if level else None,
                "evidence": question[:40] if level else "", "reason": "合成传输验证",
                "contribution_level": contribution, "contribution_evidence": question[:40] if contribution else "",
                "uncertain": False}
    return {"text": json.dumps(judgment, ensure_ascii=False), "input_tokens": 50, "output_tokens": 60,
            "valid": True, "finish_reason": "stop"}


def test_full_twelve_record_protocol_and_plan_budget(tmp_path):
    directory, settings, config, plan, records = fixture(tmp_path, 12)
    assert plan["limits"]["tokendance"]["calls"] == 144
    assert plan["limits"]["next"]["calls"] == 96
    result = runner.run(directory, settings, config=config, transport=transport)
    assert result["state"] == "finished", result
    rows = json.loads((directory / "annotations.json").read_text("utf-8"))
    real = [row for row in rows if row["record"]["term"] != "synthetic"]
    assert len(rows) == 24 and len(real) == 12 and all(row["complete"] for row in rows)
    assert all(len(row["independent"]) == len(row["review"]) == len(row["repeat"]) == 2 for row in real)
    assert all(row["dimensions"]["contribution"]["final"]["label"] is None for row in real)
    with sqlite3.connect(config.db) as db:
        counts = dict(db.execute("SELECT provider,COUNT(*) FROM attempts GROUP BY provider"))
        assert counts == {"next": 48, "tokendance": 72}
        assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 120
        assert db.execute("SELECT MAX(attempts) FROM tasks").fetchone()[0] == 1
    assert (directory / "frozen/snapshot-manifest.json").is_file()
    assert runner.create_plan(directory, records, name="fixture", source_digest=digest(records)) == plan
    assert runner.run(directory, settings, config=config, transport=lambda *args: pytest.fail("frozen rerun"))["state"] == "finished"


def test_general_batches_budget_only_up_to_forty_repeat_records(tmp_path):
    _, _, _, plan, _ = fixture(tmp_path, 41)
    assert len(plan["repeat_ids"]) == 40
    assert plan["limits"]["tokendance"]["calls"] == 2 * (2 * 41 + 2 * 40 + 24)
    assert plan["limits"]["next"]["calls"] == 2 * (2 * 41 + 24)


def test_pause_settles_calls_and_resume_keeps_attempt_identity(tmp_path):
    directory, settings, config, _, _ = fixture(tmp_path)
    trigger = threading.Event()
    errors = []
    def pausing(*args):
        if not trigger.is_set():
            trigger.set()
            try:
                assert runner.pause(directory)["state"] == "pausing"
            except Exception as error:
                errors.append(repr(error))
                raise
        time.sleep(.02)
        return transport(*args)
    result = runner.run(directory, settings, config=config, transport=pausing)
    assert result["state"] == "paused" and not (directory / "frozen").exists(), errors
    with sqlite3.connect(config.db) as db:
        previous = list(db.execute("SELECT id,task_id,status FROM attempts ORDER BY id"))
        assert previous and not db.execute("SELECT 1 FROM attempts WHERE status='running'").fetchone()
    (directory / "PAUSE").unlink()
    assert runner.run(directory, settings, config=config, transport=transport)["state"] == "finished"
    with sqlite3.connect(config.db) as db:
        assert previous == list(db.execute("SELECT id,task_id,status FROM attempts ORDER BY id LIMIT ?", (len(previous),)))
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 60


def test_sources_and_plan_are_rechecked_before_start_and_resume(tmp_path):
    directory, settings, config, _, records = fixture(tmp_path)
    with pytest.raises(ValueError, match="摘要"):
        runner.create_plan(directory.parent / "bad", records, name="bad", source_digest="0" * 64)
    source = Path(settings["runtime_root"]) / records[0]["source"]["file"]
    source.write_text("changed", "utf-8")
    with pytest.raises(ValueError, match="哈希"):
        runner.start(directory, settings)
    result = runner.run(directory, settings, config=config, transport=lambda *args: pytest.fail("source changed"))
    assert result["state"] == "failed" and not Path(config.db).exists()
    directory2, settings2, config2, _, _ = fixture(tmp_path, name="other")
    plan_path = directory2 / "plan.json"
    plan_path.write_text(plan_path.read_text("utf-8") + " ", "utf-8")
    with pytest.raises(ValueError, match="哈希"):
        runner.start(directory2, settings2)


def test_source_origin_and_character_span_are_enforced(tmp_path):
    directory, settings, _, _, records = fixture(tmp_path)
    runner._verify_sources(records, settings)
    records[0]["source"]["origin"] = "research"
    with pytest.raises(ValueError, match="根目录"):
        runner._verify_sources(records, settings)
    records[0]["source"]["origin"] = "product"
    records[0]["question"] = "不同的文本"
    with pytest.raises(ValueError, match="字符"):
        runner._verify_sources(records, settings)


def test_control_quality_failure_is_blocked_and_never_runs_real_text(tmp_path):
    directory, settings, config, _, _ = fixture(tmp_path)
    def bad_control(*args):
        data = transport(*args)
        judgment = json.loads(data["text"])
        judgment.update(level=None, probabilities=None, evidence="", contribution_level=None, contribution_evidence="")
        data["text"] = json.dumps(judgment)
        return data
    result = runner.run(directory, settings, config=config, transport=bad_control)
    assert result["state"] == "blocked" and result["reason"] == "control_quality_failed"
    assert not (directory / "frozen").exists()
    with sqlite3.connect(config.db) as db:
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 48


def test_provider_cap_holds_across_pool_instances_and_keys(tmp_path):
    _, _, config, _, _ = fixture(tmp_path)
    a = Pool(config, transport, experiment="limit", provider_limits=runner.PROVIDERS)
    b = Pool(config, transport, experiment="limit", provider_limits=runner.PROVIDERS)
    for i in range(8):
        provider = "tokendance" if i < 4 else "next"
        model = next(m["id"] for m in model_conditions() if m["provider"] == provider)
        a.enqueue(provider=provider, model=model, messages=[{"role": "user", "content": "fixture"}],
                  experiment="limit", replicate=i)
    with ThreadPoolExecutor(max_workers=8) as executor:
        claimed = list(executor.map(lambda i: (a if i % 2 else b).claim(), range(8)))
    jobs = [job for job in claimed if job]
    assert Counter(job["task"]["provider"] for job in jobs) == {"next": 2, "tokendance": 2}


def test_global_owner_idempotent_start_and_stale_status(tmp_path, monkeypatch):
    directory, settings, _, _, _ = fixture(tmp_path)
    other, _, _, _, _ = fixture(tmp_path, name="other")
    launches = []
    class Process:
        pid = os.getpid()
        def __init__(self, *args, **kwargs):
            launches.append((args, kwargs))
    monkeypatch.setattr(runner.subprocess, "Popen", Process)
    first = runner.start(directory, settings)
    second = runner.start(directory, settings)
    assert first["worker_token"] == second["worker_token"] and len(launches) == 1
    with pytest.raises(runner.RunnerBusy):
        runner.start(other, settings)
    owner_path = Path(settings["runtime_root"]) / "owner.json"
    owner = json.loads(owner_path.read_text("utf-8")); owner["pid"] = -1
    runner._write(owner_path, owner)
    assert runner.status(directory)["state"] == "paused"


def test_health_import_keeps_key_accounts_and_recovers_orphans(tmp_path):
    directory, settings, config, plan, _ = fixture(tmp_path)
    source = Path(settings["research_root"]) / "runtime/pool.sqlite3"
    source.parent.mkdir(parents=True)
    with sqlite3.connect(source) as db:
        db.executescript("CREATE TABLE accounts(name TEXT,disabled TEXT,cooldown REAL); CREATE TABLE credentials(alias TEXT,disabled TEXT);")
        db.execute("INSERT INTO accounts VALUES ('next_1_account','exhausted',0)")
        db.execute("INSERT INTO credentials VALUES ('tokendance_1','authentication')")
    before = source.read_bytes()
    pool = Pool(config, transport, experiment=plan["experiment"])
    receipt = runner._health(pool, settings)
    assert source.read_bytes() == before
    assert {r["reason"] for r in receipt} == {"exhausted", "authentication", "no_disabled_state"}
    with pool.db() as db:
        assert db.execute("SELECT disabled FROM accounts WHERE name='next_1_account'").fetchone()[0] == "exhausted"
        assert db.execute("SELECT disabled FROM accounts WHERE name='next_2_account'").fetchone()[0] is None
    assert runner._health(pool, settings) == []
    task = pool.enqueue(provider="next", model="gpt-6-sol", experiment=plan["experiment"],
                        replicate="orphan", messages=[{"role": "user", "content": "fixture"}])
    claimed = pool.claim()
    assert claimed and claimed["task_id"] == task
    runner._health(pool, settings)
    with pool.db() as db:
        assert db.execute("SELECT status FROM tasks WHERE id=?", (task,)).fetchone()[0] == "queued"
        attempt = db.execute("SELECT status,hold_tokens FROM attempts WHERE id=?", (claimed["attempt"],)).fetchone()
        assert attempt["status"] == "unknown" and attempt["hold_tokens"] > 0
    changed = replace(config, credentials=[replace(config.credentials[0], key="ROTATED"), *config.credentials[1:]])
    with pytest.raises(ValueError, match="rotation"):
        runner._health(Pool(changed), settings)


def test_cross_process_global_lock_and_readonly_research_lock(tmp_path):
    directory, settings, _, _, _ = fixture(tmp_path)
    root = Path(settings["runtime_root"])
    helper = ("from pathlib import Path; import sys,time; from aiv.product_runner import _lock; "
              "guard=_lock(Path(sys.argv[1])); guard.__enter__(); print('locked',flush=True); "
              "time.sleep(1.0); guard.__exit__(None,None,None)")
    with subprocess.Popen([sys.executable, "-c", helper, str(root / "runner.lock")],
            cwd=settings["code_root"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as child:
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(runner.RunnerBusy):
            runner.start(directory, settings)
        child.wait(timeout=5)
        assert child.returncode == 0
    lock = Path(settings["research_root"]) / "runtime/research/full-turns-v1-fixture/t3/runner.lock"
    with runner._lock(lock):
        with pytest.raises(runner.RunnerBusy):
            runner._research_idle(settings)
    runner._research_idle(settings)


def test_health_snapshot_handoff_requires_same_credentials(tmp_path, monkeypatch):
    _, settings, config, _, _ = fixture(tmp_path)
    source = Path(settings["research_root"]) / "runtime/pool.sqlite3"
    source.parent.mkdir(parents=True)
    with sqlite3.connect(source) as db:
        db.executescript("CREATE TABLE accounts(name TEXT,disabled TEXT,cooldown REAL); CREATE TABLE credentials(alias TEXT,disabled TEXT); CREATE TABLE tasks(text TEXT);")
        db.execute("INSERT INTO accounts VALUES ('next_1_account','exhausted',0)")
        db.execute("INSERT INTO tasks VALUES ('PRIVATE_TASK_TEXT')")
    monkeypatch.setattr(runner, "_config", lambda *args: config)
    snapshot = Path(settings["runtime_root"]) / "health-snapshot.json"
    receipt = runner.export_health_snapshot(settings, snapshot)
    assert receipt["credential_count"] == 4 and receipt["disabled_credentials"] == 1
    content = snapshot.read_text("utf-8")
    assert "PRIVATE_TASK_TEXT" not in content
    assert all(credential.key not in content for credential in config.credentials)
    remote = {**settings, "research_root": str(tmp_path / "unavailable-research")}
    pool = Pool(config)
    runner._health(pool, remote)
    with pool.db() as db:
        assert db.execute("SELECT disabled FROM accounts WHERE name='next_1_account'").fetchone()[0] == "exhausted"
    changed = replace(config, credentials=[replace(config.credentials[0], key="OTHER-KEY"), *config.credentials[1:]])
    with pytest.raises(ValueError, match="identity_mismatch"):
        runner._health(Pool(changed), remote)


@pytest.mark.parametrize("term", ["2027春季", "teacher-pilot", "26f"])
def test_product_plan_accepts_business_terms(tmp_path, term):
    directory, _, _, _, records = fixture(tmp_path)
    for record in records:
        record["term"] = term
    plan = runner.create_plan(directory.parent / "new-term", records, name="new term", source_digest=digest(records))
    assert set(plan["audit_strata"]) == {term}


@pytest.mark.parametrize("term", ["", "  ", "synthetic", " Synthetic ", None])
def test_product_plan_rejects_empty_or_reserved_terms(tmp_path, term):
    directory, _, _, _, records = fixture(tmp_path)
    records[0]["term"] = term
    with pytest.raises(ValueError, match="学期"):
        runner.create_plan(directory.parent / "bad-term", records, name="bad term", source_digest=digest(records))


def test_canary_progress_is_visible_before_any_request_finishes(tmp_path):
    directory, settings, config, _, _ = fixture(tmp_path)
    release = threading.Event()
    entered = threading.Event()
    def slow(*args):
        entered.set()
        assert release.wait(10)
        return transport(*args)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(runner.run, directory, settings, config=config, transport=slow)
        try:
            assert entered.wait(5)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                state = runner._read(directory / "state.json")
                if state.get("controls", {}).get("tasks", {}).get("running", 0):
                    break
                time.sleep(.05)
            assert state["phase"] == "controls"
            progress = state["controls"]
            assert progress["total"] == progress["pending"] == 12
            assert progress["completed"] == progress["succeeded"] == progress["failed"] == 0
            assert progress["tasks"]["running"] == 4 and progress["tasks"]["queued"] == 44
            rows = runner._read(directory / "annotations.json")
            assert len(rows) == 12 and all(r["record"]["term"] == "synthetic" for r in rows)
            assert sum(e["status"] == "running" for r in rows for s in ("independent", "review") for e in r[s]) == 4
        finally:
            release.set()
        assert future.result(timeout=15)["state"] == "finished"
    final = runner._read(directory / "state.json")["controls"]
    assert final["completed"] == final["succeeded"] == 12 and final["tasks"]["succeeded"] == 48


def test_control_failure_counts_are_separate_from_pending(tmp_path):
    directory, settings, config, _, _ = fixture(tmp_path)
    def one_failure(credential, task, timeout):
        question = json.loads(task["messages"][-1]["content"])["student_text"]
        if task["model"] == "deepseek-v4.1-flash" and question == controls()[0]["question"]:
            raise CallError("invalid_request", status=400)
        return transport(credential, task, timeout)
    result = runner.run(directory, settings, config=config, transport=one_failure)
    assert result["state"] == "blocked" and result["reason"] == "control_quality_failed"
    progress = result["controls"]
    assert progress["completed"] == 12 and progress["pending"] == 0
    assert progress["succeeded"] == 11 and progress["failed"] == 1
    assert progress["tasks"]["succeeded"] == 47 and progress["tasks"]["failed"] == 1
