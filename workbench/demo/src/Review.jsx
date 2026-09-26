import React, {useEffect, useState} from 'react';
import {OriginalReader, requestJSON} from './EvidenceSearch';

const names = ['记忆','理解','应用','分析','评价','创造'];
const roundNames = {'pilot-v1': '首轮试标', 'retest-v2': '第二轮复测', 'guided-v3': '第三轮 · 推荐答案辅助测评'};
const levelName = value => value == null ? '不可判断' : `L${value} ${names[value - 1]}`;

export default function Review() {
  const [token, setToken] = useState('');
  const [state, setState] = useState(null);
  const [error, setError] = useState('');
  const [label, setLabel] = useState('');
  const [evidence, setEvidence] = useState('');
  const [reason, setReason] = useState('');
  const [fillMethod, setFillMethod] = useState('manual');
  const [pendingFill, setPendingFill] = useState(null);
  const [busy, setBusy] = useState(false);
  const [clockLoadedAt, setClockLoadedAt] = useState(Date.now());
  const [now, setNow] = useState(Date.now());
  useEffect(() => {const id = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(id);}, []);
  const elapsed = Math.floor((state?.timing?.elapsed_seconds || 0) + (state?.timing?.running ? Math.max(0, now-clockLoadedAt)/1000 : 0));
  const task = state?.task;
  const guidance = task?.guidance;
  const roundId = task?.round_id || state?.timing?.round_id;
  const guided = task?.mode === 'guided' || (!task && roundId === 'guided-v3');
  const invalid = !!evidence && !task?.question?.includes(evidence);
  const missing = label !== '' && !evidence;
  const templateIncomplete = guided && /【待补充：[^】]*】/.test(reason);
  const matchesRecommendation = guidance && label === (guidance.label == null ? '' : String(guidance.label)) && evidence === guidance.evidence && reason === guidance.reason;
  function clearDraft() {setLabel(''); setEvidence(''); setReason(''); setFillMethod('manual'); setPendingFill(null);}
  function applyFill(method) {
    if (method === 'recommendation') {
      setLabel(guidance.label == null ? '' : String(guidance.label));
      setEvidence(guidance.evidence); setReason(guidance.reason);
    } else {setReason(guidance.reason_template);}
    setFillMethod(method); setPendingFill(null);
  }
  function requestFill(method) {
    const wouldReplace = method === 'recommendation' ? !!(label || evidence || reason) : !!reason;
    if (wouldReplace) setPendingFill(method); else applyFill(method);
  }
  useEffect(() => {
    if (!evidence && !reason && !label) return;
    const warn = e => {e.preventDefault(); e.returnValue = '';};
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [evidence, reason, label]);
  async function load(reset = false) {
    const next = await requestJSON('/api/review/next', {headers: {'X-Review-Token': token}});
    setState(next);
    setClockLoadedAt(Date.now()); setNow(Date.now());
    if (reset || next.task?.assignment !== task?.assignment) clearDraft();
  }
  async function start() {
    setBusy(true); setError('');
    try {await load();} catch (e) {setError(e.message);} finally {setBusy(false);}
  }
  async function submit(e) {
    e.preventDefault();
    if (invalid || missing || templateIncomplete || !reason.trim() || busy) return;
    setBusy(true); setError('');
    try {
      await requestJSON('/api/review/submit', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Review-Token': token}, body: JSON.stringify({assignment: task.assignment, label: label === '' ? null : +label, evidence, reason, fill_method: fillMethod})});
      setState(null); clearDraft();
      await load(true);
    } catch (e) {setError(e.message);} finally {setBusy(false);}
  }
  return <section className="report review-workspace">
    <p>当前第三轮采用推荐答案辅助测评：A、B 各核对 8 道题，可参考、填入并修改推荐答案。先读原文，再确认层级、证据和理由，最后提交。前两轮记录保留。</p>
    <p>使用原有角色口令，点击“开始 / 继续”领取任务。每个口令独立计时，不限时；填入推荐答案不会自动提交。</p>
    <p><a href="#evidence">原文证据检索与来源索引 →</a></p>
    <section className="notice" aria-label="标注判定卡"><b>作答前：先识别本轮要求的主要认知操作</b>
      <ol><li>标注“当前请求 AI 完成什么操作”，不按学生身份、研究目标或术语难度升档。学生自己展示的能力是另一变量。</li>
      <li>只有知识点名、课程标题且没有可判断的操作时，可选“不可判断”；不要自行补出题目或上下文。</li>
      <li>L1 识别/列举事实；L2 解释/概括关系；L3 将已有方法用于具体情境；L4 拆解/比较机制；L5 按标准检验或评价；L6 整合约束提出新方案。</li>
      <li>“设计”“分析”等动词本身不够。选择含对象与要求的完整证据片段，并说明为何支持该层级；仍不清楚时写明缺失信息。</li></ol>
      <p>示例：仅“矩阵特征值”信息不足；“列出矩阵的三个基本运算”与“解释矩阵乘法为何不交换”要求不同。仅有“我是博士生，研究新材料”不能推出 L5/L6。</p>
      <p>{task?.mode === 'blind' ? '当前领取的是历史独立盲审任务，请按该轮要求独立完成。' : '辅助测评可参考推荐答案。遇到边界题，可以保留有依据的不同判断，并在理由中写清楚。'}</p>
    </section>
    <div className="controls"><input type="password" autoComplete="off" aria-label="复核口令" placeholder="输入你的复核口令" value={token} onChange={e => {setToken(e.target.value); setState(null); clearDraft();}} disabled={busy}/><button className="primary" onClick={start} disabled={busy || !token}>{busy ? '处理中…' : '开始 / 继续'}</button></div>
    {error && <p className="notice" role="alert">{error}</p>}
    {state && <><p>评审 {state.role} · {roundNames[roundId] || '人工复核'}{task?.mode === 'blind' ? ' · 独立盲审' : ''}{guided && task && ` · 第 ${task.position} / ${task.total} 题`}</p>
      <p>本口令激活后用时：{Math.floor(elapsed/60)} 分 {elapsed%60} 秒 · 不限时{!state.timing?.running && ' · 已停止'}。计时包含激活后的停顿，仅用于记录实际成本。</p>
      {!task ? <p>{guided ? '本轮辅助测评已完成，提交结果已保存。' : '当前没有可领取任务。'}</p> : <form onSubmit={submit}>
        <OriginalReader key={task.assignment} question={task.question} onUse={setEvidence}/>
        {guidance && <section className="guidance-card" aria-label="推荐答案">
          <div className="guidance-heading"><h2>推荐答案</h2><span className="label">{levelName(guidance.label)}</span></div>
          <p>{guidance.note}</p>
          {guidance.boundary_case && <p className="guidance-boundary">边界题：重点核对下方“边界与不确定性”，允许有依据的不同层级。</p>}
          <h3>参考原文证据</h3><blockquote>{guidance.evidence}</blockquote>
          <dl>{Object.entries(guidance.reason_parts).map(([field, text]) => <div key={field}><dt>{field}</dt><dd>{text}</dd></div>)}</dl>
          <p className="muted">来源：{guidance.author} · 版本：{guidance.version}</p>
          <div className="guidance-actions"><button className="primary" type="button" disabled={busy} onClick={() => requestFill('recommendation')}>填入推荐答案</button><button type="button" disabled={busy} onClick={() => requestFill('template')}>使用理由模板</button></div>
          <p className="muted">“填入推荐答案”填写下方三项；“使用理由模板”仅填写四段理由框架，保留已选层级和证据。</p>
          {pendingFill && <div className="notice" role="alert"><p>{pendingFill === 'recommendation' ? '将替换已填写的层级、证据和理由。' : '将替换已填写的理由，保留层级和证据。'}</p><div className="guidance-actions"><button type="button" disabled={busy} onClick={() => applyFill(pendingFill)}>确认替换</button><button type="button" onClick={() => setPendingFill(null)}>保留当前填写</button></div></div>}
        </section>}
        {task.ai && <details><summary>查看 AI 建议</summary>{task.ai.map((j, i) => <p key={i}>{j.model}：{j.judgment?.reason || '无有效判断'}</p>)}</details>}
        {guided && <h2>核对并提交你的判断</h2>}
        <label>主要认知要求<select aria-label="主要认知要求" value={label} onChange={e => setLabel(e.target.value)} disabled={busy}><option value="">不可判断</option>{names.map((n, i) => <option value={i + 1} key={n}>L{i + 1} {n}</option>)}</select></label>
        <label>原文证据<textarea value={evidence} onChange={e => setEvidence(e.target.value)} placeholder="从上方提问拖选并填入，或粘贴支持判断的连续原文片段" aria-invalid={invalid || missing} aria-describedby="evidence-validation" disabled={busy}/></label>
        <p id="evidence-validation" className={invalid || missing ? 'notice' : 'evidence-valid'} aria-live="polite">{invalid ? '未在当前提问中找到这一连续片段，请检查改写、标点或换行。' : missing ? '已选择认知层级，请填写至少一段连续原文。' : evidence ? `已核验：${evidence.length} 字符为当前提问的连续原文。` : '原文证据用于支持判断；选择“不可判断”时可以留空。'}</p>
        <label>判断理由<textarea className={guided ? 'guided-reason' : ''} required maxLength={1000} aria-invalid={templateIncomplete} aria-describedby="reason-validation" value={reason} onChange={e => setReason(e.target.value)} disabled={busy}/></label>
        <p id="reason-validation" className={templateIncomplete ? 'notice' : 'muted'} aria-live="polite">{templateIncomplete ? '请将每处【待补充：…】替换为本题的具体说明，再提交。' : `${reason.length} / 1000 字符`}</p>
        {guided && <p className="evidence-valid" role="status">{fillMethod === 'manual' ? '当前手动填写，可随时参考上方推荐答案。' : fillMethod === 'template' ? '已使用理由模板，请核对四段内容。' : matchesRecommendation ? '已填入推荐答案，请核对后提交。' : '已修改推荐答案，将保存你的最终判断。'}本轮按辅助测评单独记录。</p>}
        <button className="primary" type="submit" disabled={busy || invalid || missing || templateIncomplete || !reason.trim()}>{busy ? '正在保存…' : '提交并领取下一条'}</button>
      </form>}
    </>}
  </section>;
}
