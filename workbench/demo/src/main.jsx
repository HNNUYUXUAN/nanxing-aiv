import React, {useCallback, useEffect, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {BookOpen, House, Upload, ClipboardCheck, Play, SearchCheck, ChartNoAxesColumn, NotebookText, Package, Archive, LockOpen, Lock, Menu, X, Check} from 'lucide-react';
import './style.css';
import './wb/workbench.css';
import {wb, session, modeName} from './wb/api';
import {Home, Import, Quality, Jobs, Trace, Compare, Report, Export, Sealed, PERSONAS} from './wb/pages';

const PAGES = [
  {key: 'home', label: '开始', icon: House, sub: '选择身份和模式', Page: Home},
  {key: 'import', step: 1, label: '导入资料', icon: Upload, sub: '选内置批次或上传 CSV / JSONL，保留完整学生回合。', Page: Import},
  {key: 'quality', step: 2, label: '检查质量', icon: ClipboardCheck, sub: '查看解析问题、缺失字段和指标可用性。', Page: Quality},
  {key: 'jobs', step: 3, label: '启动 / 继续任务', icon: Play, sub: '两快模型独立判断 → 两强模型独立复核 → 配对复跑；后台执行，可暂停续跑。', Page: Jobs},
  {key: 'trace', step: 4, label: '追溯异常', icon: SearchCheck, sub: '分开核对任务要求与学生贡献，回查原文并记录人工确认。', Page: Trace},
  {key: 'compare', step: 5, label: '比较模型与指标', icon: ChartNoAxesColumn, sub: '按维度和复核集合比较有效配对；合成数据提供指标实验。', Page: Compare},
  {key: 'report', step: 6, label: '分层报告', icon: NotebookText, sub: '学生、教师与管理者的证据简报，建议可回到原文核对。', Page: Report},
  {key: 'export', step: 7, label: '导出复现材料', icon: Package, sub: '结果、分层报告、人工确认和 SHA-256 清单。', Page: Export},
];
const EXTRA = [
  {key: 'evidence', label: '原文证据检索', icon: BookOpen, sub: '检索当前批次中的学生回合并核对出处。', Page: Trace},
  {key: 'sealed', label: '人工核验（封存）', icon: Archive, sub: '既有人工核验只读展示。', Page: Sealed},
];
const ALL = [...PAGES, ...EXTRA];
const current = () => {
  const key = window.location.hash.slice(1).split('?')[0];
  const legacy = {overview: 'trace', metrics: 'compare', redteam: 'compare', reports: 'report', review: 'sealed'}[key];
  return ALL.some(p => p.key === key) ? key : legacy || 'home';
};
class Boundary extends React.Component {
  state = {error: null};
  static getDerivedStateFromError(error) { return {error}; }
  componentDidUpdate(prev) { if (prev.page !== this.props.page && this.state.error) this.setState({error: null}); }
  render() {
    if (!this.state.error) return this.props.children;
    return <div className="wb-alert error" role="alert"><div><b>这个页面出错了</b><p>{String(this.state.error.message || this.state.error)}</p><button onClick={() => this.setState({error: null})}>重新加载本页</button></div></div>;
  }
}
function App() {
  const [page, setPage] = useState(current);
  const [persona, setPersonaState] = useState(() => session.get('persona'));
  const [dataset, setDatasetState] = useState(null);
  const [job, setJobState] = useState(null);
  const [unlocked, setUnlocked] = useState(false);
  const [samples, setSamples] = useState([]);
  const [jobs, setJobs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [menu, setMenu] = useState(false);
  const [traceTarget, setTraceTarget] = useState('');
  const [traceDimension, setTraceDimension] = useState('task');
  const [reportRole, setReportRole] = useState('teacher');
  const [reportScope, setReportScope] = useState({student: '', term: ''});
  const selection = useRef(0);
  const routeRequest = useRef(0);
  const datasetId = useRef('');
  const go = useCallback(key => { window.location.hash = key; setPage(key); setMenu(false); window.scrollTo(0, 0); }, []);
  const setPersona = key => { setPersonaState(key); session.set('persona', key); setReportRole(key === 'student' ? 'student' : 'teacher'); };
  const setDataset = useCallback(value => {
    selection.current += 1; datasetId.current = value?.id || '';
    setDatasetState(value); setJobState(null); setTraceTarget('');
    setReportScope({student: '', term: ''});
    if (value) setSamples(previous => previous.some(d => d.id === value.id) ? previous : [...previous, {id: value.id, name: value.name, kind: value.kind, note: '独立业务批次', locked: false}]);
    session.set('dataset', value?.id); session.set('job', '');
  }, []);
  const setJob = useCallback(value => {
    if (value && value.dataset !== datasetId.current) return;
    setJobState(value); session.set('job', value?.id);
    if (value) setJobs(previous => [value, ...previous.filter(j => j.id !== value.id)]);
  }, []);
  const refreshJobs = useCallback(async () => {
    const result = await wb('/jobs'); setJobs(result.jobs || []); return result.jobs || [];
  }, []);
  const selectDataset = useCallback(async (id, preferredJob = '') => {
    const request = ++selection.current; datasetId.current = id;
    setDatasetState(null); setJobState(null); setTraceTarget(''); setError(''); setLoading(true);
    setReportScope({student: '', term: ''});
    session.set('dataset', id); session.set('job', '');
    try {
      const [data, catalog] = await Promise.all([wb('/datasets/' + encodeURIComponent(id)), wb('/jobs')]);
      if (selection.current !== request) return;
      setDatasetState(data); setJobs(catalog.jobs || []);
      let selected = (catalog.jobs || []).find(j => j.id === preferredJob && j.dataset === id) || (catalog.jobs || []).find(j => j.dataset === id);
      if (!selected && id === 't3') selected = await wb('/jobs/t3');
      if (selection.current !== request) return;
      setJobState(selected || null); session.set('job', selected?.id); return selected;
    } catch (e) { if (selection.current === request) setError(e.message); }
    finally { if (selection.current === request) setLoading(false); }
  }, []);
  const selectJob = useCallback(async (value, stillCurrent = () => true) => {
    const selected = typeof value === 'string' ? await wb('/jobs/' + encodeURIComponent(value)) : value;
    if (!stillCurrent()) return;
    return selectDataset(selected.dataset, selected.id);
  }, [selectDataset]);
  const checkUnlock = useCallback(async (resetToDefault = true) => {
    try {
      const catalog = await wb('/samples');
      setUnlocked(catalog.unlocked); setSamples(catalog.samples || []);
      const saved = resetToDefault ? '' : session.get('dataset');
      const savedJob = resetToDefault ? '' : session.get('job');
      const allowedSaved = catalog.unlocked || (catalog.samples || []).some(s => s.id === saved && !s.locked);
      await selectDataset(allowedSaved && saved ? saved : catalog.unlocked ? 't3' : 'demo-synthetic', savedJob);
      return catalog.unlocked;
    } catch (e) { setError(e.message); setUnlocked(false); setLoading(false); return false; }
  }, [selectDataset]);
  const lock = () => { session.setToken(''); checkUnlock(true); };
  const openTrace = useCallback((id, dimension = 'task') => { setTraceTarget(id); setTraceDimension(dimension); go('trace'); }, [go]);
  const readRoute = useCallback(async () => {
    const request = ++routeRequest.current;
    const next = current(); setPage(next);
    const params = new URLSearchParams(window.location.hash.split('?')[1] || '');
    const item = params.get('item');
    if (next !== 'trace' || !item) return;
    try {
      if (params.get('job')) {
        const opened = await selectJob(params.get('job'), () => routeRequest.current === request);
        if (!opened) return;
      }
      if (routeRequest.current !== request) return;
      setTraceTarget(item); setTraceDimension(params.get('dimension') === 'contribution' ? 'contribution' : 'task');
    } catch (e) { if (routeRequest.current === request) setError(e.message); }
  }, [selectJob]);
  useEffect(() => {
    window.addEventListener('hashchange', readRoute);
    return () => window.removeEventListener('hashchange', readRoute);
  }, [readRoute]);
  useEffect(() => { checkUnlock(false).then(readRoute); }, [checkUnlock, readRoute]);
  // Polling observes the persistent worker. Closing this page never pauses a job.
  useEffect(() => {
    if (!job || !['running', 'pausing'].includes(job.state)) return;
    const controller = new AbortController(); const request = selection.current; let timer;
    const poll = async () => {
      try {
        const next = await wb('/jobs/' + encodeURIComponent(job.id), {signal: controller.signal});
        if (request === selection.current && !controller.signal.aborted) { setJob(next); setError(''); }
      } catch (e) { if (e.name !== 'AbortError' && request === selection.current) setError(e.message); }
      finally { if (!controller.signal.aborted) timer = setTimeout(poll, 2000); }
    };
    timer = setTimeout(poll, 2000);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [job?.id, job?.state, setJob]);
  const info = ALL.find(p => p.key === page) || PAGES[0];
  const ctx = {persona, setPersona, dataset, setDataset, job, setJob, jobs, samples, refreshJobs, selectDataset, selectJob, unlocked, checkUnlock, lock, go, traceTarget, traceDimension, openTrace, reportRole, setReportRole, reportScope, setReportScope};
  const path = PERSONAS[persona]?.path || [];
  const reached = key => ({import: !!dataset, quality: !!dataset, jobs: !!job, trace: job?.state === 'finished', compare: job?.state === 'finished', report: job?.state === 'finished', export: job?.state === 'finished'}[key]);
  const options = new Map(samples.filter(s => !s.locked).map(s => [s.id, {id: s.id, name: s.name}]));
  jobs.forEach(j => { if (!options.has(j.dataset)) options.set(j.dataset, {id: j.dataset, name: j.dataset_name}); });
  if (dataset) options.set(dataset.id, dataset);
  const sourceLabel = modeName(job?.mode || (dataset?.kind === 'synthetic' ? 'synthetic' : dataset?.id === 't3' ? 'frozen' : 'live'));
  return <div className="wb-layout">
    <aside className={menu ? 'open' : ''}>
      <div className="wb-brand"><BookOpen size={26} /><div>认知证据实验室<small>AI 教育价值评价工作台</small></div><button className="wb-menu-close" onClick={() => setMenu(false)} aria-label="关闭菜单"><X size={20} /></button></div>
      <nav aria-label="工作流">
        {PAGES.map(p => <button key={p.key} data-step={p.step} className={(page === p.key ? 'active ' : '') + (path.includes(p.key) ? 'suggested' : '')} onClick={() => go(p.key)} aria-current={page === p.key ? 'page' : undefined}>
          {p.step ? <span className={'wb-step' + (reached(p.key) ? ' done' : '')}>{reached(p.key) ? <Check size={12} /> : p.step}</span> : <p.icon size={18} />}<span>{p.label}</span></button>)}
        <div className="wb-nav-sep">其他</div>
        {EXTRA.map(p => <button key={p.key} className={page === p.key ? 'active' : ''} onClick={() => { setTraceTarget(''); go(p.key); }}><p.icon size={18} /><span>{p.label}</span></button>)}
      </nav>
      <div className="wb-context">
        <span className={'wb-mode-tag ' + (unlocked ? 'on' : '')}>{unlocked ? <LockOpen size={13} /> : <Lock size={13} />}{unlocked ? '工作台已解锁' : '合成教学演示'}</span>
        {PERSONAS[persona] && <span>身份：{PERSONAS[persona].name} <button className="wb-link" onClick={() => go('home')}>更换</button></span>}
        {dataset && <span title={dataset.name}>数据：{dataset.name}</span>}{job && <span title={job.id}>任务：{job.id} · {job.cursor}/{job.total}</span>}
      </div>
    </aside>
    <main data-step={info.step || undefined}>
      <header className="wb-top"><button className="wb-menu" onClick={() => setMenu(true)} aria-label="打开菜单"><Menu size={20} /></button>
        <div>{info.step && <p className="wb-eyebrow">第 {info.step} 步 / 共 7 步</p>}<h1>{page === 'home' ? '欢迎使用' : info.label}</h1>{page !== 'home' && <p>{info.sub}</p>}</div>
        <span className="wb-source">{dataset ? sourceLabel : unlocked ? '内部工作台' : '合成教学演示'}</span>
      </header>
      <div className="wb-batch-bar no-print"><label>当前批次<select aria-label="当前批次" value={dataset?.id || ''} disabled={loading} onChange={e => selectDataset(e.target.value)}>
        {!dataset && <option value="">{loading ? '正在读取批次…' : '请选择批次'}</option>}{[...options.values()].map(d => <option value={d.id} key={d.id}>{d.name}</option>)}
      </select></label>{dataset && <span className="wb-pill">{sourceLabel}</span>}{job && <small>任务 {job.id}</small>}</div>
      {error && <div className="wb-alert error" role="alert"><span>{error}</span><button onClick={() => checkUnlock(false)}>重新读取</button></div>}
      <Boundary page={page + ':' + dataset?.id + ':' + job?.id}><info.Page key={page === 'home' ? page : page + ':' + (dataset?.id || '') + ':' + (job?.id || '')} ctx={ctx} /></Boundary>
      <footer>任务要求与学生贡献分别保留证据；缺失指标显示原因。模型候选需结合原文和人工意见使用，合成数据用于教学演示。</footer>
    </main>{menu && <div className="wb-scrim" onClick={() => setMenu(false)} />}
  </div>;
}
createRoot(document.getElementById('root')).render(<App />);
