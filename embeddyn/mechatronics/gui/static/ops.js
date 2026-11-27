// Graph editing operations: add/delete nodes, connect/disconnect pins, refresh ports.
import { api, emit, findNode, markDirty, state, toast } from './store.js';

let wireCounter = 1;
const newWireId = () => {
  const ids = new Set(state.graph.wires.map((w) => w.id));
  while (ids.has('w' + wireCounter)) wireCounter++;
  return 'w' + wireCounter++;
};

export function uniqueNodeId(base) {
  const clean = (base || 'part').replace(/[^A-Za-z0-9_]/g, '_').replace(/^[^A-Za-z]+/, '') || 'part';
  const ids = new Set(state.graph.nodes.map((n) => n.id));
  for (let i = 1; ; i++) if (!ids.has(clean + i)) return clean + i;
}

const PART_BASE = {
  rc_servo: 'servo', drivetrain: 'drive', dc_motor_rl: 'motor', thermal_rc: 'thermal', pi_controller: 'pi',
  servo_pulse_gen: 'pulsegen', function_generator: 'fgen', stm32l476_servo_fw: 'mcu', renode_mcu: 'mcu',
  ngspice_circuit: 'circuit', webots_world: 'world', fmu: 'fmu', replay_csv: 'replay', slider: 'slider',
  switch: 'switch', oscilloscope: 'scope', multimeter: 'dmm', logic_analyser: 'logic', xy_plotter: 'xy',
  gauge: 'gauge', led: 'led', data_table: 'watch', tmp102: 'tmp', lis3dh: 'acc', em4100: 'card', gps: 'gps',
  can_bus: 'canbus', i2c_master: 'i2cmcu', spi_master: 'spimcu', eth_plc: 'plc', eth_io: 'io', modbus_master: 'plc',
  modbus_slave: 'drive',
};

// Re-derive the graph from its project document: bus nodes and bus wires follow the boards' event options.
export async function regraph() {
  const selection = state.selection;
  const { graph } = await api('regraph', { graph: state.graph });
  state.graph = graph;
  state.selection = selection && (selection.type === 'node' ? findNode(selection.id) : null) ? selection : null;
  markDirty();
  emit('selection');
}

const busId = (topic) => 'bus_' + topic.replace(/[^A-Za-z0-9_]/g, '_');

async function addBus(x, y) {
  let n = 1;                                     // named bus1, bus2…: rename it in the inspector (can, uart, eth…)
  while (findNode(busId(`bus${n}`))) n++;
  const topic = `bus${n}`;
  state.graph.nodes.push({ id: busId(topic), kind: 'bus', part: 'bus', label: topic, topic, icon: 'bus',
    x: Math.round(x / 10) * 10, y: Math.round(y / 10) * 10, ports: [] });
  await regraph();
  state.selection = { type: 'node', id: busId(topic) };
  toast(`Bus ${topic}: name its topic in the inspector, then wire ⇄ event pins to it`);
  return findNode(busId(topic));
}

// Publish/subscribe an event pin on a topic (null: take it off its bus).
function setEvent(node, port, topic, previous = port.signal) {
  if (!node || node.kind !== 'instance') return;
  const options = node.spec.options = node.spec.options || {};
  if (port.name.startsWith('event:')) {
    const key = port.direction === 'out' ? 'events' : 'subscribe';
    options[key] = options[key] || {};
    if (topic) options[key][port.event] = topic; else delete options[key][port.event];
    if (!Object.keys(options[key]).length) delete options[key];
  } else if (port.name === 'can:tx') {
    options.can = options.can || {};
    if (topic) options.can.publish = topic; else delete options.can.publish;
  } else if (port.name.startsWith('can:rx')) {
    options.can = options.can || {};
    let list = [].concat(options.can.subscribe || []).filter((t) => t !== previous);
    if (topic && !list.includes(topic)) list.push(topic);
    if (!list.length) delete options.can.subscribe; else options.can.subscribe = list.length === 1 ? list[0] : list;
  }
}

// Rename a bus: every board on it, its medium and the analyzer's topic list follow.
export async function renameBus(oldTopic, newTopic) {
  newTopic = (newTopic || '').trim();
  if (!newTopic || newTopic === oldTopic) return;
  if (!/^[A-Za-z0-9_.\-]+$/.test(newTopic)) return toast('A topic is letters, digits, _ . -', true);
  if (findNode(busId(newTopic))) return toast(`Bus ${newTopic} exists already: delete one of them first`, true);
  for (const node of state.graph.nodes) {
    for (const port of node.ports || []) if (port.kind === 'event' && port.signal === oldTopic && node.kind === 'instance') setEvent(node, port, newTopic, oldTopic);
  }
  const bus = findNode(busId(oldTopic));
  if (bus) Object.assign(bus, { id: busId(newTopic), topic: newTopic, label: newTopic });
  const extra = state.graph.extra || {};
  if (extra.media?.[oldTopic]) { extra.media[newTopic] = extra.media[oldTopic]; delete extra.media[oldTopic]; }
  const analyzer = extra.metadata?.analyzer;
  if (analyzer?.topics) analyzer.topics = analyzer.topics.map((t) => (t === oldTopic ? newTopic : t));
  if (analyzer?.inject) for (const [k, v] of Object.entries(analyzer.inject)) if (v === oldTopic) analyzer.inject[k] = newTopic;
  if (state.selection?.id === busId(oldTopic)) state.selection = { type: 'node', id: busId(newTopic) };
  await regraph();
}

async function connectEvent(src, dst) {
  const srcNode = findNode(src.node), dstNode = findNode(dst.node);
  const srcPort = srcNode.ports.find((p) => p.name === src.port), dstPort = dstNode.ports.find((p) => p.name === dst.port);
  if (srcPort.kind !== 'event' || dstPort.kind !== 'event') return toast('Event pins (bus connections) connect only to event pins or buses', true);
  if (srcNode.kind === 'bus' && dstNode.kind === 'bus') return toast('Connect boards to a bus, not two buses', true);
  if (srcPort.name.startsWith('uart:') || dstPort.name.startsWith('uart:')) return toast('A UART console goes to a serial monitor, not a bus', true);
  const topic = srcNode.kind === 'bus' ? srcNode.topic || srcNode.label
    : dstNode.kind === 'bus' ? dstNode.topic || dstNode.label
      : srcPort.signal || dstPort.signal || `${srcNode.id}.${srcPort.event || 'tx'}`;
  if (srcNode.kind !== 'bus') setEvent(srcNode, srcPort, topic);
  if (dstNode.kind !== 'bus') setEvent(dstNode, dstPort, topic, dstPort.name.startsWith('can:rx') ? null : dstPort.signal);
  await regraph();
  toast(`${src.node}.${src.port} → ${topic} → ${dst.node}.${dst.port}`);
}

export async function addPart(partId, x, y) {
  if (partId === 'bus') return addBus(x, y);
  const part = state.partsById[partId];
  const id = uniqueNodeId(PART_BASE[partId] || partId.replace(/^scene_/, ''));
  const result = await api('instantiate', { part: partId, id, root: state.graph.root, signals: state.graph.signals });
  const node = result.node;
  node.x = Math.round(x / 10) * 10; node.y = Math.round(y / 10) * 10;
  node.label = id;
  Object.assign(state.graph.signals, result.signals);
  state.graph.nodes.push(node);
  state.selection = { type: 'node', id: node.id };
  if (node.ports.some((p) => p.kind === 'event' && p.signal)) await regraph();      // it joins its usual bus
  else markDirty();
  toast(`Added ${part?.name || partId} as ${id}`);
  return node;
}

function cleanupWire(wire) {
  const target = findNode(wire.target.node);
  const port = target?.ports.find((p) => p.name === wire.target.port);
  if (!port) return;
  if (wire.kind === 'probe') port.signal = null;
  if (wire.kind === 'net' && port.mappable) {
    port.signal = null;
    if (target.spec?.options?.inputs) delete target.spec.options.inputs[port.name];
  }
}

export function deleteWire(id) {
  const wire = state.graph.wires.find((w) => w.id === id);
  if (!wire) return;
  if (wire.kind === 'bus') {                     // take the board's pin off the bus
    const end = findNode(wire.source.node)?.kind === 'bus' ? wire.target : wire.source;
    const node = findNode(end.node);
    setEvent(node, node.ports.find((p) => p.name === end.port), null, wire.signal);
    if (state.selection?.id === id) state.selection = null;
    regraph().catch((error) => toast(error.message, true));
    return;
  }
  cleanupWire(wire);
  state.graph.wires = state.graph.wires.filter((w) => w.id !== id);
  if (state.selection?.id === id) state.selection = null;
  markDirty();
}

function signalInUse(name) {
  return state.graph.nodes.some((n) => n.ports.some((p) => p.signal === name))
    || state.graph.wires.some((w) => w.kind === 'coupling' && (w.signal === name || w.target_signal === name));
}

export function deleteNode(id) {
  const node = findNode(id);
  if (!node) return;
  if (node.kind === 'bus') {                     // every board leaves the bus
    const topic = node.topic || node.label;
    for (const other of state.graph.nodes) {
      for (const port of other.ports || []) if (port.kind === 'event' && port.signal === topic) setEvent(other, port, null, topic);
    }
    state.graph.nodes = state.graph.nodes.filter((n) => n.id !== id);
    if (state.selection?.id === id) state.selection = null;
    regraph().catch((error) => toast(error.message, true));
    return;
  }
  for (const wire of state.graph.wires.filter((w) => w.source.node === id || w.target.node === id)) cleanupWire(wire);
  state.graph.wires = state.graph.wires.filter((w) => w.source.node !== id && w.target.node !== id);
  state.graph.nodes = state.graph.nodes.filter((n) => n.id !== id);
  for (const name of Object.keys(state.graph.signals)) {
    if (name.startsWith(id + '.') && !signalInUse(name)) delete state.graph.signals[name];
  }
  if (state.selection?.id === id) state.selection = null;
  markDirty();
}

// Give an unmapped python/FMU output a signal so it can be wired.
function ensureOutputSignal(node, port) {
  if (port.signal) return port.signal;
  const name = `${node.id}.${port.name.replace(/[^A-Za-z0-9_]+/g, '_').replace(/_+$/, '')}`;   // qpos:hinge[0] -> qpos_hinge_0
  port.signal = name;
  node.spec.options.outputs = node.spec.options.outputs || {};
  node.spec.options.outputs[port.name] = name;
  state.graph.signals[name] = state.graph.signals[name] || { domain: node.domain || 'environment', unit: port.unit || '', initial: 0 };
  return name;
}

export function connect(a, b) {
  // a, b: {node, port}; one output and one input.
  let src = a, dst = b;
  const pa = findNode(a.node)?.ports.find((p) => p.name === a.port);
  const pb = findNode(b.node)?.ports.find((p) => p.name === b.port);
  if (!pa || !pb) return;
  if (pa.direction === pb.direction) return toast('Connect an output pin (right side) to an input pin (left side)', true);
  if (pa.direction === 'in') { src = b; dst = a; }
  if (src.node === dst.node) return toast('A part cannot feed itself directly', true);
  if (pa.kind === 'event' || pb.kind === 'event') { connectEvent(src, dst).catch((error) => toast(error.message, true)); return; }
  const srcNode = findNode(src.node), dstNode = findNode(dst.node);
  const srcPort = srcNode.ports.find((p) => p.name === src.port);
  const dstPort = dstNode.ports.find((p) => p.name === dst.port);
  if (!srcPort.signal) {
    if (srcNode.kind === 'instance' && srcPort.mappable) ensureOutputSignal(srcNode, srcPort);
    else return toast('This pin has no signal yet', true);
  }
  const signal = srcPort.signal;
  // One driver per input: replace an existing wire into this pin.
  for (const wire of state.graph.wires.filter((w) => w.target.node === dst.node && w.target.port === dst.port)) {
    cleanupWire(wire);
    state.graph.wires = state.graph.wires.filter((w) => w !== wire);
  }
  let wire;
  if (dstNode.kind === 'instrument' || dstNode.kind === 'scene') {
    dstPort.signal = signal;
    wire = { kind: 'probe', signal };
  } else if (dstNode.kind === 'instance' && dstPort.mappable) {
    dstPort.signal = signal;
    dstNode.spec.options.inputs = dstNode.spec.options.inputs || {};
    dstNode.spec.options.inputs[dstPort.name] = signal;
    if (!dstPort.unit) dstPort.unit = srcPort.unit;
    else if (srcPort.unit && srcPort.unit !== dstPort.unit) {
      toast(`Units differ: ${srcPort.unit} → ${dstPort.unit}. Check (or a coupling with a gain) shows the conversion.`, true);
    }
    wire = { kind: 'net', signal };
  } else if (dstPort.signal) {
    if (dstPort.signal === signal) wire = { kind: 'net', signal };
    else wire = { kind: 'coupling', signal, target_signal: dstPort.signal, spec: {} };
  } else {
    return toast('This input has no signal to drive', true);
  }
  Object.assign(wire, { id: newWireId(), source: { ...src }, target: { ...dst } });
  state.graph.wires.push(wire);
  state.selection = { type: 'wire', id: wire.id };
  markDirty();
}

// Re-derive the ports of an instance after its spec changed; keep wires whose pins remain.
export async function refreshPorts(node) {
  const couplings = state.graph.wires.filter((w) => w.kind === 'coupling')
    .map((w) => ({ source: w.signal, target: w.target_signal }));
  const { ports } = await api('ports', { spec: node.spec, root: state.graph.root, signals: state.graph.signals, couplings });
  node.ports = ports;
  const names = new Set(ports.map((p) => p.name));
  const gone = state.graph.wires.filter((w) =>
    (w.source.node === node.id && !names.has(w.source.port)) || (w.target.node === node.id && !names.has(w.target.port)));
  for (const wire of gone) if (wire.target.node !== node.id) cleanupWire(wire);   // the reader keeps no stale mapping
  state.graph.wires = state.graph.wires.filter((w) => !gone.includes(w));
  for (const port of ports) {
    if (port.signal && port.kind !== 'event' && !state.graph.signals[port.signal]) {
      state.graph.signals[port.signal] = { domain: node.domain || 'environment', unit: port.unit || '', initial: 0 };
    }
  }
  if (ports.some((p) => p.kind === 'event') || gone.some((w) => w.kind === 'bus')) return regraph();
  markDirty();
}
