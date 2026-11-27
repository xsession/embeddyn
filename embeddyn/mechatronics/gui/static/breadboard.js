// Breadboard view: an ngspice circuit as a solderless-breadboard prototype (Fritzing style).
//
// The board document comes from embeddyn/mechatronics/gui/breadboard.py: parts whose legs sit
// in holes ("a12", rails "tp3"/"tn3"/"bp3"/"bn3"), modules beside the board (supplies, generators,
// MCU pins, signal inputs) and jumper wires. Connectivity is the real board's: the five holes of a
// column half form a strip, a rail is one conductor, a wire joins its ends. Hovering shows a net,
// the check compares the board with the netlist, and bound ngspice vectors show live values.
import { api, data, log, on, signalValue, state, toast } from './store.js';

const NS = 'http://www.w3.org/2000/svg';
const P = 18;                                  // hole pitch (0.1 inch) in drawing units
const ROW_Y = { tn: 1, tp: 2, a: 4, b: 5, c: 6, d: 7, e: 8, f: 10, g: 11, h: 12, i: 13, j: 14, bp: 16, bn: 17 };
const Y_ROW = Object.fromEntries(Object.entries(ROW_Y).map(([k, v]) => [v, k]));
const TOP = 'abcde', BOTTOM = 'fghij';
const MODULE_W = 12, MODULE_H = 4.6;
const WIRE_COLORS = ['#1f6fd1', '#e3b505', '#1e9e4a', '#e67e22', '#8e44ad', '#16a3b8', '#c0392b', '#7f8c8d', '#d12b2b', '#222222', '#f2f2f2'];
const BAND = ['#1b1b1b', '#7b4a12', '#d0241b', '#ef7d16', '#f2d21b', '#2c9a3a', '#2357c4', '#7d3fb8', '#8a8a8a', '#f4f4f4'];
const HOLE_RE = /^([a-j])(\d+)$/, RAIL_RE = /^(tn|tp|bp|bn)(\d+)$/;
const PROBE_RE = /^([VvIi])\(\s*([^,()]+?)\s*(?:,\s*([^,()]+?)\s*)?\)$/;
const PIN_NAMES = {
  resistor: ['1', '2'], capacitor: ['1', '2'], electrolytic: ['+', '-'], inductor: ['1', '2'], diode: ['A', 'K'],
  led: ['A', 'K'], photodiode: ['A', 'K'], ammeter: ['+', '-'], behavioral: ['+', '-'], npn: ['C', 'B', 'E'],
  pnp: ['C', 'B', 'E'], phototransistor: ['C', 'B', 'E'], nmos: ['D', 'G', 'S'], pmos: ['D', 'G', 'S'],
  nmos_power: ['D', 'G', 'S'], pmos_power: ['D', 'G', 'S'], jfet: ['D', 'G', 'S'], supply: ['+', '-'],
  generator: ['+', '-'], signal: ['+', '-'], current_source: ['+', '-'], current_signal: ['+', '-'],
};
const THREE_LEG = new Set(['npn', 'pnp', 'phototransistor', 'nmos', 'pmos', 'nmos_power', 'pmos_power', 'jfet']);
const DIP = new Set(['dependent', 'ic', 'switch']);

const svg = document.getElementById('breadboard');
const wrap = document.getElementById('breadboard-wrap');
const tip = document.getElementById('bb-tip');
const $ = (id) => document.getElementById(id);

export const bb = {
  active: false, circuits: [], palette: [], circuit: null, path: null, board: null, original: '', models: {},
  dirty: false, selection: null, hover: null, conn: null, undo: [], loadedFor: null,
};
const view = { x: 40, y: 40, scale: 1.4 };
let root, live = [];

const el = (tag, attrs = {}, parent) => {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
};
const text = (parent, x, y, content, attrs = {}) => { const t = el('text', { x, y, ...attrs }, parent); t.textContent = content; return t; };
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
// Geometry

const columns = () => Number(bb.board?.board?.columns || 63);
const colX = (c) => (c + 1) * P;
const railOk = (c) => c >= 1 && c <= columns() && c % 6 !== 0;

function validHole(h) {
  let m = HOLE_RE.exec(h);
  if (m) return +m[2] >= 1 && +m[2] <= columns();
  m = RAIL_RE.exec(h);
  if (m) return railOk(+m[2]);
  return typeof h === 'string' && h.startsWith('m:');
}

function moduleById(id) { return bb.board?.parts.find((p) => p.id === id && p.module); }

function terminalXY(part, pinName) {
  const index = Math.max(0, part.pins.findIndex((p) => p.name === pinName));
  const [mx, my] = part.pos || [-15, 0];
  return { x: (mx + MODULE_W) * P + 6, y: (my + 1.5 + index * 1.7) * P };
}

function holeXY(h) {
  let m = HOLE_RE.exec(h || '');
  if (m) return { x: colX(+m[2]), y: ROW_Y[m[1]] * P };
  m = RAIL_RE.exec(h || '');
  if (m) return { x: colX(+m[2]), y: ROW_Y[m[1]] * P };
  if (h?.startsWith('m:')) {
    const [, id, pin] = h.split(':');
    const part = moduleById(id);
    if (part) return terminalXY(part, pin);
  }
  return null;
}

function holeAt(x, y) {
  const c = Math.round(x / P - 1), ry = Math.round(y / P);
  if (Math.abs(x - colX(c)) > 0.48 * P || Math.abs(y - ry * P) > 0.48 * P) return null;
  const row = Y_ROW[ry];
  if (!row || c < 1 || c > columns()) return null;
  const h = `${row}${c}`;
  return validHole(h) ? h : null;
}

function busOf(h) {
  let m = HOLE_RE.exec(h);
  if (m) return `S${m[2]}${TOP.includes(m[1]) ? 't' : 'b'}`;
  m = RAIL_RE.exec(h);
  if (m) return `R${m[1]}`;
  return h;
}

// Move a hole by whole columns and pitch rows; null when it lands outside the board.
function shiftHole(h, dc, dy) {
  let m = HOLE_RE.exec(h) || RAIL_RE.exec(h);
  if (!m) return null;
  const row = Y_ROW[ROW_Y[m[1]] + dy];
  if (!row) return null;
  const moved = `${row}${+m[2] + dc}`;
  return validHole(moved) ? moved : null;
}

// ---------------------------------------------------------------------------------------------
// Connectivity, net names and the check against the netlist (mirrors breadboard.py)

function connectivity(board) {
  const parent = new Map();
  const find = (a) => { if (!parent.has(a)) parent.set(a, a); let r = a; while (parent.get(r) !== r) r = parent.get(r); parent.set(a, r); return r; };
  const join = (a, b) => { const ra = find(a), rb = find(b); if (ra !== rb) parent.set(ra > rb ? ra : rb, ra > rb ? rb : ra); };
  for (const w of board.wires) if (validHole(w.a) && validHole(w.b)) join(busOf(w.a), busOf(w.b));
  const pinGroup = new Map();
  for (const part of board.parts) for (const pin of part.pins || []) {
    const hole = part.module ? `m:${part.id}:${pin.name}` : pin.hole;
    pinGroup.set(`${part.id}|${pin.name}`, hole && validHole(hole) ? find(busOf(hole)) : `open:${part.id}:${pin.name}`);
  }
  const canonical = (n) => (['0', 'gnd', 'gnd!', 'ground'].includes(String(n).toLowerCase()) ? '0' : String(n));
  const votes = new Map();
  for (const part of board.parts) (part.pins || []).forEach((pin, i) => {
    const g = pinGroup.get(`${part.id}|${pin.name}`);
    if (g.startsWith('open:')) return;
    if (!votes.has(g)) votes.set(g, new Map());
    const node = part.spice?.nodes?.[i];
    if (node && !part.added) votes.get(g).set(canonical(node), (votes.get(g).get(canonical(node)) || 0) + 1);
  });
  const names = new Map(), used = new Set();
  const total = (m) => [...m.values()].reduce((a, b) => a + b, 0);
  for (const [g, counter] of [...votes.entries()].sort((a, b) => total(b[1]) - total(a[1]))) {
    let name = counter.has('0') ? '0' : [...counter.entries()].sort((a, b) => b[1] - a[1]).map(([n]) => n).find((n) => !used.has(n));
    if (!name) { let i = 1; while (used.has(`N${String(i).padStart(3, '0')}`)) i++; name = `N${String(i).padStart(3, '0')}`; }
    names.set(g, name); used.add(name);
  }
  const expected = new Map(), members = new Map(), issues = [];
  for (const part of board.parts) (part.pins || []).forEach((pin, i) => {
    const g = pinGroup.get(`${part.id}|${pin.name}`);
    if (g.startsWith('open:')) { issues.push({ kind: 'unplaced', part: part.id, text: `${part.id}.${pin.name} is not in a hole` }); return; }
    const node = part.spice?.nodes?.[i];
    if (!node || part.added) return;
    const n = canonical(node);
    if (!expected.has(n)) expected.set(n, new Set());
    expected.get(n).add(g);
    if (!members.has(g)) members.set(g, new Set());
    members.get(g).add(n);
  });
  for (const [n, groups] of [...expected.entries()].sort()) if (groups.size > 1) issues.push({ kind: 'open', net: n, text: `net ${n} is split into ${groups.size} unconnected pieces` });
  for (const [g, nodes] of members) if (nodes.size > 1) issues.push({ kind: 'short', group: g, nets: [...nodes].sort(), text: `short: ${[...nodes].sort().join(' + ')} are connected` });
  const groupOfHole = (h) => (h && validHole(h) ? find(busOf(h)) : null);
  const nameOf = (g) => names.get(g) || (g?.startsWith('open:') ? 'unconnected' : 'empty strip');
  return { pinGroup, names, nameOf, issues, ok: !issues.some((i) => i.kind !== 'unplaced'), groupOfHole, expected };
}

// ---------------------------------------------------------------------------------------------
// Values: netlist parameters, placeholders and live signals

function paramTable() {
  const table = {};
  for (const line of bb.original.split('\n')) {
    const m = /^\s*\.param\s+(.*)$/i.exec(line);
    if (!m) continue;
    for (const [, k, v] of m[1].matchAll(/([A-Za-z_]\w*)\s*=\s*(\{[^}]*\}\}?|\S+)/g)) table[k.toLowerCase()] = v;
  }
  return table;
}

function resolveNumber(value, depth = 0) {
  if (value === null || value === undefined || depth > 4) return null;
  let v = String(value).trim().split(/\s+/)[0];
  const placeholder = /^\{\{\s*([\w.]+)\s*\}\}$/.exec(v);
  if (placeholder) {
    const name = placeholder[1];
    const sig = state.graph?.signals?.[name];
    if (sig) return Number(sig.initial ?? 0);
    const par = state.graph?.extra?.parameters?.[name];
    if (par !== undefined) return Number(typeof par === 'object' ? par.value : par);
    return null;
  }
  const braced = /^\{\s*([A-Za-z_]\w*)\s*\}$/.exec(v);
  if (braced) return resolveNumber(paramTable()[braced[1].toLowerCase()], depth + 1);
  const m = /^([-+]?(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?)(meg|mil|[tgkmunpfµ])?/i.exec(v);
  if (!m) return null;
  const scale = { t: 1e12, g: 1e9, meg: 1e6, k: 1e3, m: 1e-3, mil: 25.4e-6, u: 1e-6, 'µ': 1e-6, n: 1e-9, p: 1e-12, f: 1e-15 };
  return Number(m[1]) * (scale[(m[2] || '').toLowerCase()] ?? 1);
}

function engineering(v, unit = '') {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  const abs = Math.abs(v);
  if (abs < 1e-15) return `0 ${unit}`.trim();
  const steps = [[1e9, 'G'], [1e6, 'M'], [1e3, 'k'], [1, ''], [1e-3, 'm'], [1e-6, 'µ'], [1e-9, 'n'], [1e-12, 'p'], [1e-15, 'f']];
  const [scale, prefix] = steps.find(([k]) => abs >= k * 0.9995) || steps[steps.length - 1];
  const n = v / scale;
  const digits = Math.abs(n) >= 100 ? 0 : Math.abs(n) >= 10 ? 1 : 2;
  return `${Number(n.toFixed(digits))} ${prefix}${unit}`.trim();
}

function bands(ohms) {
  if (!(ohms > 0) || !Number.isFinite(ohms)) return null;
  let exp = Math.floor(Math.log10(ohms)) - 1;
  let sig = Math.round(ohms / 10 ** exp);
  if (sig >= 100) { sig = Math.round(sig / 10); exp += 1; }
  const mult = exp === -1 ? '#c8a03c' : exp === -2 ? '#c0c0c0' : BAND[Math.max(0, Math.min(9, exp))];
  return [BAND[Math.floor(sig / 10)], BAND[sig % 10], mult, '#c8a03c'];
}

// Live value of a bound vector or input source, in circuit units (undoes the binding's scale/offset).
function outputValue(binding) {
  const v = signalValue(binding.signal);
  return v === null ? null : (v - (binding.offset || 0)) / (binding.scale || 1);
}
function inputValue(binding) {
  const v = signalValue(binding.signal);
  return v === null ? null : v * (binding.scale ?? 1) + (binding.offset || 0);
}
const hasRun = () => data.length > 0;

function ammeterCurrent(ref) {
  const out = (bb.circuit?.outputs || []).find((o) => {
    const m = PROBE_RE.exec(o.vector.trim());
    return m && m[1].toLowerCase() === 'i' && m[2].toLowerCase() === ref.toLowerCase();
  });
  return out ? { value: outputValue(out), binding: out } : null;
}

// ---------------------------------------------------------------------------------------------
// Rendering

function applyView() { root?.setAttribute('transform', `translate(${view.x},${view.y}) scale(${view.scale})`); }

export function render() {
  if (!svg) return;
  svg.innerHTML = '';
  live = [];
  const defs = el('defs', {}, svg);
  const glow = el('filter', { id: 'bb-glow', x: '-100%', y: '-100%', width: '300%', height: '300%' }, defs);
  el('feGaussianBlur', { stdDeviation: 4 }, glow);
  const shade = el('linearGradient', { id: 'bb-rail', x1: 0, x2: 0, y1: 0, y2: 1 }, defs);
  el('stop', { offset: 0, 'stop-color': '#fbfaf5' }, shade); el('stop', { offset: 1, 'stop-color': '#ece8dc' }, shade);
  root = el('g', {}, svg);
  applyView();
  if (!bb.board) {
    text(root, 0, 0, bb.circuits.length ? 'Loading…' : 'This project has no ngspice circuit. Open one with “Netlist path…”.', { class: 'bb-empty' });
    return;
  }
  bb.conn = connectivity(bb.board);
  drawBoard(el('g', { class: 'bb-board' }, root));
  bb.highlightLayer = el('g', { class: 'bb-hl' }, root);
  const modules = el('g', {}, root), parts = el('g', {}, root), wires = el('g', {}, root), probes = el('g', {}, root);
  for (const part of bb.board.parts) {
    if (part.module) drawModule(modules, part);
    else if ((part.pins || []).every((p) => holeXY(p.hole))) drawPart(parts, part);
  }
  for (const wire of bb.board.wires) drawWire(wires, wire);
  drawProbes(probes);
  bb.dragLayer = el('g', {}, root);
  highlight();
  updateLive();
  renderStatus();
}

function drawBoard(g) {
  const n = columns();
  const width = (n + 2.4) * P;
  el('rect', { x: P * 0.6, y: -0.2 * P, width, height: 18.4 * P, rx: 8, class: 'bb-body' }, g);
  el('rect', { x: P * 0.6, y: 8.62 * P, width, height: 0.76 * P, class: 'bb-trench' }, g);
  el('rect', { x: P * 0.6, y: -0.2 * P, width, height: 3.0 * P, rx: 8, fill: 'url(#bb-rail)', opacity: 0.6 }, g);
  el('rect', { x: P * 0.6, y: 15.2 * P, width, height: 3.0 * P, rx: 8, fill: 'url(#bb-rail)', opacity: 0.6 }, g);
  const x0 = colX(1) - P * 0.8, x1 = colX(n) + P * 0.8;
  for (const [y, cls] of [[0.4, 'neg'], [2.6, 'pos'], [15.4, 'pos'], [17.6, 'neg']]) el('line', { x1: x0, x2: x1, y1: y * P, y2: y * P, class: `bb-railline ${cls}` }, g);
  for (const [y, s] of [[1, '−'], [2, '+'], [16, '+'], [17, '−']]) {
    text(g, colX(0) - 2, y * P + 3, s, { class: 'bb-railsign' });
    text(g, colX(n + 1) + 2, y * P + 3, s, { class: 'bb-railsign' });
  }
  let d = '';
  const s = 0.36 * P;
  for (let c = 1; c <= n; c++) {
    for (const row of TOP + BOTTOM) d += `M${colX(c) - s / 2},${ROW_Y[row] * P - s / 2}h${s}v${s}h${-s}z`;
    if (railOk(c)) for (const r of ['tn', 'tp', 'bp', 'bn']) d += `M${colX(c) - s / 2},${ROW_Y[r] * P - s / 2}h${s}v${s}h${-s}z`;
    if (c === 1 || c % 5 === 0) {
      text(g, colX(c), 3.2 * P, String(c), { class: 'bb-label' });
      text(g, colX(c), 15.15 * P, String(c), { class: 'bb-label' });
    }
  }
  el('path', { d, class: 'bb-holes' }, g);
  for (const row of TOP + BOTTOM) {
    text(g, colX(0) + 3, ROW_Y[row] * P + 3, row, { class: 'bb-label' });
    text(g, colX(n + 1) - 3, ROW_Y[row] * P + 3, row, { class: 'bb-label' });
  }
  text(g, x1 - 4, 9.25 * P, bb.board.name || '', { class: 'bb-brand', 'text-anchor': 'end' });
}

function partLabel(part) {
  const v = part.value || part.spice?.value || '';
  const unit = { resistor: 'Ω', capacitor: 'F', electrolytic: 'F', inductor: 'H' }[part.type];
  if (unit) { const number = resolveNumber(v); return number !== null ? engineering(number, unit) : v.split(/\s+/)[0]; }
  if (['diode', 'led', 'photodiode'].includes(part.type)) return part.spice?.model || '';
  return '';
}

function legs(g, part, points) {
  (part.pins || []).forEach((pin, i) => {
    const h = holeXY(pin.hole);
    const from = points[i];
    if (!h || !from) return;
    el('line', { x1: from.x, y1: from.y, x2: h.x, y2: h.y, class: 'bb-leg' }, g);
    el('circle', { cx: h.x, cy: h.y, r: 0.2 * P, class: 'bb-legend', 'data-handle': `pin|${part.id}|${pin.name}` }, g);
  });
}

function drawPart(layer, part) {
  const g = el('g', { class: 'bb-part' + (bb.selection?.type === 'part' && bb.selection.id === part.id ? ' selected' : ''), 'data-part': part.id }, layer);
  const title = el('title', {}, g);
  title.textContent = `${part.ref} (${part.type}) ${part.value || part.spice?.model || ''}`;
  const pts = part.pins.map((p) => holeXY(p.hole));
  if (part.pins.length === 2 && !DIP.has(part.type)) return drawAxial(g, part, pts[0], pts[1]);
  if (THREE_LEG.has(part.type)) return drawTransistor(g, part, pts);
  return drawDip(g, part, pts);
}

function drawAxial(g, part, a, b) {
  const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
  const len = Math.hypot(b.x - a.x, b.y - a.y);
  const angle = Math.atan2(b.y - a.y, b.x - a.x) * 180 / Math.PI;
  legs(g, part, [{ x: mx, y: my }, { x: mx, y: my }]);
  const upright = ['capacitor', 'led', 'photodiode'].includes(part.type);
  const body = el('g', { transform: `translate(${mx},${my}) rotate(${upright ? 0 : angle})` }, g);
  const L = Math.min(2.3 * P, Math.max(1.2 * P, len - 0.7 * P));
  const label = partLabel(part);
  switch (part.type) {
    case 'resistor': {
      el('rect', { x: -L / 2, y: -0.34 * P, width: L, height: 0.68 * P, rx: 0.3 * P, class: 'bb-resistor' }, body);
      const colors = bands(resolveNumber(part.value || part.spice?.value));
      (colors || ['#9a8f80', '#9a8f80', '#9a8f80', '#9a8f80']).forEach((c, i) => el('rect', {
        x: -L / 2 + L * (i < 3 ? 0.2 + i * 0.16 : 0.78), y: -0.34 * P, width: 0.13 * P, height: 0.68 * P, fill: c }, body));
      break;
    }
    case 'inductor':
      el('rect', { x: -L / 2, y: -0.4 * P, width: L, height: 0.8 * P, rx: 0.35 * P, class: 'bb-inductor' }, body);
      for (let i = 1; i < 7; i++) el('line', { x1: -L / 2 + (L * i) / 7, x2: -L / 2 + (L * i) / 7, y1: -0.36 * P, y2: 0.36 * P, class: 'bb-coil' }, body);
      break;
    case 'electrolytic': {
      const r = Math.min(0.75, 0.42 + 0.06 * Math.log10(Math.max(1, (resolveNumber(part.value) || 1e-6) / 1e-6))) * P;
      el('rect', { x: -L / 2, y: -r, width: L, height: 2 * r, rx: 0.2 * P, class: 'bb-ecap' }, body);
      el('rect', { x: L / 2 - 0.4 * P, y: -r, width: 0.3 * P, height: 2 * r, class: 'bb-ecap-stripe' }, body);
      text(body, -0.1 * P, 3, '+', { class: 'bb-ecap-text' });
      break;
    }
    case 'capacitor':
      el('ellipse', { cx: 0, cy: 0, rx: 0.62 * P, ry: 0.5 * P, class: 'bb-ccap' }, body);
      break;
    case 'diode':
      el('rect', { x: -L / 2, y: -0.27 * P, width: L, height: 0.54 * P, rx: 0.1 * P, class: 'bb-diode' }, body);
      el('rect', { x: L / 2 - 0.4 * P, y: -0.27 * P, width: 0.18 * P, height: 0.54 * P, class: 'bb-band' }, body);
      break;
    case 'led': case 'photodiode': {
      const ir = /ir|940|850/i.test(part.spice?.model || '') || part.type === 'photodiode';
      const color = part.color || (ir ? '#5a4572' : /green/i.test(part.spice?.model || '') ? '#2fbf4a' : /blue/i.test(part.spice?.model || '') ? '#2f6fff' : /yellow/i.test(part.spice?.model || '') ? '#f4d03f' : '#e0302a');
      const halo = el('circle', { cx: 0, cy: 0, r: 1.3 * P, fill: ir ? '#b06cff' : color, opacity: 0, filter: 'url(#bb-glow)' }, body);
      el('circle', { cx: 0, cy: 0, r: 0.66 * P, fill: color, class: 'bb-led' }, body);
      el('circle', { cx: -0.2 * P, cy: -0.2 * P, r: 0.2 * P, fill: '#ffffff', opacity: 0.45 }, body);
      if (part.type === 'led') live.push(() => {
        const i = ledCurrent(part);
        halo.setAttribute('opacity', i === null ? 0 : Math.min(0.95, Math.abs(i) / 0.02).toFixed(3));
      });
      break;
    }
    case 'ammeter': {
      el('rect', { x: -0.95 * P, y: -0.58 * P, width: 1.9 * P, height: 1.16 * P, rx: 0.2 * P, class: 'bb-ammeter' }, body);
      const t = text(body, 0, 3, 'A', { class: 'bb-ammeter-text' });
      live.push(() => { const c = ammeterCurrent(part.ref); t.textContent = c && hasRun() ? engineering(c.value, 'A') : 'A'; });
      break;
    }
    case 'behavioral':
      el('rect', { x: -0.95 * P, y: -0.5 * P, width: 1.9 * P, height: 1.0 * P, rx: 0.12 * P, class: 'bb-chip' }, body);
      text(body, 0, 4, 'ƒ', { class: 'bb-chip-text' });
      break;
    default:
      el('rect', { x: -L / 2, y: -0.4 * P, width: L, height: 0.8 * P, rx: 0.2 * P, class: 'bb-generic' }, body);
  }
  // Label beside the body, reading along it (bottom-up for vertical parts).
  let along = upright ? 0 : angle;
  if (along > 90) along -= 180;
  if (along <= -90) along += 180;
  const rad = (along * Math.PI) / 180;
  const off = (upright ? 0.95 : 0.9) * P;
  text(g, mx + Math.sin(rad) * off, my - Math.cos(rad) * off + 2.5, `${part.ref}${label ? ' ' + label : ''}`,
    { class: 'bb-ref', transform: `rotate(${along} ${mx + Math.sin(rad) * off} ${my - Math.cos(rad) * off + 2.5})` });
}

function ledCurrent(part) {
  if (!hasRun()) return null;
  const groups = part.pins.map((p) => bb.conn.pinGroup.get(`${part.id}|${p.name}`));
  for (const other of bb.board.parts) {
    if (other.type !== 'ammeter') continue;
    const shared = other.pins.some((p) => groups.includes(bb.conn.pinGroup.get(`${other.id}|${p.name}`)));
    if (!shared) continue;
    const c = ammeterCurrent(other.ref);
    if (c && c.value !== null) return c.value;
  }
  return null;
}

function drawTransistor(g, part, pts) {
  const cx = pts.reduce((s, p) => s + p.x, 0) / pts.length;
  const top = Math.min(...pts.map((p) => p.y));
  const power = part.type.endsWith('_power');
  const by = top - (power ? 2.2 : 1.35) * P;
  const spread = Math.min(0.85 * P, ((pts.length - 1) / 2) * 0.85 * P);
  const anchors = pts.map((_, i) => ({ x: cx - spread + (pts.length > 1 ? (2 * spread * i) / (pts.length - 1) : 0), y: by + (power ? 0.6 : 0.5) * P }));
  legs(g, part, anchors);
  const body = el('g', { transform: `translate(${cx},${by})` }, g);
  if (power) {
    el('rect', { x: -1.5 * P, y: -2.7 * P, width: 3 * P, height: 1.6 * P, rx: 2, class: 'bb-tab' }, body);
    el('circle', { cx: 0, cy: -2.0 * P, r: 0.4 * P, class: 'bb-tabhole' }, body);
    el('rect', { x: -1.5 * P, y: -1.2 * P, width: 3 * P, height: 1.8 * P, rx: 2, class: 'bb-to' }, body);
  } else {
    el('path', { d: `M${-1.15 * P},${0.5 * P} L${1.15 * P},${0.5 * P} L${1.15 * P},${-0.1 * P} A${1.15 * P},${1.0 * P} 0 0 0 ${-1.15 * P},${-0.1 * P} Z`, class: part.type === 'phototransistor' ? 'bb-to bb-photo' : 'bb-to' }, body);
    if (part.type === 'phototransistor') el('circle', { cx: 0, cy: -0.3 * P, r: 0.45 * P, class: 'bb-lens' }, body);
  }
  const kind = { npn: 'NPN', pnp: 'PNP', nmos: 'N-FET', pmos: 'P-FET', nmos_power: 'N-FET', pmos_power: 'P-FET', jfet: 'JFET', phototransistor: 'PT' }[part.type];
  text(body, 0, power ? -0.2 * P : 0.25 * P, part.spice?.model || kind, { class: 'bb-chip-text small' });
  text(g, cx, by - (power ? 2.95 : 1.25) * P, `${part.ref} ${kind}`, { class: 'bb-ref' });
  part.pins.forEach((pin, i) => text(g, anchors[i].x, anchors[i].y + 0.75 * P, pin.name, { class: 'bb-pinname' }));
}

function drawDip(g, part, pts) {
  const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
  const x0 = Math.min(...xs) - 0.5 * P, x1 = Math.max(...xs) + 0.5 * P;
  const straddle = ys.some((y) => y === ROW_Y.e * P) && ys.some((y) => y === ROW_Y.f * P);
  let y0 = straddle ? ROW_Y.e * P + 0.22 * P : Math.min(...ys) - 1.8 * P, y1 = straddle ? ROW_Y.f * P - 0.22 * P : Math.min(...ys) - 0.5 * P;
  if (!straddle) legs(g, part, pts.map((p) => ({ x: p.x, y: y1 })));
  else part.pins.forEach((pin) => { const h = holeXY(pin.hole); el('rect', { x: h.x - 0.17 * P, y: h.y - 0.17 * P, width: 0.34 * P, height: 0.34 * P, class: 'bb-dippin', 'data-handle': `pin|${part.id}|${pin.name}` }, g); });
  el('rect', { x: x0, y: y0, width: x1 - x0, height: y1 - y0, rx: 2, class: 'bb-chip' }, g);
  el('path', { d: `M${x0},${(y0 + y1) / 2 - 0.25 * P} a${0.25 * P},${0.25 * P} 0 0 1 0,${0.5 * P}`, class: 'bb-notch' }, g);
  const label = { switch: `${part.ref} switch`, dependent: `${part.ref} ${part.spice?.letter}-src`, ic: `${part.ref} ${part.spice?.model || ''}` }[part.type] || part.ref;
  text(g, (x0 + x1) / 2, (y0 + y1) / 2 + 3, label, { class: 'bb-chip-text small' });
}

function moduleStyle(part) {
  const input = (bb.circuit?.inputs || []).find((b) => b.target.toLowerCase() === part.ref.toLowerCase());
  if (input?.from_domain === 'firmware') return { cls: 'mcu', title: 'MCU pin', input };
  if (part.type === 'supply') return { cls: 'supply', title: 'Bench supply', input };
  if (part.type === 'generator') return { cls: 'generator', title: 'Function generator', input };
  if (part.type.startsWith('current')) return { cls: 'isource', title: input ? 'Current input' : 'Current source', input };
  return { cls: 'signal', title: 'Signal input', input };
}

function drawModule(layer, part) {
  const [mx, my] = part.pos || [-15, 0];
  const style = moduleStyle(part);
  const g = el('g', { class: `bb-module ${style.cls}` + (bb.selection?.id === part.id ? ' selected' : ''), 'data-part': part.id }, layer);
  const x = mx * P, y = my * P, w = MODULE_W * P, h = Math.max(MODULE_H, 1.0 + part.pins.length * 1.7) * P;
  el('rect', { x, y, width: w, height: h, rx: 6, class: 'bb-module-body' }, g);
  if (style.cls === 'mcu') {
    el('rect', { x: x + 0.8 * P, y: y + 1.2 * P, width: 2.6 * P, height: 2.6 * P, rx: 2, class: 'bb-chip' }, g);
    for (let i = 0; i < 4; i++) el('rect', { x: x + 4 * P + i * 0.9 * P, y: y + 0.4 * P, width: 0.5 * P, height: 0.5 * P, class: 'bb-dippin' }, g);
  }
  text(g, x + 0.6 * P, y + 1.0 * P - 2, `${style.title} · ${part.ref}`, { class: 'bb-module-title' });
  const origin = style.input ? (style.input.from_signal ? `${style.input.from_node || ''} ${style.input.from_signal}` : style.input.signal) : (part.value || '');
  text(g, x + (style.cls === 'mcu' ? 3.8 : 0.6) * P, y + 2.1 * P, origin.length > 30 ? '…' + origin.slice(-29) : origin, { class: 'bb-module-sub' });
  const display = el('rect', { x: x + (style.cls === 'mcu' ? 3.8 : 0.6) * P, y: y + 2.6 * P, width: 6.6 * P, height: 1.4 * P, rx: 2, class: 'bb-lcd' }, g);
  const value = text(g, Number(display.getAttribute('x')) + 6.3 * P, y + 3.6 * P, '', { class: 'bb-lcd-text', 'text-anchor': 'end' });
  const unit = part.type.startsWith('current') ? 'A' : 'V';
  live.push(() => {
    if (style.input) { const v = inputValue(style.input); value.textContent = v === null ? '—' : engineering(v, unit); return; }
    const number = resolveNumber((part.value || '').replace(/^dc\s+/i, ''));
    value.textContent = part.type === 'generator' ? (part.value || '').split('(')[0] : number !== null ? engineering(number, unit) : (part.value || '—');
  });
  part.pins.forEach((pin) => {
    const t = terminalXY(part, pin.name);
    el('line', { x1: x + w, y1: t.y, x2: t.x, y2: t.y, class: 'bb-leg' }, g);
    el('circle', { cx: t.x, cy: t.y, r: 0.42 * P, class: `bb-jack ${pin.name === '-' ? 'neg' : 'pos'}`, 'data-term': `m:${part.id}:${pin.name}` }, g);
    text(g, x + w - 0.4 * P, t.y + 3, pin.name === '-' ? '−' : pin.name, { class: 'bb-module-pin', 'text-anchor': 'end' });
  });
}

function wirePath(a, b) {
  const dx = b.x - a.x, dy = b.y - a.y, len = Math.hypot(dx, dy) || 1;
  const bow = Math.min(len * 0.22, 2.2 * P);
  const cx = (a.x + b.x) / 2 - (dy / len) * bow, cy = (a.y + b.y) / 2 + (dx / len) * bow - Math.min(len * 0.08, P);
  return `M${a.x},${a.y} Q${cx},${cy} ${b.x},${b.y}`;
}

function drawWire(layer, wire) {
  const a = holeXY(wire.a), b = holeXY(wire.b);
  if (!a || !b) return;
  const g = el('g', { class: 'bb-wire' + (bb.selection?.type === 'wire' && bb.selection.id === wire.id ? ' selected' : ''), 'data-wire': wire.id }, layer);
  const d = wirePath(a, b);
  el('path', { d, class: 'bb-wire-hit' }, g);
  el('path', { d, class: 'bb-wire-outline' }, g);
  el('path', { d, class: 'bb-wire-core', stroke: wire.color || '#1f6fd1' }, g);
  for (const [end, p] of [['a', a], ['b', b]]) el('circle', { cx: p.x, cy: p.y, r: 0.2 * P, class: 'bb-wire-end', 'data-handle': `wire|${wire.id}|${end}` }, g);
}

// Output vectors: flags on the probed nets (ammeters show their own current).
function representativeHole(netName) {
  const group = [...bb.conn.names.entries()].find(([, n]) => n === netName)?.[0];
  if (!group) return null;
  for (const part of bb.board.parts) for (const pin of part.pins || []) {
    if (!part.module && bb.conn.pinGroup.get(`${part.id}|${pin.name}`) === group && holeXY(pin.hole)) return pin.hole;
  }
  return null;
}

function drawProbes(layer) {
  const outputs = bb.circuit?.outputs || [];
  let k = 0;
  for (const out of outputs) {
    const m = PROBE_RE.exec(out.vector.trim());
    if (!m || m[1].toLowerCase() === 'i') continue;
    const hole = representativeHole(m[2].toLowerCase() === 'gnd' ? '0' : m[2]);
    const p = holeXY(hole);
    if (!p) continue;
    const g = el('g', { class: 'bb-probe' }, layer);
    const fx = p.x + (k % 2 ? 1.2 : -1.2) * P, fy = (p.y < 9 * P ? -2.6 - (k % 3) * 1.5 : 20 + (k % 3) * 1.5) * P;
    k++;
    el('line', { x1: p.x, y1: p.y, x2: fx, y2: fy, class: 'bb-probe-lead' }, g);
    el('circle', { cx: p.x, cy: p.y, r: 0.28 * P, class: 'bb-probe-tip' }, g);
    const box = el('rect', { x: fx - 0.2 * P, y: fy - 0.75 * P, height: 1.3 * P, rx: 3, class: 'bb-probe-box' }, g);
    const dest = out.to_nodes?.length ? ` → ${out.to_nodes.join(', ')}` : '';
    const t = text(g, fx + 0.2 * P, fy + 0.2 * P, '', { class: 'bb-probe-text' });
    const unit = m[1].toLowerCase() === 'v' ? 'V' : '';
    const title = el('title', {}, g); title.textContent = `${out.vector} → ${out.signal} (${out.mode})${dest}`;
    live.push(() => {
      const v = hasRun() ? outputValue(out) : null;
      t.textContent = `${out.vector}${v === null ? '' : ' = ' + engineering(v, unit)}${dest}`;
      box.setAttribute('width', t.getComputedTextLength() + 0.6 * P);
    });
  }
}

export function updateLive() { for (const fn of live) { try { fn(); } catch { /* a stale element */ } } }

// Net highlight under the cursor.
function highlight() {
  const layer = bb.highlightLayer;
  if (!layer) return;
  layer.innerHTML = '';
  const group = bb.hover?.group;
  if (!group) return;
  const n = columns(), s = 0.5 * P;
  let d = '';
  const holes = [];
  for (let c = 1; c <= n; c++) {
    for (const row of TOP + BOTTOM) holes.push(`${row}${c}`);
    if (railOk(c)) holes.push(`tn${c}`, `tp${c}`, `bp${c}`, `bn${c}`);
  }
  for (const h of holes) if (bb.conn.groupOfHole(h) === group) { const p = holeXY(h); d += `M${p.x - s / 2},${p.y - s / 2}h${s}v${s}h${-s}z`; }
  el('path', { d, class: 'bb-hl-holes' }, layer);
  for (const part of bb.board.parts) if (part.module) for (const pin of part.pins) {
    if (bb.conn.pinGroup.get(`${part.id}|${pin.name}`) === group) { const t = terminalXY(part, pin.name); el('circle', { cx: t.x, cy: t.y, r: 0.6 * P, class: 'bb-hl-term' }, layer); }
  }
}

function groupAt(target, world) {
  const handle = target.closest?.('[data-handle]')?.dataset.handle;
  if (handle) {
    const [kind, id, end] = handle.split('|');
    if (kind === 'pin') { const part = bb.board.parts.find((p) => p.id === id); return bb.conn.pinGroup.get(`${id}|${end}`) || (part && null); }
    const wire = bb.board.wires.find((w) => w.id === id);
    if (wire) return bb.conn.groupOfHole(wire[end]);
  }
  const term = target.closest?.('[data-term]')?.dataset.term;
  if (term) return bb.conn.groupOfHole(term);
  const wireId = target.closest?.('[data-wire]')?.dataset.wire;
  if (wireId) { const w = bb.board.wires.find((x) => x.id === wireId); return w && bb.conn.groupOfHole(w.a); }
  const hole = holeAt(world.x, world.y);
  return hole ? bb.conn.groupOfHole(hole) : null;
}

function netValue(name) {
  for (const out of bb.circuit?.outputs || []) {
    const m = PROBE_RE.exec(out.vector.trim());
    if (m && m[1].toLowerCase() === 'v' && !m[3] && m[2].toLowerCase() === String(name).toLowerCase()) return outputValue(out);
  }
  return name === '0' ? 0 : null;
}

function showTip(event, group) {
  if (!group) { tip.style.display = 'none'; return; }
  const name = bb.conn.nameOf(group);
  const pins = [];
  for (const part of bb.board.parts) for (const pin of part.pins || []) if (bb.conn.pinGroup.get(`${part.id}|${pin.name}`) === group) pins.push(`${part.ref}.${pin.name}`);
  const v = hasRun() ? netValue(name) : null;
  tip.innerHTML = '';
  tip.append(html('b', {}, name === '0' ? 'GND (0)' : `net ${name}`), v !== null ? html('span', { class: 'v' }, ` ${engineering(v, 'V')}`) : '',
    html('div', {}, pins.length ? pins.join(', ') : 'nothing connected'));
  const rect = wrap.getBoundingClientRect();
  tip.style.left = `${event.clientX - rect.left + 14}px`;
  tip.style.top = `${event.clientY - rect.top + 14}px`;
  tip.style.display = 'block';
}

// ---------------------------------------------------------------------------------------------
// Editing

function snapshot() {
  bb.undo.push(JSON.stringify(bb.board));
  if (bb.undo.length > 60) bb.undo.shift();
}

function changed() {
  bb.dirty = true;
  render();
  if (bb.selection) refreshInspector();
}

function occupied(except = {}) {
  const used = new Map();
  for (const part of bb.board.parts) if (!part.module) for (const pin of part.pins || []) if (pin.hole && part.id !== except.part) used.set(pin.hole, `${part.ref}.${pin.name}`);
  for (const wire of bb.board.wires) for (const end of ['a', 'b']) if (!(wire.id === except.wire && end === except.end) && !wire[end].startsWith('m:')) used.set(wire[end], 'wire');
  if (except.pin) used.delete(except.pinHole);
  return used;
}

function toWorld(event) {
  const r = svg.getBoundingClientRect();
  return { x: (event.clientX - r.left - view.x) / view.scale, y: (event.clientY - r.top - view.y) / view.scale };
}

let drag = null;

function onDown(event) {
  if (!bb.board || event.button > 1) return;
  svg.focus();
  const w = toWorld(event);
  const handle = event.target.closest('[data-handle]')?.dataset.handle;
  const term = event.target.closest('[data-term]')?.dataset.term;
  const partId = event.target.closest('[data-part]')?.dataset.part;
  const wireId = event.target.closest('[data-wire]')?.dataset.wire;
  if (event.button === 1) { drag = { kind: 'pan', x: event.clientX, y: event.clientY, vx: view.x, vy: view.y }; return; }
  if (handle) {
    const [kind, id, end] = handle.split('|');
    if (kind === 'pin') {
      const part = bb.board.parts.find((p) => p.id === id);
      const pin = part.pins.find((p) => p.name === end);
      select({ type: 'part', id });
      drag = { kind: 'pin', part, pin, from: pin.hole, moved: false };
    } else {
      select({ type: 'wire', id });
      drag = { kind: 'wire-end', wire: bb.board.wires.find((x) => x.id === id), end, from: null, moved: false };
      drag.from = drag.wire[end];
    }
    return;
  }
  if (term) { drag = { kind: 'new-wire', from: term, start: w }; return; }
  if (partId) {
    const part = bb.board.parts.find((p) => p.id === partId);
    select({ type: 'part', id: partId });
    drag = { kind: part.module ? 'module' : 'part', part, start: w, pins: part.pins.map((p) => p.hole), pos: [...(part.pos || [0, 0])], moved: false };
    return;
  }
  if (wireId) { select({ type: 'wire', id: wireId }); return; }
  const hole = holeAt(w.x, w.y);
  if (hole) { drag = { kind: 'new-wire', from: hole, start: w }; return; }
  select(null);
  drag = { kind: 'pan', x: event.clientX, y: event.clientY, vx: view.x, vy: view.y };
}

function onMove(event) {
  if (!bb.board) return;
  const w = toWorld(event);
  if (!drag) {
    const group = groupAt(event.target, w);
    if (group !== bb.hover?.group) { bb.hover = group ? { group } : null; highlight(); }
    showTip(event, group);
    return;
  }
  tip.style.display = 'none';
  if (drag.kind === 'pan') {
    view.x = drag.vx + event.clientX - drag.x; view.y = drag.vy + event.clientY - drag.y; applyView();
    return;
  }
  bb.dragLayer.innerHTML = '';
  if (drag.kind === 'new-wire' || drag.kind === 'wire-end' || drag.kind === 'pin') {
    const anchor = drag.kind === 'new-wire' ? holeXY(drag.from) : drag.kind === 'pin' ? holeXY(drag.from) : holeXY(drag.wire[drag.end === 'a' ? 'b' : 'a']);
    const hole = targetAt(event, w);
    const p = hole ? holeXY(hole) : w;
    if (drag.kind === 'pin') {
      el('line', { x1: anchor.x, y1: anchor.y, x2: p.x, y2: p.y, class: 'bb-ghost-leg' }, bb.dragLayer);
    } else {
      el('path', { d: wirePath(anchor, p), class: 'bb-ghost-wire' }, bb.dragLayer);
    }
    if (hole) el('circle', { cx: p.x, cy: p.y, r: 0.42 * P, class: 'bb-ghost-target' }, bb.dragLayer);
    drag.target = hole; drag.moved = true;
    return;
  }
  if (drag.kind === 'module') {
    const dx = Math.round(((w.x - drag.start.x) / P) * 2) / 2, dy = Math.round(((w.y - drag.start.y) / P) * 2) / 2;
    if (!drag.moved && Math.hypot(dx, dy) < 0.5) return;
    if (!drag.moved) snapshot();
    drag.moved = true;
    drag.part.pos = [drag.pos[0] + dx, drag.pos[1] + dy];
    render();
    return;
  }
  if (drag.kind === 'part') {
    const dc = Math.round((w.x - drag.start.x) / P), dy = Math.round((w.y - drag.start.y) / P);
    const holes = drag.pins.map((h) => shiftHole(h, dc, dy));
    const used = occupied({ part: drag.part.id });
    const ok = holes.every((h) => h && !used.has(h));
    drag.target = ok && (dc || dy) ? holes : null;
    for (const h of holes) {
      const p = h ? holeXY(h) : null;
      if (p) el('circle', { cx: p.x, cy: p.y, r: 0.42 * P, class: ok ? 'bb-ghost-target' : 'bb-ghost-bad' }, bb.dragLayer);
    }
    const a = holeXY(drag.pins[0]);
    el('text', { x: a.x + dc * P, y: a.y + dy * P - 0.9 * P, class: 'bb-ghost-text' }, bb.dragLayer).textContent = ok ? `${drag.part.ref} → ${holes.join(' ')}` : 'blocked';
  }
}

function targetAt(event, w) {
  const term = document.elementFromPoint(event.clientX, event.clientY)?.closest?.('[data-term]')?.dataset.term;
  if (term && drag.kind !== 'pin') return term;
  const hole = holeAt(w.x, w.y);
  if (!hole) return null;
  const except = drag.kind === 'wire-end' ? { wire: drag.wire.id, end: drag.end } : drag.kind === 'pin' ? { pin: true, pinHole: drag.from } : {};
  return occupied(except).has(hole) && hole !== drag.from ? null : hole;
}

function onUp() {
  if (!drag) return;
  const d = drag;
  drag = null;
  if (bb.dragLayer) bb.dragLayer.innerHTML = '';
  if (d.kind === 'new-wire' && d.target && d.target !== d.from) {
    snapshot();
    const group = bb.conn.groupOfHole(d.from);
    const name = group ? bb.conn.nameOf(group) : '';
    const color = name === '0' ? '#222222' : name === bb.board.supply ? '#d12b2b' : WIRE_COLORS[bb.board.wires.length % 8];
    let n = bb.board.wires.length + 1;
    while (bb.board.wires.some((x) => x.id === `w${n}`)) n++;
    bb.board.wires.push({ id: `w${n}`, a: d.from, b: d.target, color });
    bb.selection = { type: 'wire', id: `w${n}` };
    changed();
  } else if (d.kind === 'wire-end' && d.moved && d.target && d.target !== d.from) {
    snapshot(); d.wire[d.end] = d.target; changed();
  } else if (d.kind === 'pin' && d.moved && d.target && d.target !== d.from && !d.target.startsWith('m:')) {
    snapshot(); d.pin.hole = d.target; changed();
  } else if (d.kind === 'part' && d.target) {
    snapshot(); d.part.pins.forEach((pin, i) => (pin.hole = d.target[i])); changed();
  } else if (d.kind === 'module' && d.moved) {
    bb.dirty = true; renderStatus();
  }
}

function select(selection) {
  bb.selection = selection;
  render();
  refreshInspector();
}

function removeSelected() {
  const sel = bb.selection;
  if (!sel) return;
  snapshot();
  if (sel.type === 'wire') bb.board.wires = bb.board.wires.filter((w) => w.id !== sel.id);
  if (sel.type === 'part') {
    const part = bb.board.parts.find((p) => p.id === sel.id);
    bb.board.parts = bb.board.parts.filter((p) => p.id !== sel.id);
    if (part?.module) bb.board.wires = bb.board.wires.filter((w) => !w.a.startsWith(`m:${part.id}:`) && !w.b.startsWith(`m:${part.id}:`));
  }
  bb.selection = null;
  changed();
  refreshInspector();
}

function undo() {
  const last = bb.undo.pop();
  if (!last) return toast('Nothing to undo');
  bb.board = JSON.parse(last);
  bb.selection = null;
  changed();
  refreshInspector();
}

function rotateSelected() {
  const part = bb.selection?.type === 'part' && bb.board.parts.find((p) => p.id === bb.selection.id);
  if (!part || part.module) return;
  snapshot();
  const holes = part.pins.map((p) => p.hole).reverse();
  part.pins.forEach((pin, i) => (pin.hole = holes[i]));
  changed();
}

function freeSpot(count) {
  const used = occupied();
  const busy = new Set([...used.keys()].map((h) => HOLE_RE.exec(h)).filter(Boolean).map((m) => +m[2]));
  const width = count === 2 ? 4 : count;
  for (let c = 2; c + width <= columns(); c++) {
    let free = true;
    for (let k = -1; k <= width; k++) if (busy.has(c + k)) free = false;
    if (free) return c;
  }
  bb.board.board.columns = columns() + width + 4;
  return columns() - width - 2;
}

function addPart(item) {
  snapshot();
  const letter = item.letter;
  let n = 1;
  const refs = new Set(bb.board.parts.map((p) => (p.ref || p.id).toUpperCase()));
  while (refs.has(`${letter}${n}`)) n++;
  const ref = `${letter}${n}`;
  const names = PIN_NAMES[item.type] || ['1', '2'];
  const part = { id: ref, ref, type: item.type, value: item.value || '', palette: item.id, added: true,
    spice: { letter, nodes: [], model: item.model || null, value: item.value || '' } };
  if (['supply', 'generator'].includes(item.type)) {
    const ys = bb.board.parts.filter((p) => p.module).map((p) => (p.pos?.[1] || 0) + 5.5);
    part.module = true; part.pos = [-15, ys.length ? Math.max(...ys) : 0];
    part.pins = names.map((name) => ({ name }));
  } else {
    const c = freeSpot(names.length);
    const holes = names.length === 2 ? [`d${c}`, `d${c + 3}`] : names.map((_, i) => `c${c + i}`);
    part.pins = names.map((name, i) => ({ name, hole: holes[i] }));
  }
  bb.board.parts.push(part);
  bb.selection = { type: 'part', id: ref };
  changed();
  toast(`${ref} added: drag it into place, then wire it`);
}

// ---------------------------------------------------------------------------------------------
// Inspector panel (shown while the breadboard view is active)

function refreshInspector() { import('./inspector.js').then((m) => m.render()); }

const field = (label, input) => html('div', { class: 'field' }, html('span', { title: label }, label), input);

export function inspectorPanel(body) {
  if (!bb.active || !bb.board) return false;
  const sel = bb.selection;
  if (sel?.type === 'part') {
    const part = bb.board.parts.find((p) => p.id === sel.id);
    if (part) { partPanel(body, part); return true; }
  }
  if (sel?.type === 'wire') {
    const wire = bb.board.wires.find((w) => w.id === sel.id);
    if (wire) { wirePanel(body, wire); return true; }
  }
  boardPanel(body);
  return true;
}

function card(part) {
  const nets = part.pins.map((pin) => bb.conn.nameOf(bb.conn.pinGroup.get(`${part.id}|${pin.name}`)));
  return [part.ref, ...nets, part.spice?.model || '', part.value || ''].filter(Boolean).join(' ');
}

function partPanel(body, part) {
  const valueInput = html('input', { value: part.value || '' });
  valueInput.onchange = () => { snapshot(); part.value = valueInput.value; changed(); };
  const modelSelect = html('input', { value: part.spice?.model || '', list: 'bb-models' });
  modelSelect.onchange = () => { snapshot(); part.spice = { ...(part.spice || {}), model: modelSelect.value || null }; changed(); };
  const datalist = html('datalist', { id: 'bb-models' }, Object.keys(bb.models).map((m) => html('option', { value: m })));
  const rows = part.pins.map((pin, i) => {
    const group = bb.conn.pinGroup.get(`${part.id}|${pin.name}`);
    const net = bb.conn.nameOf(group);
    const expected = part.spice?.nodes?.[i];
    const exp = expected && ['0', 'gnd'].includes(String(expected).toLowerCase()) ? '0' : expected;
    const ok = part.added || !exp || exp === net;
    return html('tr', {}, html('td', {}, pin.name), html('td', {}, part.module ? 'terminal' : (pin.hole || '—')),
      html('td', { class: ok ? 'ok' : 'bad', title: exp ? `netlist: ${exp}` : '' }, net + (ok ? '' : ` (netlist ${exp})`)));
  });
  const input = (bb.circuit?.inputs || []).find((b) => b.target.toLowerCase() === part.ref.toLowerCase());
  const amp = part.type === 'ammeter' ? ammeterCurrent(part.ref) : null;
  body.append(
    html('h3', {}, part.ref, ' ', html('small', { class: 'muted' }, part.type.replace('_', ' '))),
    field('value', valueInput),
    ['resistor', 'capacitor', 'electrolytic', 'inductor', 'ammeter'].includes(part.type) ? '' : field('model', modelSelect), datalist,
    html('h4', {}, 'Pins'),
    html('table', { class: 'kv bb-pins' }, html('tr', {}, html('td', {}, 'pin'), html('td', {}, 'hole'), html('td', {}, 'net')), rows),
    input ? html('div', { class: 'note' }, `Driven by ${input.signal}${input.from_signal ? ` ← ${input.from_signal} (${input.from_node})` : ''} (${input.kind}).`) : '',
    amp ? html('div', { class: 'note' }, `Current → ${amp.binding.signal}${amp.binding.to_nodes?.length ? ` → ${amp.binding.to_nodes.join(', ')}` : ''}.`) : '',
    html('h4', {}, 'SPICE card'),
    html('pre', { class: 'bb-card' }, card(part)),
    html('div', { class: 'bb-actions' },
      part.module ? '' : html('button', { onclick: rotateSelected, title: 'Swap the legs (R)' }, '⟲ Flip'),
      html('button', { onclick: removeSelected, title: 'Remove from the board (Del)' }, '🗑 Remove')),
    html('div', { class: 'note' }, part.module ? 'Drag the module; drag from a terminal to a hole to add a wire.'
      : 'Drag the body to move all legs, drag a leg end to bend it to another hole.'),
  );
}

function wirePanel(body, wire) {
  const group = bb.conn.groupOfHole(wire.a);
  const swatches = html('div', { class: 'bb-swatches' }, WIRE_COLORS.map((c) => html('button', {
    class: 'bb-swatch' + (c === wire.color ? ' active' : ''), style: `background:${c}`, title: c,
    onclick: () => { snapshot(); wire.color = c; changed(); } })));
  body.append(
    html('h3', {}, `Wire ${wire.id}`),
    field('from', html('span', {}, wire.a)), field('to', html('span', {}, wire.b)),
    field('net', html('span', {}, bb.conn.nameOf(group))),
    html('h4', {}, 'Colour'), swatches,
    html('div', { class: 'bb-actions' }, html('button', { onclick: removeSelected }, '🗑 Remove')),
    html('div', { class: 'note' }, 'Drag an end to another hole. Drag from any free hole or module terminal to draw a new wire.'));
}

function boardPanel(body) {
  const check = bb.conn;
  const parts = bb.board.parts.filter((p) => !p.module).length, modules = bb.board.parts.filter((p) => p.module).length;
  const issues = check.issues.map((issue) => html('li', { class: issue.kind, onmouseenter: () => {
    const group = issue.group || [...(check.expected.get(issue.net) || [])][0];
    bb.hover = group ? { group } : null; highlight();
  } }, issue.text));
  body.append(
    html('h3', {}, 'Breadboard'),
    html('div', { class: 'note' }, bb.path || ''),
    html('div', { class: 'note' }, bb.board.saved ? `Layout: ${bb.path?.split('/').pop()}.breadboard.yaml` : 'Automatic layout (not saved yet).'),
    html('table', { class: 'kv' },
      html('tr', {}, html('td', {}, 'parts'), html('td', {}, `${parts} on the board, ${modules} modules`)),
      html('tr', {}, html('td', {}, 'wires'), html('td', {}, String(bb.board.wires.length))),
      html('tr', {}, html('td', {}, 'nets'), html('td', {}, String(new Set(check.names.values()).size))),
      html('tr', {}, html('td', {}, 'board'), html('td', {}, `${columns()} columns`))),
    html('h4', {}, check.ok ? '✓ Matches the netlist' : `Check: ${check.issues.length} issue(s)`),
    issues.length ? html('ul', { class: 'bb-issues' }, issues) : html('div', { class: 'note' }, 'Every net on the board connects the same pins as the netlist.'),
    html('h4', {}, 'Bindings'),
    html('table', { class: 'kv' },
      (bb.circuit?.inputs || []).map((b) => html('tr', {}, html('td', {}, b.target), html('td', {}, `← ${b.signal}`))),
      (bb.circuit?.outputs || []).map((b) => html('tr', {}, html('td', {}, b.vector), html('td', {}, `→ ${b.signal}`)))),
    html('h4', {}, 'How to'),
    html('div', { class: 'note' },
      'Hover a hole, leg or wire to see its net. Drag parts and modules; drag a leg end to bend it; drag from a free hole to draw a jumper. ',
      'Del removes, R flips, Ctrl+Z undoes, F fits. “Export .cir” writes the board back as a netlist.'),
  );
}

// ---------------------------------------------------------------------------------------------
// Loading, saving, toolbar

function renderStatus() {
  const status = $('bb-status');
  if (!status || !bb.board) return;
  const c = bb.conn;
  status.className = c.ok ? 'ok' : 'bad';
  status.textContent = `${c.ok ? '✓ matches netlist' : `⚠ ${c.issues.filter((i) => i.kind !== 'unplaced').length} issue(s)`}${bb.dirty ? ' · unsaved' : ''}`;
}

export async function loadCircuits() {
  if (!state.graph) return;
  bb.loadedFor = state.graph;
  try {
    const result = await api('breadboard/circuits', { graph: state.graph });
    bb.circuits = result.circuits;
    bb.palette = result.palette;
  } catch (error) { toast(error.message, true); return; }
  const select = $('bb-circuit');
  select.innerHTML = '';
  for (const c of bb.circuits) select.append(html('option', { value: c.netlist }, `${c.instance} · ${c.relative}`));
  select.append(html('option', { value: '__open' }, 'Netlist path…'));
  const menu = $('bb-palette');
  menu.innerHTML = '';
  for (const item of bb.palette) menu.append(html('div', { class: 'menu-item', onclick: () => { $('bb-add').parentElement.classList.remove('open'); addPart(item); } }, item.label));
  const keep = bb.circuits.find((c) => c.netlist === bb.path);
  const first = keep || bb.circuits[0];
  if (first) await openNetlist(first.netlist, first);
  else { bb.board = null; bb.circuit = null; bb.path = null; render(); refreshInspector(); }
}

async function openNetlist(path, circuit = null, fresh = false) {
  if (bb.dirty && bb.path && path !== bb.path && !confirm('Discard breadboard changes?')) return;
  try {
    const result = await api(fresh ? 'breadboard/layout' : 'breadboard/open', { path, root: state.graph?.root });
    bb.board = result.board; bb.original = result.netlist; bb.models = result.models; bb.path = result.path;
    bb.circuit = circuit || bb.circuits.find((c) => c.netlist === result.path) || { inputs: [], outputs: [] };
    bb.dirty = fresh; bb.undo = []; bb.selection = null;
    $('bb-circuit').value = bb.circuits.some((c) => c.netlist === result.path) ? result.path : '__open';
    render(); fit(); refreshInspector();
  } catch (error) { toast(error.message, true); log(error.message, 'err'); }
}

async function save() {
  if (!bb.board) return;
  try {
    const result = await api('breadboard/save', { path: bb.path, board: bb.board });
    bb.dirty = false; bb.board.saved = true;
    renderStatus(); toast(`Saved ${result.sidecar}`); log(`saved breadboard ${result.sidecar}`, 'ok');
  } catch (error) { toast(error.message, true); }
}

async function showNetlist() {
  if (!bb.board) return;
  try {
    const { text: netlist } = await api('breadboard/netlist', { path: bb.path, board: bb.board });
    const pre = html('pre', {}, netlist);
    document.getElementById('modal-title').textContent = `Netlist from the breadboard (${bb.path.split('/').pop()})`;
    const bodyEl = document.getElementById('modal-body');
    bodyEl.innerHTML = '';
    bodyEl.append(pre);
    document.getElementById('modal').classList.remove('hidden');
  } catch (error) { toast(error.message, true); }
}

async function exportNetlist() {
  if (!bb.board) return;
  const target = prompt('Write the netlist from the breadboard to (.cir):', bb.path.replace(/(\.\w+)$/, '.breadboard$1'));
  if (!target) return;
  if (target === bb.path && !confirm(`Overwrite ${target}? The original lines of removed parts are kept as comments.`)) return;
  try {
    const result = await api('breadboard/export', { path: bb.path, board: bb.board, target });
    toast(`Wrote ${result.path}`); log(`breadboard netlist written to ${result.path}`, 'ok');
  } catch (error) { toast(error.message, true); }
}

export function fit() {
  if (!bb.board || !svg.clientWidth) return;
  const modules = bb.board.parts.filter((p) => p.module);
  const left = Math.min(0, ...modules.map((m) => (m.pos?.[0] || 0) * P)) - P;
  const right = (columns() + 3) * P;
  const top = -5 * P, bottom = Math.max(24 * P, ...modules.map((m) => ((m.pos?.[1] || 0) + MODULE_H + 1) * P));
  const scale = Math.min(svg.clientWidth / (right - left), svg.clientHeight / (bottom - top)) * 0.96;
  view.scale = Math.max(0.3, Math.min(4, scale));
  view.x = (svg.clientWidth - (right - left) * view.scale) / 2 - left * view.scale;
  view.y = (svg.clientHeight - (bottom - top) * view.scale) / 2 - top * view.scale;
  applyView();
}

export function setActive(active) {
  bb.active = active;
  if (active && bb.loadedFor !== state.graph) loadCircuits();
  else if (active) requestAnimationFrame(fit);
  refreshInspector();
}

export function projectSaved() { if (bb.dirty && bb.board) save(); }

function bind() {
  if (!svg) return;
  svg.addEventListener('mousedown', onDown);
  window.addEventListener('mousemove', (e) => { if (drag || e.target === svg || svg.contains(e.target)) onMove(e); });
  window.addEventListener('mouseup', onUp);
  svg.addEventListener('mouseleave', () => { if (!drag) { tip.style.display = 'none'; bb.hover = null; highlight(); } });
  svg.addEventListener('wheel', (e) => {
    e.preventDefault();
    const r = svg.getBoundingClientRect();
    const mx = e.clientX - r.left, my = e.clientY - r.top;
    const k = Math.exp(-e.deltaY * 0.0015);
    const s = Math.max(0.3, Math.min(6, view.scale * k));
    view.x = mx - ((mx - view.x) * s) / view.scale; view.y = my - ((my - view.y) * s) / view.scale; view.scale = s;
    applyView();
  }, { passive: false });
  svg.addEventListener('keydown', (e) => {
    if (e.key === 'Delete' || e.key === 'Backspace') { e.preventDefault(); removeSelected(); }
    else if ((e.ctrlKey || e.metaKey) && e.key === 'z') { e.preventDefault(); undo(); }
    else if (e.key === 'r' || e.key === 'R') rotateSelected();
    else if (e.key === 'f' || e.key === 'F') fit();
    else if (e.key === 'Escape') select(null);
  });
  $('bb-circuit').onchange = async (e) => {
    if (e.target.value !== '__open') return openNetlist(e.target.value);
    const path = prompt('Netlist (.cir), absolute or relative to the project folder:', bb.path || `${state.graph?.root || ''}/circuit.cir`);
    if (path) openNetlist(path); else e.target.value = bb.path || '';
  };
  $('bb-add').onclick = () => $('bb-add').parentElement.classList.toggle('open');
  document.addEventListener('mousedown', (e) => { if (!e.target.closest('#bb-toolbar .dropdown')) $('bb-add').parentElement.classList.remove('open'); });
  $('bb-auto').onclick = () => { if (bb.path && confirm('Replace the layout with an automatic one?')) { snapshot(); openNetlist(bb.path, bb.circuit, true); } };
  $('bb-save').onclick = save;
  $('bb-netlist').onclick = showNetlist;
  $('bb-export').onclick = exportNetlist;
  $('bb-undo').onclick = undo;
  on('graph-loaded', () => { bb.loadedFor = null; bb.path = null; bb.board = null; if (bb.active) loadCircuits(); });
}

bind();
