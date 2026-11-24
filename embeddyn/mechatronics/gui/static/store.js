// Shared state, event bus, API helpers and the run data buffer.

export const bus = new EventTarget();
export const emit = (name, detail) => bus.dispatchEvent(new CustomEvent(name, { detail }));
export const on = (name, fn) => bus.addEventListener(name, (e) => fn(e.detail));

export const state = {
  graph: null,          // see embeddyn/mechatronics/gui/document.py
  parts: [], partsById: {},
  selection: null,      // {type: 'node'|'wire', id}
  session: null,        // run info from the server
  dirty: false,
};

export const DOMAIN_COLORS = {
  firmware: '#8e5bd6', electrical: '#e67e22', mechanical: '#2f80d1', thermal: '#d64541', environment: '#27ae60',
};
export const KIND_COLORS = { source: '#16a085', instrument: '#3d4b5c', scene: '#7a6f9b', terminal: '#6b7380' };
export const GLYPHS = {
  mcu: '▣', chip: '◧', servo: '⟲', gear: '⚙', motor: 'M', circuit: '⏚', thermo: '♨', wave: '∿', table: '☷',
  robot: '⛭', box: '▢', slider: '⊶', switch: '⏻', scope: '⌁', meter: 'Ω', logic: '⫶', xy: '⌗', gauge: '◔',
  led: '●', terminal: '⌨', pendulum: '⊥', cart: '⛟', linear: '⇿', lamp: '☀', bar: '▮', net: '⏛',
};
export const TRACE_COLORS = ['#ffd60a', '#3fd0ff', '#ff6bd6', '#6bff8b', '#ff9f43', '#a29bfe', '#ff5c5c', '#e0e0e0'];

export function nodeColor(node) {
  if (node.kind === 'instance') return DOMAIN_COLORS[node.domain] || '#555';
  return KIND_COLORS[node.kind] || '#555';
}

export async function api(path, body) {
  const response = await fetch('/api/' + path, body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({ error: response.statusText }));
  if (!response.ok || data.error) throw new Error(data.error || response.statusText);
  return data;
}

export function toast(message, error = false) {
  const el = document.getElementById('toast');
  el.textContent = message;
  el.className = 'show' + (error ? ' err' : '');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.className = ''), error ? 5000 : 2200);
}

export function log(message, level = '') {
  const el = document.getElementById('console');
  const line = document.createElement('div');
  if (level) line.className = level;
  line.textContent = `[${new Date().toLocaleTimeString()}] ${message}`;
  el.appendChild(line);
  el.parentElement.scrollTop = el.parentElement.scrollHeight;
  if (level === 'err') {
    const badge = document.getElementById('console-badge');
    badge.textContent = String(Number(badge.textContent || 0) + 1);
    badge.classList.remove('hidden');
  }
}

export function markDirty() {
  state.dirty = true;
  emit('graph-changed');
}

export const findNode = (id) => state.graph?.nodes.find((n) => n.id === id);
export const findPort = (nodeId, portName) => findNode(nodeId)?.ports.find((p) => p.name === portName);

// ---------------------------------------------------------------------------
// Recorded run data: column arrays per signal, time cursor for playback.
export const data = {
  names: [], units: {}, index: {}, time: [], columns: [], cursor: -1, follow: true,
  events: [],          // {time_seconds, topic, payload, source}: UART lines and other adapter events

  reset(names = [], units = {}) {
    this.names = names; this.units = units; this.time = []; this.cursor = -1; this.events = [];
    this.index = Object.fromEntries(names.map((n, i) => [n, i]));
    this.columns = names.map(() => []);
  },
  append(rows) {
    for (const row of rows) {
      this.time.push(row[0]);
      for (let i = 0; i < this.columns.length; i++) this.columns[i].push(row[i + 1]);
    }
    if (this.follow) this.cursor = this.time.length - 1;
  },
  get length() { return this.time.length; },
  column(name) { const i = this.index[name]; return i === undefined ? null : this.columns[i]; },
  value(name, at = this.cursor) {
    const col = this.column(name);
    if (!col || at < 0 || at >= col.length) return null;
    return col[at];
  },
  now() { return this.cursor >= 0 ? this.time[this.cursor] : 0; },
  // First index with time >= t (binary search).
  indexAt(t) {
    let lo = 0, hi = this.time.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (this.time[mid] < t) lo = mid + 1; else hi = mid; }
    return lo;
  },
};

// Value of a signal: recorded value at the cursor, else a live control, else the initial value.
export function signalValue(name) {
  if (!name) return null;
  const recorded = data.value(name);
  if (recorded !== null && recorded !== undefined) return recorded;
  const spec = state.graph?.signals?.[name];
  return spec ? Number(spec.initial ?? 0) : null;
}

export function signalUnit(name) {
  return data.units[name] ?? state.graph?.signals?.[name]?.unit ?? '';
}

export function formatValue(value, unit = '') {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  if (Math.abs(value) < 1e-12) value = 0;
  const abs = Math.abs(value);
  let text;
  if (abs !== 0 && (abs < 1e-3 || abs >= 1e5)) {
    const prefixes = [[1e-9, 'n'], [1e-6, 'µ'], [1e-3, 'm'], [1, ''], [1e3, 'k'], [1e6, 'M']];
    if (unit && abs < 1e5) {
      const [scale, prefix] = prefixes.filter(([s]) => abs >= s).pop() || prefixes[0];
      return `${(value / scale).toPrecision(4)} ${prefix}${unit}`;
    }
    text = value.toExponential(3);
  } else {
    text = abs >= 100 ? value.toFixed(1) : abs >= 1 ? value.toFixed(3) : value.toFixed(4);
  }
  return unit ? `${text} ${unit}` : text;
}
