"""Versioned adaptive three-judge workflow; all outputs remain candidate labels."""
from collections import Counter, defaultdict
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import random
import time

from .annotation import JUDGES, RUBRIC, majority, parse_judgment
from .data import synthetic_records
from .pool import Pool, canonical, openai_transport

VERSION = 'adaptive-three-judge-v2'
OUTPUT_LIMITS = (2048, 8192, 4096)
RUBRIC_V2 = RUBRIC + '''
补充判定规则：
1. 仅有主题、术语或方法名且没有明确操作时，标为不可判断，不替学生补出解释或应用请求。
2. 介绍概念与一般应用场景通常属于理解；应用须有把既有方法用于给定情境的要求。评价须有标准及评价操作。
3. 泛泛索取知识清单与要求解释专业关联要区分；“推荐”“分析”不是自动升档依据。难以确定主要操作时标uncertain=true。
4. 复合请求按主导产出判断并说明依据，不能机械取动词最高档；开放创新方向请求可属创造任务，但不证明学生已创新。
5. 证据应尽量包含操作、对象和条件，不只摘一个动词。题目不足以判断时，明确缺少什么。
输出严格按前述八字段JSON结构，不输出Markdown、思维过程或额外文字。'''


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def atomic_json(path, value, *, strict=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), 'utf-8')
    # Windows readers / antivirus may briefly deny replacing an open file.
    # A monitoring snapshot must never interrupt settlement of paid API calls.
    for attempt in range(25):
        try:
            temporary.replace(path)
            return True
        except PermissionError:
            if attempt == 24:
                if strict:
                    raise
                return False
            time.sleep(.02)


def choose_sample(records, development_students, real_count=96, seed=26):
    """Balanced semester sample: random students, then one random row each."""
    if real_count < 2 or real_count % 2:
        raise ValueError('Use an even positive real sample size')
    selected, frame = [], {}
    for term in ('25f', '26s'):
        grouped = defaultdict(list)
        for record in records:
            if record['term'] == term and record['student'] not in development_students:
                grouped[record['student']].append(record)
        students = sorted(grouped)
        n = real_count // 2
        if n > len(students):
            raise ValueError('Sample exceeds unseen student frame; design a separate larger sampling protocol')
        rng = random.Random(f'{seed}|{term}|{VERSION}')
        rng.shuffle(students)
        frame[term] = {'eligible_students': len(students), 'eligible_rows': sum(map(len, grouped.values())), 'selected_students': n}
        for student in students[:n]:
            candidates = sorted(grouped[student], key=lambda r: r['id'])
            record = dict(rng.choice(candidates))
            record['sampling'] = {'partition': 'unseen_student', 'student_probability': n/len(students),
                                  'row_probability_within_student': 1/len(candidates),
                                  'inclusion_probability': n/len(students)/len(candidates)}
            selected.append(record)
    # Interleave semesters so the initial engineering gate also covers both.
    selected = [item for pair in zip(selected[:real_count//2], selected[real_count//2:]) for item in pair]
    controls = {r['label']: r for r in synthetic_records(students=6)}
    for level in range(1, 7):
        selected.append(dict(controls[level], id=f'scale-control-{level}', sampling={'partition':'constructed_control'}))
    return selected, frame


def make_plan(records, frame, source_hash, seed=26, global_concurrency=16):
    if global_concurrency not in (16, 24, 32):
        raise ValueError('Use a supported global concurrency: 16, 24 or 32')
    ordered = sorted(records, key=lambda r: digest([seed, 'repeat', r['id']]))
    repeat_ids = {r['id'] for r in ordered[:math.ceil(len(records)*.10)]}
    audit_ids = {r['id'] for r in sorted(records, key=lambda r: digest([seed, 'audit', r['id']]))[:math.ceil(len(records)*.20)]}
    audit_ids |= {r['id'] for r in records if r['term'] == 'synthetic'}
    arbiters = {r['id']: int(digest([seed, 'arbiter', r['id']])[:8], 16) % 3 for r in records}
    limits = {p: {'calls':0, 'tokens':0} for p, _, _ in JUDGES}
    logical = Counter()
    for record in records:
        for stage, judges in [('independent', range(3)), ('peer', range(3)),
                              ('arbitration', [arbiters[record['id']]]),
                              ('repeat', range(3) if record['id'] in repeat_ids else [])]:
            for j in judges:
                provider = JUDGES[j][0]
                # Deliberately conservative UTF-8 input reservation, not a cost forecast.
                bound = len(RUBRIC_V2.encode()) + len(record['question'].encode()) + (18000 if stage in ('peer','arbitration') else 2048)
                limits[provider]['calls'] += 3
                limits[provider]['tokens'] += 3 * (bound + OUTPUT_LIMITS[j])
                logical[provider] += 1
    core = {'version':VERSION, 'seed':seed, 'sample_sha256':digest(records), 'source_index_sha256':source_hash,
            'rubric_sha256':digest(RUBRIC_V2), 'judges':JUDGES, 'output_limits':OUTPUT_LIMITS,
            'account_topology':'eight_independent_accounts_user_confirmed', 'global_concurrency':global_concurrency,
            'per_account_concurrency':4, 'max_attempts':3, 'repeat_ids':sorted(repeat_ids),
            'agreement_audit_ids':sorted(audit_ids), 'arbiters':arbiters, 'limits':limits,
            'max_logical_tasks_by_provider':dict(logical), 'sampling_frame':frame,
            'real':sum(r['term']!='synthetic' for r in records), 'synthetic':sum(r['term']=='synthetic' for r in records),
            'max_peer_rounds':1, 'max_arbitration_rounds':1, 'canary_records':12,
            'canary_min_independent_valid_rate':.90, 'monetary_cost':'unknown_unverified_prices',
            'purpose':'expanded_exploratory_annotation_not_confirmatory_accuracy'}
    core['experiment'] = VERSION + '-' + digest(core)[:12]
    return core


def scoped_config(base, plan):
    credentials, accounts = [], {}
    counts = Counter(c.provider for c in base.credentials)
    if counts != {'next':4, 'tokendance':4}:
        raise ValueError('This authorized plan requires four configured keys per provider')
    if (type(plan['per_account_concurrency']) is not int or
        not 1 <= plan['per_account_concurrency'] <= 4 or
        type(plan['global_concurrency']) is not int or
        not 1 <= plan['global_concurrency'] <= min(32, len(base.credentials)*plan['per_account_concurrency'])):
        raise ValueError('Concurrency exceeds the configured independent account capacity')
    for credential in base.credentials:
        original = base.accounts[credential.group]
        group = credential.alias + '_account'
        credentials.append(replace(credential, group=group))
        limits = plan['limits'][credential.provider]
        accounts[group] = replace(original, name=group, concurrency=plan['per_account_concurrency'],
                                  call_limit=limits['calls'], token_limit=limits['tokens'])
    return replace(base, credentials=credentials, accounts=accounts, concurrency=plan['global_concurrency'], max_attempts=plan['max_attempts'])


def validated_transport(credential, task, timeout):
    result = openai_transport(credential, task, timeout)
    if result.get('valid', True):
        try:
            question = json.loads(task['messages'][1]['content'])['student_text']
            parse_judgment(result['text'], question)
        except (ValueError, TypeError, KeyError):
            result['valid'] = False
            result['validation_error'] = 'invalid_structured_judgment'
    return result


def route_reason(entries, record, plan):
    if any(not e.get('judgment') for e in entries):
        return 'invalid_independent'
    judgments = [e['judgment'] for e in entries]
    if len({j['level'] for j in judgments}) > 1:
        return 'task_disagreement'
    if len({j['contribution_level'] for j in judgments}) > 1:
        return 'contribution_disagreement'
    if any(j['uncertain'] or j['level'] is None for j in judgments):
        return 'uncertain_or_abstained'
    if any(j['evidence'] and len(j['evidence']) < 5 and len(record['question']) > 12 for j in judgments):
        return 'short_evidence'
    if record['id'] in plan['agreement_audit_ids']:
        return 'prespecified_agreement_audit'
    return 'unanimous_not_selected'


class AnnotationGraph:
    """Per-item dependencies overlap while each model's first answer stays blind."""
    def __init__(self, pool, records, plan, directory):
        self.pool, self.records, self.plan = pool, records, plan
        self.directory = Path(directory)
        self.tasks = {}
        self.last_saved = 0
        self.output = []

    def enqueue(self, record, stage, judge, context=None):
        key = (record['id'], stage, judge)
        if key in self.tasks:
            return
        provider, model, _ = JUDGES[judge]
        task = self.pool.enqueue(provider=provider, model=model, experiment=self.plan['experiment'],
            replicate=f"{record['id']}|{stage}|{judge}", output_limit=self.plan['output_limits'][judge],
            messages=[{'role':'system','content':RUBRIC_V2}, {'role':'user','content':canonical({'student_text':record['question'],'review_context':context})}])
        self.tasks[key] = task

    def rows(self):
        with self.pool.db() as db:
            return {r['id']:dict(r) for r in db.execute("SELECT id,status,result,reason,attempts FROM tasks WHERE json_extract(payload,'$.experiment')=?", (self.plan['experiment'],))}

    def entries(self, record, stage, states):
        result = []
        for j in range(3):
            task = self.tasks.get((record['id'], stage, j))
            if not task:
                continue
            state = states.get(task, {})
            judgment, error = None, None
            if state.get('status') == 'done':
                try:
                    judgment = parse_judgment(json.loads(state['result'])['text'], record['question'])
                except (ValueError, TypeError, KeyError):
                    error = 'invalid_structured_judgment'
            elif state.get('status') == 'failed':
                error = state.get('reason') or 'api_task_failed'
            result.append({'model':JUDGES[j][1], 'family':JUDGES[j][2], 'task':task,
                           'status':state.get('status','queued'), 'judgment':judgment, 'error':error,
                           'attempts':state.get('attempts',0)})
        return result

    @staticmethod
    def terminal(entries, count):
        return len(entries) == count and all(e['status'] in ('done','failed') for e in entries)

    def context(self, record, entries, stage, judge):
        candidates = [e['judgment'] for e in entries]
        random.Random(digest([record['id'],stage,judge,self.plan['seed']])).shuffle(candidates)
        instruction = ('核查候选的证据是否足够支持操作及层级，指出边界并重新判定。允许维持分歧或弃权，不追求一致。'
                       if stage == 'peer' else '候选仍有分歧。你是单次仲裁角色，逐项核对原文和标准后给出判断；证据不足可弃权。')
        return {'stage':stage, 'instruction':instruction, 'candidates':candidates}

    def refresh(self, force=False):
        for record in self.records:
            for j in range(3):
                self.enqueue(record,'independent',j)
                if record['id'] in self.plan['repeat_ids']:
                    self.enqueue(record,'repeat',j)
        states = self.rows()
        output = []
        for record in self.records:
            independent = self.entries(record,'independent',states)
            repeat = self.entries(record,'repeat',states)
            reason, done, final, final_source = 'waiting_independent', False, None, None
            if self.terminal(independent,3):
                reason = route_reason(independent,record,self.plan)
                if reason == 'invalid_independent':
                    done, final_source = True, 'invalid_independent'
                elif reason == 'unanimous_not_selected':
                    done, final_source = True, 'independent_unanimous'
                    final = majority([e['judgment'] for e in independent])
                else:
                    for j in range(3):
                        self.enqueue(record,'peer',j,self.context(record,independent,'peer',j))
            peer = self.entries(record,'peer',states)
            if self.terminal(peer,3):
                if any(not e['judgment'] for e in peer):
                    done, final_source = True, 'invalid_peer'
                elif len({(e['judgment']['level'], e['judgment']['contribution_level']) for e in peer}) > 1:
                    j = self.plan['arbiters'][record['id']]
                    self.enqueue(record,'arbitration',j,self.context(record,peer,'arbitration',j))
                else:
                    done, final_source = True, 'peer_unanimous'
                    final = majority([e['judgment'] for e in peer])
            arbitration = self.entries(record,'arbitration',states)
            if self.terminal(arbitration,1):
                done = True
                final_source = 'arbitration' if arbitration[0]['judgment'] else 'invalid_arbitration'
                final = arbitration[0]['judgment']['level'] if arbitration[0]['judgment'] else None
            repeat_done = not repeat or self.terminal(repeat,3)
            output.append({'record':record,'independent':independent,'peer':peer,
                           'arbitration':arbitration[0] if arbitration else None,'repeat':repeat,
                           'route_reason':reason,'single_model':independent[0]['judgment']['level'] if independent and independent[0]['judgment'] else None,
                           'vote':majority([e['judgment'] for e in independent]),
                           'peer_vote':majority([e['judgment'] for e in peer]),
                           'final':final,'final_source':final_source,'complete':done and repeat_done,
                           'truth_status':'model_candidate_not_human_gold'})
        self.output = output
        if force or time.monotonic() - self.last_saved >= 2:
            atomic_json(self.directory/'annotations.json', output, strict=False)
            atomic_json(self.directory/'summary.json', self.summary(), strict=False)
            self.last_saved = time.monotonic()

    def summary(self):
        stages = {}
        for stage in ('independent','peer','repeat','arbitration'):
            entries = [e for item in self.output for e in ([item[stage]] if stage=='arbitration' and item[stage] else [] if stage=='arbitration' else item[stage])]
            stages[stage] = {'requested':len(entries),'valid':sum(bool(e['judgment']) for e in entries),
                             'failed':sum(e['status']=='failed' for e in entries)}
        stable, comparable = 0, 0
        for item in self.output:
            initial = {e['model']:e['judgment'] for e in item['independent']}
            for repeat in item['repeat']:
                prior = initial.get(repeat['model'])
                if prior and repeat['judgment']:
                    comparable += 1
                    stable += prior['level'] == repeat['judgment']['level']
        return {'experiment':self.plan['experiment'],'updated_at':time.time(),'n':len(self.output),
                'real':sum(i['record']['term']!='synthetic' for i in self.output),
                'synthetic':sum(i['record']['term']=='synthetic' for i in self.output),
                'completed_records':sum(i['complete'] for i in self.output),
                'final_available':sum(i['complete'] and i['final'] is not None for i in self.output),
                'stages':stages,'routes':dict(Counter(i['route_reason'] for i in self.output)),
                'repeat_stability':{'comparable_model_pairs':comparable,'same_task_label':stable},
                'pool':self.pool.status(),'human_gold_labels':0,
                'interpretation':'Exploratory model annotation; agreement, stability and substring validity are not accuracy.'}

    def run(self):
        self.pool.run(max_idle_seconds=65,on_progress=self.refresh,
                      stop_requested=lambda:(self.directory/'STOP').exists())
        self.refresh(force=True)
        return self.summary()


def create_pool(base, plan):
    return Pool(scoped_config(base,plan),transport=validated_transport,
                experiment=plan['experiment'],limits=plan['limits'])
