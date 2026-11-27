// Analyzer dock page: decoded CAN/CANopen, BLE and 802.11 traffic of a run (a small Wireshark).
//
// Packets come decoded from the server (/api/analyzer, embeddyn/mechatronics/comms/analyzer.py).
// The page shows a lane per board (who sent what, when), the packet list with protocol toggles and
// a filter, the field tree of the selected packet and its bytes; selecting a field highlights its
// bytes. Clicking a packet moves the run's time cursor there, so instruments show that moment.
import { data, emit, state } from './store.js';

const $ = (id) => document.getElementById(id);
const COLORS = { CANopen: '#e67e22', CAN: '#d35400', BLE: '#2f80d1', '802.11': '#27ae60' };
const TYPE_TINT = { EMCY: '#ff5c5c', 'SDO abort': '#ff5c5c', NMT: '#c39bd3', SYNC: '#8d96a5', heartbeat: '#6b7380', SDO: '#f0b429' };
const an = { packets: [], session: null, polling: null, selected: null, field: null, visible: false, follow: true,
  shown: [], lastCursor: -1, rowHeight: 20 };

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
  renderList();
  renderLanes();
  renderDetail();
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
  if (moveCursor && data.length) {
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
  tree.append(html('div', { class: 'an-title' }, `#${p.no} · ${p.time.toFixed(6)} s · ${p.proto} ${p.type}`));
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
  for (let i = 0; i < bytes.length; i += 16) {
    const line = html('div', { class: 'an-hexline' }, html('span', { class: 'off' }, i.toString(16).padStart(4, '0')));
    const hexPart = html('span', { class: 'hx' }), ascii = html('span', { class: 'asc' });
    for (let j = i; j < Math.min(i + 16, bytes.length); j++) {
      const on = range && j >= range[0] && j < range[1];
      hexPart.append(html('span', { class: on ? 'on' : '' }, bytes[j] + ' '));
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
  if (!an.visible || !an.packets.length || data.cursor === an.lastCursor) return;
  an.lastCursor = data.cursor;
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
  const session = state.session?.session;
  if (!session) return;
  if (session !== an.session) { an.session = session; an.packets = []; an.selected = null; }
  try {
    const response = await fetch(`/api/analyzer?session=${session}&from=${an.packets.length}`);
    const result = await response.json();
    if (result.error) return;
    if (result.packets.length) { an.packets.push(...result.packets); render(); }
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

export function runStarted() { an.session = null; an.packets = []; an.selected = null; if (an.visible) { stopPolling(); startPolling(); } }

function bind() {
  const refilter = () => { render(); const p = current(); if (p) scrollTo(p); };   // keep the selection in view
  $('an-filter').addEventListener('input', refilter);
  for (const key of ['CANopen', 'BLE', '80211']) $(`an-p-${key}`).addEventListener('change', refilter);
  $('an-list').addEventListener('scroll', renderList);
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
