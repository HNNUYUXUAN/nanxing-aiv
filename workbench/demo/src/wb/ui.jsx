import React, {useEffect, useRef} from 'react';
import * as echarts from 'echarts';
import {TriangleAlert, Info, ArrowRight} from 'lucide-react';

const INK = '#141b26';
const MUTED = '#5d6878';
const GRID = '#e6e0d3';
// Charts inherit the step accent from CSS (--accent) so each workflow step keeps its own color.
const accent = el => getComputedStyle(el).getPropertyValue('--accent').trim() || INK;
const PALETTE = ['#2f5bd6', '#c98700', '#d6432a', '#8b3fa0', '#0d7a67', '#5b7f12'];

// Bar chart used across the workbench; resizes with its container and cleans up on unmount.
export function Bars({labels, series, title, height = 240, stacked = false, horizontal = false}) {
  const ref = useRef();
  const key = JSON.stringify([labels, series, title, stacked, horizontal]);
  useEffect(() => {
    const chart = echarts.init(ref.current);
    const lead = accent(ref.current);
    const palette = [lead, ...PALETTE.filter(c => c !== lead)];
    const category = {type: 'category', data: labels, axisLabel: {color: MUTED, interval: 0, hideOverlap: true}, axisTick: {show: false}, axisLine: {lineStyle: {color: INK}}};
    const value = {type: 'value', splitLine: {lineStyle: {color: GRID, type: 'dashed'}}, axisLabel: {color: MUTED}};
    chart.setOption({
      animationDuration: 400,
      title: title ? {text: title, left: 0, textStyle: {fontSize: 13, color: INK, fontWeight: 800}} : undefined,
      grid: {left: horizontal ? 90 : 44, right: 16, top: title ? 44 : 16, bottom: series.length > 1 ? 44 : 28},
      tooltip: {trigger: 'axis', axisPointer: {type: 'shadow'}},
      legend: series.length > 1 ? {bottom: 0, textStyle: {color: MUTED}} : undefined,
      xAxis: horizontal ? value : category,
      yAxis: horizontal ? category : value,
      series: series.map((s, i) => ({type: 'bar', name: s.name, data: s.data, stack: stacked ? 'all' : undefined,
        barMaxWidth: 34, itemStyle: {color: s.color || palette[i % palette.length], borderRadius: 0}})),
    });
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(ref.current);
    return () => { observer.disconnect(); chart.dispose(); };
  }, [key]);
  return <div ref={ref} className="wb-chart" style={{height}} role="img" aria-label={title || '柱状图'} />;
}

// Interval chart: one horizontal band per row (low–high) with a point estimate.
export function Ranges({rows, title, height = 320, unit = ''}) {
  const ref = useRef();
  const key = JSON.stringify([rows, title]);
  useEffect(() => {
    const chart = echarts.init(ref.current);
    const lead = accent(ref.current);
    const labels = rows.map(r => r.label);
    chart.setOption({
      animationDuration: 400,
      title: title ? {text: title, left: 0, textStyle: {fontSize: 13, color: INK, fontWeight: 800}} : undefined,
      grid: {left: 56, right: 20, top: title ? 44 : 16, bottom: 28},
      tooltip: {trigger: 'axis', axisPointer: {type: 'shadow'},
        formatter: p => {const r = rows[p[0].dataIndex]; return `${r.label}<br/>${r.low.toFixed(1)}–${r.high.toFixed(1)}${unit}${r.mid != null ? `<br/>基线 ${r.mid.toFixed(1)}${unit}` : ''}`;}},
      xAxis: {type: 'value', scale: true, splitLine: {lineStyle: {color: GRID, type: 'dashed'}}, axisLabel: {color: MUTED}},
      yAxis: {type: 'category', data: labels, inverse: true, axisLabel: {color: MUTED}, axisTick: {show: false}},
      series: [
        {type: 'bar', stack: 'r', data: rows.map(r => r.low), itemStyle: {color: 'transparent'}, silent: true},
        {type: 'bar', stack: 'r', data: rows.map(r => Math.max(r.high - r.low, 0.3)), itemStyle: {color: lead, opacity: 0.28}, barMaxWidth: 12},
        {type: 'scatter', data: rows.map(r => r.mid), symbol: 'rect', symbolSize: 8, itemStyle: {color: lead}},
      ],
    });
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(ref.current);
    return () => { observer.disconnect(); chart.dispose(); };
  }, [key]);
  return <div ref={ref} className="wb-chart" style={{height}} role="img" aria-label={title || '区间图'} />;
}

export function ErrorBox({error, onRetry}) {
  if (!error) return null;
  return <div className="wb-alert error" role="alert"><TriangleAlert size={18} /><div><b>操作没有完成</b><p>{error}</p>{onRetry && <button onClick={onRetry}>重试</button>}</div></div>;
}

export function Note({children, tone = 'info'}) {
  return <div className={'wb-alert ' + tone}>{tone === 'warn' ? <TriangleAlert size={18} /> : <Info size={18} />}<div>{children}</div></div>;
}

// Shown when a step's prerequisite (dataset or job) is missing; points the user back.
export function Gate({title, text, action, onAction}) {
  return <div className="wb-gate"><h2>{title}</h2><p>{text}</p><button className="wb-primary" onClick={onAction}>{action}<ArrowRight size={16} /></button></div>;
}

export function Stat({label, value, hint, tone}) {
  return <div className={'wb-stat ' + (tone || '')}><span>{label}</span><b>{value}</b>{hint && <small>{hint}</small>}</div>;
}

export function Tip({persona, show, children}) {
  if (!show.includes(persona)) return null;
  return <p className="wb-tip">{children}</p>;
}
