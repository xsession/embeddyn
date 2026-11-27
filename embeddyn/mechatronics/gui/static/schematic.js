// Schematic editor (SVG): parts with pins, wires, pan/zoom, drag & drop from the library.
import { connect, deleteNode, deleteWire, addPart } from './ops.js';
import { emit, findNode, formatValue, GLYPHS, markDirty, nodeColor, on, signalUnit, signalValue, state } from './store.js';

const NS = 'http://www.w3.org/2000/svg';
const HEAD = 36, PITCH = 20, PAD = 10;
const svg = document.getElementById('schematic');
const view = { x: 20, y: 20, scale: 1 };
let root, wireLayer, nodeLayer, dragLayer;
let valueTags = [];          // [{el, signal}]
let showValues = true;

const el = (tag, attrs = {}, parent) => {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
};

export function nodeSize(node) {
  const ins = node.ports.filter((p) => p.direction === 'in').length;
  const outs = node.ports.filter((p) => p.direction === 'out').length;
  const wide = node.kind === 'instance' && node.ports.some((p) => !p.mappable && p.kind !== 'event');
  const width = node.kind === 'terminal' ? 170 : node.kind === 'bus' ? 150 : node.kind === 'instance' ? (wide ? 250 : 210) : 160;
  return { width, height: HEAD + Math.max(ins, outs, 1) * PITCH + PAD };
}

function pinLabel(node, port) {
  let text = port.mappable || node.kind !== 'instance' ? port.name : (port.signal || port.name);
  if (node.kind === 'terminal') text = port.signal;
  if (port.kind === 'event' && node.kind === 'instance') text = `⇄ ${port.name.replace(/^event:/, '')}${port.signal ? ' · ' + port.signal : ''}`;
  if (node.kind === 'bus') text = port.name === 'tx' ? 'from boards' : 'to boards';
  if (text.length > 28) text = '…' + text.slice(-27);
  return text;
}

export function pinPosition(node, portName) {
  const { width } = nodeSize(node);
  const port = node.ports.find((p) => p.name === portName);
  if (!port) return { x: node.x, y: node.y };
  const list = node.ports.filter((p) => p.direction === port.direction);
  const index = list.indexOf(port);
  const y = node.y + HEAD + PITCH / 2 + index * PITCH;
  return port.direction === 'in' ? { x: node.x - 14, y } : { x: node.x + width + 14, y };
}

function wirePath(a, b) {
  const dx = Math.max(40, Math.abs(b.x - a.x) / 2);
  return `M${a.x},${a.y} C${a.x + dx},${a.y} ${b.x - dx},${b.y} ${b.x},${b.y}`;
}

function toWorld(event) {
  const rect = svg.getBoundingClientRect();
  return { x: (event.clientX - rect.left - view.x) / view.scale, y: (event.clientY - rect.top - view.y) / view.scale };
}

function applyView() {
  root?.setAttribute('transform', `translate(${view.x},${view.y}) scale(${view.scale})`);
  const g = 10 * view.scale;
  svg.style.backgroundImage =
    `linear-gradient(#e4e6dc 1px, transparent 1px), linear-gradient(90deg, #e4e6dc 1px, transparent 1px),` +
    `linear-gradient(#d3d6c8 1px, transparent 1px), linear-gradient(90deg, #d3d6c8 1px, transparent 1px)`;
  svg.style.backgroundSize = `${g}px ${g}px, ${g}px ${g}px, ${g * 10}px ${g * 10}px, ${g * 10}px ${g * 10}px`;
  svg.style.backgroundPosition = `${view.x}px ${view.y}px`;
}

export function render() {
  svg.innerHTML = '';
  valueTags = [];
  root = el('g', {}, svg);
  wireLayer = el('g', {}, root);
  nodeLayer = el('g', {}, root);
  dragLayer = el('g', {}, root);
  applyView();
  const graph = state.graph;
  if (!graph) return;
  if (!graph.nodes.length) {
    const t = el('text', { x: 40, y: 60, class: 'sch-empty' }, root);
    t.textContent = 'Empty project: drag parts from the library on the left.';
  }
  const connected = new Set();
  for (const wire of graph.wires) {
    connected.add(wire.source.node + '\u0000' + wire.source.port);
    connected.add(wire.target.node + '\u0000' + wire.target.port);
  }
  for (const node of graph.nodes) drawNode(node, connected);
  for (const wire of graph.wires) drawWire(wire);
  updateValues();
}

function drawNode(node, connected) {
  const { width, height } = nodeSize(node);
  const selected = state.selection?.type === 'node' && state.selection.id === node.id;
  const g = el('g', { class: 'sch-node' + (selected ? ' selected' : ''), transform: `translate(${node.x},${node.y})` }, nodeLayer);
  g.dataset.node = node.id;
  el('rect', { class: 'body', x: 0, y: 0, width, height, rx: 4 }, g);
  el('rect', { class: 'head', x: 0.7, y: 0.7, width: width - 1.4, height: HEAD - 6, rx: 3, fill: nodeColor(node) }, g);
  const glyph = el('text', { x: 9, y: 21, class: 'title', 'font-size': 15 }, g);
  glyph.textContent = GLYPHS[node.icon] || '▢';
  const title = el('text', { x: 30, y: 14, class: 'title' }, g);
  title.textContent = node.label || node.id;
  const sub = el('text', { x: 30, y: 26, class: 'subtitle' }, g);
  const part = state.partsById[node.part];
  sub.textContent = node.kind === 'instance'
    ? `${node.domain} · ${node.spec?.options?.backend || 'auto'}${part ? ' · ' + part.name : ''}`
    : node.kind === 'bus'
      ? `${node.protocol ? node.protocol + ' ' : ''}bus · ${busBoards(node)} boards${node.impaired ? ' · ⚡ impaired' : ''}`
      : (part?.name || node.kind);
  if (node.kind === 'bus') g.classList.add('sch-bus');
  const ins = node.ports.filter((p) => p.direction === 'in');
  const outs = node.ports.filter((p) => p.direction === 'out');
  ins.forEach((port, i) => drawPin(g, node, port, 0, HEAD + PITCH / 2 + i * PITCH, -1, width, connected));
  outs.forEach((port, i) => drawPin(g, node, port, width, HEAD + PITCH / 2 + i * PITCH, 1, width, connected));
  g.addEventListener('mousedown', (e) => startNodeDrag(e, node));
  g.addEventListener('dblclick', () => emit('node-open', node.id));
}

function drawPin(g, node, port, x, y, side, width, connected) {
  el('line', { class: 'pin-line', x1: x, y1: y, x2: x + side * 14, y2: y }, g);
  const isConnected = connected.has(node.id + '\u0000' + port.name);
  const cls = 'pin' + (isConnected ? ' connected' : '') + (!port.signal ? ' unmapped' : '');
  const pin = port.kind === 'event'
    ? el('rect', { class: cls + ' event', x: x + side * 14 - 4, y: y - 4, width: 8, height: 8, transform: `rotate(45 ${x + side * 14} ${y})` }, g)
    : el('circle', { class: cls, cx: x + side * 14, cy: y, r: 4.2 }, g);
  pin.dataset.node = node.id; pin.dataset.port = port.name;
  const label = el('text', { class: 'pin-label', x: x - side * 5, y: y + 3.5, 'text-anchor': side < 0 ? 'start' : 'end' }, g);
  label.textContent = pinLabel(node, port);
  const tip = el('title', {}, pin);
  tip.textContent = `${port.name}${port.signal ? ' → ' + port.signal : ' (unconnected)'}${port.unit ? ' [' + port.unit + ']' : ''}${port.label ? '\n' + port.label : ''}`;
  if (port.signal && port.kind !== 'event' && (port.direction === 'out' || node.kind === 'terminal')) {
    const value = el('text', { class: 'value', x: x + side * 18, y: y - 6, 'text-anchor': side < 0 ? 'end' : 'start' }, g);
    valueTags.push({ el: value, signal: port.signal });
  }
  pin.addEventListener('mousedown', (e) => startWireDrag(e, node, port));
}

function drawWire(wire) {
  const a = findNode(wire.source.node), b = findNode(wire.target.node);
  if (!a || !b) return;
  const p = pinPosition(a, wire.source.port), q = pinPosition(b, wire.target.port);
  const d = wirePath(p, q);
  const selected = state.selection?.type === 'wire' && state.selection.id === wire.id;
  const pa = a.ports.find((x) => x.name === wire.source.port), pb = b.ports.find((x) => x.name === wire.target.port);
  const mismatch = (wire.kind === 'net' || wire.kind === 'probe') && pa?.unit && pb?.unit && pa.unit !== pb.unit;
  el('path', { d, class: `sch-wire ${wire.kind}${selected ? ' selected' : ''}${mismatch ? ' mismatch' : ''}` }, wireLayer);
  const hit = el('path', { d, class: 'sch-wire-hit' }, wireLayer);
  const tip = el('title', {}, hit);
  tip.textContent = wire.kind === 'coupling' ? `${wire.signal} → ${wire.target_signal}`
    : wire.kind === 'bus' ? `bus ${wire.signal}: ${wire.source.node}.${wire.source.port} → ${wire.target.node}.${wire.target.port}`
      : `${wire.signal} (${wire.kind})${mismatch ? `\nunits differ: ${pa.unit} → ${pb.unit}` : ''}`;
  hit.addEventListener('mousedown', (e) => {
    e.stopPropagation();
    state.selection = { type: 'wire', id: wire.id };
    emit('selection');
    render();
  });
  const t = wire.spec?.transform || wire.spec;
  if (wire.kind === 'coupling' && t && (Number(t.gain ?? 1) !== 1 || Number(t.offset ?? 0) !== 0)) {
    const label = el('text', { class: 'sch-wire-label', x: (p.x + q.x) / 2, y: (p.y + q.y) / 2 - 4, 'text-anchor': 'middle' }, wireLayer);
    label.textContent = `×${t.gain ?? 1}${Number(t.offset ?? 0) ? ' + ' + t.offset : ''}`;
  }
}

function busBoards(node) {
  const ids = new Set();
  for (const wire of state.graph.wires) {
    if (wire.kind !== 'bus') continue;
    if (wire.source.node === node.id) ids.add(wire.target.node);
    if (wire.target.node === node.id) ids.add(wire.source.node);
  }
  return ids.size;
}

export function updateValues() {
  for (const tag of valueTags) {
    tag.el.textContent = showValues ? formatValue(signalValue(tag.signal), signalUnit(tag.signal)) : '';
  }
}

// -- interaction --------------------------------------------------------------
function startNodeDrag(e, node) {
  if (e.button !== 0 || e.target.classList.contains('pin')) return;
  e.stopPropagation();
  state.selection = { type: 'node', id: node.id };
  emit('selection');
  const start = toWorld(e), ox = node.x, oy = node.y;
  let moved = false;
  const move = (ev) => {
    const p = toWorld(ev);
    const nx = Math.round((ox + p.x - start.x) / 10) * 10, ny = Math.round((oy + p.y - start.y) / 10) * 10;
    if (nx !== node.x || ny !== node.y) { node.x = nx; node.y = ny; moved = true; render(); }
  };
  const up = () => {
    window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up);
    if (moved) markDirty(); else render();
  };
  window.addEventListener('mousemove', move); window.addEventListener('mouseup', up);
}

function startWireDrag(e, node, port) {
  e.stopPropagation(); e.preventDefault();
  const from = pinPosition(node, port.name);
  const line = el('path', { class: 'sch-drag' }, dragLayer);
  const move = (ev) => {
    const p = toWorld(ev);
    line.setAttribute('d', port.direction === 'out' ? wirePath(from, p) : wirePath(p, from));
    svg.querySelectorAll('.pin.hot').forEach((c) => c.classList.remove('hot'));
    if (ev.target.classList?.contains('pin')) ev.target.classList.add('hot');
  };
  const up = (ev) => {
    window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up);
    line.remove();
    const target = ev.target;
    if (target.classList?.contains('pin') && !(target.dataset.node === node.id && target.dataset.port === port.name)) {
      connect({ node: node.id, port: port.name }, { node: target.dataset.node, port: target.dataset.port });
    } else render();
  };
  window.addEventListener('mousemove', move); window.addEventListener('mouseup', up);
}

svg.addEventListener('mousedown', (e) => {
  if (e.target !== svg && e.target.closest('.sch-node')) return;
  svg.focus();
  const sx = e.clientX, sy = e.clientY, vx = view.x, vy = view.y;
  let moved = false;
  const move = (ev) => {
    if (Math.abs(ev.clientX - sx) + Math.abs(ev.clientY - sy) > 3) moved = true;
    view.x = vx + ev.clientX - sx; view.y = vy + ev.clientY - sy; applyView();
  };
  const up = () => {
    window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up);
    if (!moved && state.selection) { state.selection = null; emit('selection'); render(); }
  };
  window.addEventListener('mousemove', move); window.addEventListener('mouseup', up);
});

svg.addEventListener('wheel', (e) => {
  e.preventDefault();
  const rect = svg.getBoundingClientRect();
  const mx = e.clientX - rect.left, my = e.clientY - rect.top;
  const factor = Math.exp(-e.deltaY * 0.0015);
  const scale = Math.min(3, Math.max(0.25, view.scale * factor));
  view.x = mx - (mx - view.x) * (scale / view.scale);
  view.y = my - (my - view.y) * (scale / view.scale);
  view.scale = scale;
  applyView();
}, { passive: false });

svg.addEventListener('keydown', (e) => {
  if ((e.key === 'Delete' || e.key === 'Backspace') && state.selection) {
    e.preventDefault();
    if (state.selection.type === 'node') deleteNode(state.selection.id); else deleteWire(state.selection.id);
    emit('selection');
  } else if (e.key === 'f' || e.key === 'F') fit();
});

svg.addEventListener('dragover', (e) => { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; });
svg.addEventListener('drop', async (e) => {
  e.preventDefault();
  const partId = e.dataTransfer.getData('text/x-embeddyn-part');
  if (!partId || !state.graph) return;
  const p = toWorld(e);
  try { await addPart(partId, p.x - 80, p.y - 20); emit('selection'); } catch (error) { emit('error', error); }
});

export function fit() {
  const nodes = state.graph?.nodes || [];
  if (!nodes.length) { view.x = 20; view.y = 20; view.scale = 1; return applyView(); }
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const n of nodes) {
    const { width, height } = nodeSize(n);
    x0 = Math.min(x0, n.x - 60); y0 = Math.min(y0, n.y - 20); x1 = Math.max(x1, n.x + width + 90); y1 = Math.max(y1, n.y + height + 20);
  }
  const rect = svg.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  view.scale = Math.min(1.4, Math.max(0.25, Math.min(rect.width / (x1 - x0), rect.height / (y1 - y0))));
  view.x = (rect.width - (x1 - x0) * view.scale) / 2 - x0 * view.scale;
  view.y = (rect.height - (y1 - y0) * view.scale) / 2 - y0 * view.scale;
  applyView();
}

export function setShowValues(value) { showValues = value; updateValues(); }

on('graph-changed', render);
