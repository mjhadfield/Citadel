// The Stats page (#stats): every machine's CPU, memory, temperatures, GPU and disks over time, and the moments its CPU
// stayed high with the processes behind them. The history is kept by the stats collector on NZXT
// (collector/citadel_collector.py); Citadel's /api/owner/stats/* reads it back, so nothing here talks to NZXT.
// app.js tells this module when the page is shown (onView). Plan: docs/tasks/stats-collector.md.
//
// Charts are drawn here as SVG (no chart library: CSP 'self', no build step). The plot stretches to its box
// (preserveAspectRatio none, non-scaling strokes) and the axis labels are HTML on top, so text never distorts. A gap in
// the readings (the collector or the machine was off) breaks the line rather than joining across it.
//
// Everything here is [data-rs-*]; never reuse [data-act] or [data-date] (app.js and util.js treat those globally).
import * as api from './api.js';
import { $, $$, bytes, duration, esc, hhmm, ico } from './util.js';

const API = '/api/owner/stats';
const root = $('#view-stats');
const RANGES = [['6h', '6 hours', 6 * 3600], ['24h', '24 hours', 86400], ['7d', '7 days', 7 * 86400], ['30d', '30 days', 30 * 86400], ['1y', '1 year', 365 * 86400]];
const W = 1000, H = 200;                  // the plot's own units; it's stretched to its box
// Series colours follow the entity: the first series of a chart is the accent, the second the neutral silver. Status
// colours (--ok/--warn/--bad) stay reserved for state.
const C1 = 'var(--accent)', C2 = 'var(--silver-hi)', C3 = 'var(--accent-soft)';

let active = false, started = false, machines = [], collector = null, timer = null, loading = 0;
let pick = load('rs-machine', 'this'), range = load('rs-range', '24h');
const charts = new Map();                 // chart id -> what its tooltip needs

function load(k, d) { try { return localStorage.getItem(k) || d; } catch { return d; } }
function save(k, v) { try { localStorage.setItem(k, v); } catch { /* storage blocked: just not remembered */ } }

// A Machines panel in Monitoring links here for its machine (cards.js: data-rs-machine).
document.addEventListener('click', e => {
  const a = e.target.closest('a[data-rs-machine]');
  if (a) { pick = a.dataset.rsMachine; save('rs-machine', pick); if (started) render(); }
}, true);

export function onView(v) {
  const was = active;
  active = v === 'stats';
  if (active && !started) { started = true; boot(); }
  else if (active && !was) refresh();
  if (!active) { clearTimeout(timer); timer = null; }
}

function boot() {
  root.innerHTML = `<div class="rs"><div class="rs__top"><nav class="mtabs" id="rs-machines" aria-label="Machines"></nav>
    <span class="seg" role="group" aria-label="Time range">${RANGES.map(([id, label]) => `<button type="button" data-rs-range="${id}" aria-pressed="${id === range}">${label}</button>`).join('')}</span></div>
    <div class="rs__sub" id="rs-sub"></div><div class="rs__grid" id="rs-grid"><div class="rs-empty"><span class="spin"></span></div></div></div>`;
  root.addEventListener('click', onClick);
  root.addEventListener('pointermove', onHover);
  root.addEventListener('pointerleave', () => $$('.rs-chart', root).forEach(hideTip), true);
  refresh();
}

function onClick(e) {
  const m = e.target.closest('[data-rs-pick]');
  if (m) { pick = m.dataset.rsPick; save('rs-machine', pick); render(); return; }
  const r = e.target.closest('[data-rs-range]');
  if (r) { range = r.dataset.rsRange; save('rs-range', range); $$('[data-rs-range]', root).forEach(b => b.setAttribute('aria-pressed', String(b === r))); loadHistory(); }
}

async function refresh() {
  try {
    const d = await api.get(`${API}/machines`);
    machines = d.machines || [];
    collector = d.collector;
  } catch (exc) {
    if (!machines.length) $('#rs-grid').innerHTML = `<div class="rs-empty">${ico('alert')}<b>No history right now</b>${esc(exc?.message || '')}</div>`;
  }
  render();
}

function render() {
  if (!machines.length) return;
  if (!machines.some(m => m.id === pick)) pick = machines[0].id;
  $('#rs-machines').innerHTML = machines.map(m => {
    const dot = m.up === false ? '<span class="dot"></span>' : m.error && !m.stats ? '<span class="dot dot--bad"></span>' : '<span class="dot dot--ok"></span>';
    return `<button type="button" data-rs-pick="${esc(m.id)}" aria-pressed="${m.id === pick}">${dot}${esc(m.name)}</button>`;
  }).join('');
  const m = machines.find(x => x.id === pick), st = m?.stats;
  const parts = st ? [st.os, st.cpu?.cores ? `${st.cpu.cores} threads` : '', st.mem?.total ? `${bytes(st.mem.total)} RAM` : '', st.uptime_s != null && m.up !== false ? `up ${duration(st.uptime_s)}` : ''] : [];
  if (m?.up === false) parts.push(m.at ? `asleep · last seen ${hhmm(m.at)}` : 'asleep');
  const err = collector && collector.ok === false ? collector.error : m?.up !== false && m?.error ? m.error : '';
  $('#rs-sub').innerHTML = `<span>${esc(parts.filter(Boolean).join(' · '))}</span>${err ? `<span class="rs__err">${ico('alert')}${esc(err)}</span>` : ''}`;
  loadHistory();
}

async function loadHistory() {
  clearTimeout(timer);
  const mine = ++loading, span = RANGES.find(r => r[0] === range)[2];
  const end = Math.ceil(Date.now() / 1000), start = end - span;
  const q = `machine=${encodeURIComponent(pick)}&start=${start}&end=${end}`;
  let series, disks, spikes, error = '';
  try {
    [series, disks, spikes] = await Promise.all([api.get(`${API}/series?${q}`), api.get(`${API}/disks?${q}`), api.get(`${API}/spikes?${q}`)]);
  } catch (exc) { error = exc?.message || 'The stats collector isn’t answering'; }
  if (mine !== loading || !active) return;
  const grid = $('#rs-grid');
  if (error) { grid.innerHTML = `<div class="rs-empty">${ico('alert')}<b>No history right now</b>${esc(error)}</div>`; }
  else draw(grid, series, disks, spikes.spikes || [], start, end);
  // the short ranges move on by themselves; a week or more barely changes in a minute
  if (span <= 86400) timer = setTimeout(() => active && loadHistory(), 60_000);
}

// ---- the charts -----------------------------------------------------------------------------------------------------

function draw(grid, s, d, spikes, start, end) {
  charts.clear();
  const m = s.metrics || {}, out = [];
  const pct = v => `${Math.round(v)}%`, deg = v => `${v.toFixed(0)}°C`;
  const ser = (key, label, color, unit) => m[key] && { key, label, color, avg: m[key].avg, max: m[key].max, fmt: unit };
  out.push(chart('cpu', 'CPU', s, [ser('cpu', 'CPU', C1, pct)], [0, 100], pct, start, end));
  out.push(chart('mem', 'Memory', s, [ser('mem', 'Memory', C1, pct), ser('swap', 'Swap', C2, pct)], [0, 100], pct, start, end));
  const temps = [ser('cpu_temp', 'CPU', C1, deg), ser('gpu_temp', 'GPU', C2, deg)].filter(Boolean);
  if (temps.length) out.push(chart('temp', 'Temperature', s, temps, 'auto', deg, start, end));
  if (m.gpu) out.push(chart('gpu', 'GPU', s, [ser('gpu', 'GPU', C1, pct)], [0, 100], pct, start, end));
  const mounts = Object.entries(d.mounts || {});
  if (mounts.length) {
    // one axis for every disk: how full each is, whatever its size (the tooltip gives the free space)
    const ds = { t: [], bucket: d.bucket };
    const tAll = [...new Set(mounts.flatMap(([, v]) => v.t))].sort((a, b) => a - b);
    ds.t = tAll;
    const list = mounts.slice(0, 3).map(([mount, v], i) => {
      const at = new Map(v.t.map((t, j) => [t, j]));
      const used = tAll.map(t => { const j = at.get(t); return j == null || !v.total[j] ? null : 100 * (v.total[j] - v.free[j]) / v.total[j]; });
      const free = tAll.map(t => { const j = at.get(t); return j == null ? null : v.free[j]; });
      return { key: mount, label: mount, color: [C1, C2, C3][i], avg: used, max: null, free, fmt: (val, k) => `${pct(val)} full${free[k] != null ? ` · ${bytes(free[k])} free` : ''}` };
    });
    out.push(chart('disk', 'Disks', ds, list, [0, 100], pct, start, end));
  }
  out.push(spikeCard(spikes));
  grid.innerHTML = out.join('');
}

function chart(id, title, s, list, yr, fmtAxis, start, end) {
  list = list.filter(Boolean);
  const t = s.t || [], bucket = s.bucket || 30;
  if (!list.length || !t.length) {
    return `<section class="card rs-card"><div class="card__in"><div class="card__head"><span class="hud">${esc(title)}</span></div><div class="rs-none">No readings in this range</div></div></section>`;
  }
  let [lo, hi] = yr === 'auto' ? autoRange(list) : yr;
  const mid = t.map(x => x + bucket / 2);
  const X = v => ((v - start) / (end - start)) * W, Y = v => H - ((v - lo) / (hi - lo)) * H;
  const runs = idx => {                     // the runs of consecutive readings, split wherever one is missing
    const out = []; let cur = [];
    for (let i = 0; i < t.length; i++) {
      const ok = idx(i) != null;
      if (ok && cur.length && t[i] - t[cur[cur.length - 1]] > 2.5 * bucket) { out.push(cur); cur = []; }
      if (ok) cur.push(i); else if (cur.length) { out.push(cur); cur = []; }
    }
    if (cur.length) out.push(cur);
    return out;
  };
  const line = (vals, r) => r.map((i, k) => `${k ? 'L' : 'M'}${X(mid[i]).toFixed(1)} ${Y(vals[i]).toFixed(1)}`).join('');
  const paths = list.map(se => {
    const rs = runs(i => se.avg[i]);
    // the peak within each bucket, as a faint band above the average (it only differs once readings are grouped)
    const band = se.max ? rs.map(r => `<path d="${line(se.max, r)}L${r.slice().reverse().map(i => `${X(mid[i]).toFixed(1)} ${Y(se.avg[i]).toFixed(1)}`).join('L')}Z" style="fill:${se.color}" class="rs-band"/>`).join('') : '';
    const dots = rs.filter(r => r.length === 1).map(r => `<circle cx="${X(mid[r[0]]).toFixed(1)}" cy="${Y(se.avg[r[0]]).toFixed(1)}" r="2" style="stroke:${se.color}" class="rs-dot"/>`).join('');
    return `${band}<path d="${rs.map(r => line(se.avg, r)).join('')}" style="stroke:${se.color}" class="rs-line"/>${dots}`;
  }).join('');
  const ticks = [lo, lo + (hi - lo) / 2, hi];
  const grid = ticks.map(v => `<line x1="0" x2="${W}" y1="${Y(v)}" y2="${Y(v)}" class="rs-gridline"/>`).join('');
  const yl = ticks.map(v => `<span style="top:${(Y(v) / H) * 100}%">${esc(fmtAxis(v))}</span>`).join('');
  const xl = xTicks(start, end).map(v => { const at = (X(v) / W) * 100; return `<span style="left:${at}%"${at > 94 ? ' class="is-end"' : at < 4 ? ' class="is-start"' : ''}>${esc(xLabel(v, end - start))}</span>`; }).join('');
  const last = list.map(se => { const i = lastIdx(se.avg); return i == null ? null : se.fmt(se.avg[i], i); });
  const now = list.length === 1 && last[0] ? `<span class="rs-now">${esc(last[0])}</span>` : '';
  const legend = list.length > 1 ? `<span class="rs-legend">${list.map((se, k) => `<span><i style="background:${se.color}"></i>${esc(se.label)}${last[k] ? ` <b>${esc(last[k])}</b>` : ''}</span>`).join('')}</span>` : '';
  charts.set(id, { t, mid, bucket, list, start, end });
  return `<section class="card rs-card"><div class="card__in"><div class="card__head"><span class="hud">${esc(title)}</span>${legend}${now}</div>
    <div class="rs-chart" data-rs-chart="${id}"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">${grid}${paths}</svg>
      <div class="rs-y">${yl}</div><div class="rs-x">${xl}</div><div class="rs-cross" hidden></div><div class="rs-tip" hidden></div></div></div></section>`;
}

function autoRange(list) {
  const vals = list.flatMap(se => [...se.avg, ...(se.max || [])]).filter(v => v != null);
  const lo = Math.floor((Math.min(...vals) - 5) / 10) * 10, hi = Math.ceil((Math.max(...vals) + 5) / 10) * 10;
  return [Math.max(0, lo), Math.max(hi, lo + 20)];
}
const lastIdx = a => { for (let i = a.length - 1; i >= 0; i--) if (a[i] != null) return i; return null; };

function xTicks(start, end) {
  // whole hours / days / months in local time, so labels land on 12:00, Mon 17, Nov; at most 8 of them
  const span = end - start, H = 3600, D = 86400;
  const steps = [[H, 'h'], [3 * H, 'h'], [6 * H, 'h'], [D, 'd'], [2 * D, 'd'], [7 * D, 'd'], [30 * D, 'm'], [60 * D, 'm']];
  const [step, unit] = steps.find(([st]) => span / st <= 8) || steps[steps.length - 1];
  const d = new Date(start * 1000);
  if (unit === 'h') d.setMinutes(0, 0, 0); else d.setHours(0, 0, 0, 0);
  if (unit === 'm') d.setDate(1);
  const out = [];
  for (let n = 0; n < 400 && d.getTime() / 1000 <= end; n++) {
    const v = d.getTime() / 1000, ok = unit === 'h' ? d.getHours() % (step / H) === 0 : unit === 'm' ? d.getMonth() % (step / (30 * D)) === 0 : true;
    if (v >= start && ok) out.push(v);
    if (unit === 'h') d.setHours(d.getHours() + 1); else if (unit === 'd') d.setDate(d.getDate() + step / D); else d.setMonth(d.getMonth() + 1);
  }
  return out;
}
const dayFmt = new Intl.DateTimeFormat('en-GB', { weekday: 'short', day: 'numeric' });
const monFmt = new Intl.DateTimeFormat('en-GB', { month: 'short' });
const fullFmt = new Intl.DateTimeFormat('en-GB', { weekday: 'short', day: 'numeric', month: 'short' });
function xLabel(v, span) {
  const d = new Date(v * 1000);
  return span <= 86400 ? hhmm(v) : span <= 31 * 86400 ? dayFmt.format(d) : monFmt.format(d);
}
const when = (v, span) => (span <= 86400 ? hhmm(v) : `${fullFmt.format(new Date(v * 1000))} ${hhmm(v)}`);

// ---- hover: a crosshair and every series' value at that moment --------------------------------------------------------

function onHover(e) {
  const box = e.target.closest?.('.rs-chart');
  $$('.rs-chart', root).forEach(c => { if (c !== box) hideTip(c); });
  if (!box) return;
  const c = charts.get(box.dataset.rsChart);
  if (!c) return;
  const r = box.getBoundingClientRect(), f = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
  const at = c.start + f * (c.end - c.start);
  let i = nearest(c.mid, at);
  const span = c.end - c.start;
  const cross = $('.rs-cross', box), tip = $('.rs-tip', box);
  const near = i != null && Math.abs(c.mid[i] - at) <= c.bucket * 1.5;
  const x = near ? ((c.mid[i] - c.start) / span) * 100 : f * 100;
  cross.hidden = false; cross.style.left = `${x}%`;
  const rows = near ? c.list.map(se => se.avg[i] == null ? '' : `<span><i style="background:${se.color}"></i>${esc(se.label)} <b>${esc(se.fmt(se.avg[i], i))}</b>${se.max && se.max[i] != null && Math.round(se.max[i]) > Math.round(se.avg[i]) ? ` <small>peak ${esc(se.fmt(se.max[i], i))}</small>` : ''}</span>`).join('') : '';
  tip.innerHTML = `<b>${esc(when(near ? c.t[i] : at, span))}</b>${rows || '<span>No readings</span>'}`;
  tip.hidden = false;
  tip.classList.toggle('is-left', x > 60);
  tip.style.left = `${x}%`;
}
function hideTip(box) { $('.rs-cross', box)?.setAttribute('hidden', ''); $('.rs-tip', box)?.setAttribute('hidden', ''); }
function nearest(a, v) {
  if (!a.length) return null;
  let lo = 0, hi = a.length - 1;
  while (hi - lo > 1) { const m = (lo + hi) >> 1; if (a[m] < v) lo = m; else hi = m; }
  return Math.abs(a[lo] - v) <= Math.abs(a[hi] - v) ? lo : hi;
}

// ---- high CPU moments -------------------------------------------------------------------------------------------------

function spikeCard(list) {
  const body = list.length ? `<ol class="rs-spikes">${list.slice(0, 30).map(sp => {
    const mins = Math.max(1, Math.round((sp.end - sp.start + 30) / 60));
    return `<li><div class="rs-spike__head"><span>${esc(when(sp.start, Infinity))}</span><span>${mins} min · peak ${Math.round(sp.peak)}%</span></div>
      <div class="rs-procs">${sp.top.map(p => `<span class="rs-proc"><span class="rs-proc__name">${esc(p.name)}</span><span class="rs-proc__bar"><i style="width:${Math.min(100, p.percent)}%"></i></span><span class="rs-proc__pct">${Math.round(p.percent)}%</span></span>`).join('')}</div></li>`;
  }).join('')}</ol>` : '<div class="rs-none">No high CPU in this range</div>';
  return `<section class="card rs-card rs-card--spikes"><div class="card__in"><div class="card__head"><span class="hud">High CPU</span>${list.length ? `<span class="rs-now">${list.length}</span>` : ''}</div>${body}</div></section>`;
}
