"""Workbench HTTP adapter. Research snapshots are read-only; new state is local."""
from collections import Counter
from contextlib import closing
import copy
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from . import product_data as data
from .product_settings import settings, token_ok
from .full_turn_workflow import model_conditions

router=APIRouter(prefix='/api/wb')


def require(token):
    if not token_ok(token): raise HTTPException(401,'请输入内部共享口令')


def dataset(key,token):
    if key not in ('demo-synthetic','demo-dirty'): require(token)
    try: return data.get_dataset(key)
    except (OSError,ValueError,KeyError): raise HTTPException(409,'批次不存在或来源校验失败') from None


def dataset_dto(value):
    return {**{k:v for k,v in value.items() if k!='records'},'count':len(value['records']),
            'preview':[{k:r.get(k) for k in ('id','student','term','question','agent')} for r in value['records'][:5]],
            'estimated_tasks':(48+len(value['records'])*4+min(40,len(value['records']))*2) if value['kind']=='live' else 0}


def jobdir(key): return data.runtime()/'jobs'/data.safe_key(key)


def confirmations(key):
    path=data.runtime()/'confirmations.sqlite3'
    if not path.exists(): return []
    with closing(sqlite3.connect(f'file:{path.as_posix()}?mode=ro',uri=True)) as db:
        return [json.loads(r[0]) for r in db.execute('SELECT body FROM confirmations WHERE job=? ORDER BY sequence',(key,))]


def context(key,token):
    if key=='t3':
        dataset('t3',token)
        ctx=dict(data.frozen_context())
    else:
        try: meta=data.read_json(jobdir(key)/'metadata.json')
        except (OSError,ValueError,KeyError): raise HTTPException(404,'没有找到任务') from None
        batch=dataset(meta['dataset'],token)
        meta={**meta,**{k:v for k,v in data.read_json(jobdir(key)/'state.json',{}).items() if k in ('state','reason','updated_at')}}
        ctx=dict(dataset=batch,job=meta,mode=meta['mode'],plan=data.read_json(jobdir(key)/'plan.json',{}),
                 rows=data.read_json(jobdir(key)/'annotations.json',[]),metadata=dict(source_digest=batch.get('source_digest')))
    ctx['confirmations']=confirmations(key)
    return ctx


def real_rows(ctx):
    return ctx['rows'] if ctx['job']['mode']=='synthetic' else [r for r in ctx['rows'] if r['record']['term']!='synthetic']


def job_dto(ctx):
    from . import product_runner as runner
    meta=ctx['job']; mode=meta['mode']; rows=real_rows(ctx)
    state=(runner.status(jobdir(meta['id'])) if mode=='live' else
           data.read_json(jobdir(meta['id'])/'state.json',dict(state='ready')) if mode=='synthetic' else dict(state='finished'))
    counts=Counter((r.get('dimensions',{}).get('task',{}).get('final') or {}).get('status','not_run') for r in rows)
    controls=[r for r in ctx['rows'] if r['record']['term']=='synthetic'] if mode!='synthetic' else []
    models=ctx['plan'].get('models',[]) or ([] if mode=='synthetic' else model_conditions())
    return {**meta, 'state':state.get('state','ready'),'reason':state.get('reason'),
      'cursor':sum(bool(r.get('complete')) for r in rows),'total':len(ctx['dataset']['records']),
      'done':sum(bool(r.get('complete')) for r in rows),'disagree':counts['disagreement'],'abstain':counts['abstained'],
      'not_run':max(0,len(ctx['dataset']['records'])-sum(bool(r.get('complete')) for r in rows)),
      'failed':counts['technical_failure'], 'controls':state.get('controls') or dict(total=ctx['plan'].get('controls',0),completed=sum(bool(r.get('complete')) for r in controls)),
      'phase':state.get('phase'),'pending_reasons':state.get('pending_reasons',[]),
      'stages':{s:sum(e.get('status') in ('done','failed') for r in ctx['rows'] for e in r.get(s,[])) for s in ('independent','review','repeat')},
      'requests':{status:sum(e.get('status')==status for r in ctx['rows'] for stage in ('independent','review','repeat') for e in r.get(stage,[])) for status in ('done','failed','queued','running')},
      'log':state.get('log',[])[-30:], 'models':[{k:m.get(k) for k in ('id','role','provider')} for m in models],
      'capabilities':dict(start=mode!='frozen' and state.get('state')=='ready',pause=mode=='live' and state.get('state')=='running',
                          resume=mode=='live' and state.get('state') in ('paused','blocked','failed') and state.get('reason') not in ('control_quality_failed','deadline_reached','source_integrity_failed')),
      'limits':ctx['plan'].get('limits',state.get('limits',{})), 'billing':dict(cost=None,reason='未配置可靠单价；调用和用量见持久账本')}


@router.get('/samples')
def samples(x_workbench_token: str|None=Header(default=None)):
    unlocked=token_ok(x_workbench_token)
    items=[dict(id='t3',name='正式 t3 · 全回合认知证据',kind='frozen',note='3515 回合 · 401 个学生×学期单位 · 只读冻结成果',locked=not unlocked)]
    for key,name,note in [('demo-synthetic','合成教学样本','构造数据与旧模型缓存，供完整体验与权重实验'),('demo-dirty','含问题的合成样例','检验坏行、缺字段与解析问题')]:
        items.append(dict(id=key,name=name,kind='synthetic',note=note,locked=False))
    if unlocked:
        for path in sorted((data.runtime()/'datasets').glob('*/metadata.json'),key=lambda p:p.stat().st_mtime,reverse=True):
            value=data.read_json(path)
            if value['id'] in ('demo-synthetic','demo-dirty'): continue
            items.append(dict(id=value['id'],name=value['name'],kind=value['kind'],note='独立业务批次',locked=False))
    return dict(samples=items,unlocked=unlocked,default_job='demo-synthetic',models=model_conditions())


@router.post('/import/sample/{key}')
def import_sample(key:str,x_workbench_token: str|None=Header(default=None)):
    return dataset_dto(dataset(key,x_workbench_token))


@router.get('/datasets/{key}')
def get_dataset(key:str,x_workbench_token: str|None=Header(default=None)):
    return dataset_dto(dataset(key,x_workbench_token))


class ImportBody(BaseModel):
    name:str=Field(default='导入.csv',max_length=200)
    text:str=Field(max_length=6*1024*1024)
    term:str=Field(default='',max_length=80)
    cohort:str=Field(default='',max_length=120)


@router.post('/import/upload')
def upload(body:ImportBody,x_workbench_token: str|None=Header(default=None)):
    require(x_workbench_token)
    try: return dataset_dto(data.import_text(body.name,body.text,body.term,body.cohort))
    except ValueError as error: raise HTTPException(422,str(error)) from None


@router.post('/import/smoke')
def smoke(x_workbench_token: str|None=Header(default=None)):
    require(x_workbench_token)
    return dataset_dto(data.create_smoke_dataset())


class JobBody(BaseModel):
    dataset:str=Field(max_length=80)


@router.post('/jobs')
def create_job(body:JobBody,x_workbench_token: str|None=Header(default=None)):
    value=dataset(body.dataset,x_workbench_token)
    if value['id']=='t3': return job_dto(context('t3',x_workbench_token))
    if not value['records']: raise HTTPException(422,'没有可处理回合，请修正导入问题')
    # One persistent job per batch prevents duplicate clicks from purchasing duplicate requests.
    key='job-'+value['id']; directory=jobdir(key)
    from . import product_runner as runner
    with runner._lock(data.runtime()/'create-job.lock',blocking=True):
        if not (directory/'metadata.json').exists():
            meta=dict(id=key,dataset=value['id'],dataset_name=value['name'],mode='synthetic' if value['kind']=='synthetic' else 'live',created=time.time())
            directory.mkdir(parents=True,exist_ok=True)
            if meta['mode']=='live':
                try: runner.create_plan(directory,value['records'],name=value['name'],source_digest=data.digest(value['records']))
                except ValueError: raise HTTPException(422,'回合学期、来源或任务预算不满足计划要求') from None
            data.write_json(directory/'metadata.json',meta)
    return job_dto(context(key,x_workbench_token))


@router.get('/jobs')
def jobs(x_workbench_token: str|None=Header(default=None)):
    result=[]
    if token_ok(x_workbench_token):
        result.append(job_dto(context('t3',x_workbench_token)))
    for path in sorted((data.runtime()/'jobs').glob('*/metadata.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:50]:
        meta=data.read_json(path)
        if meta['mode']!='synthetic' and not token_ok(x_workbench_token): continue
        result.append(job_dto(context(meta['id'],x_workbench_token)))
    return dict(jobs=result)


@router.get('/jobs/{key}')
def get_job(key:str,x_workbench_token: str|None=Header(default=None)):
    return job_dto(context(key,x_workbench_token))


def replay_synthetic(ctx):
    from .legacy_workbench import judge_cache
    cache=judge_cache(); rows=[]; modelmap={}
    for record in ctx['dataset']['records']:
        cached=cache.get(record['question']); independent=[]; review=[]
        if cached:
            for stage,target in [('independent',independent),('peer',review)]:
                for e in cached.get(stage,[]):
                    modelmap.setdefault(e['model'],dict(id=e['model'],provider=e.get('family','cache'),role='fast' if stage=='independent' else 'review'))
                    target.append(dict(model=e['model'],status='done' if e.get('judgment') else 'failed',judgment=e.get('judgment'),attempts=0))
        label=cached.get('final') if cached else None
        final=dict(label=label,status='agreed' if label else 'abstained' if cached else 'not_run',source='synthetic-cache')
        rows.append(dict(record=record,independent=independent,review=review,repeat=[],complete=bool(cached),review_selection='random_audit',review_selected=bool(review),
            dimensions=dict(task=dict(final=final),contribution=dict(final=dict(label=None,status='abstained',source='legacy_cache_without_contribution'))),final=label))
    directory=jobdir(ctx['job']['id'])
    data.write_json(directory/'annotations.json',rows)
    data.write_json(directory/'plan.json',dict(version='synthetic-cache-v1',revision='teaching',experiment=ctx['job']['id'],models=list(modelmap.values()),controls=0,audit_ids=[r['record']['id'] for r in rows],repeat_ids=[]))
    data.write_json(directory/'state.json',dict(state='finished',log=[dict(t=time.time(),msg='合成缓存回放完成；无模型 API 调用，旧缓存不含贡献维度')]))


def control_job(key:str,action:str,x_workbench_token: str|None=Header(default=None)):
    if action not in ('start','pause','resume'): raise HTTPException(404,'没有找到操作')
    ctx=context(key,x_workbench_token)
    if ctx['job']['mode']=='frozen': raise HTTPException(409,'正式冻结成果只读')
    from . import product_runner as runner
    if ctx['job']['mode']=='synthetic':
        if action in ('start','resume'): replay_synthetic(ctx)
    else:
        try:
            if action=='pause': runner.pause(jobdir(key))
            else: runner.start(jobdir(key),settings())
        except runner.RunnerBusy: raise HTTPException(409,'另一个批次仍在运行，请先暂停并等待结算') from None
        except (ValueError,OSError): raise HTTPException(409,'任务启动校验未通过；请检查来源、配置或运行状态') from None
    return job_dto(context(key,x_workbench_token))


@router.post('/jobs/{key}/start')
def start_job(key:str,x_workbench_token: str|None=Header(default=None)):
    return control_job(key,'start',x_workbench_token)


@router.post('/jobs/{key}/pause')
def pause_job(key:str,x_workbench_token: str|None=Header(default=None)):
    return control_job(key,'pause',x_workbench_token)


@router.post('/jobs/{key}/resume')
def resume_job(key:str,x_workbench_token: str|None=Header(default=None)):
    return control_job(key,'resume',x_workbench_token)


class MetricBody(BaseModel):
    student:str
    weights:list[float]
    uncertainty:float=Field(default=.15,ge=0,le=.5)
    asymmetric:bool=False


@router.post('/jobs/{key}/metrics')
def metrics_demo(key:str,body:MetricBody,x_workbench_token: str|None=Header(default=None)):
    import numpy as np
    from .metrics import metrics, composite, NAMES
    from .legacy_workbench import clean
    ctx=context(key,x_workbench_token)
    if ctx['job']['mode']!='synthetic': raise HTTPException(409,'完整评分和权重实验仅用于合成教学示例')
    rows=[{**r['record'],'label':r['final']} for r in real_rows(ctx) if r['record']['student']==body.student and r.get('final')]
    if not rows: raise HTTPException(422,'该学生还没有可用标签')
    levels=[r['label'] for r in rows]; agents=[r.get('agent') for r in rows]; sessions=[r.get('session') for r in rows]
    try:
        values=metrics(levels,agents,sessions,asymmetric=body.asymmetric); score=composite(values,body.weights)
    except ValueError: raise HTTPException(422,'权重须为 5 个非负数且和为 1') from None
    interval=None
    if score is not None:
        rng=np.random.default_rng(26); probabilities=np.full((len(rows),6),body.uncertainty/5)
        for i,level in enumerate(levels): probabilities[i,level-1]=1-body.uncertainty
        draws=[composite(metrics([int(rng.choice(range(1,7),p=p)) for p in probabilities],agents,sessions,asymmetric=body.asymmetric),body.weights) for _ in range(200)]
        interval=np.quantile(draws,[.025,.5,.975]).tolist()
    return clean(dict(n=len(rows),metrics=values,score=score,interval=interval,missing=[n for n,w in zip(NAMES,body.weights) if w>0 and values.get(n) is None],source='synthetic_teaching'))


def item(row,ctx,dimension='task',verify=False):
    record=row['record']; dim=row.get('dimensions',{})
    final=dim.get(dimension,{}).get('final') or {}; flags=[]
    for name in ('task','contribution'):
        status=(dim.get(name,{}).get('final') or {}).get('status','not_run')
        if status!='agreed': flags.append(status)
    if row.get('review_selection')=='high_risk': flags.append('high_risk')
    return {**{k:record.get(k) for k in ('id','student','term','question','source','agent','turn_index','session_candidate_id')},
       'final':final.get('label'),'status':final.get('status','not_run'),'contribution':(dim.get('contribution',{}).get('final') or {}).get('label'),
       'dimensions':dim,'independent':row.get('independent',[]),'review':row.get('review',[]),'repeat':row.get('repeat',[]),'flags':list(set(flags)),
       'source_verification':data.verify_record_source(record) if verify else None,
       'confirmations':[c for c in ctx['confirmations'] if c['turn_id']==record['id']]}


@router.get('/jobs/{key}/anomalies')
def anomalies(key:str,dimension:str=Query('task',pattern='^(task|contribution)$'),stage:str=Query('all',pattern='^(all|independent|review|repeat)$'),
              flag:str='',q:str=Query('',max_length=300),offset:int=Query(0,ge=0),limit:int=Query(30,ge=1,le=100),x_workbench_token: str|None=Header(default=None)):
    ctx=context(key,x_workbench_token); items=[item(r,ctx,dimension) for r in real_rows(ctx) if stage=='all' or r.get(stage)]
    if flag: items=[r for r in items if flag in r['flags'] or flag==r['status'] or (flag=='disagree' and 'disagreement' in r['flags']) or (flag=='abstain' and 'abstained' in r['flags'])]
    if q: items=[r for r in items if q.lower() in (r['question']+' '+r['student']).lower()]
    return dict(items=items[offset:offset+limit],total=len(items),offset=offset,limit=limit,counts=dict(Counter(r['status'] for r in items)))


def find_row(ctx,turn_id):
    row=next((r for r in real_rows(ctx) if r['record']['id']==turn_id),None)
    if row is None: raise HTTPException(404,'没有找到该回合')
    return row


@router.get('/jobs/{key}/items/{turn_id}')
def detail(key:str,turn_id:str,x_workbench_token: str|None=Header(default=None)):
    ctx=context(key,x_workbench_token)
    return item(find_row(ctx,turn_id),ctx,verify=True)


class ConfirmationBody(BaseModel):
    dimension:str=Field(pattern='^(task|contribution)$')
    decision:str=Field(pattern='^(accepted|needs_review|insufficient_evidence)$')
    note:str=Field(default='',max_length=4000)
    operator:str=Field(default='内部体验者',max_length=100)


@router.post('/jobs/{key}/items/{turn_id}/confirmations')
def confirm(key:str,turn_id:str,body:ConfirmationBody,x_workbench_token: str|None=Header(default=None)):
    ctx=context(key,x_workbench_token); row=find_row(ctx,turn_id)
    verification=data.verify_record_source(row['record'])
    if not verification['ok']: raise HTTPException(409,'来源校验失败，暂不能保存人工确认')
    record=dict(id=uuid.uuid4().hex,job=key,turn_id=turn_id,created_at=time.time(),**body.model_dump(),
       source_sha256=data.digest(row['record']),model_output_sha256=data.digest({k:row.get(k) for k in ('independent','review','repeat','dimensions')}),
       experiment=ctx['plan'].get('experiment'),revision=ctx['plan'].get('revision'))
    data.runtime().mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(data.runtime()/'confirmations.sqlite3',timeout=30)) as db:
        db.execute('CREATE TABLE IF NOT EXISTS confirmations(sequence INTEGER PRIMARY KEY AUTOINCREMENT,job TEXT,turn_id TEXT,body TEXT)')
        db.execute('INSERT INTO confirmations(job,turn_id,body) VALUES(?,?,?)',(key,turn_id,json.dumps(record,ensure_ascii=False)))
        db.commit()
    return dict(saved=True,confirmation=record)


@router.get('/jobs/{key}/compare')
def compare(key:str,dimension:str=Query('task',pattern='^(task|contribution)$'),group:str=Query('independent',pattern='^(independent|random_audit|high_risk|repeat)$'),x_workbench_token: str|None=Header(default=None)):
    from .product_reports import compare
    return compare(context(key,x_workbench_token),dimension,group)


@router.get('/jobs/{key}/report')
def report(key:str,role:str=Query('teacher',pattern='^(student|teacher|administrator)$'),student:str='',term:str='',x_workbench_token: str|None=Header(default=None)):
    from .product_reports import report
    return report(context(key,x_workbench_token),role,student,term)


def usage(ctx):
    path=data.runtime()/'pool.sqlite3'; experiment=ctx['plan'].get('experiment')
    if ctx['job']['mode']=='frozen':
        return dict(source='frozen_summary',summary=data.read_json(Path(settings()['research_root'])/data.T3_PATH/'frozen/summary.json'),cost=None)
    if not path.exists() or ctx['job']['mode']=='synthetic': return dict(source='synthetic_cache' if ctx['job']['mode']=='synthetic' else 'not_started',calls=0,cost=None)
    with closing(sqlite3.connect(f'file:{path.as_posix()}?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        rows=db.execute('SELECT provider,model,COUNT(*) calls,SUM(input_tokens) input_tokens,SUM(output_tokens) output_tokens,SUM(CASE WHEN status="done" THEN 1 ELSE 0 END) successes,SUM(CASE WHEN input_tokens IS NULL OR output_tokens IS NULL THEN 1 ELSE 0 END) unknown_usage_attempts FROM attempts WHERE experiment=? GROUP BY provider,model',(experiment,)).fetchall()
        statuses=db.execute('SELECT status,error_kind,COUNT(*) count FROM attempts WHERE experiment=? GROUP BY status,error_kind',(experiment,)).fetchall()
    return dict(source='persistent_pool_ledger',experiment=experiment,models=[dict(r) for r in rows],states=[dict(r) for r in statuses],cost=None)


@router.get('/jobs/{key}/export')
def export(key:str,request:Request,role:str=Query('teacher',pattern='^(student|teacher|administrator)$'),student:str='',term:str='',x_workbench_token: str|None=Header(default=None)):
    from .product_reports import export_bundle
    ctx=context(key,x_workbench_token)
    ctx['metadata']={**ctx.get('metadata',{}),'application_url':os.getenv('WORKBENCH_PUBLIC_URL') or str(request.base_url)}
    return Response(export_bundle(ctx,usage(ctx),role=role,student=student,term=term),media_type='application/zip',headers={'Content-Disposition':f'attachment; filename="{data.safe_key(key)}.zip"'})


@router.get('/jobs/{key}/results.csv')
def csv_export(key:str,role:str=Query('teacher',pattern='^(student|teacher|administrator)$'),student:str='',term:str='',x_workbench_token: str|None=Header(default=None)):
    from .product_reports import results_csv, _export_context
    ctx=_export_context(context(key,x_workbench_token),role,student,term)[0]
    return Response(results_csv(ctx),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':f'attachment; filename="{data.safe_key(key)}.csv"'})


@router.get('/review-summary')
def review_summary(x_workbench_token: str|None=Header(default=None)):
    require(x_workbench_token)
    path=Path(settings()['research_root'])/'runtime/research/review.sqlite3'
    if not path.exists(): return dict(available=False,rounds=[])
    try:
        with closing(sqlite3.connect(f'file:{path.as_posix()}?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row
            rows=db.execute('SELECT round_id,mode,COUNT(*) assigned,SUM(submitted IS NOT NULL) completed FROM assignments GROUP BY round_id,mode ORDER BY MIN(rowid)').fetchall()
        return dict(available=True,rounds=[dict(r) for r in rows])
    except sqlite3.Error: return dict(available=False,rounds=[])


@router.get('/health')
def health():
    return dict(ok=True,product='e-workbench',version='product-four-model-v1',runtime_persistent=True)
