import React, {useEffect, useRef, useState} from 'react';
import {GraduationCap, School, Database, ArrowRight, KeyRound, LockOpen, Lock, Upload, FileSpreadsheet,
  Play, Pause, RotateCw, Check, Printer, Download, Package, Search, X} from 'lucide-react';
import {wb, session, download, LEVELS, levelName, pct, num, modeName, stateName, dimensionName} from './api';
import {Bars, Ranges, ErrorBox, Note, Gate, Stat, Tip} from './ui';
import Redteam from './Redteam';

export const PERSONAS = {
  student: {step: 1, icon: GraduationCap, name: '师范生', goal: '从原文理解任务要求与自己已展示的贡献', path: ['import', 'trace', 'report'], cta: '从样例开始学'},
  teacher: {step: 6, icon: School, name: '一线教师', goal: '查看所选批次的证据覆盖与教学建议', path: ['report'], cta: '查看分层报告'},
  engineer: {step: 3, icon: Database, name: '数据工程师 / 测评运营', goal: '导入数据、跑任务、查异常、导出复现包', path: ['import', 'quality', 'jobs', 'trace', 'compare', 'report', 'export'], cta: '进入完整流程'},
};
const FLAGS = {agreed: '已有候选', disagreement: '模型分歧', disagree: '模型分歧', abstained: '弃权 / 证据不足', abstain: '弃权 / 证据不足', uncertain: '尚不确定', unfinished: '未完成', not_run: '未运行', technical_failure: '技术失败', failed: '调用失败', high_risk: '高风险复核', reviewed: '独立复核', vs_constructed: '与构造标签不同', arbitrated: '历史仲裁'};
const DECISIONS = {accepted: '采纳', needs_review: '待复核', insufficient_evidence: '证据不足'};
const queryString = values => new URLSearchParams(Object.entries(values).filter(([, v]) => v !== '' && v != null)).toString();
const finalValue = value => typeof value === 'object' && value !== null ? value.label : value;
const displayValue = value => value == null ? '—' : typeof value === 'object' ? JSON.stringify(value) : String(value);
function useResource(path, revision = '') {
  const [result, setResult] = useState({path: '', data: null});
  const [error, setError] = useState('');
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    setError('');
    if (!path) return;
    const controller = new AbortController();
    wb(path, {signal: controller.signal}).then(value => { if (!controller.signal.aborted) setResult({path, data: value}); })
      .catch(e => { if (e.name !== 'AbortError') setError(e.message); });
    return () => controller.abort();
  }, [path, revision, refresh]);
  // Keep the same record mounted while refreshing its history; a new path never sees old data.
  return {data: result.path === path ? result.data : null, error, reload: () => setRefresh(v => v + 1)};
}
function NoJob({ctx, report = false}) {
  return <Gate title="当前批次还没有任务" text={report ? '先建立并运行任务，报告会使用这批资料的原文和模型结果。' : '任务、原文证据与结果始终跟随上方所选批次。'} action="去任务页" onAction={() => ctx.go('jobs')} />;
}
function Missing({values}) {
  if (!values || (Array.isArray(values) ? !values.length : !Object.keys(values).length)) return null;
  const rows = Array.isArray(values) ? values : Object.entries(values).map(([key, value]) => typeof value === 'object' ? {key, ...value} : {key, reason: value});
  return <Note tone="warn"><b>可用性与分母说明</b><ul className="wb-list">{rows.map((v, i) => <li key={v.key || i}>{typeof v === 'string' ? v : [v.key || v.metric, v.reason || v.why || v.message].filter(Boolean).join('：')}</li>)}</ul></Note>;
}

export function Home({ctx}) {
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [draft, setDraft] = useState('');
  async function start(key) {
    ctx.setPersona(key); setError('');
    if (key !== 'teacher') return ctx.go(PERSONAS[key].path[0]);
    try {
      setBusy('正在读取报告批次…');
      if (ctx.unlocked) await ctx.selectDataset('t3');
      else if (!ctx.job || ctx.job.state !== 'finished') {
        const data = await wb('/import/sample/demo-synthetic', {method: 'POST'});
        ctx.setDataset(data);
        const job = await wb('/jobs', {method: 'POST', body: {dataset: data.id}});
        ctx.setJob(await wb('/jobs/' + job.id + '/start', {method: 'POST'}));
      }
      ctx.go('report');
    } catch (e) { setError(e.message); } finally { setBusy(''); }
  }
  async function unlock(e) {
    e.preventDefault(); setBusy('正在解锁…'); setError('');
    session.setToken(draft.trim()); setDraft('');
    try { if (!(await ctx.checkUnlock())) { session.setToken(''); setError('口令不正确，仍显示合成教学示例。'); } }
    finally { setBusy(''); }
  }
  return <div className="wb-home">
    <section className="wb-hero"><p className="wb-eyebrow">赛道一 · AI 教育价值评价工具 · E 问应用探索</p>
      <h1>从学生与 AI 的提问记录，<br />到教师看得懂、别人复算得出的教学诊断</h1>
      <p>四个模型分工完成独立判断、独立复核与配对复跑；任务要求和学生贡献分开保留证据，分歧可回到原文，并形成可复算的分层报告。</p>
    </section>
    <h2 className="wb-section-title">你是谁？选一个身份开始</h2>
    <div className="wb-personas">{Object.entries(PERSONAS).map(([key, p]) => <button key={key} data-step={p.step} className={'wb-persona' + (ctx.persona === key ? ' on' : '')} onClick={() => start(key)} disabled={!!busy}>
      <p.icon size={28} /><b>{p.name}</b><span>{p.goal}</span><em>{p.cta}<ArrowRight size={15} /></em></button>)}</div>
    {busy && <p className="wb-busy" role="status"><RotateCw size={16} className="spin" />{busy}</p>}<ErrorBox error={error} />
    <h2 className="wb-section-title">一条完整的工作流</h2>
    <ol className="wb-flow">{[['导入资料', 'CSV / JSONL 或内置批次'], ['检查质量', '解析、缺字段与指标可用性'], ['启动 / 继续任务', '后台运行，暂停后可续跑'],
      ['追溯异常', '双维证据与人工确认'], ['比较模型与指标', '有效配对与集合分母'], ['分层报告', '学生、教师与管理者'], ['导出复现', 'ZIP + SHA-256 清单']]
      .map(([t, d], i) => <li key={t} data-step={i + 1}><span>{i + 1}</span><b>{t}</b><small>{d}</small></li>)}</ol>
    <div className="wb-modes"><div className={'wb-mode' + (!ctx.unlocked ? ' on' : '')}><h3>合成教学演示</h3><p>用构造样本学习判断流程，也可以体验指标与权重实验。</p></div>
      <div className={'wb-mode' + (ctx.unlocked ? ' on' : '')}><h3>{ctx.unlocked ? <LockOpen size={17} /> : <Lock size={17} />}内部工作台</h3>
        <p>解锁后默认打开正式 t3 成果，可导入新业务资料并启动真实模型任务。</p>
        {ctx.unlocked ? <button onClick={ctx.lock}>锁定工作台</button> : <form onSubmit={unlock} className="wb-inline-form"><input type="password" value={draft} onChange={e => setDraft(e.target.value)} placeholder="工作台口令" aria-label="工作台口令" autoComplete="off" /><button className="wb-primary" disabled={!draft.trim() || !!busy}><KeyRound size={15} />解锁</button></form>}
      </div></div>
    <Note>正式 t3 是冻结研究成果；新业务运行和人工确认单独保存。真实记录缺少完整 CTQ 与可比条件时，报告会说明原因。</Note>
  </div>;
}

export function Import({ctx}) {
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const [drag, setDrag] = useState(false);
  const [term, setTerm] = useState('');
  const [cohort, setCohort] = useState('');
  const input = useRef();
  async function pickSample(id) {
    setError(''); setBusy(id);
    try { await wb('/import/sample/' + encodeURIComponent(id), {method: 'POST'}); await ctx.selectDataset(id); ctx.go('quality'); }
    catch (e) { setError(e.message); } finally { setBusy(''); }
  }
  async function upload(file) {
    if (!file) return; setError('');
    if (!/\.(csv|jsonl)$/i.test(file.name)) return setError('请选择 UTF-8 编码的 CSV 或 JSONL 文件。');
    if (file.size > 6 * 1024 * 1024) return setError('文件超过 6 MB，请拆分后分批导入。');
    setBusy('upload');
    try {
      const text = await file.text();
      const data = await wb('/import/upload', {method: 'POST', body: {name: file.name, text, term: term.trim(), cohort: cohort.trim()}});
      ctx.setDataset(data); ctx.go('quality');
    } catch (e) { setError(e.message); } finally { setBusy(''); if (input.current) input.current.value = ''; }
  }
  return <><Tip persona={ctx.persona} show={['student']}>第一次用？选“合成教学样本”，对照构造标签练习；真实日志的标签是待人工核对的模型候选。</Tip>
    <div className="wb-grid-2"><section className="wb-card"><h2>用内置数据</h2><div className="wb-samples">
      {ctx.samples.map(s => <button key={s.id} className={'wb-sample' + (ctx.dataset?.id === s.id ? ' on' : '')} disabled={s.locked || !!busy} onClick={() => pickSample(s.id)}>
        <FileSpreadsheet size={22} /><div><b>{s.name}</b><span>{s.note}</span>{s.locked && <em><Lock size={13} />需要工作台口令（首页解锁）</em>}</div>
        {busy === s.id ? <RotateCw size={16} className="spin" /> : ctx.dataset?.id === s.id ? <Check size={18} /> : <ArrowRight size={16} />}</button>)}</div></section>
      <section className="wb-card"><h2>上传自己的导出文件</h2>
        <div className="wb-form wb-import-fields"><label>补充学期（仅用于缺失值）<input type="text" value={term} onChange={e => setTerm(e.target.value)} placeholder="如 2025-2026-1" /></label><label>补充分组（仅用于缺失值）<input type="text" value={cohort} onChange={e => setCohort(e.target.value)} placeholder="如课程或教学小组" /></label></div>
        <div className={'wb-drop' + (drag ? ' drag' : '')} onDragOver={e => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)} onDrop={e => { e.preventDefault(); setDrag(false); if (!busy && ctx.unlocked) upload(e.dataTransfer.files[0]); }}>
          <Upload size={30} /><p>把 CSV / JSONL 拖到这里，或</p><button className="wb-primary" onClick={() => input.current.click()} disabled={!!busy || !ctx.unlocked}>{busy === 'upload' ? '正在读取…' : ctx.unlocked ? '选择文件' : '请先在首页解锁'}</button>
          <input ref={input} type="file" accept=".csv,.jsonl,text/csv,application/x-ndjson" hidden onChange={e => upload(e.target.files[0])} /></div>
        <h3>兼容中文列名和标准字段</h3><table className="wb-table compact"><thead><tr><th>字段</th><th>用途</th></tr></thead><tbody>
          <tr><td>学号 / student</td><td>按学生汇总</td></tr><tr><td>问答记录 / question</td><td>完整解析学生回合，保留原文位置</td></tr>
          <tr><td>问题建立时间 / timestamp</td><td>时间与顺序核对</td></tr><tr><td>智能体类型 / agent</td><td>工具信息与 MAB 可用性</td></tr>
          <tr><td>学期 / term、分组 / cohort</td><td>报告范围与学生×学期单位</td></tr><tr><td>会话 / session</td><td>保留会话线索，仍需验证相邻关系</td></tr></tbody></table>
        <p className="wb-muted">原文件、解析结果和新任务单独保存在产品运行目录。</p>
      </section></div><ErrorBox error={error} /></>;
}

export function Quality({ctx}) {
  const [open, setOpen] = useState('');
  const d = ctx.dataset;
  if (!d) return <Gate title="还没有导入资料" text="先选择内置批次或上传资料，再查看解析结果。" action="去导入资料" onAction={() => ctx.go('import')} />;
  const q = d.quality;
  if (!q) return <Note>正在读取质量信息。</Note>;
  const checks = q.checks || [];
  const dropped = q.dropped_rows ?? 0;
  return <><div className="wb-stats"><Stat label="原始行数" value={q.raw_rows} /><Stat label="可用学生回合" value={q.usable} tone="good" />
    <Stat label="剔除源行" value={dropped} tone={dropped ? 'warn' : ''} hint="一行问答可解析出多个回合" /><Stat label="学生数" value={q.students} /></div>
    <div className="wb-grid-2"><section className="wb-card"><h2>逐项检查</h2>{!checks.length && <p className="wb-ok"><Check size={16} />没有需要报告的解析问题。</p>}
      <ul className="wb-checks">{checks.map(c => <li key={c.key} className={c.count ? c.severity : 'pass'}><button onClick={() => setOpen(open === c.key ? '' : c.key)} disabled={!c.examples?.length} aria-expanded={open === c.key}>
        <span className="dot" />{c.label}<b>{c.count ? c.count + ' 行' : '通过'}</b></button>{open === c.key && <ul className="wb-examples">{c.examples.map((x, i) => <li key={i}>第 {x.row} 行：{x.reason}</li>)}</ul>}</li>)}</ul>
      <p className="wb-muted">质量检查对应源文件位置；“可用学生回合”可能大于源行数。</p></section>
      <section className="wb-card"><h2>哪些指标能算</h2><table className="wb-table"><thead><tr><th>指标</th><th>状态</th><th>原因</th></tr></thead><tbody>
        {Object.entries(q.identifiable || {}).map(([k, v]) => <tr key={k}><td><b>{k}</b></td><td><span className={'wb-pill ' + (v.ok ? 'good' : 'warn')}>{v.ok ? '可计算' : '不可判断'}</span></td><td>{v.why}</td></tr>)}</tbody></table>
        {!!q.boundaries?.length && <><h3>数据边界</h3><ul className="wb-list">{q.boundaries.map(b => <li key={b}>{b}</li>)}</ul></>}
        <Tip persona={ctx.persona} show={['student', 'engineer']}>缺字段的指标保留为空，不能按 0 分对待。</Tip></section></div>
    <div className="wb-next"><span>数据集：{d.name}</span><button className="wb-primary" onClick={() => ctx.go('jobs')} disabled={!q.usable}>{ctx.job ? '查看这批数据的任务' : '用这批数据建任务'}<ArrowRight size={16} /></button></div></>;
}

export function Jobs({ctx}) {
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const active = useRef(true);
  const job = ctx.job;
  useEffect(() => { active.current = true; ctx.refreshJobs().catch(e => { if (active.current) setError(e.message); }); return () => { active.current = false; }; }, [ctx.refreshJobs]);
  async function create() {
    setError(''); setBusy('create');
    try {
      const next = await wb('/jobs', {method: 'POST', body: {dataset: ctx.dataset.id}});
      if (active.current) ctx.setJob(next);
      await ctx.refreshJobs();
    } catch (e) { if (active.current) setError(e.message); } finally { if (active.current) setBusy(''); }
  }
  async function action(verb) {
    setError(''); setBusy(verb);
    try { const next = await wb('/jobs/' + job.id + '/' + verb, {method: 'POST'}); if (active.current) ctx.setJob(next); }
    catch (e) { if (active.current) setError(e.message); } finally { if (active.current) setBusy(''); }
  }
  const progress = job ? Math.min(1, job.cursor / Math.max(job.total, 1)) : 0;
  const can = job?.capabilities || {};
  const synthetic = job?.mode === 'synthetic';
  const controlTasks = job?.controls?.tasks;
  return <>{!ctx.dataset && !job && <NoJob ctx={ctx} />}
    {ctx.dataset && !job && <section className="wb-card wb-create"><div><h2>新建测评任务</h2><p>数据集：<b>{ctx.dataset.name}</b>，{ctx.dataset.quality?.usable} 个学生回合。</p></div><button className="wb-primary" onClick={create} disabled={!!busy || !ctx.dataset.quality?.usable}><Play size={16} />{busy ? '正在建立…' : '新建任务'}</button></section>}
    {job && <section className="wb-card"><div className="wb-job-head"><div><h2>{job.dataset_name}</h2><p className="wb-muted">任务 {job.id} · {modeName(job.mode)} · {stateName(job.state)}</p></div>
      <div className="wb-actions">{can.start && <button className="wb-primary" onClick={() => action('start')} disabled={!!busy}><Play size={16} />开始</button>}{can.resume && <button className="wb-primary" onClick={() => action('resume')} disabled={!!busy}><Play size={16} />继续</button>}
        {can.pause && <button onClick={() => action('pause')} disabled={!!busy}><Pause size={16} />暂停</button>}{job.state === 'finished' && <button className="wb-primary" onClick={() => ctx.go('trace')}>查看证据<ArrowRight size={16} /></button>}</div></div>
      <div className="wb-progress" role="progressbar" aria-valuenow={Math.round(progress * 100)} aria-valuemin="0" aria-valuemax="100"><span style={{width: pct(progress)}} /></div><p className="wb-muted">{job.cursor} / {job.total} 个学生回合已结算{!synthetic && <>；工程控制题 {job.controls?.completed ?? 0} / {job.controls?.total ?? 0} 单列</>}。</p>
      {synthetic ? <><ol className="wb-stages"><li><b>① 三模型独立判断缓存</b><span>{job.stages?.independent ?? 0} 条缓存结果</span></li><li><b>② 历史互评缓存</b><span>{job.stages?.review ?? 0} 条缓存结果</span></li><li><b>③ 构造样本对照</b><span>与合成标签对照学习</span></li></ol><Note>本任务回放既有合成教学缓存。旧缓存没有完整学生贡献结果，贡献维度保留缺失；本次回放不调用模型 API。</Note></> : <ol className="wb-stages"><li><b>① 两快模型独立判断</b><span>{job.stages?.independent ?? 0} 个请求结算</span></li><li><b>② 两强模型独立复核</b><span>{job.stages?.review ?? 0} 个请求结算</span></li><li><b>③ 配对复跑</b><span>{job.stages?.repeat ?? 0} 个请求结算</span></li></ol>}
      {controlTasks && <p className="wb-muted">控制题模型请求：成功 {controlTasks.succeeded ?? 0} / {controlTasks.total ?? 0} · 在途 {controlTasks.running ?? 0} · 待运行 {controlTasks.queued ?? 0} · 失败 {controlTasks.failed ?? 0}。</p>}
      <div className="wb-stats"><Stat label="完成回合" value={job.done} tone="good" /><Stat label="存在分歧" value={job.disagree} tone={job.disagree ? 'warn' : ''} /><Stat label="弃权 / 证据不足" value={job.abstain} /><Stat label="尚未运行" value={job.not_run} /><Stat label="技术失败回合" value={job.failed ?? 0} tone={job.failed ? 'warn' : ''} /></div>
      {job.requests && <p className="wb-muted">模型请求：成功 {job.requests.done ?? 0} · 失败 {job.requests.failed ?? 0} · 待运行 {job.requests.queued ?? 0} · 在途 {job.requests.running ?? 0}。最终回合状态与单次模型请求分别统计。</p>}
      {job.reason && <Note tone={['blocked', 'failed'].includes(job.state) ? 'warn' : 'info'}>{job.reason}</Note>}
      {job.state === 'pausing' && <Note>已停止领取新请求；在途调用结算后即可继续。</Note>}
      {synthetic && job.state === 'ready' ? <p className="wb-muted">开始回放后，按缓存条目展示实际模型名称。</p> : !!job.models?.length && <p className="wb-muted">{synthetic ? '缓存模型' : '本任务模型'}：{job.models.map(m => m.id || m.model).join(' · ')}</p>}
      {job.limits && <details className="wb-log"><summary>本批次预算与限制</summary><pre className="wb-json">{JSON.stringify(job.limits, null, 2)}</pre></details>}
      <details className="wb-log"><summary>运行日志（{job.log?.length ?? 0}）</summary><ul>{[...(job.log || [])].reverse().map((l, i) => <li key={i}><time>{l.t ? new Date(typeof l.t === 'number' ? l.t * 1000 : l.t).toLocaleTimeString() : ''}</time>{l.msg}</li>)}</ul></details>
    </section>}<ErrorBox error={error} />
    {!!ctx.jobs.length && <section className="wb-card"><h2>最近的任务</h2><div className="wb-scroll"><table className="wb-table"><thead><tr><th>任务</th><th>数据集</th><th>进度</th><th>状态</th><th /></tr></thead><tbody>
      {ctx.jobs.map(j => <tr key={j.id} className={job?.id === j.id ? 'selected' : ''}><td>{j.id}</td><td>{j.dataset_name}<small>{modeName(j.mode)}</small></td><td>{j.cursor}/{j.total}</td><td>{stateName(j.state)}</td><td><button onClick={() => ctx.selectJob(j).catch(e => setError(e.message))}>打开</button></td></tr>)}</tbody></table></div>
      <p className="wb-muted">运行由后台进程持续执行，关闭页面不影响任务。服务重启后，未完成任务可从持久账本继续。</p></section>}</>;
}

export function Trace({ctx}) {
  const [dimension, setDimension] = useState(ctx.traceDimension || 'task');
  const [stage, setStage] = useState('all');
  const [flag, setFlag] = useState('');
  const [query, setQuery] = useState('');
  const [search, setSearch] = useState('');
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState(ctx.traceTarget || '');
  const job = ctx.job;
  useEffect(() => { const timer = setTimeout(() => { setSearch(query); setOffset(0); }, 250); return () => clearTimeout(timer); }, [query]);
  const {data, error} = useResource(job ? '/jobs/' + job.id + '/anomalies?' + queryString({dimension, stage, flag, q: search, offset, limit: 30}) : '', job?.cursor);
  useEffect(() => { if (ctx.traceTarget) setSelected(ctx.traceTarget); }, [ctx.traceTarget]);
  useEffect(() => { setDimension(ctx.traceDimension || 'task'); }, [ctx.traceDimension]);
  useEffect(() => { if (data && !(ctx.traceTarget && selected === ctx.traceTarget) && !data.items.some(i => i.id === selected)) setSelected(data.items[0]?.id || ''); }, [data]);
  const detail = useResource(job && selected ? '/jobs/' + job.id + '/items/' + encodeURIComponent(selected) : '', job?.cursor);
  const filter = (set, value) => { set(value); setOffset(0); setSelected(''); };
  if (!job) return <NoJob ctx={ctx} />;
  const flags = {...FLAGS, ...data?.flags};
  return <><Tip persona={ctx.persona} show={['student']}>先根据原文判断“要求完成什么”和“学生展示了什么”，再核对各模型的依据。</Tip>
    <div className="wb-filter-bar wb-form"><label>判断维度<select value={dimension} onChange={e => filter(setDimension, e.target.value)}><option value="task">任务要求</option><option value="contribution">学生贡献</option></select></label>
      <label>模型阶段<select value={stage} onChange={e => filter(setStage, e.target.value)}><option value="all">全部阶段</option><option value="independent">基础独立判断</option><option value="review">独立复核</option><option value="repeat">配对复跑</option></select></label></div>
    <div className="wb-chips" role="group" aria-label="按异常类型筛选"><button className={!flag ? 'on' : ''} onClick={() => filter(setFlag, '')}>全部回合</button>
      {Object.entries(data?.counts || {}).filter(([k, n]) => n > 0 && flags[k]).map(([k, n]) => <button key={k} className={flag === k ? 'on' : ''} onClick={() => filter(setFlag, k)}>{flags[k]} {n}</button>)}</div>
    <ErrorBox error={error} />
    <div className="wb-split"><section className="wb-card flush"><label className="wb-search"><Search size={16} /><input value={query} onChange={e => setQuery(e.target.value)} placeholder="搜索提问或学生编号" aria-label="搜索异常提问" />{query && <button onClick={() => setQuery('')} aria-label="清除"><X size={14} /></button>}</label>
      {!data ? <p className="wb-muted pad">正在读取当前范围…</p> : <><ul className="wb-rows">{data.items.map(i => <li key={i.id}><button className={selected === i.id ? 'on' : ''} onClick={() => setSelected(i.id)}>
        <span className="wb-row-top"><b>{i.student}</b><small>{i.term}</small></span><span className="wb-row-q">{i.question}</span><span className="wb-row-flags">{(i.flags || []).map(f => <em key={f} className={'f-' + f}>{flags[f] || f}</em>)}</span>
      </button></li>)}</ul><p className="wb-muted pad">匹配 {data.total} 条 · 第 {data.total ? offset + 1 : 0}–{Math.min(offset + data.items.length, data.total)} 条</p>
      <div className="wb-pagination"><button disabled={!offset} onClick={() => setOffset(Math.max(0, offset - 30))}>上一页</button><button disabled={offset + 30 >= data.total} onClick={() => setOffset(offset + 30)}>下一页</button></div></>}
    </section><div><ErrorBox error={detail.error} />{detail.data ? <TraceDetail key={detail.data.id} item={detail.data} dimension={dimension} stage={stage} ctx={ctx} onSaved={detail.reload} /> : <section className="wb-card"><p className="wb-muted">{selected ? '正在读取原文和模型依据…' : '选择一个回合查看原文与依据。'}</p></section>}</div></div></>;
}

function TraceDetail({item, dimension, stage, ctx, onSaved}) {
  const [operator, setOperator] = useState(() => session.get('operator'));
  const [decision, setDecision] = useState('needs_review');
  const [note, setNote] = useState('');
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const stages = [['independent', '基础独立判断', item.independent], ['review', '独立复核', item.review], ['repeat', '配对复跑', item.repeat]].filter(([key]) => stage === 'all' || key === stage);
  const task = finalValue(item.dimensions?.task?.final) ?? finalValue(item.final);
  const contribution = finalValue(item.dimensions?.contribution?.final) ?? finalValue(item.contribution);
  const verification = item.source_verification;
  async function submit(e) {
    e.preventDefault(); setError(''); setSaved(false); setBusy(true);
    try {
      await wb('/jobs/' + ctx.job.id + '/items/' + encodeURIComponent(item.id) + '/confirmations', {method: 'POST', body: {dimension, decision, note: note.trim(), operator: operator.trim()}});
      session.set('operator', operator.trim()); setSaved(true); setNote(''); onSaved();
    } catch (e) { setError(e.message); } finally { setBusy(false); }
  }
  return <section className="wb-card"><h2>原文 · {item.student}</h2><p className="wb-muted">{item.term} · {item.id}</p><blockquote className="wb-quote">{item.question}</blockquote>
    <div className="wb-verdict"><Stat label="任务要求候选" value={levelName(task)} tone={task == null ? 'warn' : 'good'} /><Stat label="学生贡献候选" value={levelName(contribution)} tone={contribution == null ? 'warn' : 'good'} /></div>
    {item.constructed != null && <p className="wb-muted">合成构造标签：{levelName(item.constructed)}</p>}
    <details className="wb-log"><summary>来源定位与校验 · {verification?.ok === true ? '已通过' : verification?.ok === false ? '未通过' : '查看校验状态'}</summary>
      <pre className="wb-json">{JSON.stringify({source: item.source, verification}, null, 2)}</pre></details>
    {item.status === 'not_run' && <Note>该回合尚未运行，当前没有模型候选。</Note>}
    <h3>{dimensionName(dimension)} · 模型依据</h3>
    {stages.map(([key, name, list]) => <div key={key}><h3>{name}</h3>{!list?.length ? <p className="wb-muted">此回合没有该阶段的结果。</p> : list.map((model, i) => {
      const j = model.judgment || model;
      const level = dimension === 'task' ? j.level : j.contribution_level;
      const evidence = dimension === 'task' ? j.evidence : j.contribution_evidence;
      return <details key={(model.model || model.id) + ':' + i} className="wb-reason"><summary><b>{model.model || model.id || model.family}</b> · {levelName(level)} <small>{model.status}</small></summary>
        {!!evidence && <p><small>引用证据</small>{displayValue(evidence)}</p>}<p>{j.reason || '此条结果未提供理由。'}</p>{model.attempts != null && <p className="wb-muted">尝试次数：{Array.isArray(model.attempts) ? model.attempts.length : model.attempts}</p>}</details>;
    })}</div>)}
    <h3>人工确认 · {dimensionName(dimension)}</h3>
    {ctx.unlocked ? <form className="wb-form" onSubmit={submit}><label>处理意见<select value={decision} onChange={e => setDecision(e.target.value)}>{Object.entries(DECISIONS).map(([key, name]) => <option key={key} value={key}>{name}</option>)}</select></label>
      <label>确认人<input type="text" value={operator} onChange={e => setOperator(e.target.value)} placeholder="姓名或内部代号" required maxLength={80} /></label>
      <label>依据 / 备注<textarea value={note} onChange={e => setNote(e.target.value)} placeholder="记录采纳依据或需要补充的证据" rows={3} maxLength={4000} /></label>
      <button className="wb-primary" disabled={busy || !operator.trim()}>{busy ? '正在保存…' : '保存人工确认'}</button></form> : <p className="wb-muted">解锁工作台后可保存人工确认。</p>}
    {saved && <p className="wb-ok"><Check size={16} />已保存独立确认记录。</p>}<ErrorBox error={error} />
    {!!item.confirmations?.length && <details className="wb-log" open><summary>确认历史（{item.confirmations.length}）</summary><ul>{[...item.confirmations].reverse().map((c, i) => <li key={c.id || i}><div><b>{dimensionName(c.dimension)} · {DECISIONS[c.decision] || c.decision}</b><p>{c.note || '无备注'}<br /><small>{c.operator} · {displayValue(c.created_at || c.timestamp || c.t)}</small></p></div></li>)}</ul></details>}
    <p className="wb-muted">人工意见保留修改历史并关联当前证据版本，报告中与模型候选分别展示。</p>
  </section>;
}

export function Compare({ctx}) {
  const [tab, setTab] = useState('models');
  const [dimension, setDimension] = useState('task');
  const [group, setGroup] = useState('independent');
  const job = ctx.job;
  const synthetic = job?.mode === 'synthetic';
  const {data, error} = useResource(job ? '/jobs/' + job.id + '/compare?' + queryString({dimension, group}) : '', job?.cursor);
  const tabs = synthetic ? [['models', '模型一致性'], ['metrics', 'AIV 指标实验'], ['robust', '权重稳健性'], ['redteam', '抗操纵检验']] : [['models', '模型一致性']];
  if (!job) return <NoJob ctx={ctx} />;
  return <><div className="wb-tabs" role="tablist">{tabs.map(([k, name]) => <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? 'on' : ''} onClick={() => setTab(k)}>{name}</button>)}</div>
    {tab === 'models' && <div className="wb-filter-bar wb-form"><label>判断维度<select value={dimension} onChange={e => setDimension(e.target.value)}><option value="task">任务要求</option><option value="contribution">学生贡献</option></select></label><label>比较集合<select value={group} onChange={e => setGroup(e.target.value)}><option value="independent">基础独立判断</option><option value="random_audit">随机复核</option><option value="high_risk">高风险复核</option><option value="repeat">配对复跑</option></select></label></div>}
    <ErrorBox error={error} />{tab === 'redteam' ? <Redteam /> : !data ? <p className="wb-muted">正在计算当前批次…</p> : tab === 'models' ? <Models data={data} synthetic={synthetic} /> : tab === 'metrics' ? <MetricLab key={job.id} data={data} job={job} /> : <Robust data={data} />}
  </>;
}
function Models({data, synthetic}) {
  return <><div className="wb-stats"><Stat label="当前批次回合" value={data.total} /><Stat label="本集合分母" value={data.denominator} /><Stat label="有效候选" value={data.labelled} tone="good" />
    {(data.pairs || []).map(p => <Stat key={p.a + ':' + p.b} label={p.a + ' × ' + p.b} value={pct(p.agreement)} hint={p.n + ' 个双侧有效配对'} />)}</div>
    <div className="wb-grid-2"><section className="wb-card"><Bars title={dimensionName(data.dimension) + ' · 各模型层级分布'} labels={LEVELS.map((n, i) => 'L' + (i + 1))} series={(data.models || []).map(m => ({name: m.family || m.model, data: m.distribution}))} height={280} />
      <Bars title="最终候选分布" labels={LEVELS.map((n, i) => 'L' + (i + 1) + ' ' + n)} series={[{name: '回合数', data: data.final_distribution || [0, 0, 0, 0, 0, 0]}]} height={210} /></section>
      <section className="wb-card"><h2>逐模型 · 当前比较集合</h2><div className="wb-scroll"><table className="wb-table"><thead><tr><th>模型</th><th>有效</th><th>弃权</th><th>失败</th><th>待运行</th></tr></thead><tbody>
        {(data.models || []).map(m => <tr key={m.model}><td><b>{m.family || m.model}</b><small>{m.model}</small></td><td>{m.answered}</td><td>{m.abstained ?? '—'}</td><td>{m.failed ?? '—'}</td><td>{m.pending ?? '—'}</td></tr>)}</tbody></table></div>
        <h3>配对分母与缺失</h3><div className="wb-scroll"><table className="wb-table compact"><thead><tr><th>配对</th><th>计划</th><th>双侧有效</th><th>单侧弃权</th><th>双侧弃权</th></tr></thead><tbody>{(data.pairs || []).map(p => <tr key={p.a + p.b}><td>{p.a}<br />{p.b}</td><td>{p.planned_pairs ?? '—'}</td><td>{p.both_valid ?? p.n}</td><td>{p.one_abstained ?? '—'}</td><td>{p.both_abstained ?? '—'}</td></tr>)}</tbody></table></div>
      </section></div><Missing values={data.missing} />
    <p className="wb-muted">{synthetic ? '合成样本用于演示计算流程；缓存结果的一致率不代表真实数据上的准确率。' : '控制题单列，不计入真实学生回合。随机复核、高风险复核与复跑各用各自有效分母；模型一致不代表判断正确。'}</p></>;
}

const SCHEMES = {balanced: '均衡型', higher_order: '高阶优先型', process: '过程优先型', custom: '自定义'};
function MetricLab({data, job}) {
  const [student, setStudent] = useState(data.students[0]?.student);
  const [scheme, setScheme] = useState('balanced');
  const [custom, setCustom] = useState('0.2, 0.2, 0.2, 0.2, 0.2');
  const [noise, setNoise] = useState(0.15);
  const [asym, setAsym] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState('');
  const weights = scheme === 'custom' ? custom.split(/[,，\s]+/).filter(Boolean).map(Number) : data.weights[scheme];
  useEffect(() => {
    if (!student) return;
    const controller = new AbortController();
    setError('');
    wb(`/jobs/${job.id}/metrics`, {method: 'POST', signal: controller.signal, body: {student, weights, uncertainty: noise, asymmetric: asym}})
      .then(setResult).catch(e => { if (e.name !== 'AbortError') { setError(e.message); setResult(null); } });
    return () => controller.abort();
  }, [student, JSON.stringify(weights), noise, asym]);
  return <div className="wb-grid-2">
    <section className="wb-card">
      <h2>调参数，看分数怎么变</h2>
      <div className="wb-form">
        <label>学生<select value={student} onChange={e => setStudent(e.target.value)}>{data.students.map(s => <option key={s.student}>{s.student}</option>)}</select></label>
        <label>权重方案<select value={scheme} onChange={e => setScheme(e.target.value)}>{Object.entries(SCHEMES).map(([k, n]) => <option key={k} value={k}>{n}</option>)}</select></label>
        {scheme === 'custom' && <label>ABL, HOT, CTQ, DHI, MAB 五个权重（和为 1）<input value={custom} onChange={e => setCustom(e.target.value)} /></label>}
        <label>标签扰动概率 {Math.round(noise * 100)}%<input type="range" min="0" max="0.5" step="0.05" value={noise} onChange={e => setNoise(+e.target.value)} /></label>
        <label className="wb-check"><input type="checkbox" checked={asym} onChange={e => setAsym(e.target.checked)} />非对称 DHI（低阶过多罚得更重）</label>
      </div>
      <p className="wb-muted">权重：{weights.map(w => Number.isFinite(w) ? Math.round(w * 100) + '%' : '?').join(' / ')}。可以试着输入和不为 1 或带负数的权重，看系统如何拒绝。</p>
    </section>
    <section className="wb-card">
      <ErrorBox error={error} />
      {result && <>
        <div className="wb-score"><span>AIV</span><b>{num(result.score, 1)}</b><small>/ 100 · {result.n} 条提问</small></div>
        {result.interval && <p className="wb-muted">标签随机扰动 200 次后的 2.5%–97.5% 范围：{num(result.interval[0], 1)} – {num(result.interval[2], 1)}。这是敏感性分析，不是已验证的置信区间。</p>}
        {result.missing.length > 0 && <Note tone="warn">{result.missing.join('、')} 缺少数据，综合分不可判断；把这些指标的权重设为 0 可以得到部分分数。</Note>}
        <Bars labels={['ABL', 'HOT', 'CTQ', 'DHI', 'MAB']} series={[{name: '0–1', data: ['ABL', 'HOT', 'CTQ', 'DHI', 'MAB'].map(k => result.metrics[k])}]} title="五个子指标（0–1）" height={220} />
      </>}
    </section>
  </div>;
}

function Robust({data}) {
  const rows = data.sensitivity.map(s => ({label: s.student, low: s.score_low, high: s.score_high,
    mid: data.students.find(x => x.student === s.student)?.balanced})).sort((a, b) => (b.mid ?? 0) - (a.mid ?? 0));
  const ranks = data.sensitivity.map(s => ({label: s.student, low: s.rank_low, high: s.rank_high})).sort((a, b) => a.low - b.low);
  const unstable = ranks.filter(r => r.high - r.low >= 3).length;
  return <>
    <Note>每个权重在 ±10% 内随机扰动 200 次后重新归一化，看分数和排名能动多少。排名跨度大的学生，不应该据此排先后。</Note>
    <div className="wb-stats"><Stat label="学生数" value={rows.length} /><Stat label="排名可能变动 ≥3 位" value={unstable} tone={unstable ? 'warn' : 'good'} /></div>
    <div className="wb-grid-2">
      <section className="wb-card"><Ranges title="均衡型 AIV 在权重 ±10% 下的范围" rows={rows} height={Math.max(260, rows.length * 22)} /></section>
      <section className="wb-card"><Ranges title="排名范围（1 = 最高）" rows={ranks} height={Math.max(260, ranks.length * 22)} unit=" 名" /></section>
    </div>
  </>;
}



export function Report({ctx}) {
  const role = ctx.reportRole;
  const setRole = ctx.setReportRole;
  const {student, term} = ctx.reportScope;
  const setStudent = value => ctx.setReportScope(previous => ({...previous, student: value}));
  const setTerm = value => ctx.setReportScope(previous => ({...previous, term: value}));
  const [options, setOptions] = useState({students: [], terms: []});
  const job = ctx.job;
  const {data, error} = useResource(job ? '/jobs/' + job.id + '/report?' + queryString({role, student, term}) : '', job?.cursor);
  useEffect(() => { if (data?.options) setOptions(data.options); if (role === 'student' && !student && data?.scope?.student) setStudent(data.scope.student); }, [data, role, student]);
  if (!job) return <NoJob ctx={ctx} report />;
  const names = {student: '学生证据简报', teacher: '教师教学诊断简报', administrator: '管理者校准简报'};
  const controls = <div className="wb-filter-bar wb-form no-print">
    <label>报告视角<select value={role} onChange={e => setRole(e.target.value)}><option value="student">学生</option><option value="teacher">教师</option><option value="administrator">管理者</option></select></label>
    <label>学期范围<select value={term} onChange={e => { setTerm(e.target.value); setStudent(''); }}><option value="">全部学期</option>{options.terms.map(t => <option key={t} value={t}>{t}</option>)}</select></label>
    <label>学生范围<select value={student} onChange={e => setStudent(e.target.value)}><option value="">所选批次</option>{options.students.map(s => <option key={s} value={s}>{s}</option>)}</select></label>
  </div>;
  if (error) return <>{controls}<ErrorBox error={error} /></>;
  if (!data) return <>{controls}<p className="wb-muted">正在生成当前范围的报告…</p></>;
  if (!data.ready) return <>{controls}<Note tone="warn">{data.message || '该范围还没有可汇总的模型结果；可返回任务页查看运行进度。'}</Note></>;
  const summary = data.summary || {};
  return <>{controls}<article className="wb-report">
    <div className="wb-report-bar no-print"><span className="wb-muted">当前范围：{data.scope?.description || '所选批次'} · {modeName(job.mode)}</span><button onClick={() => window.print()}><Printer size={16} />打印 / 存 PDF</button></div>
    <header><p className="wb-eyebrow">{names[role]}</p><h2>{data.dataset}</h2><p className="wb-muted">生成于 {data.generated} · {data.metadata?.version || '当前结果版本'} · {modeName(job.mode)}</p></header>
    <p className="wb-headline">{data.headline}</p>
    <div className="wb-stats"><Stat label="真实回合 / 示例回合" value={summary.total} /><Stat label="任务要求候选" value={summary.task_candidates} tone="good" hint={'覆盖 ' + pct(summary.coverage)} />
      <Stat label="学生贡献候选" value={summary.contribution_candidates} tone="good" hint={'覆盖 ' + pct(summary.contribution_coverage)} /><Stat label="学生 × 学期单位" value={summary.student_terms} /></div>
    <div className="wb-grid-2"><section><h3>所选范围 · 双维候选分布</h3><Bars labels={LEVELS.map((n, i) => 'L' + (i + 1) + ' ' + n)} series={[{name: '任务要求', data: data.distributions?.task || []}, {name: '学生贡献', data: data.distributions?.contribution || []}]} height={240} /></section>
      <section><h3>下一步可以做什么</h3><ol className="wb-actions-list">{(data.actions || []).map((a, i) => <li key={i}><div><b>{typeof a === 'string' ? a : a.text}</b>
        {a.condition && <p className="wb-muted">适用条件：{a.condition}</p>}{a.reconsider && <p className="wb-muted">重新判断：{a.reconsider}</p>}
        {a.evidence?.map(e => <button key={e.id} className="wb-evidence-link no-print" onClick={() => ctx.openTrace(e.id)}>核对 {e.student || e.id} 的原文<ArrowRight size={13} /></button>)}</div></li>)}</ol></section></div>
    {role === 'administrator' && <><h3>跨范围比较前的条件检查</h3><div className="wb-scroll"><table className="wb-table"><thead><tr><th>可比条件</th><th>状态</th><th>依据 / 缺失</th><th>补采与校准</th></tr></thead><tbody>
      {(data.comparability || []).map(c => <tr key={c.key}><td>{c.label}</td><td>{displayValue(c.status)}</td><td>{c.reason}</td><td>{c.next_step}</td></tr>)}</tbody></table></div></>}
    {role === 'student' && <><h3>原文、任务要求与已展示贡献</h3>{!student && <p className="wb-muted">当前展示批次中的证据示例；选定学生可缩小范围。</p>}
      {(data.evidence || []).slice(0, 12).map(e => <section key={e.id} className="wb-evidence-card"><p><b>{e.student}</b> · {e.term}</p><blockquote className="wb-quote">{e.question}</blockquote>
        <p>任务要求：{levelName(finalValue(e.task))} · 学生贡献：{levelName(finalValue(e.contribution))}</p>
        <p className="wb-muted">任务状态：{e.task_status}；贡献状态：{e.contribution_status}</p>{e.contribution_evidence && <p className="wb-muted">贡献依据：{displayValue(e.contribution_evidence)}</p>}
        <button onClick={() => ctx.openTrace(e.id)} className="no-print">回查模型与原文<ArrowRight size={14} /></button></section>)}</>}
    <h3>{role === 'teacher' ? '需要关注的学生 × 学期单位' : '所选范围的证据覆盖'}</h3>
    <ReportRows rows={role === 'teacher' ? data.attention || [] : (data.rows || []).slice(0, 20)} synthetic={job.mode === 'synthetic'} />
    <details className="wb-log"><summary>全部 {data.rows?.length || 0} 个学生 × 学期单位</summary><ReportRows rows={data.rows || []} synthetic={job.mode === 'synthetic'} /></details>
    <h3>人工处理意见</h3><p className="wb-muted">本范围 {data.confirmations?.length || 0} 条独立确认记录；模型候选保持原始版本。</p>
    {!!data.confirmations?.length && <div className="wb-scroll"><table className="wb-table compact"><thead><tr><th>回合</th><th>维度</th><th>人工意见</th><th>确认人 / 备注</th></tr></thead><tbody>
      {data.confirmations.map((c, i) => <tr key={c.id || i}><td><button className="wb-evidence-link" onClick={() => ctx.openTrace(c.turn_id || c.item_id)}>{c.turn_id || c.item_id || '原文'}</button></td><td>{dimensionName(c.dimension)}</td><td>{DECISIONS[c.decision] || c.decision}</td><td>{c.operator}<small>{c.note}</small></td></tr>)}</tbody></table></div>}
    <h3>这份报告的使用边界</h3><ul className="wb-list">{(data.limits || []).map(l => <li key={l}>{l}</li>)}</ul>
    <details className="wb-log"><summary>报告版本与来源</summary><pre className="wb-json">{JSON.stringify(data.metadata, null, 2)}</pre></details>
  </article></>;
}
function ReportRows({rows, synthetic}) {
  return <div className="wb-scroll"><table className="wb-table"><thead><tr><th>学生 / 学期</th><th>回合</th><th>任务候选</th><th>贡献候选</th><th>高阶占比</th><th>{synthetic ? 'AIV 示例' : '指标可用性'}</th><th>建议</th></tr></thead><tbody>
    {rows.map((s, i) => <tr key={s.student + ':' + s.term + ':' + i}><td><b>{s.student}</b><small>{s.term}</small></td><td>{s.n}</td><td>{s.task_candidates}</td><td>{s.contribution_candidates}</td><td>{pct(s.HOT)}</td><td>{synthetic ? num(s.AIV ?? s.balanced, 1) : <><span>CTQ {num(s.CTQ)}</span><small>AIV {num(s.AIV)}</small><small>排名 {s.rank ?? '不可判断'}</small></>}</td><td>{(s.tips || []).join('；')}</td></tr>)}</tbody></table></div>;
}

export function Export({ctx}) {
  const [error, setError] = useState('');
  const [done, setDone] = useState('');
  const [busy, setBusy] = useState('');
  const job = ctx.job;
  const role = ctx.reportRole;
  const {student, term} = ctx.reportScope;
  const filters = queryString({role, student, term});
  const {data: reportData, error: scopeError} = useResource(job ? '/jobs/' + job.id + '/report?' + filters : '');
  const [options, setOptions] = useState({students: [], terms: []});
  useEffect(() => { if (reportData?.options) setOptions(reportData.options); if (role === 'student' && !student && reportData?.scope?.student) ctx.setReportScope(previous => ({...previous, student: reportData.scope.student})); }, [reportData, role, student]);
  if (!job) return <NoJob ctx={ctx} />;
  async function save(kind) {
    setBusy(kind); setError(''); setDone('');
    try { await download('/jobs/' + job.id + (kind === 'zip' ? '/export?' : '/results.csv?') + filters, job.id + (kind === 'zip' ? '-reproduce.zip' : '-results.csv')); setDone(kind); }
    catch (e) { setError(e.message); } finally { setBusy(''); }
  }
  const files = [['results.csv', '每个学生×学期单位的指标、双维覆盖和缺失原因'], ['turn-results.csv', '逐回合双维标签、状态、请求计数和来源坐标'], ['reports/*.json / *.html', '学生、教师与管理者三类报告，附来源和版本'],
    ['confirmations.json', '独立人工确认及修改历史'], ['job-usage.json', '任务状态、调用与用量账本摘要'], ['README.md', '复算说明与指标边界'], ['manifest.json', '各文件 SHA-256 与来源清单']];
  return <><div className="wb-filter-bar wb-form"><label>报告视角<select value={role} onChange={e => ctx.setReportRole(e.target.value)}><option value="student">学生</option><option value="teacher">教师</option><option value="administrator">管理者</option></select></label>
    <label>学期范围<select value={term} onChange={e => ctx.setReportScope({student: '', term: e.target.value})}><option value="">全部学期</option>{options.terms.map(t => <option key={t} value={t}>{t}</option>)}</select></label>
    <label>学生范围<select value={student} onChange={e => ctx.setReportScope(previous => ({...previous, student: e.target.value}))}><option value="">所选批次</option>{options.students.map(s => <option key={s} value={s}>{s}</option>)}</select></label></div>
    <ErrorBox error={scopeError} /><div className="wb-grid-2"><section className="wb-card"><h2><Package size={20} />复现包内容</h2><table className="wb-table"><tbody>{files.map(([file, description]) => <tr key={file}><td><code>{file}</code></td><td>{description}</td></tr>)}</tbody></table></section>
    <section className="wb-card"><h2>导出</h2><p>任务：<b>{job.id}</b>（{job.dataset_name}）</p><p>{modeName(job.mode)} · {job.cursor}/{job.total} 个回合{job.state !== 'finished' ? '，导出当前已保存的结果' : ''}</p>
      {job.mode !== 'synthetic' && <Note>真实批次导出用于内部复核，包含学生代号和来源定位；原始研究材料保持冻结。</Note>}
      <div className="wb-actions"><button className="wb-primary" onClick={() => save('zip')} disabled={!!busy}><Package size={16} />{busy === 'zip' ? '正在打包…' : '下载复现包 ZIP'}</button>
        <button onClick={() => save('csv')} disabled={!!busy}><Download size={16} />结果 CSV</button></div>
      {done && <p className="wb-ok"><Check size={16} />已开始下载{done === 'zip' ? '；解压后按 README.md 复算并核对 manifest.json。' : '结果 CSV。'}</p>}<ErrorBox error={error} />
    </section></div></>;
}

export function Sealed() {
  const {data, error} = useResource('/review-summary');
  const names = {'pilot-v1': '首轮试标', 'retest-v2': '第二轮复测', 'guided-v3': '第三轮 · 推荐答案辅助'};
  return <><Note>既有人工核验按轮次封存，新业务人工确认在“追溯异常”页单独记录。</Note><ErrorBox error={error} />
    <section className="wb-card"><h2>各轮人工核验</h2>{!data ? <p className="wb-muted">正在读取…</p> : !data.available || !data.rounds.length ? <p className="wb-muted">没有可读的历史人工核验记录。</p> :
      <table className="wb-table"><thead><tr><th>轮次</th><th>方式</th><th>分配</th><th>已提交</th></tr></thead><tbody>{data.rounds.map(r => <tr key={r.round_id + r.mode}><td>{names[r.round_id] || r.round_id}</td><td>{{blind: '盲审', guided: '推荐辅助', open: '开放'}[r.mode] || r.mode}</td><td>{r.assigned}</td><td>{r.completed ?? 0}</td></tr>)}</tbody></table>}</section></>;
}
