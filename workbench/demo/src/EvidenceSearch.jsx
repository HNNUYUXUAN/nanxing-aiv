import React, {useEffect, useRef, useState} from 'react';

export async function requestJSON(url, options = {}) {
  const response = await fetch(url, {...options, cache: 'no-store'});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求参数无效');
  return data;
}

function hits(text, query) {
  const ranges = [];
  for (const word of query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean)) {
    const lower = text.toLocaleLowerCase();
    let start = 0;
    while ((start = lower.indexOf(word, start)) !== -1) {
      ranges.push([start, start + word.length]);
      start += word.length;
    }
  }
  ranges.sort((a, b) => a[0] - b[0]);
  return ranges.reduce((merged, range) => {
    const last = merged[merged.length - 1];
    if (last && range[0] < last[1]) last[1] = Math.max(last[1], range[1]);
    else merged.push([...range]);
    return merged;
  }, []);
}

function Highlight({text, query, active = -1}) {
  const parts = [];
  let cursor = 0;
  hits(text, query).forEach(([start, end], index) => {
    parts.push(text.slice(cursor, start));
    parts.push(<mark key={start} className={index === active ? 'current-hit' : ''}>{text.slice(start, end)}</mark>);
    cursor = end;
  });
  parts.push(text.slice(cursor));
  return parts;
}

export function OriginalReader({question, onUse, initialQuery = ''}) {
  const [query, setQuery] = useState(initialQuery);
  const [active, setActive] = useState(0);
  const [selection, setSelection] = useState('');
  const [message, setMessage] = useState('');
  const textRef = useRef(null);
  const count = hits(question, query).length;
  useEffect(() => {
    textRef.current?.querySelectorAll('mark')[active]?.scrollIntoView({block: 'nearest'});
  }, [query, active]);
  function capture() {
    const selected = window.getSelection();
    if (!selected || selected.isCollapsed || !selected.rangeCount) return;
    const range = selected.getRangeAt(0);
    const root = textRef.current;
    if (!root.contains(range.startContainer) || !root.contains(range.endContainer)) return;
    const prefix = document.createRange();
    prefix.selectNodeContents(root);
    prefix.setEnd(range.startContainer, range.startOffset);
    const start = prefix.toString().length;
    const exact = question.slice(start, start + range.toString().length);
    if (exact) { setSelection(exact); setMessage(''); }
  }
  async function copy(text) {
    try { await navigator.clipboard.writeText(text); setMessage('已复制，可粘贴到证据栏。'); }
    catch { setMessage('剪贴板不可用，请选中文字后按 Ctrl+C 复制。'); }
  }
  return <section className="original-reader">
    <div className="reader-heading"><h2>学生提问原文</h2><span className="muted">{question.length} 字符</span></div>
    <div className="reader-find">
      <input aria-label="在当前原文中查找" placeholder="在当前原文中查找…" value={query} onChange={e => {setQuery(e.target.value); setActive(0);}}/>
      <span aria-live="polite">{count ? `${active + 1} / ${count}` : query ? '无匹配' : '全文'}</span>
      <button type="button" disabled={!count} onClick={() => setActive((active + 1) % count)}>下一处</button>
    </div>
    <blockquote ref={textRef} tabIndex="0" aria-label="学生提问完整原文" className="original-text" onMouseUp={capture} onKeyUp={capture} onTouchEnd={capture}><Highlight text={question} query={query} active={active}/></blockquote>
    <p className="muted">拖选支持判断的连续片段，保留原字句和标点。也可用键盘复制后粘贴。</p>
    {selection && <p className="selection-preview">已选 {selection.length} 字符：<q>{selection}</q></p>}
    <div className="reader-actions">
      <button type="button" className="primary" disabled={!selection} onClick={() => onUse ? (onUse(selection), setMessage('已填入原文证据栏。')) : copy(selection)}>{onUse ? '将选中片段填入证据' : '复制选中片段'}</button>
      <button type="button" onClick={() => copy(question)}>复制整段提问</button>
    </div>
    <p className="reader-status" role="status">{message}</p>
  </section>;
}

function SourceGuide() {
  return <details className="source-guide"><summary>“原文证据”在哪里？查看来源索引与打分口径</summary>
    <div className="source-guide-body">
      <p>人工复核中的证据来自当前题目的学生首段提问。选择认知层级后，填写能支持该判断的连续原文；判断理由另写。</p>
      <dl><dt>首页合成示例</dt><dd><code>aiv/data.py → synthetic_records()</code>，192 条构造记录，6 种示例提问。</dd>
        <dt>真实提问索引</dt><dd><code>runtime/research/records.json</code>，含来源文件、行号、SHA-256。</dd>
        <dt>2025 秋季 / 2026 春季问答</dt><dd><code>data/25f/csv/qa.csv</code> / <code>data/26s/csv/qa.csv</code>，“问答记录”列。原始工作簿分别在 <code>archive/raw/25f/qa.xlsx</code>、<code>archive/raw/26s/qa.xlsx</code>。</dd>
        <dt>更多材料</dt><dd><code>data/README.md</code> 为讨论、反馈及图片资料索引；<code>data/sources.md</code> 为原件对照；<code>docs/标注与人工校验手册.md</code> 为评分说明。</dd></dl>
      <p>本页全文检索覆盖筛选后的 QA 学生首段提问。图片题、讨论资料和论文文献不在此检索范围内。来源行号按 CSV 逻辑记录计算，含表头；多行单元格会使文本编辑器物理行号不同。</p>
    </div>
  </details>;
}

const pageSize = 25;
const emptyRecords = [];
const termNames = {'25f': '2025 秋季', '26s': '2026 春季', synthetic: '合成示例'};
export default function EvidenceSearch({records = emptyRecords}) {
  const initialId = new URLSearchParams(window.location.hash.split('?')[1] || '').get('id') || '';
  const [scope, setScope] = useState('synthetic');
  const [query, setQuery] = useState(initialId);
  const [term, setTerm] = useState('');
  const [offset, setOffset] = useState(0);
  const [draftToken, setDraftToken] = useState('');
  const [token, setToken] = useState('');
  const [result, setResult] = useState(null);
  const [selected, setSelected] = useState('');
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState('');
  const [detailError, setDetailError] = useState('');
  const [loading, setLoading] = useState(false);
  const [copyStatus, setCopyStatus] = useState('');
  const privateScope = scope === 'private';
  useEffect(() => {
    const controller = new AbortController();
    setResult(null); setSelected(''); setDetail(null); setError(''); setCopyStatus('');
    if (!privateScope) {
      const words = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
      const matched = records.filter(r => words.every(w => `${r.id} ${r.student} ${r.question} ${r.timestamp}`.toLocaleLowerCase().includes(w)));
      const items = matched.slice(offset, offset + pageSize).map(r => ({...r, preview: r.question}));
      setResult({total: matched.length, indexed: records.length, items});
      setSelected(items[0]?.id || ''); setLoading(false);
      return () => controller.abort();
    }
    if (!token) {setLoading(false); return () => controller.abort();}
    setLoading(true);
    const timer = setTimeout(() => {
      const params = new URLSearchParams({q: query, term, offset: String(offset), limit: String(pageSize)});
      requestJSON('/api/evidence/search?' + params, {headers: {'X-Review-Token': token}, signal: controller.signal})
        .then(data => {if (!controller.signal.aborted) {setResult(data); setSelected(data.items[0]?.id || '');}})
        .catch(e => {if (!controller.signal.aborted) setError(e.message);})
        .finally(() => {if (!controller.signal.aborted) setLoading(false);});
    }, 180);
    return () => {clearTimeout(timer); controller.abort();};
  }, [privateScope, query, term, offset, token, records]);
  useEffect(() => {
    const controller = new AbortController();
    setDetail(null); setDetailError(''); setCopyStatus('');
    if (!selected) return () => controller.abort();
    if (!privateScope) {setDetail(records.find(r => r.id === selected) || null); return () => controller.abort();}
    requestJSON('/api/evidence/records/' + encodeURIComponent(selected), {headers: {'X-Review-Token': token}, signal: controller.signal})
      .then(data => {if (!controller.signal.aborted) setDetail(data);})
      .catch(e => {if (!controller.signal.aborted) setDetailError(e.message);});
    return () => controller.abort();
  }, [selected, privateScope, token, records]);
  function changeScope(value) {setScope(value); setQuery(''); setOffset(0); setTerm(''); setSelected(''); setDetail(null); setResult(null); setError('');}
  function lock() {setToken(''); setDraftToken(''); setResult(null); setDetail(null); setSelected(''); setQuery(''); setOffset(0);}
  async function copyCitation() {
    const citation = privateScope ? `${detail.id}\n${detail.source_file} · CSV 逻辑行 ${detail.source_row}\nSHA-256: ${detail.source_sha256}` : `${detail.id}\n合成示例 · aiv/data.py → synthetic_records()`;
    try {await navigator.clipboard.writeText(citation); setCopyStatus('已复制来源定位。');}
    catch {setCopyStatus('剪贴板不可用，请手动复制下方来源。');}
  }
  return <section className="source-workspace">
    <div className="source-tabs" aria-label="原文数据范围">
      <button className={!privateScope ? 'primary' : ''} onClick={() => changeScope('synthetic')}>合成示例</button>
      <button className={privateScope ? 'primary' : ''} onClick={() => changeScope('private')}>真实记录全库</button>
      <a href="#review" className="review-return">前往人工复核 →</a>
    </div>
    <SourceGuide/>
    {privateScope && <div className="private-access">
      <p>全库查阅使用 C 角色口令。A/B 请在人工复核页检索当前已领取题目。浏览全库不会领取任务或提交评分。</p>
      {token && result ? <div className="access-summary"><span>已解锁 · {result.indexed} 条真实提问 · 秋季 {result.terms['25f']} / 春季 {result.terms['26s']}</span><button onClick={lock}>锁定资料</button></div> : <form className="unlock-form" onSubmit={e => {e.preventDefault(); setToken(draftToken.trim()); setDraftToken(''); setOffset(0);}}>
        <input type="password" aria-label="全库查阅口令" placeholder="输入 C 角色口令" autoComplete="off" value={draftToken} onChange={e => setDraftToken(e.target.value)}/><button className="primary" disabled={!draftToken.trim() || loading}>解锁全库</button>
      </form>}
    </div>}
    {error && <p className="notice" role="alert">{error}</p>}
    {(!privateScope || token) && <>
      <div className="source-filters">
        <label className="source-query">检索原文<input placeholder="关键词、记录编号、来源行号… 空格分隔多个关键词" aria-label="检索原文" maxLength={300} value={query} onChange={e => {setQuery(e.target.value); setOffset(0);}}/></label>
        {privateScope && <label>学期<select aria-label="学期" value={term} onChange={e => {setTerm(e.target.value); setOffset(0);}}><option value="">全部学期</option><option value="25f">2025 秋季</option><option value="26s">2026 春季</option></select></label>}
        <button onClick={() => {setQuery(''); setTerm(''); setOffset(0);}}>清空筛选</button>
      </div>
      <div className="source-columns">
        <section aria-label="检索结果" aria-busy={loading}>
          <p className="result-count" role="status">{loading ? '正在检索…' : result ? `找到 ${result.total} 条 · ${privateScope ? '真实提问' : '合成示例'}` : '等待解锁'}</p>
          <div className="source-results">{result?.items.map(r => <button key={r.id} className={'source-result ' + (selected === r.id ? 'selected-result' : '')} aria-pressed={selected === r.id} onClick={() => setSelected(r.id)}>
            <span className="result-meta">{termNames[r.term]} · {privateScope ? `来源行 ${r.source_row}` : r.student}</span>
            <span className="result-text"><Highlight text={r.preview} query={query}/></span>
            <span className="result-id">{r.id}</span>
          </button>)}{result?.total === 0 && <p className="empty-result">没有匹配的原文。试试缩短关键词或清空筛选。</p>}</div>
          {result && result.total > 0 && <div className="source-pagination"><button disabled={offset === 0 || loading} onClick={() => setOffset(Math.max(0, offset - pageSize))}>上一页</button><span>{offset + 1}–{Math.min(offset + pageSize, result.total)} / {result.total}</span><button disabled={offset + pageSize >= result.total || loading} onClick={() => setOffset(offset + pageSize)}>下一页</button></div>}
        </section>
        <section className="source-detail" aria-label="原文阅读区">
          {detailError && <p className="notice" role="alert">{detailError}</p>}
          {detail ? <>
            <div className="source-detail-title"><span>{termNames[detail.term]}</span><code>{detail.id}</code></div>
            <OriginalReader key={detail.id} question={detail.question} initialQuery={query.split(/\s+/).filter(word => word && detail.question.toLocaleLowerCase().includes(word.toLocaleLowerCase())).join(' ')}/>
            <div className="provenance"><h3>来源定位</h3>{privateScope ? <><p><code>{detail.source_file}</code> · 第 {detail.source_row} 逻辑行</p><p className="muted">{detail.row_definition}</p><p className="verified-source">源文件哈希与提问内容已核验</p><details><summary>文件 SHA-256</summary><code>{detail.source_sha256}</code></details></> : <p><code>aiv/data.py → synthetic_records()</code> · 构造示例</p>}<button onClick={copyCitation}>复制来源定位</button><span className="muted" role="status"> {copyStatus}</span></div>
            {privateScope && <details className="raw-context"><summary>展开原始问答上下文（含 AI 回答）</summary><p className="notice">本轮评分单位是上方学生首段提问。以下 AI 回答及后续对话仅供核对，不能填作该题的学生证据。</p><pre>{detail.raw_qa}</pre></details>}
          </> : <p className="empty-result">{selected ? '正在核验来源并读取原文…' : '从左侧选择一条记录，查看完整原文和来源。'}</p>}
        </section>
      </div>
    </>}
  </section>;
}
