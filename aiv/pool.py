"""SQLite-backed, bounded API scheduling. Keys never enter the database.

Unknown charged usage retains its reservation, including timeouts and lost workers.
Recovery permits another attempt, not a claim of exactly-once provider billing.
"""

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
import hashlib
import json
import random
import sqlite3
import time
from pathlib import Path

from .pool_config import Config


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def retry_after(value, now=None):
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (ValueError, TypeError):
        try:
            return max(
                0.0,
                parsedate_to_datetime(value).timestamp()
                - (time.time() if now is None else now),
            )
        except (ValueError, TypeError, OverflowError):
            return None


class CallError(Exception):
    def __init__(self, kind="temporary", delay=None, status=None):
        self.kind, self.delay, self.status = kind, delay, status
        super().__init__(kind)


def openai_transport(credential, task, timeout):
    from openai import OpenAI

    try:
        with OpenAI(
            api_key=credential.key,
            base_url=credential.base_url,
            timeout=timeout,
            max_retries=0,
        ) as client:
            params = dict(task["params"])
            if task["protocol"] == "chat":
                response = client.chat.completions.create(
                    model=task["model"],
                    messages=task["messages"],
                    max_tokens=task["output_limit"],
                    **params,
                )
                text = response.choices[0].message.content
                if response.choices[0].finish_reason != "stop" or not text:
                    # Usage is still returned for incomplete output; scheduler records cost.
                    valid = False
                else:
                    valid = True
                usage = response.usage
                inp = getattr(usage, "prompt_tokens", None)
                out = getattr(usage, "completion_tokens", None)
                details = getattr(usage, "prompt_tokens_details", None)
                finish_reason = response.choices[0].finish_reason
            else:
                response = client.responses.create(
                    model=task["model"],
                    input=task["messages"],
                    max_output_tokens=task["output_limit"],
                    **params,
                )
                text = response.output_text
                valid = response.status == "completed" and bool(text)
                usage = response.usage
                inp = getattr(usage, "input_tokens", None)
                out = getattr(usage, "output_tokens", None)
                details = getattr(usage, "input_tokens_details", None)
                finish_reason = response.status
            return {
                "text": text or "",
                "input_tokens": inp,
                "output_tokens": out,
                "cached_tokens": getattr(details, "cached_tokens", None),
                "valid": valid,
                "finish_reason": finish_reason,
            }
    except Exception as error:
        status = getattr(error, "status_code", None)
        body = getattr(error, "body", None)
        code = body.get("code", "") if isinstance(body, dict) else ""
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            code = body["error"].get("code", code)
        # Only known categorical signals are retained; response text can contain keys.
        if status in (401, 403):
            kind = "authentication"
        elif status == 402 or code in (
            "insufficient_quota",
            "quota_exceeded",
            "insufficient_balance",
        ):
            kind = "exhausted"
        elif status == 429:
            kind = "rate_limit"
        elif status == 408 or 'timeout' in type(error).__name__.lower():
            kind = "timeout"
        elif status in (409, 500, 502, 503, 504, 524) or status is None:
            kind = "temporary"
        else:
            kind = "invalid_request"
        headers = getattr(getattr(error, "response", None), "headers", {})
        raise CallError(kind, retry_after(headers.get("retry-after")), status) from None


class Pool:
    def __init__(self, config: Config, transport=openai_transport, clock=time.time,
                 experiment=None, limits=None, capture_outputs=False):
        self.config, self.transport, self.clock = config, transport, clock
        self.experiment = experiment
        self.capture_outputs = capture_outputs
        self.paused_models = set()
        self.allowed_task_ids = None
        # Optional, bounded fairness lane; selection never bypasses any quota.
        self.priority_task_ids = set()
        self._claims_since_priority = 3
        self.credentials = {c.alias: c for c in config.credentials}
        Path(config.db).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts(name TEXT PRIMARY KEY, disabled TEXT, cooldown REAL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS credentials(alias TEXT PRIMARY KEY, disabled TEXT);
                CREATE TABLE IF NOT EXISTS tasks(
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL,
                    attempts INTEGER DEFAULT 0, available REAL DEFAULT 0, lease REAL,
                    current_attempt INTEGER, result TEXT, reason TEXT, cache_hit INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS attempts(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, alias TEXT, account TEXT,
                    provider TEXT, model TEXT, experiment TEXT, started REAL, finished REAL,
                    status TEXT, error_kind TEXT, http_status INTEGER,
                    reserved_tokens INTEGER, reserved_cost REAL, currency TEXT,
                    input_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER,
                    known_cost REAL, latency REAL, hold_tokens INTEGER, hold_cost REAL);
                CREATE TABLE IF NOT EXISTS cache(id TEXT PRIMARY KEY, result TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS attempt_account_time ON attempts(account, started);
                CREATE TABLE IF NOT EXISTS experiment_budgets(
                    experiment TEXT,provider TEXT,call_limit INTEGER,token_limit INTEGER,
                    PRIMARY KEY(experiment,provider));
                CREATE TABLE IF NOT EXISTS attempt_outputs(
                    attempt_id INTEGER PRIMARY KEY, response_json TEXT NOT NULL,
                    validation_code TEXT, finish_reason TEXT);
            """)
            for name in config.accounts:
                db.execute("INSERT OR IGNORE INTO accounts(name) VALUES (?)", (name,))
            for alias in self.credentials:
                db.execute(
                    "INSERT OR IGNORE INTO credentials(alias) VALUES (?)", (alias,)
                )
        if limits:
            if not experiment:
                raise ValueError('Experiment limits require a scoped pool')
            with self.transaction() as db:
                for provider, limit in limits.items():
                    calls, tokens = limit['calls'], limit['tokens']
                    if type(calls) is not int or type(tokens) is not int or min(calls, tokens) <= 0:
                        raise ValueError('Positive integer experiment limits required')
                    old = db.execute('SELECT call_limit,token_limit FROM experiment_budgets WHERE experiment=? AND provider=?', (experiment, provider)).fetchone()
                    if old and tuple(old) != (calls, tokens):
                        raise ValueError('Existing experiment budget is immutable')
                    db.execute('INSERT OR IGNORE INTO experiment_budgets VALUES (?,?,?,?)', (experiment, provider, calls, tokens))

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.config.db, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def enqueue(
        self,
        *,
        provider,
        model,
        messages,
        experiment,
        replicate,
        protocol="chat",
        params=None,
        output_limit=512,
        input_limit=None,
        credential_alias=None,
        output_contract=None,
    ):
        params = params or {}
        if not experiment or replicate is None:
            raise ValueError("Experiment version and replicate ID are required")
        if self.experiment is not None and experiment != self.experiment:
            raise ValueError('Task is outside the scoped experiment')
        if set(params) - {"temperature", "top_p", "seed", "response_format", "reasoning_effort"}:
            raise ValueError("Only reviewed sampling parameters are allowed")
        if 'response_format' in params:
            fmt=params['response_format']
            if not isinstance(fmt,dict) or fmt.get('type') not in ('json_object','json_schema'):
                raise ValueError('Unsupported structured response format')
            if fmt['type']=='json_schema' and (set(fmt)!={'type','json_schema'} or not isinstance(fmt['json_schema'],dict) or fmt['json_schema'].get('strict') is not True):
                raise ValueError('Strict JSON schema is required')
        if 'reasoning_effort' in params and params['reasoning_effort'] not in ('low','medium','high'):
            raise ValueError('Unsupported reasoning effort')
        if any(c.key and c.key in canonical(params) for c in self.credentials.values()):
            raise ValueError('Credential found in task parameters')
        if not messages or any(
            set(m) != {"role", "content"}
            or m["role"] not in ("system", "user", "assistant")
            or not isinstance(m["content"], str)
            for m in messages
        ):
            raise ValueError("Text-only messages with explicit roles are required")
        raw = canonical(messages)
        if any(c.key and c.key in raw for c in self.credentials.values()):
            raise ValueError("Credential found in task content")
        # Conservative text reservation, not a claimed tokenizer measurement.
        bound = len(raw.encode("utf-8")) + 1024
        input_limit = bound if input_limit is None else input_limit
        if (
            input_limit < bound
            or output_limit <= 0
            or input_limit + output_limit > 2000000
        ):
            raise ValueError("Invalid text token reservation/output limit")
        routes = sorted(
            {c.base_url for c in self.credentials.values() if c.provider == provider}
        )
        source = hashlib.sha256(canonical(routes).encode()).hexdigest()
        payload = dict(
            provider=provider,
            model=model,
            protocol=protocol,
            messages=messages,
            experiment=experiment,
            replicate=str(replicate),
            params=params,
            output_limit=int(output_limit),
            input_limit=int(input_limit),
            source=source,
        )
        if output_contract is not None:
            if output_contract!='cognitive-seven-fields-v1':raise ValueError('Unknown output contract')
            payload['output_contract']=output_contract
        # Only diagnostic probes bind an alias; ordinary task caches remain key-independent.
        if credential_alias is not None:
            if credential_alias not in self.credentials:
                raise ValueError("Unknown diagnostic credential alias")
            payload["credential_alias"] = credential_alias
        task_id = hashlib.sha256(canonical(payload).encode()).hexdigest()
        with self.transaction() as db:
            cached = db.execute(
                "SELECT result FROM cache WHERE id=?", (task_id,)
            ).fetchone()
            db.execute(
                "INSERT OR IGNORE INTO tasks(id,payload,status,result,cache_hit) VALUES (?,?,?,?,?)",
                (
                    task_id,
                    canonical(payload),
                    "done" if cached else "queued",
                    cached["result"] if cached else None,
                    int(bool(cached)),
                ),
            )
            if cached:
                db.execute("UPDATE tasks SET cache_hit=1 WHERE id=?", (task_id,))
        return task_id

    def price(self, task, account):
        for p in self.config.prices:
            if (
                p.get("verified") is True
                and p.get("source")
                and (p.get("provider"), p.get("model"), p.get("currency"))
                == (task["provider"], task["model"], account.currency)
            ):
                if min(p["input_per_million"], p["output_per_million"]) < 0:
                    raise ValueError("Negative price")
                return p
        return None

    @staticmethod
    def cost(price, inp, out):
        return (
            None
            if price is None or inp is None or out is None
            else (inp * price["input_per_million"] + out * price["output_per_million"])
            / 1000000
        )

    def recover(self, db, now):
        rows = db.execute(
            "SELECT id,current_attempt,attempts FROM tasks WHERE status='running' AND lease<? AND (? IS NULL OR json_extract(payload,'$.experiment')=?)",
            (now, self.experiment, self.experiment),
        ).fetchall()
        for row in rows:
            db.execute(
                "UPDATE attempts SET status='unknown',error_kind='lease_expired',finished=? WHERE id=? AND status='running'",
                (now, row["current_attempt"]),
            )
            db.execute(
                "UPDATE tasks SET status=?,reason='recovered_unknown_attempt',lease=NULL WHERE id=?",
                (
                    "queued"
                    if row["attempts"] < self.config.max_attempts
                    else "failed",
                    row["id"],
                ),
            )

    def claim(self, stop_requested=None):
        now = self.clock()
        with self.transaction() as db:
            # STOP can arrive while BEGIN IMMEDIATE waits for another writer.
            if stop_requested and stop_requested():
                return None
            self.recover(db, now)
            running = db.execute(
                "SELECT COUNT(*) FROM attempts WHERE status='running'"
            ).fetchone()[0]
            if running >= self.config.concurrency:
                return None
            tasks = db.execute(
                "SELECT * FROM tasks WHERE status='queued' AND available<=? AND (? IS NULL OR json_extract(payload,'$.experiment')=?) ORDER BY rowid",
                (now, self.experiment, self.experiment),
            ).fetchall()
            if self.priority_task_ids and self._claims_since_priority >= 3:
                tasks.sort(key=lambda row: row['id'] not in self.priority_task_ids)
            # A claim changes the ledger only when it returns a job. Reuse these
            # transaction-consistent aggregates while scanning blocked tasks.
            account_states = {r['name']: r for r in db.execute('SELECT * FROM accounts')}
            key_states = {r['alias']: r['disabled'] for r in db.execute('SELECT * FROM credentials')}
            account_stats, recent_stats, key_calls_cache = {}, {}, {}
            experiment_limits, experiment_usage = {}, {}

            def set_reason(row, reason):
                if row['reason'] != reason:
                    db.execute('UPDATE tasks SET reason=? WHERE id=?', (reason, row['id']))

            for row in tasks:
                if self.allowed_task_ids is not None and row['id'] not in self.allowed_task_ids:
                    continue
                task = json.loads(row["payload"])
                if task['model'] in self.paused_models:
                    set_reason(row, 'model_quality_paused')
                    continue
                candidates, reasons = [], set()
                tokens = task["input_limit"] + task["output_limit"]
                scope = (task['experiment'], task['provider'])
                if scope not in experiment_limits:
                    experiment_limits[scope] = db.execute('SELECT call_limit,token_limit FROM experiment_budgets WHERE experiment=? AND provider=?', scope).fetchone()
                limit = experiment_limits[scope]
                if limit:
                    if scope not in experiment_usage:
                        experiment_usage[scope] = db.execute('SELECT COUNT(*),COALESCE(SUM(COALESCE(input_tokens,0)+COALESCE(output_tokens,0)+COALESCE(hold_tokens,0)),0) FROM attempts WHERE experiment=? AND provider=?', scope).fetchone()
                    usage = experiment_usage[scope]
                    limit_reason = 'experiment_call_budget' if usage[0] >= limit['call_limit'] else 'experiment_token_budget' if usage[1] + tokens > limit['token_limit'] else None
                    if limit_reason:
                        set_reason(row, limit_reason)
                        continue
                for credential in self.credentials.values():
                    if (
                        task.get("credential_alias")
                        and task["credential_alias"] != credential.alias
                    ):
                        continue
                    if (credential.provider, credential.protocol) != (
                        task["provider"],
                        task["protocol"],
                    ) or task["model"] not in credential.models:
                        continue
                    account = self.config.accounts[credential.group]
                    state = account_states[account.name]
                    key_state = key_states[credential.alias]
                    reason = None
                    if key_state or state["disabled"]:
                        reason = key_state or state["disabled"]
                    elif account.expires_at is not None and account.expires_at <= now:
                        reason = "expired"
                    elif state["cooldown"] > now:
                        reason = "cooldown"
                    if reason:
                        reasons.add(reason)
                        continue
                    if account.name not in account_stats:
                        account_stats[account.name] = db.execute(
                        """SELECT COUNT(*) calls, COALESCE(SUM(hold_tokens),0) held,
                        COALESCE(SUM(COALESCE(input_tokens,0)+COALESCE(output_tokens,0)),0) used,
                        COALESCE(SUM(hold_cost),0) money_held, COALESCE(SUM(known_cost),0) money_used,
                        SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) active
                        FROM attempts WHERE account=?""",
                        (account.name,),
                        ).fetchone()
                    stats = account_stats[account.name]
                    price = self.price(task, account)
                    reserved_cost = self.cost(
                        price, task["input_limit"], task["output_limit"]
                    )
                    if account.name not in recent_stats:
                        recent_stats[account.name] = db.execute(
                        """SELECT COUNT(*),COALESCE(SUM(MAX(reserved_tokens,COALESCE(input_tokens,0)+COALESCE(output_tokens,0))),0)
                        FROM attempts WHERE account=? AND started>?""",
                        (account.name, now - 60),
                        ).fetchone()
                    recent = recent_stats[account.name]
                    if (stats["active"] or 0) >= account.concurrency:
                        reason = "concurrency"
                    elif stats["calls"] >= account.call_limit:
                        reason = "call_budget"
                    elif stats["held"] + stats["used"] + tokens > account.token_limit:
                        reason = "token_budget"
                    elif account.budget is not None and reserved_cost is None:
                        reason = "unverified_price"
                    elif (
                        account.budget is not None
                        and stats["money_held"] + stats["money_used"] + reserved_cost
                        > account.budget
                    ):
                        reason = "money_budget"
                    elif (account.rpm and recent[0] >= account.rpm) or (
                        account.tpm and recent[1] + tokens > account.tpm
                    ):
                        reason = "rate_window"
                    if reason:
                        reasons.add(reason)
                        continue
                    if credential.alias not in key_calls_cache:
                        key_calls_cache[credential.alias] = db.execute(
                            "SELECT COUNT(*) FROM attempts WHERE alias=?",
                            (credential.alias,),
                        ).fetchone()[0]
                    key_calls = key_calls_cache[credential.alias]
                    candidates.append(
                        (
                            account.expires_at or float("inf"),
                            stats["active"] or 0,
                            stats["calls"],
                            key_calls,
                            credential.alias,
                            reserved_cost,
                        )
                    )
                if not candidates:
                    set_reason(row, ",".join(sorted(reasons)) or "no_compatible_account")
                    continue
                _, _, _, _, alias, reserve = min(candidates)
                credential = self.credentials[alias]
                account = self.config.accounts[credential.group]
                if stop_requested and stop_requested():
                    return None
                attempt = db.execute(
                    """INSERT INTO attempts(task_id,alias,account,provider,model,experiment,started,status,
                    reserved_tokens,reserved_cost,currency,hold_tokens,hold_cost) VALUES(?,?,?,?,?,?,?,'running',?,?,?,?,?)""",
                    (
                        row["id"],
                        alias,
                        account.name,
                        task["provider"],
                        task["model"],
                        task["experiment"],
                        now,
                        tokens,
                        reserve,
                        account.currency,
                        tokens,
                        reserve,
                    ),
                ).lastrowid
                db.execute(
                    "UPDATE tasks SET status='running',attempts=attempts+1,current_attempt=?,lease=?,reason=NULL WHERE id=?",
                    (attempt, now + self.config.timeout + 60, row["id"]),
                )
                self._claims_since_priority = (0 if row['id'] in self.priority_task_ids
                                               else self._claims_since_priority + 1)
                return dict(
                    task_id=row["id"],
                    attempt=attempt,
                    task=task,
                    credential=credential,
                    number=row["attempts"] + 1,
                    started=now,
                )
        return None

    def execute(self, job):
        try:
            result = self.transport(job["credential"], job["task"], self.config.timeout)
            if any(
                c.key in canonical(result)
                for c in self.credentials.values()
                if c.key
            ):
                raise CallError("response_contains_credential")
            return result, None
        except CallError as error:
            return None, error
        except Exception:
            return None, CallError("temporary")

    def finish(self, job, result, error):
        now = self.clock()
        # Keep the invariant even for callers that bypass execute (tests/recovery).
        if result and any(c.key and c.key in canonical(result) for c in self.credentials.values()):
            result, error = None, CallError('response_contains_credential')
        account = self.config.accounts[job["credential"].group]
        inp = result.get("input_tokens") if result else None
        out = result.get("output_tokens") if result else None
        complete_usage = (
            isinstance(inp, int) and isinstance(out, int) and inp >= 0 and out >= 0
        )
        if not complete_usage:
            inp = out = None
        price = self.price(job["task"], account)
        cost = self.cost(price, inp, out)
        valid = bool(result and result.get("valid", True) and result.get("text"))
        if result and not valid:
            error = CallError('invalid_structured_judgment' if result.get('validation_error') == 'invalid_structured_judgment' else 'incomplete_output')
        with self.transaction() as db:
            prior = db.execute(
                "SELECT * FROM attempts WHERE id=?", (job["attempt"],)
            ).fetchone()
            if prior["status"] not in ("running", "unknown"):
                return
            if self.capture_outputs:
                fields = ('text','input_tokens','output_tokens','cached_tokens','valid',
                          'validation_error','validation_code','finish_reason','normalized_text','normalization_flags')
                safe = {k:result[k] for k in fields if k in result} if result else {}
                db.execute('INSERT OR REPLACE INTO attempt_outputs VALUES (?,?,?,?)',
                           (job['attempt'],canonical(safe),safe.get('validation_code'),safe.get('finish_reason')))
            db.execute(
                """UPDATE attempts SET finished=?,status=?,error_kind=?,http_status=?,input_tokens=?,output_tokens=?,
                cached_tokens=?,known_cost=?,latency=?,hold_tokens=?,hold_cost=? WHERE id=?""",
                (
                    now,
                    "done" if valid else "error",
                    error.kind if error else None,
                    error.status if error else None,
                    inp,
                    out,
                    result.get("cached_tokens") if result else None,
                    cost,
                    now - job["started"],
                    0 if complete_usage else prior["reserved_tokens"],
                    0 if cost is not None else prior["reserved_cost"],
                    job["attempt"],
                ),
            )
            if complete_usage and inp + out > prior["reserved_tokens"]:
                db.execute(
                    "UPDATE accounts SET disabled='reservation_exceeded' WHERE name=?",
                    (account.name,),
                )
            task_row = db.execute(
                "SELECT current_attempt,status FROM tasks WHERE id=?", (job["task_id"],)
            ).fetchone()
            if (
                task_row["current_attempt"] != job["attempt"]
                or task_row["status"] != "running"
            ):
                return  # Fenced late worker cannot overwrite a recovered task.
            if valid:
                saved = canonical(result)
                db.execute(
                    "UPDATE tasks SET status='done',result=?,lease=NULL,reason=NULL WHERE id=?",
                    (saved, job["task_id"]),
                )
                db.execute(
                    "INSERT OR REPLACE INTO cache VALUES (?,?)", (job["task_id"], saved)
                )
            else:
                kind = error.kind if error else "empty_output"
                delay = (
                    error.delay
                    if error and error.delay is not None
                    else min(60, 2 ** job["number"]) + random.uniform(0, 1)
                )
                if kind == "authentication":
                    db.execute(
                        "UPDATE credentials SET disabled=? WHERE alias=?",
                        (kind, job["credential"].alias),
                    )
                if kind == "exhausted":
                    db.execute(
                        "UPDATE accounts SET disabled=? WHERE name=?",
                        (kind, account.name),
                    )
                if kind in ("rate_limit", "temporary", "timeout"):
                    db.execute(
                        "UPDATE accounts SET cooldown=MAX(cooldown,?) WHERE name=?",
                        (now + delay, account.name),
                    )
                terminal = job["number"] >= self.config.max_attempts or kind in (
                    "invalid_request",
                    "response_contains_credential",
                )
                # Authentication/quota failures can immediately move to a compatible account.
                available = (
                    now if kind in ("authentication", "exhausted") else now + delay
                )
                db.execute(
                    "UPDATE tasks SET status=?,reason=?,available=?,lease=NULL WHERE id=?",
                    (
                        "failed" if terminal else "queued",
                        kind,
                        available,
                        job["task_id"],
                    ),
                )

    def heartbeat(self, jobs):
        with self.transaction() as db:
            for job in jobs:
                db.execute(
                    "UPDATE tasks SET lease=? WHERE id=? AND status='running' AND current_attempt=?",
                    (
                        self.clock() + self.config.timeout + 60,
                        job["task_id"],
                        job["attempt"],
                    ),
                )

    def run(self, max_idle_seconds=60, on_progress=None, stop_requested=None, worker_limit=None):
        futures = {}
        idle_since = time.monotonic()
        next_heartbeat = 0
        if on_progress:
            on_progress()
        with ThreadPoolExecutor(max_workers=worker_limit or self.config.concurrency) as workers:
            while True:
                stopping = bool(stop_requested and stop_requested())
                while not stopping and len(futures) < self.config.concurrency:
                    if stop_requested and stop_requested():
                        stopping = True
                        break
                    job = self.claim(stop_requested=stop_requested)
                    if job is None:
                        break
                    futures[workers.submit(self.execute, job)] = job
                if futures:
                    done, _ = wait(futures, timeout=0.1, return_when=FIRST_COMPLETED)
                    if time.monotonic() >= next_heartbeat:
                        self.heartbeat(futures.values())
                        next_heartbeat = time.monotonic() + 5
                    for future in done:
                        job = futures.pop(future)
                        self.finish(job, *future.result())
                    if done and on_progress:
                        on_progress()
                    idle_since = time.monotonic()
                else:
                    with self.db() as db:
                        pending = db.execute(
                            "SELECT id,reason FROM tasks WHERE status='queued' AND (? IS NULL OR json_extract(payload,'$.experiment')=?)",
                            (self.experiment, self.experiment),
                        ).fetchall()
                    if self.allowed_task_ids is not None:
                        pending=[r for r in pending if r['id'] in self.allowed_task_ids]
                    if stopping or not pending or time.monotonic() - idle_since >= max_idle_seconds:
                        break
                    temporary = any(
                        r["reason"]
                        and any(
                            s in r["reason"]
                            for s in (
                                "cooldown",
                                "rate_window",
                                "temporary",
                                "timeout",
                                "rate_limit",
                                "incomplete_output",
                                "invalid_structured_judgment",
                                "recovered",
                                "concurrency",
                            )
                        )
                        for r in pending
                    )
                    if not temporary:
                        break
                time.sleep(0.1)
        if on_progress:
            on_progress()
        return self.status()

    def status(self):
        with self.db() as db:
            tasks = {
                r[0]: r[1]
                for r in db.execute("SELECT status,COUNT(*) FROM tasks WHERE (? IS NULL OR json_extract(payload,'$.experiment')=?) GROUP BY status", (self.experiment, self.experiment))
            }
            reasons = [
                dict(r)
                for r in db.execute(
                    "SELECT reason,COUNT(*) count FROM tasks WHERE status IN ('queued','failed') AND (? IS NULL OR json_extract(payload,'$.experiment')=?) GROUP BY reason",
                    (self.experiment, self.experiment),
                )
            ]
            ledger = [
                dict(r)
                for r in db.execute("""SELECT account,currency,COUNT(*) attempts,SUM(input_tokens) input_tokens,
                SUM(output_tokens) output_tokens,SUM(known_cost) known_cost,SUM(hold_tokens) held_tokens,SUM(hold_cost) held_cost,
                SUM(CASE WHEN input_tokens IS NULL OR output_tokens IS NULL THEN 1 ELSE 0 END) unknown_usage_attempts
                FROM attempts WHERE (? IS NULL OR experiment=?) GROUP BY account,currency""", (self.experiment, self.experiment))
            ]
        return {"tasks": tasks, "pending_reasons": reasons, "ledger": ledger}

    def result(self, task_id):
        with self.db() as db:
            row = db.execute(
                "SELECT status,result FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            return (
                json.loads(row["result"]) if row and row["status"] == "done" else None
            )
