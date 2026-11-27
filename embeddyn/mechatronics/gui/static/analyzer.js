// Analyzer dock page: decoded CAN/CANopen, BLE and 802.11 traffic of a run (a small Wireshark).
//
// Packets come decoded from the server (/api/analyzer, embeddyn/mechatronics/comms/analyzer.py).
// The page shows a lane per board (who sent what, when), the packet list with protocol toggles and
// a filter, the field tree of the selected packet and its bytes; selecting a field highlights its
// bytes. Clicking a packet moves the run's time cursor there, so instruments show that moment.
import { api, data, emit, markDirty, state, toast } from './store.js';
import { buildStats, correlate, messageKey, readingOf, readings } from './bytediff.js';

const $ = (id) => document.getElementById(id);
const COLORS = { CANopen: '#e67e22', CAN: '#d35400', BLE: '#2f80d1', '802.11': '#27ae60' };
const TYPE_TINT = { EMCY: '#ff5c5c', 'SDO abort': '#ff5c5c', NMT: '#c39bd3', SYNC: '#8d96a5', heartbeat: '#6b7380', SDO: '#f0b429' };
const an = { packets: [], session: null, polling: null, selected: null, field: null, visible: false, follow: true,
  shown: [], lastCursor: -1, rowHeight: 20,
  mode: 'list', diffKey: null, diffRange: null, diffReading: null,
  capture: null, captureName: '', fit: null, corr: [], corrSignal: '' };

// Decoder settings of the open project (metadata.analyzer); named fields are written here.
function config() {
  const extra = (state.graph.extra = state.graph.extra || {});
  const metadata = (extra.metadata = extra.metadata || {});
  return (metadata.analyzer = metadata.analyzer || {});
}
const sessionId = () => an.capture || state.session?.session;

const html = (tag, attrs = {}, ...children) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') e.className = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) if (c !== null && c !== undefined) e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return e;
};

// ---------------------------------------------------------------------------------------------
// Filter: words match the summary/source/target/type; key==value terms test fields.
function compileFilter(text) {
  const terms = text.trim().split(/\s+/).filter(Boolean).map((term) => {
    const m = /^(\w+)(==|!=|>=|<=|>|<|~)(.+)$/.exec(term);
    if (!m) { const t = term.toLowerCase(); return (p) => `${p.summary} ${p.source} ${p.target} ${p.type} ${p.proto}`.toLowerCase().includes(t); }
    const [, key, op, raw] = m;
    const num = Number(raw);
    return (p) => {
      let v = key === 'id' ? p.id : key === 'node' ? p.node : key === 'src' ? p.source : key === 'dst' ? p.target
        : key === 'ch' || key === 'channel' ? p.channel : key === 'len' ? p.length : key === 'rssi' ? p.rssi : p[key];
      if (key === 'node' && v === undefined && p.id !== undefined) v = p.id & 0x7f;
      if (v === undefined || v === null) return op === '!=';
      if (op === '~') return String(v).toLowerCase().includes(raw.toLowerCase());
      const a = typeof v === 'number' && !Number.isNaN(num) ? v : String(v).toLowerCase();
      const b = typeof v === 'number' && !Number.isNaN(num) ? num : raw.toLowerCase();
      return op === '==' ? a === b : op === '!=' ? a !== b : op === '>' ? a > b : op === '<' ? a < b : op === '>=' ? a >= b : a <= b;
    };
  });
  return (p) => terms.every((t) => t(p));
}

function protoOn(p) {
  const key = p.proto === 'CAN' ? 'CANopen' : p.proto;
  return $(`an-p-${key.replace('.', '')}`)?.checked !== false;
}

function filtered() {
  let test;
  try { test = compileFilter($('an-filter').value); $('an-filter').classList.remove('bad'); } catch { test = () => true; $('an-filter').classList.add('bad'); }
  return an.packets.filter((p) => protoOn(p) && test(p));
}

// ---------------------------------------------------------------------------------------------
// Rendering

function render() {
  if (!an.visible) return;
  an.shown = filtered();
  const counts = {};
  for (const p of an.packets) counts[p.proto === 'CAN' ? 'CANopen' : p.proto] = (counts[p.proto === 'CAN' ? 'CANopen' : p.proto] || 0) + 1;
  for (const key of ['CANopen', 'BLE', '802.11']) $(`an-c-${key.replace('.', '')}`).textContent = counts[key] || 0;
  $('an-count').textContent = `${an.shown.length} of ${an.packets.length} packets`;
  renderLanes();
  if (an.mode === 'diff') return renderDiff();
  renderList();
  renderDetail();
}

// ---------------------------------------------------------------------------------------------
// Changes view (bytediff.js): one row per message, changed bytes highlighted.

function diffUntil() { return !an.capture && data.length && !data.follow ? data.now() : Infinity; }

function renderDiff() {
  const until = diffUntil();
  const stats = buildStats(an.shown, until, $('an-d-mask').checked);
  an.diffStats = stats;
  const fade = Math.max(0.01, Number($('an-d-fade').value) / 1000 || 0.5);
  const now = Number.isFinite(until) ? until : Math.max(0, ...[...stats.values()].map((s) => s.last));
  const hideStatic = $('an-d-static').checked;
  const order = { CANopen: 0, CAN: 0, BLE: 1, '802.11': 2 };
  const rows = [...stats.values()].filter((s) => !hideStatic || s.dynamic)
    .sort((a, b) => order[a.proto] - order[b.proto] || a.key.localeCompare(b.key, undefined, { numeric: true }));
  const table = $('an-diff-table');
  const keep = table.scrollTop;
  table.innerHTML = '';
  if (!rows.length) table.append(html('div', { class: 'empty' }, stats.size ? 'No message changed: untick "hide static".' : 'Run the project first.'));
  for (const s of rows) {
    const bytes = html('span', { class: 'bytes' });
    s.bytes.forEach((b, i) => {
      const changes = s.changes[i] || 0;
      let cls = 'db', style = '';
      if (s.mask.has(i)) cls += ' mask';
      else if (s.changedNow[i] && now - s.last < fade) cls += ' hot';
      else if (changes) {
        const age = now - (s.lastChange[i] ?? -1e9);
        const alpha = age < fade ? 0.85 * (1 - age / fade) + 0.15 : 0;
        if (alpha) style = `background: rgba(240,180,41,${alpha.toFixed(2)}); color: ${alpha > 0.5 ? '#111' : ''}`;
      } else cls += ' static';
      if (an.diffKey === s.key && an.diffRange && i >= an.diffRange[0] && i < an.diffRange[1]) cls += ' sel';
      bytes.append(html('span', { class: cls, style,
        title: `byte ${i}: 0x${b.toString(16).padStart(2, '0')} = ${b}` + (changes ? `, changed ${changes}× (last ${(s.lastChange[i]).toFixed(4)} s)` : ', never changed'),
        onclick: (e) => { e.stopPropagation(); pickByte(s.key, i, e.shiftKey); } }, b.toString(16).padStart(2, '0')));
    });
    table.append(html('div', { class: 'dr' + (an.diffKey === s.key ? ' selected' : ''), onclick: () => { an.diffKey = s.key; an.diffRange = null; renderDiff(); } },
      html('span', { class: 'k', title: s.key }, s.key), html('span', { class: 'ty', title: s.latest.summary }, s.type),
      html('span', { class: 'n' }, s.count), html('span', { class: 'per' }, s.period ? `${(s.period * 1000).toFixed(1)} ms` : ''), bytes));
  }
  table.scrollTop = keep;
  renderDiffSide();
}

function pickByte(key, index, extend) {
  if (extend && an.diffKey === key && an.diffRange) {
    const start = Math.min(an.diffRange[0], index), end = Math.max(an.diffRange[1], index + 1);
    an.diffRange = [start, Math.min(end, start + 4)];
  } else an.diffRange = [index, index + 1];
  an.diffKey = key;
  const width = an.diffRange[1] - an.diffRange[0];
  if (!an.diffReading || !an.diffReading.startsWith(width === 1 ? 'u8' : `u${8 * width}`) && !an.diffReading.startsWith(width === 1 ? 'i8' : `i${8 * width}`)) {
    an.diffReading = width === 1 ? 'u8' : `u${8 * width} LE`;
  }
  renderDiff();
}

function correlationTools(side) {
  const names = an.capture ? [] : data.names.filter((n) => data.column(n)?.some((v) => v !== null));
  const select = html('select', {}, html('option', { value: '' }, names.length ? 'signal…' : 'run the project first'),
    names.map((n) => html('option', { value: n, selected: n === an.corrSignal }, n)));
  const run = () => {
    an.corrSignal = select.value;
    if (!an.corrSignal) return;
    const column = data.column(an.corrSignal);
    const signalAt = (t) => { const i = Math.min(data.length - 1, data.indexAt(t)); return column[i]; };
    an.corr = correlate(an.shown, signalAt, $('an-d-mask').checked);
    renderDiffSide();
  };
  side.append(html('h4', {}, 'Find the bytes that carry a signal'),
    html('div', { class: 'an-tools' }, select, html('button', { onclick: run, title: 'Correlate every 1/2/4-byte reading of every message with the signal' }, 'Find')));
  if (an.corrSignal && an.corr.length) {
    const table = html('table', { class: 'corr' }, html('tr', {}, ['message', 'bytes', 'as', 'r', 'scale', 'offset'].map((h) => html('td', { class: 'muted' }, h))));
    for (const c of an.corr) {
      const on = an.diffKey === c.key && an.diffRange?.[0] === c.start && an.diffRange?.[1] === c.end && an.diffReading === c.kind;
      table.append(html('tr', { class: on ? 'on' : '', title: `${an.corrSignal} ≈ ${c.scale.toPrecision(5)} × reading + ${c.offset.toPrecision(5)} (${c.n} packets)`,
        onclick: () => { an.diffKey = c.key; an.diffRange = [c.start, c.end]; an.diffReading = c.kind; an.fit = { ...c, signal: an.corrSignal }; renderDiff(); } },
      html('td', {}, c.key), html('td', {}, c.end - c.start > 1 ? `${c.start}–${c.end - 1}` : c.start), html('td', {}, c.kind),
      html('td', { style: `color:${Math.abs(c.r) > 0.98 ? 'var(--ok)' : Math.abs(c.r) > 0.8 ? 'var(--warn)' : ''}` }, c.r.toFixed(3)),
      html('td', {}, c.scale.toPrecision(4)), html('td', {}, c.offset.toPrecision(4))));
    }
    side.append(table);
  } else if (an.corrSignal) side.append(html('div', { class: 'note' }, 'No changing bytes to compare yet.'));
}

function namedFields(side, key) {
  const fields = (config().fields || {})[key] || [];
  if (!fields.length) return;
  side.append(html('h4', {}, 'Named fields of this message'), html('div', { class: 'named' }, fields.map((f, i) => html('div', {},
    `${f.name}: bytes ${f.start}, ${f.reading}, × ${f.scale} + ${f.offset} ${f.unit || ''}`,
    html('b', { title: 'Remove', onclick: () => { fields.splice(i, 1); saveConfig(); } }, '×')))));
}

function nameBytes(s, start, end) {
  const fit = an.fit && an.fit.key === s.key && an.fit.start === start && an.fit.end === end && an.fit.kind === an.diffReading ? an.fit : null;
  const name = prompt(`Name for bytes ${start}${end - start > 1 ? '–' + (end - 1) : ''} of ${s.key} (${an.diffReading}):`, fit ? fit.signal.split('.').pop() : '');
  if (!name) return;
  const unit = prompt('Unit (optional):', fit ? (data.units[fit.signal] || '') : '') ?? '';
  const scale = Number(prompt('Scale (value = scale × raw + offset):', fit ? String(Number(fit.scale.toPrecision(6))) : '1'));
  const offset = Number(prompt('Offset:', fit ? String(Number(fit.offset.toPrecision(6))) : '0'));
  const cfg = config();
  cfg.fields = cfg.fields || {};
  (cfg.fields[s.key] = cfg.fields[s.key] || []).push({ name, start, reading: an.diffReading, scale: Number.isFinite(scale) ? scale : 1,
    offset: Number.isFinite(offset) ? offset : 0, unit });
  saveConfig();
  toast(`${name} added to the decoder (saved with the project)`);
}

async function saveConfig() {
  markDirty();
  const session = sessionId();
  if (!session) return renderDiffSide();
  try {
    await api('analyzer/config', { session, config: config() });
    an.packets = []; an.selected = null;          // decode again with the new fields
    stopPolling(); startPolling();
  } catch (error) { toast(error.message, true); }
}

function renderDiffSide() {
  const side = $('an-diff-side');
  side.innerHTML = '';
  correlationTools(side);
  const s = an.diffStats?.get(an.diffKey);
  if (!s) { side.append(html('div', { class: 'empty' }, 'Select a message. Click a byte (shift-click for up to 4) to read it as a number and plot it.')); return; }
  side.append(html('div', { class: 'an-title' }, `${s.key} · ${s.count} packets` + (s.period ? ` · every ${(s.period * 1000).toFixed(1)} ms` : '') +
    (s.lengths.size > 1 ? ` · lengths ${[...s.lengths].join('/')}` : ` · ${s.bytes.length} bytes`)), html('div', { class: 'note' }, s.latest.summary));
  // Bit grid: flip counts as a heat map, current bit values, bits that flipped in the latest packet outlined.
  const bitsTitle = html('h4', {}, 'Bits of the changing bytes (flip-count heat map, MSB left)');
  const grid = html('table', { class: 'bitgrid' });
  const maxFlips = Math.max(1, ...s.flips.filter(Boolean).flat());
  grid.append(html('tr', {}, html('td', { class: 'idx' }, ''), html('td', { class: 'idx' }, ''), [7, 6, 5, 4, 3, 2, 1, 0].map((b) => html('td', { class: 'idx' }, b))));
  s.bytes.forEach((byte, i) => {
    const selected = an.diffRange && i >= an.diffRange[0] && i < an.diffRange[1];
    if (!selected && (s.mask.has(i) || !s.changes[i])) return;      // constants and checksums: not here
    const row = html('tr', { class: selected ? 'sel' : '' }, html('td', { class: 'idx', onclick: (e) => pickByte(s.key, i, e.shiftKey) }, i),
      html('td', { class: 'hx' }, byte.toString(16).padStart(2, '0')));
    for (let b = 7; b >= 0; b--) {
      const flips = s.flips[i]?.[b] || 0;
      const heat = flips / maxFlips;
      const flippedNow = s.before && s.before[i] !== undefined && ((s.before[i] ^ byte) >> b & 1);
      row.append(html('td', { class: flippedNow ? 'now' : '', title: `byte ${i} bit ${b}: flipped ${flips}×`,
        style: `background: ${flips ? `rgba(255,${Math.round(200 - 140 * heat)},40,${(0.25 + 0.75 * heat).toFixed(2)})` : '#22262c'}; color: ${heat > 0.5 ? '#111' : '#9aa3b0'}`,
        onclick: (e) => pickByte(s.key, i, e.shiftKey) }, byte >> b & 1));
    }
    grid.append(row);
  });
  if (!an.diffRange) { namedFields(side, s.key); side.append(bitsTitle, grid); return; }
  const [start, end] = an.diffRange;
  const decoded = [];
  const walk = (fields) => { for (const f of fields || []) { if (f.bytes && f.bytes[0] <= start && f.bytes[1] >= end && !f.children?.length) decoded.push(`${f.label}: ${f.value}`); walk(f.children); } };
  walk(s.latest.fields);
  side.append(html('h4', {}, `Bytes ${start}${end - start > 1 ? `–${end - 1}` : ''} read as`));
  if (decoded.length) side.append(html('div', { class: 'note' }, `decoder: ${decoded.join(' · ')}`));
  const table = html('table', { class: 'readings' });
  for (const [kind, value] of readings(s.bytes, start, end)) {
    table.append(html('tr', { class: kind === an.diffReading ? 'on' : '', onclick: () => { an.diffReading = kind; renderDiffSide(); } },
      html('td', {}, kind), html('td', {}, value), html('td', {}, `0x${(value < 0 ? value + 2 ** (8 * (end - start)) : value).toString(16).toUpperCase()}`)));
  }
  side.append(table, html('canvas', { id: 'an-spark', title: 'The selected reading over the run (yellow: time cursor)' }),
    html('div', { class: 'an-tools' }, html('button', { onclick: () => nameBytes(s, start, end), title: 'Add these bytes to the decoder as a named field' }, '🏷 Name these bytes…')));
  namedFields(side, s.key);
  side.append(bitsTitle, grid);
  drawSpark(s, start, end);
}

function drawSpark(s, start, end) {
  const canvas = $('an-spark');
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (!w) return;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const g = canvas.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  const points = s.history.map((p) => [p.time, readingOf((p.hex.match(/../g) || []).map((x) => parseInt(x, 16)), start, end, an.diffReading)])
    .filter(([, v]) => v !== null);
  if (!points.length) return;
  const t1 = Math.max(state.graph?.duration_seconds || 0, points[points.length - 1][0], 1e-3);
  let lo = Math.min(...points.map((p) => p[1])), hi = Math.max(...points.map((p) => p[1]));
  if (lo === hi) { lo -= 1; hi += 1; }
  const x = (t) => 4 + (t / t1) * (w - 8), y = (v) => h - 14 - ((v - lo) / (hi - lo)) * (h - 26);
  g.strokeStyle = '#3fd0ff'; g.lineWidth = 1.3; g.beginPath();
  points.forEach(([t, v], i) => { if (i) g.lineTo(x(t), y(points[i - 1][1])); (i ? g.lineTo : g.moveTo).call(g, x(t), y(v)); });
  g.stroke();
  g.fillStyle = '#8d96a5'; g.font = '10px monospace';
  g.fillText(`max ${hi}`, 6, 10); g.fillText(`min ${lo}  (${an.diffReading}, ${points.length} samples)`, 6, h - 3);
  if (data.length) { g.fillStyle = '#ffd60a'; g.fillRect(x(data.now()), 0, 1, h); }
}

function renderList() {
  const list = $('an-list');
  const body = $('an-rows');
  const total = an.shown.length;
  body.style.height = `${total * an.rowHeight}px`;
  const top = list.scrollTop, height = list.clientHeight;
  const first = Math.max(0, Math.floor(top / an.rowHeight) - 10), last = Math.min(total, Math.ceil((top + height) / an.rowHeight) + 10);
  body.innerHTML = '';
  for (let i = first; i < last; i++) {
    const p = an.shown[i];
    const row = html('div', { class: 'an-row' + (an.selected === p.no ? ' selected' : '') + (p.ok ? '' : ' bad'),
      style: `top:${i * an.rowHeight}px; border-left-color:${TYPE_TINT[p.type] || COLORS[p.proto]}`, onclick: () => select(p) },
    html('span', { class: 'c-no' }, p.no), html('span', { class: 'c-t' }, p.time.toFixed(6)),
    html('span', { class: 'c-proto', style: `color:${COLORS[p.proto]}` }, p.proto),
    html('span', { class: 'c-src' }, p.source || ''), html('span', { class: 'c-dst' }, p.target || ''),
    html('span', { class: 'c-len' }, p.length), html('span', { class: 'c-info' }, p.summary));
    body.append(row);
  }
}

function select(p, moveCursor = true) {
  an.selected = p.no;
  an.field = null;
  if (moveCursor && data.length && !an.capture) {
    data.follow = false;
    const follow = document.getElementById('opt-follow');
    if (follow) follow.checked = false;
    data.cursor = Math.min(data.length - 1, data.indexAt(p.time));
    an.lastCursor = data.cursor;
    emit('control');                 // redraw instruments at the new cursor
  }
  renderList(); renderLanes(); renderDetail();
}

function current() { return an.packets.find((p) => p.no === an.selected) || null; }

function renderDetail() {
  const p = current();
  const tree = $('an-tree'), hex = $('an-hex');
  tree.innerHTML = ''; hex.innerHTML = '';
  if (!p) { tree.append(html('div', { class: 'empty' }, an.packets.length ? 'Select a packet.' : 'Run the project: bus and radio traffic shows here.')); return; }
  tree.append(html('div', { class: 'an-title' }, `#${p.no} · ${p.time.toFixed(6)} s · ${p.proto} ${p.type}`,
    html('button', { class: 'mini', title: 'Send this packet again into the running simulation', onclick: () => replay(p) }, '↻ Replay'),
    html('button', { class: 'mini', title: 'Copy into the sender to change and send it', onclick: () => toSender(p) }, '✎ Edit & send')));
  const add = (items, parent, depth) => {
    for (const f of items || []) {
      const node = html('div', { class: 'an-field' + (an.field === f ? ' selected' : ''), style: `padding-left:${depth * 14 + 4}px`,
        onclick: (e) => { e.stopPropagation(); an.field = f; renderDetail(); } },
      f.children?.length ? '▾ ' : '• ', html('b', {}, f.label), f.value !== '' && f.value !== undefined ? `: ${f.value}` : '');
      parent.append(node);
      if (f.children?.length) add(f.children, parent, depth + 1);
    }
  };
  add(p.fields, tree, 0);
  const bytes = p.hex.match(/../g) || [];
  const range = an.field?.bytes;
  const key = messageKey(p);
  const index = an.packets.indexOf(p);
  let previous = null;
  for (let k = index - 1; k >= 0; k--) if (messageKey(an.packets[k]) === key) { previous = an.packets[k].hex.match(/../g) || []; break; }
  for (let i = 0; i < bytes.length; i += 16) {
    const line = html('div', { class: 'an-hexline' }, html('span', { class: 'off' }, i.toString(16).padStart(4, '0')));
    const hexPart = html('span', { class: 'hx' }), ascii = html('span', { class: 'asc' });
    for (let j = i; j < Math.min(i + 16, bytes.length); j++) {
      const on = range && j >= range[0] && j < range[1];
      const changed = previous && previous[j] !== undefined && previous[j] !== bytes[j];
      hexPart.append(html('span', { class: (on ? 'on ' : '') + (changed ? 'chg' : ''), title: changed ? `was ${previous[j]}` : '' }, bytes[j] + ' '));
      const c = parseInt(bytes[j], 16);
      ascii.append(html('span', { class: on ? 'on' : '' }, c >= 32 && c < 127 ? String.fromCharCode(c) : '.'));
    }
    line.append(hexPart, ascii);
    hex.append(line);
  }
  if (!bytes.length) hex.append(html('div', { class: 'empty' }, 'no payload'));
}

// Lanes: one per sender, ticks coloured by protocol over the run (or the visible window).
function renderLanes() {
  const canvas = $('an-lanes');
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (!w || !h) return;
  const dpr = window.devicePixelRatio || 1;
  if (canvas.width !== w * dpr) { canvas.width = w * dpr; canvas.height = h * dpr; }
  const g = canvas.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);
  const senders = [...new Set(an.packets.map((p) => p.source || '?'))];
  const t1 = Math.max(state.graph?.duration_seconds || 0, ...an.packets.slice(-1).map((p) => p.time), 1e-3);
  const left = 70, laneH = Math.max(10, Math.min(18, (h - 14) / Math.max(1, senders.length)));
  an.lanes = { senders, t1, left, laneH, w };
  g.font = '10px sans-serif';
  senders.forEach((s, i) => {
    const y = 4 + i * laneH;
    g.fillStyle = i % 2 ? '#22262c' : '#1d2025'; g.fillRect(0, y, w, laneH);
    g.fillStyle = '#8d96a5'; g.fillText(s.length > 11 ? s.slice(0, 10) + '…' : s, 4, y + laneH - 5);
  });
  const shown = new Set(an.shown.map((p) => p.no));
  for (const p of an.packets) {
    const i = senders.indexOf(p.source || '?');
    const x = left + (p.time / t1) * (w - left - 6);
    g.fillStyle = shown.has(p.no) ? (TYPE_TINT[p.type] && p.type !== 'heartbeat' && p.type !== 'SYNC' ? TYPE_TINT[p.type] : COLORS[p.proto]) : '#3a4049';
    g.fillRect(x, 5 + i * laneH, p.type === 'EMCY' ? 3 : 1.2, laneH - 2);
  }
  if (data.length) {
    const x = left + (data.now() / t1) * (w - left - 6);
    g.fillStyle = '#ffd60a'; g.fillRect(x, 0, 1, h);
  }
  const p = current();
  if (p) {
    const x = left + (p.time / t1) * (w - left - 6), i = senders.indexOf(p.source || '?');
    g.strokeStyle = '#fff'; g.lineWidth = 1.5; g.strokeRect(x - 3, 3 + i * laneH, 6, laneH);
  }
  g.fillStyle = '#6b7380';
  g.fillText('0 s', left, h - 2);
  g.fillText(`${t1.toFixed(2)} s`, w - 40, h - 2);
}

function lanesClick(e) {
  if (!an.lanes || !an.shown.length) return;
  const r = e.target.getBoundingClientRect();
  const { senders, t1, left, laneH, w } = an.lanes;
  const t = ((e.clientX - r.left - left) / (w - left - 6)) * t1;
  const lane = senders[Math.floor((e.clientY - r.top - 4) / laneH)];
  let best = null;
  for (const p of an.shown) if (!lane || (p.source || '?') === lane) if (!best || Math.abs(p.time - t) < Math.abs(best.time - t)) best = p;
  if (best) { select(best); scrollTo(best); }
}

function scrollTo(p) {
  const i = an.shown.indexOf(p);
  if (i < 0) return;
  const list = $('an-list');
  const y = i * an.rowHeight;
  if (y < list.scrollTop || y > list.scrollTop + list.clientHeight - an.rowHeight) list.scrollTop = y - list.clientHeight / 2;
  renderList();
}

// Follow the run's time cursor (replay, scrubbing): select the last packet at or before it.
export function onFrame() {
  if (!an.visible || !an.packets.length || data.cursor === an.lastCursor || an.capture) return;
  an.lastCursor = data.cursor;
  if (an.mode === 'diff') { renderLanes(); renderDiff(); return; }
  if (!$('an-sync').checked) { renderLanes(); return; }
  const now = data.now();
  let best = null;
  for (const p of an.shown) { if (p.time <= now + 1e-12) best = p; else break; }
  if (best && best.no !== an.selected) { an.selected = best.no; an.field = null; scrollTo(best); renderDetail(); }
  renderLanes();
}

// ---------------------------------------------------------------------------------------------
// Data

async function poll() {
  const session = sessionId();
  if (!session) return;
  if (session !== an.session) { an.session = session; an.packets = []; an.selected = null; }
  try {
    const response = await fetch(`/api/analyzer?session=${session}&from=${an.packets.length}`);
    const result = await response.json();
    if (result.error) return;
    if (result.packets.length) { an.packets.push(...result.packets); render(); }
    renderInjections(result.injections || []);
    for (const proto of ['can', 'ble', 'wifi']) $(`an-pcap-${proto}`).href = `/api/pcap?session=${session}&proto=${proto}`;
    if (['done', 'stopped', 'error'].includes(result.status) && result.total === an.packets.length) stopPolling();
  } catch { /* the server restarted or the run is gone */ }
}

function startPolling() { if (!an.polling) { an.polling = setInterval(poll, 400); poll(); } }
function stopPolling() { clearInterval(an.polling); an.polling = null; }

export function setVisible(visible) {
  an.visible = visible;
  if (visible) { startPolling(); requestAnimationFrame(render); } else stopPolling();
}

export function runStarted() {
  an.session = null; an.packets = []; an.selected = null; an.capture = null; an.corr = [];
  $('an-count').classList.remove('an-capture');
  if (an.visible) { stopPolling(); startPolling(); }
}

// ---------------------------------------------------------------------------------------------
// Frame sender, captures, DBC

function payloadOf(p) {
  if (p.proto === 'CAN' || p.proto === 'CANopen') return { proto: 'can', id: p.id, data: p.hex };
  return { proto: p.proto === 'BLE' ? 'ble' : 'wifi', channel: p.channel, rssi: p.rssi, data: p.hex };
}

function injectTopic(proto) {
  const map = config().inject || {};
  return map[proto] || (proto === 'can' ? 'can' : proto);
}

async function send(topic, payload, periodMs = 0, count = 0) {
  if (an.capture) return toast('A capture is open: run the project to send frames into it', true);
  const session = state.session?.session;
  if (!session || !['running', 'paused', 'starting'].includes(state.session?.status)) return toast('Start a run first: frames go into the running simulation', true);
  try {
    const result = await api('analyzer/inject', { session, topic, payload, period_ms: periodMs, count });
    renderInjections(result.injections);
    if (!an.polling) startPolling();
  } catch (error) { toast(error.message, true); }
}

function replay(p) { const payload = payloadOf(p); send(injectTopic(payload.proto), payload); }

function toSender(p) {
  const payload = payloadOf(p);
  $('an-send').classList.remove('hidden');
  $('an-s-proto').value = payload.proto;
  $('an-s-topic').value = injectTopic(payload.proto);
  $('an-s-id').value = payload.id !== undefined ? '0x' + payload.id.toString(16).toUpperCase() : '';
  $('an-s-data').value = (p.hex.match(/../g) || []).join(' ');
  $('an-s-data').focus();
}

function renderInjections(list) {
  const el = $('an-s-list');
  el.innerHTML = '';
  for (const item of list) el.append(html('span', { class: 'an-inj', title: JSON.stringify(item.payload) },
    `${item.topic} ${item.payload?.id !== undefined ? '0x' + Number(item.payload.id).toString(16) : ''} every ${item.period * 1000} ms (${item.sent} sent)`,
    html('b', { onclick: () => api('analyzer/inject_stop', { session: state.session?.session, id: item.id }).then((r) => renderInjections(r.injections)) }, '×')));
}

function senderSend() {
  const proto = $('an-s-proto').value;
  const hex = $('an-s-data').value.replace(/0x/gi, '').replace(/[^0-9a-f]/gi, '');
  if (hex.length % 2) return toast('Data needs whole bytes (an even number of hex digits)', true);
  const payload = { proto, data: hex.toLowerCase() };
  if (proto === 'can') {
    const raw = $('an-s-id').value.trim();
    const id = raw.toLowerCase().startsWith('0x') ? parseInt(raw, 16) : parseInt(raw, 10);
    if (!Number.isFinite(id)) return toast('CAN id needed (0x123 or decimal)', true);
    payload.id = id;
    if (id > 0x7ff) payload.ext = true;
  } else { payload.channel = proto === 'ble' ? 37 : 6; payload.rssi = -40; }
  send($('an-s-topic').value.trim() || injectTopic(proto), payload, Number($('an-s-period').value) || 0, Number($('an-s-count').value) || 0);
}

async function openCapture() {
  const path = prompt('Capture file (pcap, pcapng or candump log), absolute or relative to the project folder:', an.captureName || `${state.graph?.root || ''}/capture.pcap`);
  if (!path) return;
  try {
    const canopen = !!config().canopen && confirm('Decode CAN frames as CANopen, with this project\'s node names and PDO layouts?\n\nCancel: plain CAN (for captures of other buses).');
    const result = await api('analyzer/import', { path, root: state.graph?.root, graph: state.graph, canopen });
    an.capture = result.session; an.captureName = path; an.session = null; an.packets = []; an.selected = null; an.corr = [];
    $('an-count').classList.add('an-capture');
    toast(`${result.events} packets from ${path.split('/').pop()}`);
    stopPolling(); startPolling();
  } catch (error) { toast(error.message, true); }
}

async function showDbc() {
  try {
    const { text } = await api('analyzer/dbc', { config: config() });
    const link = html('a', { class: 'button', download: `${state.graph?.name || 'embeddyn'}.dbc`,
      href: URL.createObjectURL(new Blob([text], { type: 'text/plain' })) }, 'Download .dbc');
    $('modal-title').textContent = 'CAN database (DBC): CANopen PDO layouts and named fields';
    const body = $('modal-body');
    body.innerHTML = '';
    body.append(link, html('pre', {}, text));
    $('modal').classList.remove('hidden');
  } catch (error) { toast(error.message, true); }
}

function bind() {
  const refilter = () => { render(); const p = current(); if (p) scrollTo(p); };   // keep the selection in view
  $('an-filter').addEventListener('input', refilter);
  for (const key of ['CANopen', 'BLE', '80211']) $(`an-p-${key}`).addEventListener('change', refilter);
  $('an-list').addEventListener('scroll', renderList);
  const setMode = (mode) => {
    an.mode = mode;
    $('an-mode-list').classList.toggle('active', mode === 'list');
    $('an-mode-diff').classList.toggle('active', mode === 'diff');
    $('an-main').classList.toggle('hidden', mode !== 'list');
    $('an-diff').classList.toggle('hidden', mode !== 'diff');
    if (mode === 'diff' && !an.diffKey) {
      const p = current();
      if (p) an.diffKey = messageKey(p);
    }
    render();
  };
  $('an-open').onclick = openCapture;
  $('an-dbc').onclick = showDbc;
  $('an-send-toggle').onclick = () => {
    $('an-send').classList.toggle('hidden');
    const topics = new Set([...Object.values(config().inject || {}), ...an.packets.map((p) => p.topic)]);
    $('an-s-topics').innerHTML = '';
    for (const t of topics) $('an-s-topics').append(html('option', { value: t }));
    if (!$('an-s-topic').value) $('an-s-topic').value = injectTopic($('an-s-proto').value);
  };
  $('an-s-proto').onchange = () => { $('an-s-topic').value = injectTopic($('an-s-proto').value); };
  $('an-s-send').onclick = senderSend;
  $('an-s-data').addEventListener('keydown', (e) => { if (e.key === 'Enter') senderSend(); });
  $('an-s-stop').onclick = () => state.session && api('analyzer/inject_stop', { session: state.session.session }).then((r) => renderInjections(r.injections));
  $('an-mode-list').onclick = () => setMode('list');
  $('an-mode-diff').onclick = () => setMode('diff');
  for (const id of ['an-d-static', 'an-d-mask', 'an-d-fade']) $(id).addEventListener('input', () => an.mode === 'diff' && renderDiff());
  $('an-lanes').addEventListener('click', lanesClick);
  $('an-list').addEventListener('keydown', (e) => {
    if (!['ArrowDown', 'ArrowUp'].includes(e.key) || !an.shown.length) return;
    e.preventDefault();
    const i = Math.max(0, an.shown.findIndex((p) => p.no === an.selected));
    const next = an.shown[Math.max(0, Math.min(an.shown.length - 1, i + (e.key === 'ArrowDown' ? 1 : -1)))];
    select(next); scrollTo(next);
  });
  window.addEventListener('resize', () => an.visible && render());
}

bind();
