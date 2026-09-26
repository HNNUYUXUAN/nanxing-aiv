"""Bounded four-model product runs, isolated from the research task ledger.

Only the worker reads credentials. Plans and snapshots contain source provenance,
never credentials; pauses settle in-flight calls and retain the same task IDs.
"""

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import csv
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid

from dotenv import dotenv_values

from .fast_strong_workflow import RUBRIC, canary_gate, controls, digest, validated_transport
from .full_turn_workflow import TurnGraph, model_conditions, repetition_sample, stratified_audit
from .pool import Pool, canonical
from .pool_config import load_config

VERSION = "product-four-model-v1"
PROVIDERS = {"next": 2, "tokendance": 2}
HEALTH_REASONS = {"authentication", "exhausted", "reservation_exceeded"}
ACTIVE = {"running", "pausing"}


class RunnerBusy(ValueError):
    pass


def _read(path, default=None):
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except FileNotFoundError:
        return default


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", "utf-8")
    os.replace(temporary, path)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@contextmanager
def _lock(path, *, blocking=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        # Windows denies reads of a byte locked by another handle. Inspect length,
        # not byte content, before trying the lock itself.
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except OSError:
            raise RunnerBusy("已有任务正在运行，请先暂停或等待完成") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _locked(path):
    try:
        with _lock(path):
            return False
    except RunnerBusy:
        return True


def _alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)) and code.value == 259)
        finally:
            kernel.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _state(directory, state=None, reason=None, **extra):
    directory = Path(directory)
    with _lock(directory / "state.lock", blocking=True):
        value = _read(directory / "state.json", {"state": "ready", "reason": None, "log": []})
        changed = state is not None and (state != value.get("state") or reason != value.get("reason"))
        if state is not None:
            value.update(state=state, reason=reason)
        value.update(extra, updated_at=time.time())
        if changed:
            value.setdefault("log", []).append({"t": value["updated_at"], "msg": state + (": " + reason if reason else "")})
        value["log"] = value.get("log", [])[-100:]
        _write(directory / "state.json", value)
        return value


def _paths(directory, settings):
    values = {name: str(Path(settings[name]).resolve()) for name in
              ("credentials_file", "research_root", "runtime_root", "code_root")}
    directory = Path(directory).resolve()
    root = Path(values["runtime_root"])
    if directory.parent != root / "jobs" or not directory.name:
        raise ValueError("任务目录必须位于产品 runtime/jobs")
    if root == Path(values["research_root"]) / "runtime":
        raise ValueError("产品账本不能使用研究 runtime")
    return directory, values


def _budgets(records, models):
    """Reserve every planned attempt using Pool.enqueue's exact text bound."""
    limits = {provider: {"calls": 0, "tokens": 0} for provider in PROVIDERS}
    repeat_ids = set(repetition_sample([r for r in records if r["term"] != "synthetic"]))
    for record in records:
        for model in models:
            copies = 2 if record["id"] in repeat_ids and model["role"] == "fast" else 1
            messages = [{"role": "system", "content": RUBRIC + model.get("system_suffix", "")},
                        {"role": "user", "content": canonical({"student_text": record["question"]})}]
            reservation = len(canonical(messages).encode("utf-8")) + 1024 + model["output_limit"]
            if reservation > 2_000_000:
                raise ValueError("单条文本超过模型任务预留上限")
            limits[model["provider"]]["calls"] += copies * 2
            limits[model["provider"]]["tokens"] += copies * 2 * reservation
    return limits


def create_plan(directory, records, *, name, source_digest):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    with _lock(directory / "plan.lock"):
        records = json.loads(json.dumps(records, ensure_ascii=False))
        if not records:
            raise ValueError("真实体验批次须包含学生回合")
        if len({record.get("id") for record in records}) != len(records):
            raise ValueError("回合 ID 重复")
        if source_digest != digest(records):
            raise ValueError("来源摘要与规范回合不一致")
        for record in records:
            source = record.get("source") or {}
            if (not isinstance(record.get("id"), str) or not record["id"] or
                    record["id"] in {r["id"] for r in controls()} or not record.get("student") or
                    not isinstance(record.get("term"), str) or not record["term"].strip() or
                    record["term"].strip().casefold() == "synthetic" or
                    not isinstance(record.get("question"), str) or not record["question"].strip() or
                    not isinstance(source, dict) or not source.get("file") or len(source.get("sha256", "")) != 64):
                raise ValueError("回合缺少有效学生、学期、文本或来源凭证")
        if (directory / "plan.json").exists():
            plan, sample = _validate_plan(directory)
            if plan["records_sha256"] != digest(records) or plan["source_digest"] != source_digest:
                raise ValueError("任务计划已存在且来源不同")
            return plan
        sample = records + controls()
        audit, strata = stratified_audit(records)
        models = model_conditions()
        plan = {"schema_version": 1, "version": VERSION, "revision": "t1",
                "experiment": "product-" + directory.name, "name": str(name)[:120],
                "created_at": time.time(), "seed": 26, "duration_seconds": 7200,
                "stop_claiming_at": None, "real": len(records), "controls": 12,
                "models": models, "rubric": RUBRIC, "rubric_sha256": digest(RUBRIC),
                "sample_sha256": digest(sample), "records_sha256": digest(records),
                "source_digest": source_digest, "audit_ids": audit, "audit_strata": strata,
                "repeat_ids": repetition_sample(records), "engineering_ids": [],
                "review_policy": "term_stratified_random_10pct_min40_plus_all_risks",
                "maximum_concurrency": 4, "provider_concurrency": PROVIDERS,
                "max_attempts_per_task_condition": 2, "timeout_seconds": 90,
                "limits": _budgets(sample, models)}
        _write(directory / "sample.json", sample)
        _write(directory / "plan.json", plan)
        _write(directory / "integrity.json", {"plan_sha256": _sha(directory / "plan.json"),
                                               "sample_sha256": _sha(directory / "sample.json")})
        _write(directory / "annotations.json", [])
        _write(directory / "summary.json", {})
        _state(directory, "ready", limits=plan["limits"])
        return plan


def _validate_plan(directory):
    directory = Path(directory)
    integrity = _read(directory / "integrity.json", {})
    for name in ("plan", "sample"):
        if _sha(directory / (name + ".json")) != integrity.get(name + "_sha256"):
            raise ValueError("计划或样本哈希校验失败")
    plan, sample = _read(directory / "plan.json"), _read(directory / "sample.json")
    real = [r for r in sample if r.get("term") != "synthetic"]
    if (plan.get("version") != VERSION or digest(sample) != plan["sample_sha256"] or
            digest(real) != plan["records_sha256"] or digest(real) != plan["source_digest"] or
            digest(plan["rubric"]) != plan["rubric_sha256"] or
            plan["limits"] != _budgets(sample, plan["models"])):
        raise ValueError("批次来源或预算校验失败")
    return plan, sample


def _verify_sources(sample, settings):
    checked = {}
    parsed = {}
    for record in sample:
        if record["term"] == "synthetic":
            continue
        source = record["source"]
        if source.get("origin", "research") not in {"product", "research"}:
            raise ValueError("来源根目录无效")
        root = Path(settings["runtime_root" if source.get("origin") == "product" else "research_root"]).resolve()
        candidate = Path(source["file"])
        path = (root / candidate).resolve()
        if candidate.is_absolute() or not path.is_relative_to(root) or not path.is_file():
            raise ValueError("来源文件不存在或超出指定根目录")
        if str(path) not in checked:
            checked[str(path)] = _sha(path)
        if checked[str(path)] != source["sha256"]:
            raise ValueError("来源文件哈希变化")
        start, end = source.get("char_start"), source.get("char_end")
        if isinstance(start, int) and isinstance(end, int):
            row_number = source.get("row")
            if not isinstance(row_number, int):
                raise ValueError("来源行号缺失")
            if path.suffix.lower() == ".csv":
                if path not in parsed:
                    with path.open(encoding="utf-8-sig", newline="") as stream:
                        parsed[path] = list(csv.DictReader(stream))
                rows = parsed[path]
                index = row_number - 2
                row = rows[index] if 0 <= index < len(rows) else {}
            elif path.suffix.lower() == ".jsonl":
                if path not in parsed:
                    parsed[path] = path.read_text("utf-8-sig").splitlines()
                lines = parsed[path]
                row = json.loads(lines[row_number - 1]) if 1 <= row_number <= len(lines) else {}
            else:
                raise ValueError("不支持校验该来源格式")
            raw = row.get(source.get("field", "问答记录"), "")
            if not isinstance(raw, str) or not 0 <= start <= end <= len(raw) or raw[start:end] != record["question"]:
                raise ValueError("来源字符位置与学生文本不一致")
            if source.get("field_sha256") and hashlib.sha256(raw.encode()).hexdigest() != source["field_sha256"]:
                raise ValueError("来源字段摘要变化")


def _research_idle(settings):
    # Inspect only already-existing locks; never create files in the research checkout.
    for path in Path(settings["research_root"]).glob("runtime/research/full-turns-v1-*/t*/runner.lock"):
        with path.open("rb") as stream:
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except OSError:
                raise RunnerBusy("研究批次仍在使用模型账户") from None


def status(directory):
    directory = Path(directory).resolve()
    value = _read(directory / "state.json", {"state": "ready", "reason": None, "log": []})
    if value["state"] in ACTIVE:
        runtime = directory.parent.parent
        owner = _read(runtime / "owner.json", {})
        owns = owner.get("directory") == str(directory) and owner.get("token") == value.get("worker_token")
        launching = owns and _alive(owner.get("pid")) and time.time() - owner.get("launched_at", 0) < 30
        if not owns or not (_locked(runtime / "runner.lock") or launching):
            value = _state(directory, "paused", "worker_interrupted")
    return value


def start(directory, settings):
    directory, settings = _paths(directory, settings)
    runtime = Path(settings["runtime_root"])
    with _lock(runtime / "control.lock", blocking=True):
        current = status(directory)
        if current["state"] in ACTIVE or current["state"] == "finished":
            return current
        plan, sample = _validate_plan(directory)
        _verify_sources(sample, settings)
        _research_idle(settings)
        if (directory / "frozen").exists():
            return _state(directory, "finished")
        owner = _read(runtime / "owner.json", {})
        owner_state = _read(Path(owner["directory"]) / "state.json", {}) if owner.get("directory") else {}
        if (owner_state.get("state") in ACTIVE and _alive(owner.get("pid")) and time.time() - owner.get("launched_at", 0) < 30):
            raise RunnerBusy("另一批任务正在启动")
        with _lock(runtime / "runner.lock"):
            token = uuid.uuid4().hex
            _write(directory / "settings.json", settings)
            pause_path = directory / "PAUSE"
            if pause_path.exists():
                pause_path.unlink()
            if not (directory / "execution.json").exists():
                now = time.time()
                _write(directory / "execution.json", {"started_at": now, "stop_claiming_at": now + plan["duration_seconds"]})
            execution = _read(directory / "execution.json")
            if execution["stop_claiming_at"] <= time.time():
                return _state(directory, "blocked", "deadline_reached")
            _state(directory, "running", worker_token=token)
            command = [sys.executable, str(Path(settings["code_root"]) / "scripts/run_product.py"),
                       "--directory", str(directory), "--token", token]
            try:
                process = subprocess.Popen(command, cwd=settings["code_root"], stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    start_new_session=os.name != "nt")
            except OSError:
                return _state(directory, "failed", "worker_start_failed")
            _write(runtime / "owner.json", {"directory": str(directory), "pid": process.pid,
                                              "token": token, "launched_at": time.time()})
            return _state(directory, "running", worker_token=token, worker_pid=process.pid,
                          stop_claiming_at=execution["stop_claiming_at"])


def pause(directory):
    directory = Path(directory).resolve()
    with _lock(directory.parent.parent / "control.lock", blocking=True):
        current = status(directory)
        if current["state"] not in ACTIVE:
            return current
        (directory / "PAUSE").touch()
        return _state(directory, "pausing", "settling_inflight")


def _config(settings, plan):
    credential_path = Path(settings["credentials_file"])
    defaults_path = Path(settings["code_root"]) / "config/pool-defaults.json"
    values = {**(_read(defaults_path, {}) or {}), **dotenv_values(credential_path), **os.environ}
    price_path = Path(values.get("POOL_PRICE_FILE") or "config/pool-prices.json")
    values["POOL_PRICE_FILE"] = str(price_path if price_path.is_absolute() else Path(settings["code_root"]) / price_path)
    base = load_config(env=values)
    credentials, accounts = [], {}
    for credential in base.credentials:
        if credential.provider not in PROVIDERS:
            continue
        group = credential.alias + "_account"
        account = base.accounts[credential.group]
        credentials.append(replace(credential, group=group,
            models=tuple(m["id"] for m in plan["models"] if m["provider"] == credential.provider)))
        # Product experiment budgets replace historical preflight counters.
        accounts[group] = replace(account, name=group, concurrency=2,
                                   call_limit=10**12, token_limit=10**18)
    if set(c.provider for c in credentials) != set(PROVIDERS):
        raise ValueError("provider_credentials_missing")
    return replace(base, credentials=credentials, accounts=accounts,
                   db=str(Path(settings["runtime_root"]) / "pool.sqlite3"),
                   concurrency=4, max_attempts=2, timeout=90)


def _health(pool, settings):
    """Carry only auditable disabled states, never research tasks or usage rows."""
    source = Path(settings["research_root"]) / "runtime/pool.sqlite3"
    source_states, source_keys = {}, {}
    snapshot_path = Path(settings["runtime_root"]) / "health-snapshot.json"
    snapshot_keys = {}
    if snapshot_path.is_file():
        snapshot = _read(snapshot_path)
        payload = snapshot.get("payload", {})
        if payload.get("schema_version") != 1 or digest(payload) != snapshot.get("sha256"):
            raise ValueError("health_snapshot_integrity_failed")
        snapshot_keys = {row["alias"]: row for row in payload["entries"]}
        if len(snapshot_keys) != len(payload["entries"]):
            raise ValueError("health_snapshot_integrity_failed")
        for credential in pool.credentials.values():
            row = snapshot_keys.get(credential.alias)
            if (row is None or row.get("fingerprint") != hashlib.sha256(credential.key.encode()).hexdigest() or
                    row.get("account") != credential.group or row.get("provider") != credential.provider):
                raise ValueError("health_snapshot_identity_mismatch")
            source_states[credential.group] = (row.get("account_disabled"), row.get("cooldown", 0))
            source_keys[credential.alias] = row.get("credential_disabled")
        source = snapshot_path
    elif source.is_file() and source.resolve() != Path(pool.config.db).resolve():
        with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "accounts" in tables:
                source_states = {r[0]: (r[1], r[2]) for r in db.execute("SELECT name,disabled,cooldown FROM accounts")}
            if "credentials" in tables:
                source_keys = dict(db.execute("SELECT alias,disabled FROM credentials"))
    receipts = []
    with pool.transaction() as db:
        db.execute("CREATE TABLE IF NOT EXISTS product_key_identity(alias TEXT PRIMARY KEY,fingerprint TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS product_health_import(alias TEXT PRIMARY KEY,source TEXT,reason TEXT,imported REAL)")
        for credential in pool.credentials.values():
            fingerprint = hashlib.sha256(credential.key.encode()).hexdigest()
            prior = db.execute("SELECT fingerprint FROM product_key_identity WHERE alias=?", (credential.alias,)).fetchone()
            if prior and prior[0] != fingerprint:
                # A rotation needs explicit reconciliation; never silently revive an exhausted alias.
                raise ValueError("credential_rotation_requires_reconciliation")
            db.execute("INSERT OR IGNORE INTO product_key_identity VALUES (?,?)", (credential.alias, fingerprint))
            imported = db.execute("SELECT 1 FROM product_health_import WHERE alias=?", (credential.alias,)).fetchone()
            if imported:
                continue
            disabled, cooldown = source_states.get(credential.group, (None, 0))
            key_disabled = source_keys.get(credential.alias)
            if disabled in HEALTH_REASONS:
                db.execute("UPDATE accounts SET disabled=? WHERE name=?", (disabled, credential.group))
            if key_disabled in HEALTH_REASONS:
                db.execute("UPDATE credentials SET disabled=? WHERE alias=?", (key_disabled, credential.alias))
            if isinstance(cooldown, (int, float)) and cooldown > time.time():
                db.execute("UPDATE accounts SET cooldown=MAX(cooldown,?) WHERE name=?", (cooldown, credential.group))
            reason = disabled if disabled in HEALTH_REASONS else key_disabled if key_disabled in HEALTH_REASONS else "no_disabled_state"
            db.execute("INSERT INTO product_health_import VALUES (?,?,?,?)", (credential.alias, str(source), reason, time.time()))
            receipts.append({"alias": credential.alias, "account": credential.group, "reason": reason})
        # With the global worker lock held, any previous running attempts are orphaned.
        for task in db.execute("SELECT id,current_attempt,attempts FROM tasks WHERE status='running'").fetchall():
            db.execute("UPDATE attempts SET status='unknown',error_kind='worker_interrupted',finished=? WHERE id=? AND status='running'",
                       (time.time(), task["current_attempt"]))
            db.execute("UPDATE tasks SET status=?,reason='recovered_unknown_attempt',lease=NULL WHERE id=?",
                       ("queued" if task["attempts"] < 2 else "failed", task["id"]))
    return receipts


def export_health_snapshot(settings, destination):
    """Export private health-only handoff data; no task history or plaintext key.

    Copy the resulting file to runtime_root/health-snapshot.json on the product
    host. Import verifies every active credential's identity before any API call.
    """
    config = _config(settings, {"models": model_conditions()})
    source = Path(settings["research_root"]) / "runtime/pool.sqlite3"
    if not source.is_file():
        raise ValueError("research_health_ledger_missing")
    with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.execute("BEGIN")
        accounts = {r[0]: (r[1], r[2]) for r in db.execute("SELECT name,disabled,cooldown FROM accounts")}
        keys = dict(db.execute("SELECT alias,disabled FROM credentials"))
    entries = []
    for credential in config.credentials:
        disabled, cooldown = accounts.get(credential.group, (None, 0))
        key_disabled = keys.get(credential.alias)
        entries.append({"alias": credential.alias, "account": credential.group, "provider": credential.provider,
            "fingerprint": hashlib.sha256(credential.key.encode()).hexdigest(),
            "account_disabled": disabled if disabled in HEALTH_REASONS else None,
            "credential_disabled": key_disabled if key_disabled in HEALTH_REASONS else None,
            "cooldown": cooldown if isinstance(cooldown, (int, float)) else 0,
            "source_account_present": credential.group in accounts,
            "source_credential_present": credential.alias in keys})
    payload = {"schema_version": 1, "kind": "research_credential_health", "created_at": time.time(), "entries": entries}
    _write(destination, {"payload": payload, "sha256": digest(payload)})
    return {"credential_count": len(entries),
            "disabled_credentials": sum(bool(row["credential_disabled"] or row["account_disabled"]) for row in entries),
            "source_accounts_found": sum(row["source_account_present"] for row in entries),
            "sha256": _sha(destination)}


class _Controller:
    def tick(self):
        pass


class ProductGraph(TurnGraph):
    def __init__(self, pool, records, plan, directory, canary=False):
        super().__init__(pool, records, plan, directory, canary=canary)
        self.controller = _Controller()

    def stopped(self):
        return (self.directory / "PAUSE").exists() or time.time() >= self.plan["stop_claiming_at"]

    def refresh(self, force=False):
        # Product progress is refreshed by Pool completion/heartbeat callbacks.
        # Do not apply the research graph's extra two-second throttle: the first
        # heartbeat must expose in-flight control calls before any result arrives.
        super().refresh(force=True)
        if self.output:
            if self.canary:
                # A resumed canary rechecks existing tasks. Preserve previously
                # settled real rows while publishing its live control results.
                previous = _read(self.directory / "annotations.json", [])
                rows = [r for r in previous if r["record"]["term"] != "synthetic"] + self.output
            else:
                rows = self.output
            _write(self.directory / "annotations.json", rows)
            _write(self.directory / "summary.json", self.summary())
            control_rows = [r for r in rows if r["record"]["term"] == "synthetic"]
            entries = [e for r in control_rows for stage in ("independent", "review") for e in r.get(stage, [])]
            counts = {state: sum(e["status"] == state for e in entries) for state in ("queued", "running", "done", "failed")}
            failed_rows = sum(bool(r.get("complete")) and any(e["status"] == "failed" for stage in ("independent", "review")
                                  for e in r.get(stage, [])) for r in control_rows)
            completed = sum(bool(r.get("complete")) for r in control_rows)
            success = sum(bool(r.get("complete")) and all(e["status"] == "done" and e.get("judgment") is not None
                          for stage in ("independent", "review") for e in r.get(stage, [])) for r in control_rows)
            progress = {"total": self.plan["controls"], "completed": completed,
                        "pending": self.plan["controls"] - completed, "succeeded": success, "failed": failed_rows,
                        "tasks": {"total": self.plan["controls"] * len(self.plan["models"]),
                                  "queued": counts["queued"], "running": counts["running"],
                                  "succeeded": counts["done"], "failed": counts["failed"]}}
            _state(self.directory, phase="controls" if self.canary else "real_records", controls=progress,
                   pending_reasons=self.pool.status()["pending_reasons"])


def _freeze(directory, plan):
    final = directory / "frozen"
    if final.exists():
        return
    staging = directory / ("frozen-" + uuid.uuid4().hex + ".tmp")
    staging.mkdir()
    rows = _read(directory / "annotations.json", [])
    real = [r for r in rows if r["record"]["term"] != "synthetic"]
    for name in ("annotations.json", "summary.json"):
        shutil.copyfile(directory / name, staging / name)
    _write(staging / "snapshot-manifest.json", {"schema_version": 1, "experiment": plan["experiment"],
        "status": "completed", "frozen_at": time.time(), "sample_sha256": plan["sample_sha256"],
        "real_planned": plan["real"], "real_completed": sum(r["complete"] for r in real),
        "real_candidate_labeled": sum(r["complete"] and r["final"] is not None for r in real),
        "files": {name: _sha(staging / name) for name in ("annotations.json", "summary.json")}})
    staging.rename(final)


def _blocked_reason(pool):
    reasons = [str(r["reason"]) for r in pool.status()["pending_reasons"] if r.get("reason")]
    # These are scheduler categories, not provider response text.
    return "; ".join(sorted(set(reasons)))[:400] or "tasks_incomplete"


def run(directory, settings, *, token=None, config=None, transport=None):
    """Worker entry; injected config/transport support local, unpaid verification."""
    directory, settings = _paths(directory, settings)
    runtime = Path(settings["runtime_root"])
    try:
        with _lock(runtime / "runner.lock", blocking=token is not None), _lock(directory / "runner.lock"):
            if token is not None:
                owner = _read(runtime / "owner.json", {})
                if owner.get("token") != token or owner.get("directory") != str(directory):
                    raise RunnerBusy("worker_identity_mismatch")
            else:
                token = uuid.uuid4().hex
                _write(runtime / "owner.json", {"directory": str(directory), "pid": os.getpid(),
                                                  "token": token, "launched_at": time.time()})
            plan, sample = _validate_plan(directory)
            _verify_sources(sample, settings)
            _research_idle(settings)
            if (directory / "frozen").exists():
                return _state(directory, "finished")
            if not (directory / "execution.json").exists():
                now = time.time()
                _write(directory / "execution.json", {"started_at": now, "stop_claiming_at": now + plan["duration_seconds"]})
            plan = {**plan, **_read(directory / "execution.json")}
            if time.time() >= plan["stop_claiming_at"]:
                return _state(directory, "blocked", "deadline_reached")
            pool = Pool(config or _config(settings, plan), transport or validated_transport,
                        experiment=plan["experiment"], limits=plan["limits"], capture_outputs=True,
                        provider_limits=PROVIDERS)
            _write(directory / "health-import.json", _health(pool, settings))
            _state(directory, "running", worker_pid=os.getpid(), worker_token=token,
                   stop_claiming_at=plan["stop_claiming_at"])
            canary = ProductGraph(pool, [r for r in sample if r["term"] == "synthetic"], plan, directory, True)
            canary.run()
            if (directory / "PAUSE").exists():
                return _state(directory, "paused", "user_paused", usage=pool.status())
            if not all(r["complete"] for r in canary.output):
                return _state(directory, "blocked", "control_incomplete: " + _blocked_reason(pool), usage=pool.status())
            gate = canary_gate(canary.output, plan["models"])
            _write(directory / "canary-gate.json", gate)
            if not gate["passed"]:
                return _state(directory, "blocked", "control_quality_failed", usage=pool.status())
            graph = ProductGraph(pool, sample, plan, directory)
            summary = graph.run()
            if (directory / "PAUSE").exists():
                return _state(directory, "paused", "user_paused", usage=pool.status())
            if summary["completed_records"] == len(sample):
                _freeze(directory, plan)
                return _state(directory, "finished", usage=pool.status())
            reason = "deadline_reached" if time.time() >= plan["stop_claiming_at"] else _blocked_reason(pool)
            return _state(directory, "blocked", reason, usage=pool.status())
    except RunnerBusy:
        raise
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as error:
        # Never serialize exception text: it may originate from configuration or an API.
        reason = str(error) if str(error) in {
            "provider_credentials_missing", "credential_rotation_requires_reconciliation",
            "health_snapshot_identity_mismatch", "health_snapshot_integrity_failed"
        } else "validation_or_configuration_failed"
        return _state(directory, "failed", reason)
