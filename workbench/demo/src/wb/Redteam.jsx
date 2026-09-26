import React, {useEffect, useState} from 'react';
import guarded from '../../../results/synthetic/redteam.json';
import text from '../../../results/synthetic/text-redteam.json';
import {Bars, Note} from './ui';
import {num, levelName} from './api';

const CASES = {baseline: '正常提问', repetition: '重复提问', verbs: '堆砌高阶动词', verbosity: '刻意写长',
  tool_stacking: '堆砌智能体', copied_ai: '粘贴 AI 回答', improvement: '真实改进（对照）'};
// Abstaining scenarios have a null score; ECharts shows null as a gap instead of 0.
const round = v => (v == null ? null : +v.toFixed(2));

// Anti-manipulation evidence: fixed-label metric attacks + real cached text attacks + known-truth simulation.
export default function Redteam() {
  const [simulation, setSimulation] = useState(null);
  useEffect(() => { fetch('/api/demo').then(r => r.json()).then(d => setSimulation(d.simulation)).catch(() => setSimulation([])); }, []);
  const models = [...new Set(text.map(r => r.model))];
  const cases = [...new Set(text.map(r => r.case))];
  return <>
    <Note tone="warn">这里展示的是公式的<b>失败条件</b>：哪些刷分手段能抬高分数、哪些不能。保留失败，是为了说清楚这个指标能用在什么范围。</Note>
    <div className="wb-grid-2">
      <section className="wb-card">
        <h2>刷分手段对 AIV 的影响（固定标签）</h2>
        <Bars labels={guarded.map(r => r.scenario)} series={[{name: '原公式', data: guarded.map(r => round(r.delta))}, {name: '保守对照（MAB 不计入）', data: guarded.map(r => round(r.guarded_delta))}]} height={280} />
        <p className="wb-muted">相对基线的分数变化。堆砌智能体会抬高原公式；把 MAB 单独报告、不计入综合分后，这个漏洞被关掉。“分段操纵”把每条提问拆到不同会话，CTQ 不可判断，所以没有柱子。</p>
      </section>
      <section className="wb-card">
        <h2>文本级攻击 · 真实模型调用缓存</h2>
        <div className="wb-scroll"><table className="wb-table compact"><thead><tr><th>攻击方式</th>{models.map(m => <th key={m}>{m.split('-')[0]}</th>)}</tr></thead><tbody>
          {cases.map(c => <tr key={c}><td>{CASES[c] || c}</td>{models.map(m => { const r = text.find(x => x.case === c && x.model === m); return <td key={m}>{r?.level == null ? '弃权' : levelName(r.level)}</td>; })}</tr>)}
        </tbody></table></div>
        <p className="wb-muted">每种条件每个模型调用一次，样本太少，不能估计普遍的攻击成功率。</p>
      </section>
    </div>
    <section className="wb-card">
      <h2>已知真值的因果模拟</h2>
      <p>模拟中 AI 使用的真实效应设为 0.4，能力强的学生更常用 AI。不控制能力时估计会偏高——真实教学日志没有能力测量时，也会有同样的问题。</p>
      {simulation?.length ? <table className="wb-table"><thead><tr><th>估计方法</th><th>估计值</th><th>95% 区间</th><th>真值</th></tr></thead><tbody>
        {simulation.map(r => <tr key={r.model}><td>{{naive: '直接比较', adjusted: '控制能力后'}[r.model] || r.model}</td><td>{num(r.estimate)}</td><td>{num(r.lower)} – {num(r.upper)}</td><td>{num(r.true_effect)}</td></tr>)}
      </tbody></table> : <p className="wb-muted">{simulation ? '模拟结果不可用。' : '正在读取…'}</p>}
    </section>
  </>;
}
