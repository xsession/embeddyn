// Virtual instruments (observers) and live controls (sources) in the dock.
import { api, data, emit, formatValue, on, signalUnit, signalValue, state, TRACE_COLORS } from './store.js';
import { updateValues } from './schematic.js';

const grid = document.getElementById('instrument-grid');
const empty = document.getElementById('instrument-empty');
let cards = new Map();        // node id -> {node, root, canvas, draw}
let signature = '';

const TIMEBASES = [0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 30, 60];

function h(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') e.className = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return e;
}

const channels = (node) => node.ports.filter((p) => p.signal);

function select(options, value, onchange, format = (v) => v) {
  const s = h('select', { onchange: (e) => onchange(e.target.value) },
    options.map((o) => h('option', { value: o, selected: String(o) === String(value) }, format(o))));
  return s;
}

function fmtTime(t) { return t >= 1 ? `${t} s` : `${t * 1000} ms`; }

// -- canvas helpers --------------------------------------------------------------
function fitCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, hgt = canvas.clientHeight;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(hgt * dpr)) {
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(hgt * dpr);
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h: hgt };
}

function range(col, i0, i1) {
  let lo = Infinity, hi = -Infinity;
  for (let i = i0; i < i1; i++) { const v = col[i]; if (v !== null && v < lo) lo = v; if (v !== null && v > hi) hi = v; }
  if (!Number.isFinite(lo)) return [-1, 1];
  if (hi - lo < 1e-12) { const pad = Math.abs(hi) * 0.1 || 1; return [lo - pad, hi + pad]; }
  const pad = (hi - lo) * 0.08;
  return [lo - pad, hi + pad];
}

function trace(ctx, col, i0, i1, x0, x1, t0, t1, y0, y1, lo, hi, color, step = false) {
  if (i1 - i0 < 1) return;
  const sx = (x1 - x0) / (t1 - t0 || 1), sy = (y1 - y0) / (hi - lo || 1);
  const X = (t) => x0 + (t - t0) * sx, Y = (v) => y1 - (v - lo) * sy;
  ctx.strokeStyle = color; ctx.lineWidth = 1.4; ctx.beginPath();
  const n = i1 - i0, px = Math.max(1, x1 - x0);
  if (n > px * 3) {                       // min/max per pixel column
    let col0 = -1, mn = 0, mx = 0;
    for (let i = i0; i < i1; i++) {
      const v = col[i]; if (v === null) continue;
      const c = Math.floor(X(data.time[i]));
      if (c !== col0) {
        if (col0 >= 0) { ctx.lineTo(col0, Y(mn)); ctx.lineTo(col0, Y(mx)); }
        col0 = c; mn = mx = v;
      } else { if (v < mn) mn = v; if (v > mx) mx = v; }
    }
    if (col0 >= 0) { ctx.lineTo(col0, Y(mn)); ctx.lineTo(col0, Y(mx)); }
  } else {
    let started = false, prev = null;
    for (let i = i0; i < i1; i++) {
      const v = col[i]; if (v === null) { started = false; continue; }
      const x = X(data.time[i]), y = Y(v);
      if (!started) { ctx.moveTo(x, y); started = true; } else if (step && prev !== null) { ctx.lineTo(x, Y(prev)); ctx.lineTo(x, y); } else ctx.lineTo(x, y);
      prev = v;
    }
  }
  ctx.stroke();
}

function screen(ctx, w, hgt) {
  ctx.fillStyle = '#0d1410'; ctx.fillRect(0, 0, w, hgt);
  ctx.strokeStyle = '#1f3a2a'; ctx.lineWidth = 1;
  for (let i = 1; i < 10; i++) { const x = Math.round((w * i) / 10) + 0.5; ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, hgt); ctx.stroke(); }
  for (let i = 1; i < 8; i++) { const y = Math.round((hgt * i) / 8) + 0.5; ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); }
}

function windowOf(node) {
  const tb = node.settings.timebase === 'all' ? null : Number(node.settings.timebase || 0.5);
  const now = data.now();
  const duration = state.graph?.duration_seconds || 1;
  let t0, t1;
  if (tb === null) { t0 = 0; t1 = Math.max(duration, now); } else if (node.settings.mode === 'sweep') {
    t0 = Math.floor(now / tb) * tb; t1 = t0 + tb;
  } else { t1 = Math.max(now, tb); t0 = t1 - tb; }
  const i0 = Math.max(0, data.indexAt(t0) - 1), i1 = Math.min(data.length, data.cursor + 1);
  return { t0, t1, i0, i1 };
}

// -- instruments -----------------------------------------------------------------
const DRAW = {
  oscilloscope(node, ctx, w, hgt) {
    screen(ctx, w, hgt);
    const chans = channels(node);
    if (!data.length || !chans.length) return hint(ctx, w, hgt, chans.length ? 'run the simulation' : 'wire signals to A–D');
    const { t0, t1, i0, i1 } = windowOf(node);
    const stack = node.settings.layout !== 'overlay';
    const lanes = stack ? chans.length : 1;
    chans.forEach((port, k) => {
      const col = data.column(port.signal); if (!col) return;
      const [lo, hi] = range(col, i0, i1);
      const y0 = stack ? (hgt * k) / lanes : 0, y1 = stack ? (hgt * (k + 1)) / lanes : hgt;
      const color = TRACE_COLORS['ABCD'.indexOf(port.name)] || TRACE_COLORS[k];
      trace(ctx, col, i0, i1, 0, w, t0, t1, y0 + 4, y1 - 4, lo, hi, color);
      ctx.fillStyle = color; ctx.font = '11px ui-monospace, monospace';
      const unit = signalUnit(port.signal);
      ctx.fillText(`${port.name} ${port.signal}  ${formatValue(data.value(port.signal), unit)}`, 6, (stack ? y0 : 0) + 13 + (stack ? 0 : k * 14));
      ctx.fillStyle = '#7fa08a'; ctx.textAlign = 'right';
      ctx.fillText(`${formatValue(hi, unit)} / ${formatValue(lo, unit)}`, w - 6, (stack ? y0 : 0) + 13 + (stack ? 0 : k * 14));
      ctx.textAlign = 'left';
    });
    ctx.fillStyle = '#7fa08a'; ctx.font = '10px ui-monospace, monospace';
    ctx.fillText(`${formatValue(t0, 's')} … ${formatValue(t1, 's')}   ${node.settings.timebase === 'all' ? '' : fmtTime(Number(node.settings.timebase) / 10) + '/div'}`, 6, hgt - 5);
  },

  logic_analyser(node, ctx, w, hgt) {
    ctx.fillStyle = '#10131a'; ctx.fillRect(0, 0, w, hgt);
    const chans = channels(node);
    if (!data.length || !chans.length) return hint(ctx, w, hgt, chans.length ? 'run the simulation' : 'wire digital signals');
    const { t0, t1, i0, i1 } = windowOf(node);
    const th = Number(node.settings.threshold ?? 0.5);
    const lane = Math.min(40, hgt / chans.length);
    chans.forEach((port, k) => {
      const col = data.column(port.signal); if (!col) return;
      const y0 = k * lane + 6, y1 = (k + 1) * lane - 4;
      const color = TRACE_COLORS[k % TRACE_COLORS.length];
      ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.beginPath();
      const X = (t) => 90 + ((t - t0) / (t1 - t0)) * (w - 96);
      let prev = null, rises = [], highTime = 0;
      for (let i = i0; i < i1; i++) {
        const v = col[i]; if (v === null) continue;
        const bit = v > th, y = bit ? y0 : y1, x = X(data.time[i]);
        if (prev === null) ctx.moveTo(x, y); else { if (bit !== prev) { ctx.lineTo(x, prev ? y0 : y1); if (bit) rises.push(data.time[i]); } ctx.lineTo(x, y); }
        if (bit && i > i0) highTime += data.time[i] - data.time[i - 1];
        prev = bit;
      }
      ctx.stroke();
      ctx.fillStyle = color; ctx.font = '11px ui-monospace, monospace';
      ctx.fillText(port.signal.length > 12 ? '…' + port.signal.slice(-11) : port.signal, 4, (y0 + y1) / 2 + 4);
      if (rises.length > 1) {
        const period = (rises[rises.length - 1] - rises[0]) / (rises.length - 1);
        ctx.fillStyle = '#9aa'; ctx.textAlign = 'right';
        ctx.fillText(`${formatValue(1 / period, 'Hz')}  duty ${(100 * highTime / (t1 - t0)).toFixed(0)}%`, w - 4, y0 + 10);
        ctx.textAlign = 'left';
      }
    });
  },

  xy_plotter(node, ctx, w, hgt) {
    screen(ctx, w, hgt);
    const [a, b] = [node.ports[0], node.ports[1]];
    if (!a?.signal || !b?.signal || !data.length) return hint(ctx, w, hgt, 'wire X to A and Y to B');
    const ca = data.column(a.signal), cb = data.column(b.signal), n = data.cursor + 1;
    const [xl, xh] = range(ca, 0, n), [yl, yh] = range(cb, 0, n);
    ctx.strokeStyle = TRACE_COLORS[1]; ctx.lineWidth = 1.2; ctx.beginPath();
    const stride = Math.max(1, Math.floor(n / 4000));
    for (let i = 0; i < n; i += stride) {
      const x = ((ca[i] - xl) / (xh - xl)) * (w - 10) + 5, y = hgt - 5 - ((cb[i] - yl) / (yh - yl)) * (hgt - 10);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    }
    ctx.stroke();
    const x = ((ca[n - 1] - xl) / (xh - xl)) * (w - 10) + 5, y = hgt - 5 - ((cb[n - 1] - yl) / (yh - yl)) * (hgt - 10);
    ctx.fillStyle = TRACE_COLORS[0]; ctx.beginPath(); ctx.arc(x, y, 4, 0, 7); ctx.fill();
    ctx.fillStyle = '#7fa08a'; ctx.font = '11px ui-monospace, monospace';
    ctx.fillText(`X ${a.signal} ${formatValue(xl)}…${formatValue(xh)}`, 6, 14);
    ctx.fillText(`Y ${b.signal} ${formatValue(yl)}…${formatValue(yh)}`, 6, 28);
  },

  gauge(node, ctx, w, hgt) {
    ctx.fillStyle = '#15181d'; ctx.fillRect(0, 0, w, hgt);
    const port = node.ports[0];
    let lo = Number(node.settings.minimum ?? -1), hi = Number(node.settings.maximum ?? 1);
    if (!(hi > lo)) [lo, hi] = [-1, 1];
    const value = signalValue(port?.signal);
    const cx = w / 2, cy = hgt * 0.62, r = Math.min(w / 2.4, hgt * 0.5);
    const a0 = Math.PI * 0.8, a1 = Math.PI * 2.2;
    ctx.lineWidth = 10; ctx.strokeStyle = '#2b3038'; ctx.beginPath(); ctx.arc(cx, cy, r, a0, a1); ctx.stroke();
    const frac = value === null ? 0 : Math.max(0, Math.min(1, (value - lo) / (hi - lo)));
    ctx.strokeStyle = '#4ea1ff'; ctx.beginPath(); ctx.arc(cx, cy, r, a0, a0 + (a1 - a0) * frac); ctx.stroke();
    ctx.strokeStyle = '#ddd'; ctx.lineWidth = 1;
    for (let i = 0; i <= 10; i++) {
      const a = a0 + ((a1 - a0) * i) / 10;
      ctx.beginPath(); ctx.moveTo(cx + Math.cos(a) * (r - 16), cy + Math.sin(a) * (r - 16)); ctx.lineTo(cx + Math.cos(a) * (r - 8), cy + Math.sin(a) * (r - 8)); ctx.stroke();
    }
    const a = a0 + (a1 - a0) * frac;
    ctx.strokeStyle = '#ff5c5c'; ctx.lineWidth = 3; ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(cx + Math.cos(a) * (r - 12), cy + Math.sin(a) * (r - 12)); ctx.stroke();
    ctx.fillStyle = '#eee'; ctx.textAlign = 'center'; ctx.font = '600 18px system-ui';
    ctx.fillText(port?.signal ? formatValue(value, signalUnit(port.signal)) : 'wire a signal to A', cx, cy + r * 0.45 + 8);
    ctx.font = '11px system-ui'; ctx.fillStyle = '#889';
    ctx.fillText(`${formatValue(lo)} … ${formatValue(hi)}`, cx, hgt - 6);
    ctx.textAlign = 'left';
  },

  led(node, ctx, w, hgt) {
    ctx.fillStyle = '#15181d'; ctx.fillRect(0, 0, w, hgt);
    const port = node.ports[0];
    const value = signalValue(port?.signal);
    const lit = value !== null && value > Number(node.settings.threshold ?? 0.5);
    const color = node.settings.color || '#ff3b30';
    const r = Math.min(w, hgt) * 0.25, cx = w / 2, cy = hgt / 2 - 8;
    if (lit) { const g = ctx.createRadialGradient(cx, cy, r * 0.2, cx, cy, r * 2.4); g.addColorStop(0, color + 'aa'); g.addColorStop(1, color + '00'); ctx.fillStyle = g; ctx.fillRect(0, 0, w, hgt); }
    ctx.fillStyle = lit ? color : '#3a2525'; ctx.beginPath(); ctx.arc(cx, cy, r, 0, 7); ctx.fill();
    ctx.strokeStyle = '#000'; ctx.lineWidth = 2; ctx.stroke();
    ctx.fillStyle = '#aab'; ctx.textAlign = 'center'; ctx.font = '12px system-ui';
    ctx.fillText(port?.signal ? `${port.signal} = ${formatValue(value)}` : 'wire a signal to A', cx, hgt - 10);
    ctx.textAlign = 'left';
  },
};

function hint(ctx, w, hgt, text) {
  ctx.fillStyle = '#5d7a68'; ctx.font = '12px system-ui'; ctx.textAlign = 'center';
  ctx.fillText(text, w / 2, hgt / 2); ctx.textAlign = 'left';
}

function meterStats(signal, seconds) {
  const col = data.column(signal); if (!col || data.cursor < 0) return null;
  const t1 = data.now(), i0 = data.indexAt(t1 - seconds), i1 = data.cursor + 1;
  let n = 0, sum = 0, sq = 0, mn = Infinity, mx = -Infinity;
  for (let i = i0; i < i1; i++) { const v = col[i]; if (v === null) continue; n++; sum += v; sq += v * v; mn = Math.min(mn, v); mx = Math.max(mx, v); }
  return n ? { mean: sum / n, rms: Math.sqrt(sq / n), min: mn, max: mx, pp: mx - mn } : null;
}

// -- cards -------------------------------------------------------------------------
function header(node, ...controls) {
  const part = state.partsById[node.part];
  return h('div', { class: 'card-head' }, h('span', {}, part?.name || node.kind, ':'), h('b', { title: node.id }, node.label || node.id), ...controls);
}

function setting(node, key, value) {
  node.settings[key] = value;
  state.dirty = true;
  redrawAll();
}

function canvasCard(node, controls = [], wide = false) {
  const canvas = h('canvas');
  const root = h('div', { class: 'card' + (wide ? ' wide' : '') }, header(node, ...controls), h('div', { class: 'card-body' }, canvas));
  root.style.height = wide ? '230px' : '200px';
  return { root, canvas };
}

function buildCard(node) {
  const kind = node.part;
  if (kind === 'oscilloscope') {
    const c = canvasCard(node, [
      select(['all', ...TIMEBASES], node.settings.timebase ?? 0.5, (v) => setting(node, 'timebase', v === 'all' ? 'all' : Number(v)), (v) => (v === 'all' ? 'whole run' : fmtTime(v))),
      select(['roll', 'sweep'], node.settings.mode || 'roll', (v) => setting(node, 'mode', v)),
      select(['stack', 'overlay'], node.settings.layout || 'stack', (v) => setting(node, 'layout', v)),
    ], true);
    return { ...c, draw: () => { const { ctx, w, h: hh } = fitCanvas(c.canvas); DRAW.oscilloscope(node, ctx, w, hh); } };
  }
  if (kind === 'logic_analyser') {
    const c = canvasCard(node, [select(['all', ...TIMEBASES], node.settings.timebase ?? 0.5, (v) => setting(node, 'timebase', v === 'all' ? 'all' : Number(v)), (v) => (v === 'all' ? 'whole run' : fmtTime(v)))], true);
    return { ...c, draw: () => { const { ctx, w, h: hh } = fitCanvas(c.canvas); DRAW.logic_analyser(node, ctx, w, hh); } };
  }
  if (kind in DRAW) {
    const c = canvasCard(node);
    return { ...c, draw: () => { const { ctx, w, h: hh } = fitCanvas(c.canvas); DRAW[kind](node, ctx, w, hh); } };
  }
  if (kind === 'multimeter') {
    const big = h('div', { class: 'big' }), stats = h('div', { class: 'note' });
    const root = h('div', { class: 'card' }, header(node, select(['value', 'mean', 'rms', 'min', 'max', 'pp'], node.settings.mode || 'value', (v) => setting(node, 'mode', v)),
      select([0.02, 0.1, 0.5, 1, 5], node.settings.window || 0.1, (v) => setting(node, 'window', Number(v)), (v) => 'window ' + fmtTime(v))),
    h('div', { class: 'control' }, big, stats));
    return { root, draw: () => {
      const port = node.ports[0];
      if (!port.signal) { big.textContent = '—'; stats.textContent = 'wire a signal to A'; return; }
      const unit = signalUnit(port.signal), s = meterStats(port.signal, Number(node.settings.window || 0.1));
      const mode = node.settings.mode || 'value';
      big.textContent = formatValue(mode === 'value' || !s ? signalValue(port.signal) : s[mode], unit);
      stats.textContent = s ? `${port.signal} · mean ${formatValue(s.mean, unit)} · rms ${formatValue(s.rms, unit)} · min ${formatValue(s.min, unit)} · max ${formatValue(s.max, unit)}` : port.signal;
    } };
  }
  if (kind === 'data_table') {
    const table = h('table', { class: 'kv' });
    const root = h('div', { class: 'card' }, header(node), h('div', { class: 'control' }, table));
    return { root, draw: () => {
      table.innerHTML = '';
      const chans = channels(node);
      if (!chans.length) table.append(h('tr', {}, h('td', {}, 'wire signals to A–H')));
      for (const p of chans) table.append(h('tr', {}, h('td', {}, p.signal), h('td', {}, formatValue(signalValue(p.signal), signalUnit(p.signal)))));
    } };
  }
  if (kind === 'serial_monitor') return serialCard(node);
  if (kind === 'slider') return sliderCard(node);
  if (kind === 'switch') return switchCard(node);
  return null;
}

export async function setControl(node, value) {
  node.settings.value = value;
  const signal = node.ports[0]?.signal;
  if (!signal) return;
  if (state.graph.signals[signal]) state.graph.signals[signal].initial = value;
  state.dirty = true;
  updateValues();
  emit('control', { node, value });
  const session = state.session;
  if (session && !['done', 'stopped', 'error'].includes(session.status)) {
    try { await api('force', { session: session.session, signal, value }); } catch (error) { emit('error', error); }
  }
}

function serialCard(node) {
  const screenEl = h('pre', { class: 'serial' });
  const input = h('input', { placeholder: 'send to the firmware (Enter)…', style: 'flex:1' });
  const status = h('span', { class: 'note' });
  const endings = { CRLF: '\r\n', LF: '\n', none: '' };
  const send = async () => {
    const signal = node.ports[0]?.signal;
    const session = state.session;
    if (!signal) return (status.textContent = 'wire a UART pin to A');
    if (!session || ['done', 'stopped', 'error'].includes(session.status)) return (status.textContent = 'run the simulation first');
    try {
      await api('uart', { session: session.session, signal, text: input.value + (endings[node.settings.line_end] ?? '\r\n') });
      input.value = ''; status.textContent = '';
    } catch (error) { status.textContent = error.message; }
  };
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') send(); });
  const ending = select(['CRLF', 'LF', 'none'], node.settings.line_end || 'CRLF', (v) => setting(node, 'line_end', v));
  const root = h('div', { class: 'card wide' }, header(node, ending),
    h('div', { class: 'serial-body' }, screenEl, h('div', { class: 'serial-input' }, input, h('button', { onclick: send }, 'Send'), status)));
  root.style.height = '230px';
  let shown = -1;
  return { root, draw: () => {
    const signal = node.ports[0]?.signal;
    const now = data.now();
    const lines = signal ? data.events.filter((e) => (e.topic === signal || e.topic === signal + '.tx') && e.time_seconds <= now + 1e-9) : [];
    if (lines.length === shown) return;
    shown = lines.length;
    screenEl.textContent = !signal ? 'Wire a Renode UART pin (uart:…) to channel A.'
      : lines.slice(-400).map((e) => `${e.time_seconds.toFixed(3).padStart(8)}  ${e.topic.endsWith('.tx') ? '> ' : ''}${String(e.payload).replace(/[\r\n]+$/, '')}`).join('\n') || '(no data yet)';
    screenEl.scrollTop = screenEl.scrollHeight;
  } };
}

function sliderCard(node) {
  const s = node.settings;
  const big = h('div', { class: 'big' });
  const input = h('input', { type: 'range', min: s.minimum, max: s.maximum, step: s.step || (s.maximum - s.minimum) / 1000, value: s.value });
  input.addEventListener('input', () => { setControl(node, Number(input.value)); big.textContent = formatValue(Number(input.value), s.unit); });
  const num = (key) => h('input', { type: 'number', value: s[key], style: 'width:70px', onchange: (e) => { s[key] = Number(e.target.value); input.min = s.minimum; input.max = s.maximum; input.step = s.step || (s.maximum - s.minimum) / 1000; state.dirty = true; } });
  const root = h('div', { class: 'card' }, header(node, 'min', num('minimum'), 'max', num('maximum')),
    h('div', { class: 'control' }, big, input, h('div', { class: 'note' }, `drives ${node.ports[0].signal} live`)));
  big.textContent = formatValue(Number(s.value), s.unit);
  return { root, draw: () => {} };
}

function switchCard(node) {
  const s = node.settings;
  const toggle = h('div', { class: 'toggle' + (Number(s.value) === Number(s.on) ? ' on' : '') });
  const label = h('div', { class: 'big' });
  const paint = () => { const isOn = Number(s.value) === Number(s.on); toggle.classList.toggle('on', isOn); label.textContent = `${isOn ? 'ON' : 'OFF'}  (${formatValue(Number(s.value), s.unit)})`; };
  const set = (isOn) => { setControl(node, Number(isOn ? s.on : s.off)); paint(); };
  if (s.momentary) {
    toggle.addEventListener('mousedown', () => set(true));
    window.addEventListener('mouseup', () => { if (Number(s.value) === Number(s.on)) set(false); });
  } else toggle.addEventListener('click', () => set(Number(s.value) !== Number(s.on)));
  const momentary = h('input', { type: 'checkbox', checked: !!s.momentary, onchange: (e) => { s.momentary = e.target.checked; state.dirty = true; rebuild(true); } });
  const root = h('div', { class: 'card' }, header(node, h('label', {}, momentary, ' push button')),
    h('div', { class: 'control' }, h('div', { style: 'display:flex;gap:14px;align-items:center' }, toggle, label), h('div', { class: 'note' }, `drives ${node.ports[0].signal} live`)));
  paint();
  return { root, draw: () => {} };
}

export function rebuild(force = false) {
  const nodes = (state.graph?.nodes || []).filter((n) => n.kind === 'instrument' || n.kind === 'source');
  const sig = JSON.stringify(nodes.map((n) => [n.id, n.label, n.part, n.ports.map((p) => p.signal)]));
  if (!force && sig === signature) return redrawAll();
  signature = sig;
  grid.innerHTML = '';
  cards = new Map();
  for (const node of nodes) {
    const card = buildCard(node);
    if (!card) continue;
    card.node = node;
    card.root.addEventListener('mousedown', () => { state.selection = { type: 'node', id: node.id }; emit('selection'); emit('graph-changed-soft'); });
    cards.set(node.id, card);
    grid.append(card.root);
  }
  empty.classList.toggle('hidden', cards.size > 0);
  redrawAll();
}

export function redrawAll() {
  for (const [id, card] of cards) {
    card.root.classList.toggle('selected', state.selection?.type === 'node' && state.selection.id === id);
    try { card.draw(); } catch (error) { console.error(error); }
  }
}

export function reveal(nodeId) {
  const card = cards.get(nodeId);
  if (card) { card.root.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); card.root.classList.add('selected'); }
}

on('graph-changed', () => rebuild());
on('selection', () => redrawAll());
