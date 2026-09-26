"""Single-reviewer final sign-off, isolated from historical review assignments."""
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

from .guidance import REASON_FIELDS, PLACEHOLDER_PATTERN, validate_guidance

ROUND_ID = 'final-verification-v1'
LEVEL_NAMES = ('记忆', '理解', '应用', '分析', '评价', '创造')
NEXT_PHASE = 'Jupyter Notebook 可视化材料与论文 LaTeX 撰写'


def validate_decision(data, snapshot):
    """Validate structure, source and literal contradictions; never assert AI truth."""
    errors, warnings = [], []
    decision, label = data.get('decision'), data.get('label')
    if decision not in ('resolved', 'insufficient', 'disputed'):
        errors.append('请选择裁定、不可判断或保留争议。')
    valid_level = type(label) is int and 1 <= label <= 6
    if decision == 'resolved' and not valid_level:
        errors.append('已裁定时须选择 L1 至 L6。')
    if decision in ('insufficient', 'disputed') and label is not None:
        errors.append('不可判断或保留争议时不填写单一确定层级。')
    candidates = data.get('candidate_labels', [])
    if not isinstance(candidates, list) or any(x is not None and (type(x) is not int or not 1 <= x <= 6) for x in candidates):
        errors.append('争议候选层级无效。')
    elif decision == 'disputed' and len(set(candidates)) < 2:
        errors.append('保留争议时请至少选择两个候选判断。')
    evidence = data.get('evidence', '')
    if not isinstance(evidence, str) or not evidence or evidence not in snapshot['question']:
        errors.append('请填写当前学生提问中的非空连续原文证据。')
    parts = data.get('reason_parts', {})
    if not isinstance(parts, dict):
        parts = {}
    for field in REASON_FIELDS:
        value = parts.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f'请补全“{field}”。')
        elif len(value) > 800 or re.search(PLACEHOLDER_PATTERN, value):
            errors.append(f'“{field}”须填写具体内容，且不超过 800 字符。')
    rule = data.get('rule_statement', '')
    if not isinstance(rule, str) or not rule.strip() or len(rule) > 1200:
        errors.append('请填写本题采用的判定规则，最多 1200 字符。')
    note = data.get('departure_note', '')
    if not isinstance(note, str) or len(note) > 1200:
        errors.append('不同意见说明最多 1200 字符。')
    elif (decision != 'resolved' or label != snapshot['assistant']['label']) and not note.strip():
        errors.append('与 AI 推荐不同或保留争议时，请写明不同意见依据。')
    reason_text = '\n'.join(v for v in parts.values() if isinstance(v, str))
    for level, name in re.findall(r'[Ll]([1-6])\s*(?:[·：:\-]\s*)?(记忆|理解|应用|分析|评价|创造)', reason_text):
        if LEVEL_NAMES[int(level)-1] != name:
            errors.append(f'层级名称冲突：L{level} 对应“{LEVEL_NAMES[int(level)-1]}”，请核对“L{level} {name}”。')
    if data.get('fill_method') not in ('manual', 'recommendation', 'template'):
        errors.append('填写方式无效。')
    if data.get('confirmed') is not True:
        errors.append('请勾选已核对原文、AI 建议和最终裁定。')
    if isinstance(evidence, str) and evidence and len(evidence) <= 4:
        warnings.append('证据较短，请确认已保留支持判断的操作和对象；主题名用于说明信息不足时可以很短。')
    if decision == 'disputed':
        warnings.append('本题将以争议状态封存，后续材料须保留候选判断。')
    if decision == 'resolved' and label != snapshot['assistant']['label']:
        warnings.append('将保留你的不同判断和说明；与 AI 不同本身不代表错误。')
    if not errors:
        warnings.append('结构和原文匹配检查通过；语义充分性与最终裁定由你确认。')
    return {'valid': not errors, 'errors': list(dict.fromkeys(errors)), 'warnings': warnings}


class FinalVerificationStore:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS final_rounds(
                id TEXT PRIMARY KEY, manifest_sha256 TEXT NOT NULL, created REAL NOT NULL,
                started REAL, completed REAL, expected INTEGER NOT NULL, owner TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS final_cases(
                id TEXT PRIMARY KEY, round_id TEXT NOT NULL, position INTEGER NOT NULL,
                snapshot_json TEXT NOT NULL, started REAL, submitted REAL, reviewer TEXT,
                decision_json TEXT, elapsed REAL, decision_sha256 TEXT);
            ''')

    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def seed(self, cases):
        if not cases or len({c['item'] for c in cases}) != len(cases):
            raise ValueError('最终核验清单为空或重复')
        for case in cases:
            validate_guidance(case['assistant'], case['question'])
        serialized = json.dumps(cases, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM final_rounds WHERE id=?', (ROUND_ID,)).fetchone()
            if old:
                if old['manifest_sha256'] != digest:
                    raise ValueError('最终核验清单已冻结，不能覆盖既有版本')
                return
            db.execute('INSERT INTO final_rounds(id,manifest_sha256,created,expected,owner) VALUES(?,?,?,?,?)',
                       (ROUND_ID,digest,time.time(),len(cases),'C'))
            for index, case in enumerate(cases):
                db.execute('INSERT INTO final_cases(id,round_id,position,snapshot_json) VALUES(?,?,?,?)',
                           (case['item'],ROUND_ID,index+1,json.dumps(case,ensure_ascii=False,sort_keys=True)))

    def state(self):
        with self.db() as db:
            db.execute('BEGIN')
            round_row = db.execute('SELECT * FROM final_rounds WHERE id=?',(ROUND_ID,)).fetchone()
            rows = db.execute('SELECT * FROM final_cases WHERE round_id=? ORDER BY position',(ROUND_ID,)).fetchall()
        if not round_row:
            raise ValueError('最终核验清单尚未准备')
        cases = []
        for row in rows:
            cases.append({'item':row['id'],'position':row['position'],
                'snapshot':json.loads(row['snapshot_json']), 'started_at':row['started'],
                'submitted_at':row['submitted'],'reviewer':row['reviewer'],
                'decision':json.loads(row['decision_json']) if row['decision_json'] else None,
                'elapsed_seconds':row['elapsed'],'decision_sha256':row['decision_sha256']})
        return {'round_id':ROUND_ID,'owner':'C','total':round_row['expected'],
                'completed':sum(r['submitted_at'] is not None for r in cases),
                'closed':round_row['completed'] is not None,'closed_at':round_row['completed'],
                'manifest_sha256':round_row['manifest_sha256'],
                'next_phase':NEXT_PHASE,'cases':cases}

    def start(self, item):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM final_cases WHERE id=? AND round_id=?',(item,ROUND_ID)).fetchone()
            if not row:
                raise ValueError('没有找到该核验题目')
            round_row = db.execute('SELECT * FROM final_rounds WHERE id=?',(ROUND_ID,)).fetchone()
            if row['submitted'] is not None or round_row['completed'] is not None:
                return
            now=time.time()
            db.execute('UPDATE final_cases SET started=COALESCE(started,?) WHERE id=?',(now,item))
            db.execute('UPDATE final_rounds SET started=COALESCE(started,?) WHERE id=?',(now,ROUND_ID))

    def check(self, item, data):
        with self.db() as db:
            row=db.execute('SELECT snapshot_json FROM final_cases WHERE id=? AND round_id=?',(item,ROUND_ID)).fetchone()
        if not row:
            raise ValueError('没有找到该核验题目')
        return validate_decision(data,json.loads(row['snapshot_json']))

    def submit(self, item, data, reviewer='C'):
        if reviewer != 'C':
            raise ValueError('最终核验由 C 角色负责人完成')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM final_cases WHERE id=? AND round_id=?',(item,ROUND_ID)).fetchone()
            round_row=db.execute('SELECT * FROM final_rounds WHERE id=?',(ROUND_ID,)).fetchone()
            if not row or round_row['completed'] is not None or row['submitted'] is not None:
                raise ValueError('该裁定已封存或不可提交')
            if row['started'] is None:
                raise ValueError('请先开始本题核验')
            snapshot=json.loads(row['snapshot_json'])
            checked=validate_decision(data,snapshot)
            if not checked['valid']:
                raise ValueError(' '.join(checked['errors']))
            saved={k:data[k] for k in ('decision','label','evidence','rule_statement','fill_method','confirmed')}
            saved['reason_parts']={field:data['reason_parts'][field].strip() for field in REASON_FIELDS}
            saved['departure_note']=data.get('departure_note','').strip()
            saved['candidate_labels']=list(dict.fromkeys(data.get('candidate_labels',[]))) if data['decision']=='disputed' else []
            saved['ai_version']=snapshot['assistant']['version']
            saved['same_as_ai_label']=data['decision']=='resolved' and data['label']==snapshot['assistant']['label']
            serialized=json.dumps(saved,ensure_ascii=False,sort_keys=True)
            now=time.time()
            db.execute('UPDATE final_cases SET submitted=?,reviewer=?,decision_json=?,elapsed=?,decision_sha256=? WHERE id=?',
                       (now,reviewer,serialized,max(0,now-row['started']),hashlib.sha256(serialized.encode()).hexdigest(),item))
            remaining=db.execute('SELECT COUNT(*) FROM final_cases WHERE round_id=? AND submitted IS NULL',(ROUND_ID,)).fetchone()[0]
            if remaining==0:
                db.execute('UPDATE final_rounds SET completed=? WHERE id=?',(now,ROUND_ID))
        return self.state()

    def export(self):
        return {'schema_version':1,'kind':'human_final_verification',
                'scope':'Four targeted assisted sign-offs; not independent gold labels.',
                'exported_at':time.time(),**self.state()}
