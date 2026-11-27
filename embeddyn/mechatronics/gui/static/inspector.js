// Property inspector: project, part, wire.
import { deleteNode, deleteWire, refreshPorts } from './ops.js';
import { inspectorPanel } from './breadboard.js';
import { api, data, emit, findNode, formatValue, markDirty, nodeColor, on, signalUnit, state, toast } from './store.js';

const body = document.getElementById('inspector-body');

function h(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') e.className = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) if (c !== null && c !== undefined) e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return e;
}

const field = (label, input) => h('div', { class: 'field' }, h('span', { title: label }, label), input);

function input(value, onchange, type = 'text', extra = {}) {
  const el = h('input', { type, value: value ?? '', ...extra });
  el.addEventListener('change', () => onchange(type === 'number' ? (el.value === '' ? null : Number(el.value)) : el.value));
  return el;
}

function signalSelect(value, onchange) {
  const names = Object.keys(state.graph.signals).sort();
  const s = h('select', {}, h('option', { value: '' }, '— none —'),
    names.map((n) => h('option', { value: n, selected: n === value }, n)));
  s.addEventListener('change', () => onchange(s.value || null));
  return s;
}

export function render() {
  body.innerHTML = '';
  const graph = state.graph;
  if (!graph) return;
  if (inspectorPanel(body)) return;           // breadboard view: its own parts and wires
  const sel = state.selection;
  if (sel?.type === 'node') {
    const node = findNode(sel.id);
    if (node) return node.kind === 'instance' ? instancePanel(node) : node.kind === 'terminal' ? terminalPanel(node) : gadgetPanel(node);
  }
  if (sel?.type === 'wire') {
    const wire = graph.wires.find((w) => w.id === sel.id);
    if (wire) return wirePanel(wire);
  }
  projectPanel();
}

function projectPanel() {
  const g = state.graph;
  body.append(
    h('h3', {}, 'Project'),
    field('name', input(g.name, (v) => { g.name = v; markDirty(); })),
    field('macro step [s]', input(g.step_seconds, (v) => { g.step_seconds = v; markDirty(); }, 'number', { step: 'any' })),
    field('duration [s]', input(g.duration_seconds, (v) => { g.duration_seconds = v; markDirty(); }, 'number', { step: 'any' })),
    h('div', { class: 'field' }, h('span', {}, 'description'),
      (() => { const t = h('textarea', { rows: 4 }); t.value = g.description || ''; t.onchange = () => { g.description = t.value; state.dirty = true; }; return t; })()),
    h('div', { class: 'note' }, g.path ? `File: ${g.path}` : `Unsaved. Relative paths resolve against ${g.root}`),
    h('h4', {}, `Signals (${Object.keys(g.signals).length})`),
  );
  const table = h('table', { class: 'kv' }, h('tr', {}, h('td', {}, 'name'), h('td', {}, 'unit'), h('td', {}, 'initial'), h('td', {}, 'now')));
  for (const [name, spec] of Object.entries(g.signals).sort()) {
    table.append(h('tr', {},
      h('td', { title: `${name} (${spec.domain || 'environment'})` }, name),
      h('td', {}, input(spec.unit || '', (v) => { spec.unit = v; state.dirty = true; }, 'text', { style: 'width:46px' })),
      h('td', {}, input(spec.initial ?? 0, (v) => { spec.initial = v ?? 0; state.dirty = true; }, 'number', { step: 'any', style: 'width:64px' })),
      h('td', {}, formatValue(data.value(name), spec.unit || ''))));
  }
  body.append(table, h('div', { class: 'note', style: 'margin-top:8px' },
    'Select a part or a wire to edit it. Sections such as metrics, verification and scheduler are kept as they are in the file.'));
}

function partOfModel(model) {
  return state.parts.find((p) => p.kind === 'instance' && p.template?.model === model);
}

function instancePanel(node) {
  const spec = node.spec;
  spec.options = spec.options || {};
  const options = spec.options;
  body.append(
    h('h3', {}, h('span', { class: 'tag', style: `background:${nodeColor(node)}` }, node.domain), ' ', node.id),
    field('label', input(node.label, (v) => { node.label = v || node.id; markDirty(); })),
    field('domain', (() => {
      const s = h('select', {}, ['firmware', 'electrical', 'mechanical', 'thermal', 'environment'].map((d) => h('option', { selected: d === spec.domain }, d)));
      s.onchange = () => { spec.domain = s.value; node.domain = s.value; markDirty(); render(); };
      return s;
    })()),
    field('backend', (() => {
      const s = h('select', {}, ['python', 'renode', 'ngspice-shared', 'ngspice', 'ltspice', 'webots', 'fmu', 'replay', 'mock', 'auto']
        .map((b) => h('option', { selected: b === (options.backend || 'auto') }, b)));
      s.onchange = async () => { options.backend = s.value; await refreshPorts(node); render(); };
      return s;
    })()),
    field('model', input(spec.model || '', async (v) => { spec.model = v || undefined; await refreshPorts(node).catch((e) => toast(e.message, true)); render(); })),
  );
  if (options.backend === 'python') {
    const part = partOfModel(spec.model);
    if (part?.parameters && Object.keys(part.parameters).length) {
      body.append(h('h4', {}, 'Plant parameters'));
      const params = options.parameters = options.parameters || {};
      const table = h('table', { class: 'kv' });
      for (const [name, def] of Object.entries(part.parameters)) {
        const current = params[name];
        const el = h('input', { value: current === undefined ? '' : (Array.isArray(current) ? current.join(', ') : current), placeholder: Array.isArray(def) ? def.join(', ') : String(def) });
        el.onchange = () => {
          const raw = el.value.trim();
          if (raw === '') delete params[name];
          else if (Array.isArray(def)) params[name] = raw.split(',').map(Number);
          else if (typeof def === 'number') params[name] = raw.startsWith('@') ? raw : Number(raw);
          else params[name] = raw;
          state.dirty = true;
        };
        table.append(h('tr', {}, h('td', {}, name), h('td', {}, el)));
      }
      body.append(table, h('div', { class: 'note' }, 'Empty = plant default. "@name" uses a project parameter.'));
    }
  }
  body.append(h('h4', {}, 'Options (YAML)'));
  const area = h('textarea', { rows: 9 });
  api('yaml/dump', { value: options }).then((r) => (area.value = r.text));
  body.append(area, h('div', { class: 'btn-row' }, h('button', { onclick: async () => {
    try {
      const { value } = await api('yaml/parse', { text: area.value });
      spec.options = value;
      await refreshPorts(node);
      toast('Options applied');
      render();
    } catch (error) { toast(error.message, true); }
  } }, 'Apply options')));
  body.append(h('h4', {}, `Pins (${node.ports.length})`));
  const table = h('table', { class: 'kv' });
  for (const port of node.ports) {
    table.append(h('tr', {}, h('td', {}, (port.direction === 'in' ? '→ ' : '← ') + port.name),
      h('td', { title: port.label || '' }, port.signal ? `${port.signal}${port.unit ? ' [' + port.unit + ']' : ''}` : h('i', {}, port.direction === 'in' ? 'unconnected (default)' : 'not mapped'))));
  }
  body.append(table, h('div', { class: 'btn-row' }, h('button', { class: 'danger', onclick: () => { deleteNode(node.id); emit('selection'); } }, 'Delete part')));
}

function terminalPanel(node) {
  const port = node.ports[0];
  body.append(h('h3', {}, 'Net label'), h('div', { class: 'note' },
    `Signal ${port.signal} is ${port.direction === 'out' ? 'read by a coupling' : 'driven by a coupling'} but no part exposes it as a pin.`));
}

function gadgetPanel(node) {
  const part = state.partsById[node.part];
  node.settings = node.settings || {};
  body.append(
    h('h3', {}, h('span', { class: 'tag', style: `background:${nodeColor(node)}` }, node.kind), ' ', node.id),
    h('div', { class: 'note' }, part?.description || ''),
    field('label', input(node.label, (v) => { node.label = v || node.id; markDirty(); })),
    h('h4', {}, node.kind === 'source' ? 'Output' : 'Channels'),
  );
  for (const port of node.ports) {
    if (node.kind === 'source') {
      body.append(field(port.name, h('code', {}, port.signal)));
      continue;
    }
    body.append(field(port.name, signalSelect(port.signal, (signal) => {
      state.graph.wires = state.graph.wires.filter((w) => !(w.target.node === node.id && w.target.port === port.name));
      port.signal = signal;
      if (signal) {
        const producer = state.graph.nodes.find((n) => n.id !== node.id && n.ports.some((p) => p.direction === 'out' && p.signal === signal));
        if (producer) {
          const p = producer.ports.find((q) => q.direction === 'out' && q.signal === signal);
          state.graph.wires.push({ id: 'p' + Date.now().toString(36), kind: 'probe', signal, source: { node: producer.id, port: p.name }, target: { node: node.id, port: port.name } });
        }
      }
      markDirty();
    })));
  }
  body.append(h('h4', {}, 'Settings'));
  for (const [key, value] of Object.entries(node.settings || {})) {
    if (value !== null && typeof value === 'object') continue;
    let el;
    if (key === 'timebase' || key === 'mode' || key === 'layout' || key === 'window') continue;   // set on the instrument card
    if (typeof value === 'boolean') {
      el = h('input', { type: 'checkbox', checked: value });
      el.onchange = () => { node.settings[key] = el.checked; markDirty(); };
    } else if (typeof value === 'number') {
      el = input(value, (v) => { node.settings[key] = v ?? 0; markDirty(); }, 'number', { step: 'any' });
    } else if (key === 'color') {
      el = input(value, (v) => { node.settings[key] = v; markDirty(); }, 'color');
    } else if (key === 'unit' && node.kind === 'scene') {
      el = h('select', {}, ['auto', 'rad', 'deg'].map((u) => h('option', { selected: u === value }, u)));
      el.onchange = () => { node.settings[key] = el.value; state.dirty = true; };
    } else {
      el = input(value, (v) => {
        node.settings[key] = v;
        if (key === 'unit' && node.kind === 'source' && state.graph.signals[node.ports[0].signal]) state.graph.signals[node.ports[0].signal].unit = v;
        markDirty();
      });
    }
    body.append(field(key, el));
  }
  if (node.kind === 'scene') body.append(h('div', { class: 'note' }, 'x, z: position on the floor [m]; yaw [deg]. Angles in deg or rad follow the signal unit unless "unit" is set.'));
  body.append(h('div', { class: 'btn-row' }, h('button', { class: 'danger', onclick: () => { deleteNode(node.id); emit('selection'); } }, 'Delete')));
}

function wirePanel(wire) {
  body.append(h('h3', {}, `Wire · ${wire.kind}`));
  body.append(field('from', h('code', {}, `${wire.source.node}.${wire.source.port}`)), field('to', h('code', {}, `${wire.target.node}.${wire.target.port}`)),
    field('signal', h('code', {}, wire.signal + (signalUnit(wire.signal) ? ` [${signalUnit(wire.signal)}]` : ''))));
  if (wire.kind === 'coupling') {
    body.append(field('target signal', h('code', {}, wire.target_signal)));
    const spec = wire.spec = wire.spec || {};
    if (!spec.transform && ('gain' in spec || 'offset' in spec)) {      // shorthand -> explicit transform
      spec.transform = { type: 'affine', gain: spec.gain ?? 1, offset: spec.offset ?? 0 };
      delete spec.gain; delete spec.offset;
    }
    const t = spec.transform = spec.transform || {};
    body.append(h('h4', {}, 'Transform (target = f(source))'),
      field('type', (() => {
        const s = h('select', {}, ['affine', 'identity', 'clamp', 'deadband', 'quantize'].map((x) => h('option', { selected: x === (t.type || 'affine') }, x)));
        s.onchange = () => { t.type = s.value; markDirty(); };
        return s;
      })()),
      // A number, or a project parameter: @name, -@name, 1/@name (e.g. gain @motor.kt).
      ...['gain', 'offset', 'min', 'max', 'deadband'].map((k) => field(k, input(t[k], (v) => {
        const text = String(v ?? '').trim();
        if (text === '') delete t[k];
        else if (Number.isFinite(Number(text))) t[k] = Number(text);
        else if (/^-?\s*(1\s*\/\s*)?@[A-Za-z_][\w.\-]*$/.test(text)) t[k] = text;
        else { toast(`${k}: a number or @parameter`, true); return; }
        markDirty();
      }, 'text', { placeholder: k === 'gain' ? '1 or @param' : k === 'offset' ? '0 or @param' : '' }))),
      field('delay steps', input(spec.delay_steps ?? 0, (v) => { spec.delay_steps = v || 0; markDirty(); }, 'number', { min: 0, step: 1 })));
  } else if (wire.kind === 'net') {
    body.append(h('div', { class: 'note' }, 'The input reads this signal directly. To scale it, map the input to its own signal in the options and add a coupling.'));
  } else {
    body.append(h('div', { class: 'note' }, 'Probe: the instrument or body observes the signal; the simulation is unchanged.'));
  }
  body.append(h('div', { class: 'btn-row' }, h('button', { class: 'danger', onclick: () => { deleteWire(wire.id); emit('selection'); } }, 'Delete wire')));
}

on('selection', render);
on('graph-loaded', render);
