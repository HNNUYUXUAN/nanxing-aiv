"""Manifest-driven full-turn cognitive coding, isolated from the frozen 384 run."""

from collections import Counter
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import random
import time

from .fast_strong_workflow import (
    AdaptiveController, Graph, FORMAT_CLARIFICATION, MODELS, RUBRIC,
    consensus, controls, digest, parse, priority, resolve_dimension, save_json,
)

VERSION = "full-turns-v1"
SEED = 26


def model_conditions():
    """Carry forward the validated r3 output conditions, never its old samples."""
    models = [dict(m, replica_revision="t1") for m in MODELS]
    for m in models:
        if m["id"] in ("deepseek-v4.1-flash", "claude-sonnet-5"):
            m.update(output_contract="cognitive-seven-fields-v1",
                     system_suffix=FORMAT_CLARIFICATION, params={"temperature": 0})
        if m["id"] == "deepseek-v4.1-flash":
            m["output_limit"] = 8192
    return models


def make_full_config(base, plan):
    """One shared 8-key scheduler. Provider quotas remain enforced by the API."""
    if Counter(c.provider for c in base.credentials) != {"next": 4, "tokendance": 4}:
        raise ValueError("Expected the eight configured platform keys")
    accounts = {}
    credentials = []
    for c in base.credentials:
        group = c.alias + "_account"
        source = base.accounts[c.group]
        accounts[group] = replace(source, name=group, concurrency=4,
                                  call_limit=10_000_000, token_limit=10**12)
        credentials.append(replace(c, group=group,
                                   models=tuple(m["id"] for m in plan["models"]
                                                if m["provider"] == c.provider)))
    return replace(base, credentials=credentials, accounts=accounts,
                   concurrency=24, max_attempts=2, timeout=90)


def read_turns(path):
    """Load only actual student turns and retain exact source coordinates."""
    records = []
    ids = set()
    with Path(path).open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            source = json.loads(line)
            if source.get("model_eligible") is not True:
                continue
            turn_id = source["turn_id"]
            text = source["text"]
            if not isinstance(turn_id, str) or not turn_id or turn_id in ids:
                raise ValueError(f"Duplicate or empty turn ID on line {line_number}")
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"Empty student text on line {line_number}")
            if source["term"] not in ("25f", "26s"):
                raise ValueError(f"Unexpected term on line {line_number}")
            if not source.get("student_id") or not isinstance(source.get("source"), dict):
                raise ValueError(f"Missing student/source on line {line_number}")
            ids.add(turn_id)
            records.append({"id": turn_id, "term": source["term"],
                            "student": source["student_id"], "question": text,
                            "partition": "restored_turn", "source": source["source"],
                            "session_candidate_id": source.get("session_candidate_id"),
                            "turn_index": source.get("turn_index"),
                            "record_timestamp": source.get("record_timestamp"),
                            "turn_timestamp": source.get("turn_timestamp"),
                            "parse_status": source.get("parse_status")})
    if not records:
        raise ValueError("No student turns")
    return records


def stratified_audit(records, seed=SEED):
    """Fixed term-stratified 10% audit, with an overall minimum of 40."""
    total = len(records)
    target = min(total, max(40, math.ceil(total * .10)))
    terms = sorted({r["term"] for r in records})
    sizes = {term: sum(r["term"] == term for r in records) for term in terms}
    quotas = {term: min(sizes[term], math.floor(target * sizes[term] / total))
              for term in terms}
    remainder = target - sum(quotas.values())
    order = sorted(terms, key=lambda t: (-(target * sizes[t] / total - quotas[t]), t))
    for term in order:
        if remainder and quotas[term] < sizes[term]:
            quotas[term] += 1
            remainder -= 1
    if remainder:
        raise AssertionError("Unable to allocate stratified audit")
    selected = []
    for term in terms:
        frame = sorted((r["id"] for r in records if r["term"] == term))
        selected.extend(random.Random(f"{seed}|{VERSION}|audit|{term}").sample(frame, quotas[term]))
    return sorted(selected), {t: {"frame": sizes[t], "selected": quotas[t],
                                 "inclusion_probability": quotas[t] / sizes[t]}
                              for t in terms}


def repetition_sample(records, seed=SEED):
    return [r["id"] for r in sorted(records, key=lambda r: digest([seed, VERSION, "repeat", r["id"]]))[:min(40, len(records))]]


class ThroughputController(AdaptiveController):
    """Keep transient per-task format failures bounded without halting the model."""

    window_seconds = 120
    recovery_cooldown = 600
    probe_step = 4

    def __init__(self, pool, directory):
        super().__init__(pool, directory)
        self.healthy_windows = 0
        self.blocked_until = 0
        self.probe_from = None
        checkpoint = self.directory / "concurrency.json"
        if checkpoint.exists():
            try:
                saved = json.loads(checkpoint.read_text("utf-8"))
                if saved.get("experiment", pool.experiment) == pool.experiment:
                    cap = saved["limit"]
                    if type(cap) is int and 16 <= cap <= 32:
                        pool.config.concurrency = cap
                        self.events = saved.get("events", [])
                        self.blocked_until = saved.get("blocked_until", 0)
                        # An interrupted probe has no completed comparison window.
                        # Resume at its previous, established cap instead.
                        if saved.get("probing") and saved.get("probe_from") in (16, 20, 24, 28):
                            pool.config.concurrency = saved["probe_from"]
                        self.events.append({"at": self.start, "event": "resumed_limit",
                                            "new_limit": pool.config.concurrency})
            except (OSError, ValueError, KeyError, TypeError):
                # A torn monitor checkpoint is not authority to raise concurrency.
                pool.config.concurrency = min(pool.config.concurrency, 16)

    def evaluate_window(self, now, metric, capacity):
        cap = self.pool.config.concurrency
        healthy = metric["rate_limit_rate"] <= .02 and metric["timeout_rate"] <= .02
        if self.probing:
            keep = (healthy and metric["goodput"] >= 1.05 * self.baseline["goodput"]
                    and metric["p95"] <= 1.5 * self.baseline["p95"])
            if not keep:
                self.pool.config.concurrency = self.probe_from
                self.blocked_until = now + self.recovery_cooldown
            self.events.append({"at": now, "event": "probe_kept" if keep else "probe_reverted",
                                "tested_limit": cap, "new_limit": self.pool.config.concurrency,
                                "metrics": metric, "baseline": self.baseline})
            self.probing = False
            self.probe_from = None
            self.healthy_windows = 0
        elif not healthy:
            self.pool.config.concurrency = max(16, cap - self.probe_step)
            self.blocked_until = now + self.recovery_cooldown
            self.healthy_windows = 0
            self.events.append({"at": now, "event": "unhealthy_window", "metrics": metric,
                                "new_limit": self.pool.config.concurrency,
                                "blocked_until": self.blocked_until})
        else:
            self.healthy_windows += 1
            if (self.healthy_windows >= 2 and now >= self.blocked_until
                    and cap < 32 and capacity >= cap + self.probe_step):
                self.baseline = metric
                self.probe_from = cap
                self.pool.config.concurrency = min(32, cap + self.probe_step)
                self.probing = True
                self.healthy_windows = 0
                self.events.append({"at": now, "event": "probe_started",
                                    "new_limit": self.pool.config.concurrency,
                                    "available_capacity": capacity, "baseline": metric})
        self.start = now

    def tick(self):
        now = time.time()
        if now - self.last_tick < 2:
            return
        self.last_tick = now
        with self.pool.db() as db:
            rows = [dict(r) for r in db.execute(
                "SELECT status,error_kind,latency FROM attempts "
                "WHERE experiment=? AND status!='running' AND finished>? AND finished<=? ORDER BY id",
                (self.pool.experiment, self.start, now))]
            pending = Counter(r[1] for r in db.execute(
                "SELECT id,json_extract(payload,'$.provider') FROM tasks "
                "WHERE status='queued' AND json_extract(payload,'$.experiment')=?",
                (self.pool.experiment,))
                if self.pool.allowed_task_ids is None or r[0] in self.pool.allowed_task_ids)
            states = {r['name']: r for r in db.execute('SELECT * FROM accounts')}
            keys = {r['alias']: r['disabled'] for r in db.execute('SELECT * FROM credentials')}
        enabled = {c.group for c in self.pool.credentials.values()
                   if not keys[c.alias] and not states[c.group]['disabled']
                   and states[c.group]['cooldown'] <= now
                   and (self.pool.config.accounts[c.group].expires_at is None
                        or self.pool.config.accounts[c.group].expires_at > now)}
        capacities = Counter()
        for name in enabled:
            account = self.pool.config.accounts[name]
            capacities[account.provider] += account.concurrency
        capacity = sum(min(pending[p], n) for p, n in capacities.items())
        elapsed = now - self.start
        if elapsed >= self.window_seconds and len(rows) >= 50:
            self.evaluate_window(now, self.metrics(rows, elapsed), capacity)
        # Invalid responses are rejected by the parser and retried at most twice.
        # Keep processing other records while retaining every failure in the ledger.
        save_json(self.directory / "concurrency.json",
                  {"experiment": self.pool.experiment,
                   "limit": self.pool.config.concurrency, "paused_models": [],
                   "blocked_until": self.blocked_until, "probing": self.probing,
                   "probe_from": self.probe_from, "healthy_windows": self.healthy_windows,
                   "events": self.events}, strict=False)


class TurnGraph(Graph):
    """Route every fast-disagreement, uncertainty or failure to both reviewers."""

    def __init__(self, pool, records, plan, directory, canary=False):
        super().__init__(pool, records, plan, directory, canary=canary)
        self.controller = ThroughputController(pool, directory)
        self.last_refresh = 0

    def refresh(self, force=False):
        if not force and time.monotonic() - self.last_refresh < 2:
            return
        self.last_refresh = time.monotonic()
        if not self.initialized:
            for record in self.records:
                for model in self.fast:
                    self.enqueue(record, "independent", model)
                if record["id"] in self.selected:
                    for model in self.review:
                        self.enqueue(record, "review", model)
            self.initialized = True
        states = self.states()
        fast = {r["id"]: self.entries(r, "independent", self.fast, states)
                for r in self.records}
        if not self.canary:
            for record in self.records:
                rid = record["id"]
                if rid not in self.selected and self.terminal(fast[rid], len(self.fast)):
                    high_risk = priority(fast[rid], len(self.fast)) is not None
                    parse_risk = record.get("parse_status") not in ("structural_alternating_unverified",)
                    if high_risk or parse_risk:
                        self.selected.add(rid)
                        self.priority_ids.add(rid)
                        for model in self.review:
                            self.enqueue(record, "review", model)
                if rid in self.plan["repeat_ids"] and self.terminal(fast[rid], len(self.fast)):
                    for model in self.fast:
                        self.enqueue(record, "repeat", model)
            states = self.states()
        output = []
        for record in self.records:
            rid = record["id"]
            basic = fast[rid]
            review = self.entries(record, "review", self.review, states)
            repeat = self.entries(record, "repeat", self.fast, states)
            selected = rid in self.selected
            dimensions = {}
            for name, field in (("task", "level"), ("contribution", "contribution_level")):
                base = consensus(basic, field, len(self.fast))
                reviewed = consensus(review, field, len(self.review))
                dimensions[name] = {"basic": base, "review": reviewed if selected else None,
                                    "final": resolve_dimension(base, reviewed, selected)}
            complete = (self.terminal(basic, len(self.fast))
                        and (not selected or self.terminal(review, len(self.review)))
                        and (self.canary or rid not in self.plan["repeat_ids"]
                             or self.terminal(repeat, len(self.fast))))
            final = dimensions["task"]["final"]
            output.append({"record": record, "independent": basic, "review": review,
                           "repeat": repeat, "peer": [], "arbitration": None,
                           "dimensions": dimensions, "final": final["label"],
                           "final_source": final["source"] + "_" + final["status"],
                           "complete": complete, "review_selected": selected,
                           "review_selection": ("control" if record["term"] == "synthetic"
                                                else "random_audit" if rid in self.plan["audit_ids"]
                                                else "high_risk" if rid in self.priority_ids else None),
                           "truth_status": "model_candidate_not_human_gold"})
        self.output = output
        self.pool.allowed_task_ids = set(self.tasks.values())
        self.pool.priority_task_ids = {task for (_, stage, _), task in self.tasks.items()
                                       if stage == "repeat"}
        self.controller.tick()
        if force or time.monotonic() - self.saved >= 20:
            prefix = "canary-" if self.canary else ""
            save_json(self.directory / (prefix + "annotations.json"), output, strict=False)
            save_json(self.directory / (prefix + "summary.json"), self.summary(), strict=False)
            self.saved = time.monotonic()

    def summary(self):
        value = super().summary()
        value["review_high_risk_selected"] = len(self.priority_ids)
        value["review_random_selected"] = sum(r["review_selection"] == "random_audit"
                                               for r in self.output)
        value["schema_version"] = 4
        value["version"] = VERSION
        return value

    def run(self):
        # A final fast result may route a review after the pool has just emptied.
        # Continue until that newly enqueued review has also settled.
        for _ in range(4):
            before = (len(self.tasks), sum(r["complete"] for r in self.output))
            self.pool.run(max_idle_seconds=20, on_progress=self.refresh,
                          stop_requested=self.stopped, worker_limit=32)
            self.refresh(force=True)
            if self.stopped() or all(r["complete"] for r in self.output):
                break
            after = (len(self.tasks), sum(r["complete"] for r in self.output))
            if after == before:
                break
        return self.summary()
