// A small dependency-free 3-D view (flat-shaded painter's renderer) for the mechanism.
// Bodies come from "scene" parts on the schematic; their pose follows wired signals.
import { signalUnit, signalValue, state } from './store.js';

const canvas = document.getElementById('scene');
const HOME = { yaw: -0.7, pitch: 0.5, dist: 0.35, target: [0, 0.03, 0] };
const camera = { ...HOME, target: [...HOME.target] };
let framed = '';
const LIGHT = normalize([0.4, 0.9, 0.3]);

// -- math --------------------------------------------------------------------------
function normalize(v) { const l = Math.hypot(...v) || 1; return v.map((x) => x / l); }
const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const I = () => [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];
function mul(a, b) {          // column-major 4x4: (a*b)
  const r = new Array(16);
  for (let c = 0; c < 4; c++) for (let rI = 0; rI < 4; rI++) {
    r[c * 4 + rI] = a[rI] * b[c * 4] + a[4 + rI] * b[c * 4 + 1] + a[8 + rI] * b[c * 4 + 2] + a[12 + rI] * b[c * 4 + 3];
  }
  return r;
}
const T = (x, y, z) => [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, x, y, z, 1];
const S = (x, y, z) => [x, 0, 0, 0, 0, y, 0, 0, 0, 0, z, 0, 0, 0, 0, 1];
const RX = (a) => { const c = Math.cos(a), s = Math.sin(a); return [1, 0, 0, 0, 0, c, s, 0, 0, -s, c, 0, 0, 0, 0, 1]; };
const RY = (a) => { const c = Math.cos(a), s = Math.sin(a); return [c, 0, -s, 0, 0, 1, 0, 0, s, 0, c, 0, 0, 0, 0, 1]; };
const RZ = (a) => { const c = Math.cos(a), s = Math.sin(a); return [c, s, 0, 0, -s, c, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]; };
const chain = (...ms) => ms.reduce((a, b) => mul(a, b), I());
const apply = (m, v) => [m[0] * v[0] + m[4] * v[1] + m[8] * v[2] + m[12], m[1] * v[0] + m[5] * v[1] + m[9] * v[2] + m[13], m[2] * v[0] + m[6] * v[1] + m[10] * v[2] + m[14]];

// -- meshes (unit size, centred) ------------------------------------------------------
const BOX = (() => {
  const v = [[-.5, -.5, -.5], [.5, -.5, -.5], [.5, .5, -.5], [-.5, .5, -.5], [-.5, -.5, .5], [.5, -.5, .5], [.5, .5, .5], [-.5, .5, .5]];
  const f = [[0, 3, 2, 1], [4, 5, 6, 7], [0, 1, 5, 4], [2, 3, 7, 6], [1, 2, 6, 5], [0, 4, 7, 3]];
  return { v, f };
})();
const CYL = (() => {              // axis along Y, radius 0.5, height 1
  const n = 20, v = [], f = [];
  for (let i = 0; i < n; i++) { const a = (i / n) * Math.PI * 2; v.push([Math.cos(a) * .5, -.5, Math.sin(a) * .5], [Math.cos(a) * .5, .5, Math.sin(a) * .5]); }
  for (let i = 0; i < n; i++) { const j = (i + 1) % n; f.push([2 * i, 2 * i + 1, 2 * j + 1, 2 * j]); }
  f.push([...Array(n).keys()].map((i) => 2 * i));
  f.push([...Array(n).keys()].map((i) => 2 * (n - 1 - i) + 1));
  return { v, f };
})();

function rgb(hex) {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex || '');
  const n = m ? parseInt(m[1], 16) : 0x888888;
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

// -- bodies ---------------------------------------------------------------------------
function angleOf(node, portName) {
  const port = node.ports.find((p) => p.name === portName);
  if (!port?.signal) return 0;
  const v = Number(signalValue(port.signal) ?? 0);
  const unit = node.settings.unit && node.settings.unit !== 'auto' ? node.settings.unit : signalUnit(port.signal);
  return unit === 'deg' ? (v * Math.PI) / 180 : v;
}
function valueOf(node, portName) {
  const port = node.ports.find((p) => p.name === portName);
  return port?.signal ? Number(signalValue(port.signal) ?? 0) : 0;
}

function bodies(node) {
  const s = node.settings, base = chain(T(Number(s.x || 0), 0, Number(s.z || 0)), RY((Number(s.yaw || 0) * Math.PI) / 180));
  const color = s.color || '#888';
  const out = [];
  const add = (mesh, m, c, emissive = false) => out.push({ mesh, m: mul(base, m), color: rgb(c), emissive });
  switch (node.icon) {
    case 'servo': {
      const a = angleOf(node, 'angle'), L = Number(s.length || 0.12);
      add(BOX, chain(T(0, 0.019, 0), S(0.04, 0.038, 0.02)), '#1d2733');
      add(BOX, chain(T(0, 0.03, 0), S(0.055, 0.003, 0.02)), '#1d2733');
      add(CYL, chain(T(0.01, 0.041, 0), S(0.012, 0.006, 0.012)), '#dddddd');
      const arm = chain(T(0.01, 0.046, 0), RY(-a));
      add(BOX, mul(arm, chain(T(L / 2 - 0.01, 0, 0), S(L, 0.005, 0.012))), color);
      add(CYL, mul(arm, chain(T(L - 0.012, 0.008, 0), S(0.014, 0.012, 0.014))), '#ffffff');
      break;
    }
    case 'motor': {
      const a = angleOf(node, 'angle'), R = Number(s.radius || 0.05);
      add(BOX, chain(T(0, 0.005, 0), S(0.09, 0.01, 0.07)), '#444b55');
      add(CYL, chain(T(0, R + 0.01, 0), RZ(Math.PI / 2), S(0.05, 0.08, 0.05)), color);
      const shaft = chain(T(0.05, R + 0.01, 0), RX(a));
      add(CYL, mul(shaft, chain(RZ(Math.PI / 2), S(0.01, 0.04, 0.01))), '#cccccc');
      add(CYL, mul(shaft, chain(T(0.03, 0, 0), RZ(Math.PI / 2), S(2 * R, 0.012, 2 * R))), '#7d8794');
      add(BOX, mul(shaft, chain(T(0.037, R * 0.6, 0), S(0.004, R * 0.5, 0.01))), '#ff5c5c');
      break;
    }
    case 'pendulum': {
      const a = angleOf(node, 'angle') + (Number(s.offset || 0) * Math.PI) / 180, L = Number(s.length || 0.25), H = L + 0.08;
      add(BOX, chain(T(0, 0.005, 0), S(0.12, 0.01, 0.08)), '#3d434c');
      add(BOX, chain(T(0, H / 2, -0.03), S(0.015, H, 0.015)), '#59616b');
      const rod = chain(T(0, H, 0), RZ(a));
      add(BOX, mul(rod, chain(T(0, -L / 2, 0), S(0.008, L, 0.008))), '#bbbbbb');
      add(CYL, mul(rod, chain(T(0, -L, 0), RX(Math.PI / 2), S(0.045, 0.02, 0.045))), color);
      break;
    }
    case 'cart': {
      const x = valueOf(node, 'position'), r = Number(s.wheel_radius || 0.04), heading = angleOf(node, 'heading');
      const body = chain(T(x, 0, 0), RY(heading));
      add(BOX, mul(body, chain(T(0, r + 0.02, 0), S(0.16, 0.04, 0.1))), color);
      for (const z of [-0.06, 0.06]) add(CYL, mul(body, chain(T(0, r, z), RX(Math.PI / 2), RY(-x / r), S(2 * r, 0.02, 2 * r))), '#222222');
      for (const z of [-0.06, 0.06]) add(BOX, mul(body, chain(T(0, r, z * 1.17), RZ(-x / r), S(0.01, 2 * r * 0.9, 0.003))), '#bbbbbb');
      break;
    }
    case 'linear': {
      const stroke = Number(s.stroke || 0.3), x = Math.max(-stroke / 2, Math.min(stroke / 2, valueOf(node, 'position') - stroke / 2));
      add(BOX, chain(T(0, 0.01, 0), S(stroke + 0.06, 0.02, 0.04)), '#59616b');
      add(BOX, chain(T(x, 0.035, 0), S(0.05, 0.03, 0.06)), color);
      break;
    }
    case 'lamp': {
      const v = valueOf(node, 'value'), th = Number(s.threshold ?? 0.5);
      const level = Math.max(0, Math.min(1, th > 0 && th < 1 ? v : v > th ? 1 : 0));
      add(BOX, chain(T(0, 0.04, 0), S(0.01, 0.08, 0.01)), '#59616b');
      const c = rgb(color), dim = c.map((x) => Math.round(40 + (x - 40) * level));
      out.push({ mesh: CYL, m: mul(base, chain(T(0, 0.095, 0), S(0.035, 0.03, 0.035))), color: dim, emissive: level > 0.05 });
      break;
    }
    case 'bar': {
      const lo = Number(s.minimum ?? 0), hi = Number(s.maximum ?? 1), v = valueOf(node, 'value');
      const hgt = Math.max(0.002, Math.min(1, (v - lo) / (hi - lo || 1)) * 0.2);
      add(BOX, chain(T(0, hgt / 2, 0), S(0.03, hgt, 0.03)), color);
      break;
    }
    default: break;
  }
  return out;
}

function frame(nodes) {
  // Aim at the bodies' centre, far enough back to see all of them.
  Object.assign(camera, { ...HOME, target: [...HOME.target] });
  if (!nodes.length) return;
  const xs = nodes.map((n) => Number(n.settings.x || 0)), zs = nodes.map((n) => Number(n.settings.z || 0));
  const span = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...zs) - Math.min(...zs));
  const big = nodes.some((n) => ['pendulum', 'cart', 'linear'].includes(n.icon));
  camera.target = [(Math.max(...xs) + Math.min(...xs)) / 2, big ? 0.16 : 0.03, (Math.max(...zs) + Math.min(...zs)) / 2];
  camera.dist = Math.max(big ? 0.8 : 0.35, span * 1.6);
}

// -- rendering -------------------------------------------------------------------------
function eye() {
  const { yaw, pitch, dist, target } = camera;
  return [target[0] + dist * Math.cos(pitch) * Math.sin(yaw), target[1] + dist * Math.sin(pitch), target[2] + dist * Math.cos(pitch) * Math.cos(yaw)];
}

export function draw() {
  if (!canvas.offsetParent) return;          // hidden tab
  const dpr = window.devicePixelRatio || 1, w = canvas.clientWidth, h = canvas.clientHeight;
  if (canvas.width !== Math.round(w * dpr)) { canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr); }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const sky = ctx.createLinearGradient(0, 0, 0, h);
  sky.addColorStop(0, '#9cc3e6'); sky.addColorStop(1, '#e8eef3');
  ctx.fillStyle = sky; ctx.fillRect(0, 0, w, h);

  const e = eye(), fwd = normalize(sub(camera.target, e)), right = normalize(cross(fwd, [0, 1, 0])), up = cross(right, fwd);
  const focal = h / (2 * Math.tan(0.45));
  const project = (p) => {
    const d = sub(p, e), z = dot(d, fwd);
    return { x: w / 2 + (dot(d, right) * focal) / z, y: h / 2 - (dot(d, up) * focal) / z, z };
  };
  const faces = [];
  // Floor: checkerboard 2 m x 2 m, 0.1 m tiles.
  for (let i = -10; i < 10; i++) for (let j = -10; j < 10; j++) {
    const quad = [[i / 10, 0, j / 10], [(i + 1) / 10, 0, j / 10], [(i + 1) / 10, 0, (j + 1) / 10], [i / 10, 0, (j + 1) / 10]];
    const shade = (i + j) & 1 ? 205 : 228;
    faces.push({ pts: quad, color: [shade, shade, shade - 6], floor: true });
  }
  const nodes = (state.graph?.nodes || []).filter((n) => n.kind === 'scene');
  const labels = [];
  const key = nodes.map((n) => `${n.id}:${n.settings.x}:${n.settings.z}`).join('|');
  if (key !== framed) { framed = key; frame(nodes); }
  for (const node of nodes) {
    for (const body of bodies(node)) {
      for (const face of body.mesh.f) {
        const pts = face.map((k) => apply(body.m, body.mesh.v[k]));
        const n = normalize(cross(sub(pts[1], pts[0]), sub(pts[2], pts[0])));
        const center = pts.reduce((a, p) => [a[0] + p[0] / pts.length, a[1] + p[1] / pts.length, a[2] + p[2] / pts.length], [0, 0, 0]);
        if (dot(n, sub(e, center)) <= 0) continue;          // back face
        const light = body.emissive ? 1.15 : 0.45 + 0.55 * Math.max(0, dot(n, LIGHT));
        faces.push({ pts, color: body.color.map((c) => Math.min(255, Math.round(c * light))), center });
      }
    }
    labels.push({ text: node.label || node.id, p: [Number(node.settings.x || 0), 0.17, Number(node.settings.z || 0)] });
  }
  const projected = faces.map((f) => {
    const p = f.pts.map(project);
    return { ...f, p, depth: f.floor ? Infinity : Math.max(...p.map((q) => q.z)) };
  }).filter((f) => f.p.every((q) => q.z > 0.01));
  projected.sort((a, b) => b.depth - a.depth);
  for (const f of projected) {
    ctx.beginPath();
    f.p.forEach((q, k) => (k ? ctx.lineTo(q.x, q.y) : ctx.moveTo(q.x, q.y)));
    ctx.closePath();
    ctx.fillStyle = `rgb(${f.color[0]},${f.color[1]},${f.color[2]})`;
    ctx.fill();
    if (!f.floor) { ctx.strokeStyle = 'rgba(0,0,0,.18)'; ctx.lineWidth = 0.6; ctx.stroke(); }
  }
  // axes
  const o = project([0, 0.0005, 0]);
  for (const [v, c, t] of [[[0.05, 0, 0], '#d33', 'x'], [[0, 0.05, 0], '#3a3', 'y'], [[0, 0, 0.05], '#33d', 'z']]) {
    const q = project(v); if (q.z <= 0 || o.z <= 0) continue;
    ctx.strokeStyle = c; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(o.x, o.y); ctx.lineTo(q.x, q.y); ctx.stroke();
    ctx.fillStyle = c; ctx.font = '11px system-ui'; ctx.fillText(t, q.x + 3, q.y);
  }
  ctx.font = '12px system-ui'; ctx.textAlign = 'center';
  for (const l of labels) {
    const q = project(l.p); if (q.z <= 0) continue;
    ctx.fillStyle = 'rgba(20,30,40,.75)'; const tw = ctx.measureText(l.text).width + 10;
    ctx.fillRect(q.x - tw / 2, q.y - 14, tw, 18); ctx.fillStyle = '#fff'; ctx.fillText(l.text, q.x, q.y);
  }
  ctx.textAlign = 'left';
  if (!nodes.length) {
    ctx.fillStyle = '#334'; ctx.font = '14px system-ui';
    ctx.fillText('Add bodies from "3-D bodies" in the library and wire angle/position signals to them.', 16, 28);
  }
}

// -- camera interaction --------------------------------------------------------------
canvas.addEventListener('mousedown', (ev) => {
  const sx = ev.clientX, sy = ev.clientY, start = { ...camera, target: [...camera.target] };
  const pan = ev.shiftKey || ev.button === 2;
  const move = (m) => {
    const dx = m.clientX - sx, dy = m.clientY - sy;
    if (pan) {
      const s = camera.dist / 600, yaw = camera.yaw;
      camera.target = [start.target[0] - (Math.cos(yaw) * dx) * s, start.target[1] + dy * s, start.target[2] + (Math.sin(yaw) * dx) * s];
    } else {
      camera.yaw = start.yaw - dx * 0.008;
      camera.pitch = Math.max(0.05, Math.min(1.5, start.pitch + dy * 0.006));
    }
  };
  const up = () => { window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up); };
  window.addEventListener('mousemove', move); window.addEventListener('mouseup', up);
});
canvas.addEventListener('contextmenu', (e) => e.preventDefault());
canvas.addEventListener('wheel', (e) => { e.preventDefault(); camera.dist = Math.max(0.15, Math.min(6, camera.dist * Math.exp(e.deltaY * 0.001))); }, { passive: false });
canvas.addEventListener('dblclick', () => { framed = ''; });
