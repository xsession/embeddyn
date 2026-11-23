// Graph editing operations: add/delete nodes, connect/disconnect pins, refresh ports.
import { api, findNode, markDirty, state, toast } from './store.js';

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
  gauge: 'gauge', led: 'led', data_table: 'watch',
};

export async function addPart(partId, x, y) {
  const part = state.partsById[partId];
  const id = uniqueNodeId(PART_BASE[partId] || partId.replace(/^scene_/, ''));
  const result = await api('instantiate', { part: partId, id, root: state.graph.root, signals: state.graph.signals });
  const node = result.node;
  node.x = Math.round(x / 10) * 10; node.y = Math.round(y / 10) * 10;
  node.label = id;
  Object.assign(state.graph.signals, result.signals);
  state.graph.nodes.push(node);
  state.selection = { type: 'node', id: node.id };
  markDirty();
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
  state.graph.wires = state.graph.wires.filter((w) =>
    !((w.source.node === node.id && !names.has(w.source.port)) || (w.target.node === node.id && !names.has(w.target.port))));
  for (const port of ports) {
    if (port.signal && !state.graph.signals[port.signal]) {
      state.graph.signals[port.signal] = { domain: node.domain || 'environment', unit: port.unit || '', initial: 0 };
    }
  }
  markDirty();
}
