"""Source-preserving product imports and hash-pinned, read-only research adapter."""
from __future__ import annotations
from collections import Counter
import csv
import hashlib
import io
import json
from pathlib import Path
import random
import re
import time
import uuid

from .full_turn_reporting import load_frozen, digest, sha256
from .turns import parse_qa_record, MARKER, URL_ONLY
from .product_settings import settings

T3_PATH = 'runtime/research/full-turns-v1-20260926T143225Z/t3'
MATERIALS_PATH = 'runtime/research/full-turn-materials-t3-v1'
T3_HASH = '7e885ceecf12a180349369ed9ddfc962ad71f1ea270f68b037a86a396430631d'
MATERIALS_HASH = 'c71988c24a8ecbb97889f28fa5b6f3af7f9ac1326cf33bd773b407468eb36543'
_frozen_cache = {}


def read_json(path, default=None):
    if not path.exists() and default is not None:
        return default
    return json.loads(path.read_text('utf-8'))


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False), 'utf-8')
    tmp.replace(path)


def runtime():
    return Path(settings()['runtime_root'])


def safe_key(key):
    if not re.fullmatch(r'[\w-]{1,80}', key):
        raise KeyError('Invalid identifier')
    return key


def quality(records, raw_rows, dropped=0, checks=None, synthetic=False):
    why = '需核实相邻回合关系；会话编号本身不是证据'
    return dict(raw_rows=raw_rows, usable=len(records), students=len({r['student'] for r in records}),
        dropped_rows=dropped, checks=checks or [],
        identifiable={k: dict(ok=bool(records) if k in ('ABL','HOT','DHI') else False,
          why=('模型形成有效候选后按实际分母计算' if k in ('ABL','HOT','DHI') else
               '需同时具备工具类型和足够有效候选' if k == 'MAB' else why if k == 'CTQ' else '真实 CTQ、完整评价条件缺失'))
          for k in ('ABL','HOT','CTQ','DHI','MAB','AIV')},
        boundaries=['合成教学数据，构造标签仅用于演示' if synthetic else '模型候选不是人工金标准；不用于班级排名或学习增益判断',
                    '保留原始回合及断点，不将缺失贡献记为低水平', why])


def frozen_context():
    root = Path(settings()['research_root'])
    directory, materials = root / T3_PATH, root / MATERIALS_PATH
    paths = [directory/'plan.json', directory/'sample.json', directory/'frozen/snapshot-manifest.json',
             directory/'frozen/annotations.json', directory/'frozen/summary.json',
             root/'runtime/research/turns-v1/student_turns.jsonl', root/'runtime/research/turns-v1/manifest.json',
             materials/'manifest.json']
    manifest = read_json(materials/'manifest.json')
    paths += [materials / name for name in manifest['files']]
    fingerprint = tuple((str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in paths)
    cached = _frozen_cache.get(str(root))
    if cached and cached[0] == fingerprint:
        unchanged=all(Path(p).exists() and (Path(p).stat().st_size,Path(p).stat().st_mtime_ns)==(size,stamp) for p,size,stamp in cached[1]['_source_stats'])
        if unchanged: return cached[1]
    if sha256(materials/'manifest.json') != MATERIALS_HASH:
        raise ValueError('正式 t3 派生清单哈希不符')
    for name, expected in manifest['files'].items():
        p = (materials/name).resolve()
        if not p.is_relative_to(materials.resolve()) or sha256(p) != expected:
            raise ValueError('正式 t3 派生文件哈希不符')
    frozen = load_frozen(directory/'plan.json', T3_HASH, root=root)
    if manifest['source']['manifest_sha256'] != T3_HASH or manifest['source']['plan_sha256'] != sha256(directory/'plan.json'):
        raise ValueError('正式 t3 派生来源不符')
    real = [r['record'] for r in frozen.rows if r['record']['term'] != 'synthetic']
    # Verify original source files too; the frozen turn join alone cannot detect later CSV changes.
    for source in {r['source']['file']: r['source'] for r in real}.values():
        p = (root/source['file']).resolve()
        if not p.is_relative_to(root) or sha256(p) != source['sha256']:
            raise ValueError('正式 t3 原始来源哈希不符')
        paths.append(p)
    with (materials/'student-indicators.csv').open(encoding='utf-8-sig',newline='') as stream:
        indicators = list(csv.DictReader(stream))
    if len(real) != 3515 or len(indicators) != 401:
        raise ValueError('正式 t3 分母不符')
    data = dict(id='t3', name='正式 t3 · 全回合认知证据', kind='frozen', private=True,
                records=real, quality=quality(real, 1225, 0), created=frozen.source['frozen_at'])
    turn_counts=read_json(root/'runtime/research/turns-v1/manifest.json')['counts']
    data['quality']['checks']=[
        dict(key='unusable_turns',label='不可处理学生回合',count=turn_counts['student_turns']-len(real),severity='warning',examples=[]),
        dict(key='parse_review',label='Q/A 结构需复核源行',count=turn_counts['records_needing_review'],severity='warning',examples=[]),
        dict(key='missing_answer',label='末尾学生提问没有回答',count=turn_counts['issues']['last_question_has_no_answer'],severity='warning',examples=[]),
        dict(key='source_hash',label='冻结清单、派生文件与原文哈希',count=0,severity='pass',examples=[])]
    data['quality']['boundaries'].append('研究选定 1225 条源记录，解析出 3522 个学生回合；其中 7 个不满足模型处理条件。')
    data['quality']['identifiable']['MAB'] = dict(ok=True,why='257 个学生×学期单位具备可计算条件')
    context = dict(dataset=data, job=dict(id='t3',dataset='t3',dataset_name=data['name'],mode='frozen',state='finished'),
                   mode='frozen',plan=frozen.plan,rows=frozen.rows,student_indicators=indicators,
                   metadata={**frozen.source, 'snapshot_manifest_sha256':T3_HASH,'materials_manifest_sha256':MATERIALS_HASH},confirmations=[])
    # Include original sources in future invalidation without re-reading all annotations on polling.
    _frozen_cache[str(root)] = (fingerprint, context)
    context['_source_stats'] = [(str(p),p.stat().st_size,p.stat().st_mtime_ns) for p in paths[-len({r['source']['file'] for r in real}):]]
    return context


def get_dataset(key):
    safe_key(key)
    if key == 't3':
        ctx = frozen_context()
        for path,size,stamp in ctx['_source_stats']:
            p=Path(path)
            if (p.stat().st_size,p.stat().st_mtime_ns)!=(size,stamp):
                _frozen_cache.clear()
                return frozen_context()['dataset']
        return ctx['dataset']
    path = runtime()/'datasets'/key/'metadata.json'
    if not path.exists() and key in ('demo-synthetic','demo-dirty'):
        from .data import synthetic_records
        from .legacy_workbench import dirty_csv
        if key == 'demo-dirty':
            return import_text('含问题的合成样例.csv',dirty_csv(),term='synthetic',key=key,private=False)
        records=synthetic_records()
        for r in records:
            r['term']='synthetic'
            if not isinstance(r.get('source'),dict): r['source']={'kind':'synthetic','description':str(r.get('source',''))}
        value=dict(id=key,name='合成教学样本（24 名学生 × 8 条）',kind='synthetic',private=False,
                   records=records,quality=quality(records,len(records),synthetic=True),created=time.time())
        write_json(path,value)
    return read_json(path)


ALIASES = dict(student=('student','student_id','学号','学生'),question=('question','text','提问','问答记录'),
               term=('term','学期'),timestamp=('timestamp','time','问题建立时间','时间'),
               agent=('agent','tool','智能体类型','工具'),session=('session_id','session','会话','会话编号'),
               role=('role','角色'),cohort=('cohort','class','班级','分组'),id=('id','record_id','编号'))


def import_text(name,text,term='',cohort='',key=None,private=True):
    if not text.strip() or len(text.encode('utf-8')) > 5*1024*1024:
        raise ValueError('文件为空或超过 5 MiB')
    key=key or 'batch-'+uuid.uuid4().hex[:12]
    directory=runtime()/'datasets'/safe_key(key)
    jsonl = name.lower().endswith(('.jsonl','.ndjson')) or text.lstrip().startswith('{')
    extension='jsonl' if jsonl else 'csv'
    source_path=directory/('source.'+extension)
    source_path.parent.mkdir(parents=True,exist_ok=True)
    source_path.write_text(text,encoding='utf-8',newline='')
    source_hash=sha256(source_path)
    issues={}; inputs=[]; dropped=set(); all_turns=[]; records=[]; audits=[]; seen=set()
    def issue(kind,row,reason):
        issues.setdefault(kind,[]).append(dict(row=row,reason=reason))
    if jsonl:
        for n,line in enumerate(text.lstrip('\ufeff').splitlines(),1):
            if not line.strip(): continue
            try:
                row=json.loads(line)
                if not isinstance(row,dict): raise ValueError()
                inputs.append((n,row))
            except (ValueError,TypeError):
                inputs.append((n,None)); issue('bad_row',n,'无法解析 JSON 对象'); dropped.add(n)
    else:
        try:
            reader=csv.DictReader(io.StringIO(text.lstrip('\ufeff'),newline=''),strict=True)
            if not reader.fieldnames or len(set(reader.fieldnames))!=len(reader.fieldnames):
                raise ValueError('CSV 表头为空或有重复列名')
            for n,row in enumerate(reader,2):
                inputs.append((n,row))
        except csv.Error:
            raise ValueError('CSV 引号或行结构损坏，请修正后重新导入') from None
    for n,row in inputs:
        if row is None: continue
        if None in row or any(v is None for v in row.values()):
            issue('bad_row',n,'列数与表头不一致'); dropped.add(n); continue
        cols={k:next((alias for alias in aliases if alias in row),None) for k,aliases in ALIASES.items()}
        def value(k): return str(row.get(cols[k],'') or '').strip()
        student=value('student'); semester=value('term') or term
        raw=str(row.get(cols['question'],'') or '')
        signature=digest(row)
        if signature in seen:
            issue('duplicate',n,'与前面某行相同'); dropped.add(n); continue
        seen.add(signature)
        if not student or not semester or not raw.strip():
            issue('missing_fields',n,'缺少学生标识、学期或文字；学期可在导入页补充'); dropped.add(n); continue
        role=value('role').lower()
        if not value('timestamp'): issue('missing_time',n,'缺少时间；不推断相邻关系')
        if not value('agent'): issue('missing_tool',n,'缺少工具；工具匹配指标可能不可用')
        if not (value('cohort') or cohort): issue('missing_cohort',n,'缺少已核验分组；报告使用所选批次')
        rid=digest([key,n,value('id')])[:24]
        base=dict(id=rid,student=student,term=semester,timestamp=value('timestamp'),agent=value('agent'),
                  source_file=source_path.relative_to(runtime()).as_posix(),source_row=n,source_sha256=source_hash)
        if MARKER.search(raw) and role in ('','student','user','学生','q'):
            turns,audit=parse_qa_record(base,raw); audits.append(audit)
            for message in turns:
                message['source'].update(origin='product',field=cols['question'])
            if not audit['structural_alternating']: issue('parse_issue',n,'Q/A 标记没有交替，保留原文并等待复核')
        else:
            start=len(raw)-len(raw.lstrip()); end=len(raw.rstrip()); body=raw[start:end]
            eligible=role in ('','student','user','学生','q') and bool(body) and not URL_ONLY.fullmatch(body) and body not in ('（图片）','[图片]')
            turns=[dict(turn_id=rid,student_id=student,term=semester,text=body,role=role or 'student',agent=value('agent'),
                session_candidate_id=value('session') or 'row-'+rid,session_status='unverified',turn_index=1,
                record_timestamp=value('timestamp'),turn_timestamp=None,parse_status='single_text_unverified',model_eligible=bool(eligible),
                source=dict(origin='product',file=base['source_file'],row=n,sha256=source_hash,field=cols['question'],
                            char_start=start,char_end=end,field_sha256=hashlib.sha256(raw.encode()).hexdigest()))]
        all_turns.extend(turns)
        eligible=[t for t in turns if t['model_eligible']]
        if not eligible:
            issue('no_eligible_turn',n,'没有可处理学生回合（助手消息、链接、空白或解析问题）'); dropped.add(n)
        for t in eligible:
            records.append(dict(id=t['turn_id'],student=t['student_id'],term=t['term'],question=t['text'],
              source=t['source'],session_candidate_id=t['session_candidate_id'],session_status=t['session_status'],
              turn_index=t['turn_index'],parse_status=t['parse_status'],record_timestamp=t['record_timestamp'],
              turn_timestamp=t['turn_timestamp'],agent=t['agent'],cohort=value('cohort') or cohort,role='student'))
    labels=dict(bad_row='坏行',duplicate='重复源行',missing_fields='缺少必要字段',missing_time='缺少时间',missing_tool='缺少工具类型',missing_cohort='分组未核验',parse_issue='Q/A 结构问题',no_eligible_turn='无可处理学生回合')
    checks=[dict(key=k,label=labels.get(k,k),count=len(v),severity='error' if k in ('bad_row','missing_fields','parse_issue') else 'warning',examples=v[:8]) for k,v in issues.items()]
    data=dict(id=key,name=name,kind='live' if private else 'synthetic',private=private,created=time.time(),records=records,
              quality=quality(records,len(inputs),len(dropped),checks,not private),source_digest=digest(records),
              source_manifest=dict(file=source_path.relative_to(runtime()).as_posix(),sha256=source_hash,format=extension))
    write_json(directory/'parsed-turns.json',all_turns); write_json(directory/'parse-audits.json',audits)
    write_json(directory/'metadata.json',data)
    return data


def verify_record_source(record, config=None):
    cfg=config or settings(); source=record.get('source') or {}
    if not isinstance(source,dict):
        return dict(ok=record.get('term')=='synthetic',reason='合成教学示例' if record.get('term')=='synthetic' else '来源格式无效')
    if not source.get('file'):
        return dict(ok=record.get('term')=='synthetic',reason='合成教学示例' if record.get('term')=='synthetic' else '缺少来源')
    root=Path(cfg['runtime_root'] if source.get('origin')=='product' else cfg['research_root']).resolve()
    path=(root/source['file']).resolve()
    if not path.is_relative_to(root) or not path.is_file(): return dict(ok=False,reason='来源路径不符或不存在')
    if sha256(path)!=source.get('sha256'): return dict(ok=False,reason='来源文件哈希不符')
    try:
        if path.suffix.lower() in ('.jsonl','.ndjson'):
            row=json.loads(path.read_text('utf-8-sig').splitlines()[int(source['row'])-1])
        else:
            with path.open(encoding='utf-8-sig',newline='') as stream:
                row=next(r for i,r in enumerate(csv.DictReader(stream),2) if i==int(source['row']))
        raw=str(row[source['field']]); start=int(source['char_start']); end=int(source['char_end'])
        if hashlib.sha256(raw.encode()).hexdigest()!=source['field_sha256'] or raw[start:end]!=record['question']:
            return dict(ok=False,reason='原文位置或字段哈希不符')
        return dict(ok=True,reason='文件、字段及字符位置核验通过',file=source['file'],row=source['row'],char_start=start,char_end=end)
    except (KeyError,ValueError,IndexError,StopIteration): return dict(ok=False,reason='无法定位原文')


def create_smoke_dataset():
    key='smoke-t3-seed26'
    path=runtime()/'datasets'/key/'metadata.json'
    if path.exists(): return read_json(path)
    ctx=frozen_context(); rng=random.Random(26); selected=[]; strata=[]
    for state in ('agreed','disagreement','abstained'):
        for term in ('25f','26s'):
            candidates=sorted([r for r in ctx['rows'] if r['record']['term']==term and r['dimensions']['task']['final']['status']==state],key=lambda r:r['record']['id'])
            chosen=rng.sample(candidates,2)
            for row in chosen:
                record=json.loads(json.dumps(row['record'])); record['source']['origin']='research'
                selected.append(record); strata.append(dict(id=record['id'],term=term,selection=state))
    data=dict(id=key,name='演示用途 · t3 固定 12 回合（种子 26）',kind='live',private=True,created=time.time(),records=selected,
              quality=quality(selected,12),source_digest=digest(selected),sampling=dict(seed=26,purpose='demonstration',strata=strata,source_manifest=T3_HASH))
    write_json(path,data)
    return data
