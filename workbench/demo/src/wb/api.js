// Workbench API client. Every call goes to the local FastAPI server (/api/wb/*); nothing here is mocked.
// The workbench token is kept in sessionStorage only, so closing the tab locks private data again.

const read = (store, key, fallback) => {
  try { return store.getItem(key) ?? fallback; } catch { return fallback; }
};
const write = (store, key, value) => {
  try { value == null ? store.removeItem(key) : store.setItem(key, value); } catch { /* storage blocked: keep in memory */ }
};

export const session = {
  token: () => read(sessionStorage, 'wb-token', ''),
  setToken: value => write(sessionStorage, 'wb-token', value || null),
  get: key => read(localStorage, 'wb-' + key, ''),
  set: (key, value) => write(localStorage, 'wb-' + key, value || null),
};

export async function wb(path, {method = 'GET', body, signal} = {}) {
  const headers = {};
  const token = session.token();
  if (token) headers['X-Workbench-Token'] = token;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch('/api/wb' + path, {method, headers, signal, cache: 'no-store',
    body: body === undefined ? undefined : JSON.stringify(body)});
  let data = null;
  try { data = await response.json(); } catch { /* non-JSON error page */ }
  if (!response.ok) {
    const detail = data?.detail;
    throw new Error(typeof detail === 'string' ? detail : response.status === 422 ? '输入不符合要求' : `服务器返回 ${response.status}`);
  }
  return data;
}

export async function download(path, filename) {
  const headers = {};
  const token = session.token();
  if (token) headers['X-Workbench-Token'] = token;
  const response = await fetch('/api/wb' + path, {headers, cache: 'no-store'});
  if (!response.ok) {
    let message = `导出失败（${response.status}）`;
    try { message = (await response.json()).detail || message; } catch { /* keep default */ }
    throw new Error(message);
  }
  const url = URL.createObjectURL(await response.blob());
  const link = Object.assign(document.createElement('a'), {href: url, download: filename});
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export const LEVELS = ['记忆', '理解', '应用', '分析', '评价', '创造'];
export const levelName = v => (v == null ? '不可判断' : `L${v} ${LEVELS[v - 1]}`);
export const pct = v => (v == null ? '—' : `${Math.round(v * 100)}%`);
export const num = (v, d = 2) => (v == null || Number.isNaN(v) ? '不可判断' : Number(v).toFixed(d));
export const modeName = mode => ({frozen: '正式 t3 · 冻结成果', live: '新业务批次 · 真实模型', synthetic: '合成教学示例'}[mode] || '业务批次');
export const stateName = state => ({ready: '待开始', running: '进行中', pausing: '等待在途请求结算', paused: '可继续', finished: '已完成', blocked: '等待处理', failed: '运行失败'}[state] || state);
export const dimensionName = dimension => dimension === 'contribution' ? '学生贡献' : '任务要求';
