// embeddyn Lab: application shell (toolbar, library, run control, streaming, timeline).
import { api, data, emit, GLYPHS, KIND_COLORS, DOMAIN_COLORS, log, on, state, toast } from './store.js';
import { fit, render as renderSchematic, setShowValues, updateValues } from './schematic.js';
import { rebuild as rebuildInstruments, redrawAll, reveal } from './instruments.js';
import { render as renderInspector } from './inspector.js';
import { draw as drawScene } from './scene3d.js';
import { onFrame as analyzerFrame, runStarted as analyzerRunStarted, setVisible as setAnalyzer } from './analyzer.js';
import { fit as fitBreadboard, projectSaved, setActive as setBreadboard, updateLive as updateBreadboard, bb } from './breadboard.js';

const $ = (id) => document.getElementById(id);
let stream = null;
let frameDirty = true;
let replayTimer = null;

// -- library ------------------------------------------------------------------------
const CATEGORY_ORDER = ['Controllers', 'Electrical', 'Mechanical', 'Thermal', 'Sources', 'Instruments', '3-D bodies'];

function renderLibrary() {
  const query = $('part-search').value.trim().toLowerCase();
  const list = $('part-list');
  list.innerHTML = '';
  const groups = {};
  for (const part of state.parts) {
    if (query && !`${part.name} ${part.description} ${part.category}`.toLowerCase().includes(query)) continue;
    (groups[part.category] = groups[part.category] || []).push(part);
  }
  for (const category of [...CATEGORY_ORDER, ...Object.keys(groups).filter((c) => !CATEGORY_ORDER.includes(c))]) {
    if (!groups[category]) continue;
    const cat = document.createElement('div');
    cat.className = 'cat';
    cat.innerHTML = `<div class="cat-title">${category}</div>`;
    for (const part of groups[category]) {
      const item = document.createElement('div');
      item.className = 'part';
      item.draggable = true;
      item.title = part.description;
      const color = part.kind === 'instance' ? DOMAIN_COLORS[part.domain] : KIND_COLORS[part.kind];
      item.innerHTML = `<div class="glyph" style="background:${color}">${GLYPHS[part.icon] || '▢'}</div>
        <div class="name">${part.name}<small>${part.kind === 'instance' ? (part.template.options.backend) : part.kind}</small></div>`;
      item.addEventListener('dragstart', (e) => { e.dataTransfer.setData('text/x-embeddyn-part', part.id); e.dataTransfer.effectAllowed = 'copy'; });
      item.addEventListener('dblclick', async () => {
        const { addPart } = await import('./ops.js');
        const nodes = state.graph?.nodes || [];
        const y = nodes.length ? Math.max(...nodes.map((n) => n.y + 60 + 20 * n.ports.length / 2)) + 40 : 60;
        const x = nodes.length ? Math.min(...nodes.map((n) => n.x)) : 60;
        try { await addPart(part.id, x + 80, y + 20); emit('selection'); requestAnimationFrame(fit); } catch (error) { toast(error.message, true); }
      });
      cat.appendChild(item);
    }
    list.appendChild(cat);
  }
}

// -- documents -----------------------------------------------------------------------
function setGraph(graph) {
  stopReplay();
  state.graph = graph;
  state.selection = null;
  state.dirty = false;
  data.reset();
  $('project-name').textContent = graph.path ? `${graph.name} — ${graph.path}` : `${graph.name} (unsaved)`;
  $('project-name').title = graph.path || graph.root;
  document.title = `${graph.name} · embeddyn Lab`;
  setTimeline();
  emit('graph-changed');
  emit('graph-loaded');
  requestAnimationFrame(fit);
  log(`opened ${graph.path || graph.name}: ${graph.nodes.length} parts, ${graph.wires.length} wires`, 'ok');
}

async function openPath(path) {
  try {
    if (state.dirty && !confirm('Discard unsaved changes?')) return;
    setGraph(await api('open', { path }));
    $('open-menu').parentElement.classList.remove('open');
    localStorage.setItem('embeddyn-lab-last', path);
  } catch (error) { toast(error.message, true); log(error.message, 'err'); }
}

async function save(as = false) {
  const g = state.graph;
  let path = g.path;
  if (as || !path) {
    path = prompt('Save project as (.yaml, absolute or relative to the project folder):', path || `${g.root}/${g.name || 'project'}.yaml`);
    if (!path) return;
  }
  try {
    const result = await api('save', { graph: g, path });
    const selection = state.selection;
    state.graph = result.graph;
    state.selection = selection;
    state.dirty = false;
    $('project-name').textContent = `${result.graph.name} — ${result.path}`;
    emit('graph-changed');
    projectSaved();
    toast(`Saved ${result.path}`);
    log(`saved ${result.path}`, 'ok');
  } catch (error) { toast(error.message, true); log(error.message, 'err'); }
}

function modal(title, content) {
  $('modal-title').textContent = title;
  const bodyEl = $('modal-body');
  bodyEl.innerHTML = '';
  bodyEl.append(content);
  $('modal').classList.remove('hidden');
}

async function showYaml() {
  try {
    const { yaml } = await api('yaml', { graph: state.graph });
    const pre = document.createElement('pre');
    pre.textContent = yaml;
    modal(state.graph.path || 'project.yaml (unsaved)', pre);
  } catch (error) { toast(error.message, true); }
}

async function validate(quiet = false) {
  const result = await api('validate', { graph: state.graph });
  for (const w of result.warnings) log(w, 'warn');
  for (const e of result.errors) log(e, 'err');
  if (!quiet || !result.ok) {
    toast(result.ok ? `Project OK${result.warnings.length ? ` (${result.warnings.length} warnings)` : ''}` : `${result.errors.length} error(s): ${result.errors[0]}`, !result.ok);
  }
  if (!result.ok) showDock('console');
  return result.ok;
}

// -- running ---------------------------------------------------------------------------
function setStatus(info) {
  state.session = info;
  const pill = $('sim-status');
  pill.textContent = info?.status || 'idle';
  pill.className = 'pill ' + (info?.status || 'idle');
  const live = info && ['starting', 'running', 'paused'].includes(info.status);
  $('btn-run').disabled = !!live;
  $('btn-pause').disabled = !live || info.status === 'starting';
  $('btn-pause').textContent = info?.status === 'paused' ? '▶' : '❚❚';
  $('btn-stop').disabled = !live;
  const csv = $('btn-csv');
  csv.classList.toggle('disabled', !info?.rows);
  if (info) csv.href = `/api/csv?session=${info.session}`;
}

let loggedLines = 0;
async function run() {
  if (!state.graph) return;
  stopReplay();
  try {
    if (!(await validate(true))) return;
    const body = { graph: state.graph, mock: $('opt-mock').checked, realtime: $('opt-realtime').checked, speed: Number($('opt-speed').value) || 1 };
    const info = await api('run', body);
    data.reset(info.names || [], info.units || {});
    analyzerRunStarted();
    loggedLines = 0;
    setStatus(info);
    log(`run ${info.session} started${body.mock ? ' (mock hardware)' : ''}`);
    openStream(info.session);
  } catch (error) { toast(error.message, true); log(error.message, 'err'); }
}

function openStream(session) {
  if (stream) stream.close();
  stream = new EventSource(`/api/stream?session=${session}&from=0`);
  stream.addEventListener('status', (e) => {
    const info = JSON.parse(e.data);
    if (info.names?.length && data.names.length !== info.names.length) data.reset(info.names, info.units);
    const lines = info.log || [];
    for (let i = loggedLines; i < lines.length; i++) log(lines[i], /Error|Traceback/.test(lines[i]) ? 'err' : '');
    loggedLines = lines.length;
    setStatus(info);
    if (info.status === 'error') { toast(info.error, true); showDock('console'); }
    if (info.metrics && Object.keys(info.metrics).length) renderMetrics(info.metrics);
  });
  stream.addEventListener('events', (e) => {
    data.events.push(...JSON.parse(e.data).events);
    frameDirty = true;
  });
  stream.addEventListener('rows', (e) => {
    const payload = JSON.parse(e.data);
    if (payload.from !== data.length) return;            // out of order: ignore (reconnect replays)
    data.append(payload.rows);
    frameDirty = true;
  });
  stream.addEventListener('end', () => {
    stream.close(); stream = null;
    setTimeline();
    frameDirty = true;
    api(`session?session=${session}`).then((info) => {
      setStatus(info);
      log(`run ${info.status}: ${data.length} rows, ${info.wall_seconds} s wall time`, info.status === 'error' ? 'err' : 'ok');
      if (info.metrics && Object.keys(info.metrics).length) renderMetrics(info.metrics);
    }).catch(() => {});
  });
  stream.onerror = () => { if (stream && stream.readyState === EventSource.CLOSED) stream = null; };
}

async function control(action, extra = {}) {
  if (!state.session) return;
  try { setStatus(await api('control', { session: state.session.session, action, ...extra })); } catch (error) { toast(error.message, true); }
}

function renderMetrics(metrics) {
  const rows = Object.entries(metrics).map(([k, v]) => `<tr><td>${k}</td><td>${typeof v === 'number' ? v.toPrecision(5) : JSON.stringify(v)}</td></tr>`);
  $('metrics').innerHTML = rows.length ? `<table>${rows.join('')}</table>` : '<div class="empty">The project defines no metrics.</div>';
}

// -- timeline / replay --------------------------------------------------------------------
function setTimeline() {
  const slider = $('time-cursor');
  slider.max = Math.max(0, data.length - 1);
  slider.value = Math.max(0, data.cursor);
}

function stopReplay() { if (replayTimer) { cancelAnimationFrame(replayTimer); replayTimer = null; $('btn-replay').textContent = '⟲ replay'; } }

function replay() {
  if (replayTimer) return stopReplay();
  if (!data.length) return toast('Nothing recorded yet: run first', true);
  data.follow = false; $('opt-follow').checked = false;
  const speed = Number($('opt-speed').value) || 1;
  const t0 = performance.now(), start = data.cursor >= data.length - 1 ? 0 : data.now();
  $('btn-replay').textContent = '■ stop replay';
  const tick = () => {
    const t = start + ((performance.now() - t0) / 1000) * speed;
    data.cursor = Math.min(data.length - 1, data.indexAt(t));
    frameDirty = true;
    if (data.cursor >= data.length - 1) return stopReplay();
    replayTimer = requestAnimationFrame(tick);
  };
  replayTimer = requestAnimationFrame(tick);
}

// -- frame loop -------------------------------------------------------------------------------
function frame() {
  if (frameDirty) {
    frameDirty = false;
    $('sim-time').textContent = `t = ${data.now().toFixed(3)} s`;
    const slider = $('time-cursor');
    slider.max = Math.max(0, data.length - 1);
    if (document.activeElement !== slider) slider.value = Math.max(0, data.cursor);
    updateValues();
    if (bb.active) updateBreadboard();
    analyzerFrame();
    redrawAll();
  }
  drawScene();
  requestAnimationFrame(frame);
}

function showDock(name) {
  document.querySelectorAll('#dock-tabs button').forEach((b) => b.classList.toggle('active', b.dataset.dock === name));
  for (const page of ['instruments', 'console', 'metrics', 'analyzer']) $('dock-' + page).classList.toggle('hidden', page !== name);
  setAnalyzer(name === 'analyzer');
  if (name === 'console') $('console-badge').classList.add('hidden'), ($('console-badge').textContent = '0');
}

// -- wiring up ----------------------------------------------------------------------------------
function bind() {
  $('part-search').addEventListener('input', renderLibrary);
  $('btn-new').onclick = async () => {
    if (state.dirty && !confirm('Discard unsaved changes?')) return;
    const name = prompt('Project name:', 'my-system');
    if (!name) return;
    setGraph(await api('new', { name, root: state.graph?.root }));
  };
  $('btn-open').onclick = () => $('open-menu').parentElement.classList.toggle('open');
  document.addEventListener('mousedown', (e) => { if (!e.target.closest('.dropdown')) $('open-menu').parentElement.classList.remove('open'); });
  $('btn-open-path').onclick = () => openPath($('open-path').value.trim());
  $('open-path').addEventListener('keydown', (e) => { if (e.key === 'Enter') openPath($('open-path').value.trim()); });
  $('btn-save').onclick = () => save(false);
  $('btn-saveas').onclick = () => save(true);
  $('btn-yaml').onclick = showYaml;
  $('btn-validate').onclick = () => validate();
  $('btn-run').onclick = run;
  $('btn-pause').onclick = () => control(state.session?.status === 'paused' ? 'resume' : 'pause');
  $('btn-stop').onclick = () => control('stop');
  $('opt-speed').onchange = () => control('speed', { value: Number($('opt-speed').value) || 1, realtime: $('opt-realtime').checked });
  $('opt-realtime').onchange = () => control('speed', { value: Number($('opt-speed').value) || 1, realtime: $('opt-realtime').checked });
  $('opt-values').onchange = (e) => setShowValues(e.target.checked);
  $('opt-follow').onchange = (e) => { data.follow = e.target.checked; if (data.follow) data.cursor = data.length - 1; frameDirty = true; };
  $('time-cursor').addEventListener('input', (e) => {
    stopReplay();
    data.follow = false; $('opt-follow').checked = false;
    data.cursor = Number(e.target.value); frameDirty = true;
  });
  $('btn-replay').onclick = replay;
  $('btn-fit').onclick = () => (bb.active ? fitBreadboard() : fit());
  $('modal-close').onclick = () => $('modal').classList.add('hidden');
  $('modal').addEventListener('mousedown', (e) => { if (e.target.id === 'modal') $('modal').classList.add('hidden'); });
  document.querySelectorAll('#view-tabs button[data-view]').forEach((b) => (b.onclick = () => {
    document.querySelectorAll('#view-tabs button[data-view]').forEach((x) => x.classList.toggle('active', x === b));
    $('views').className = 'view-' + b.dataset.view;
    setBreadboard(b.dataset.view === 'breadboard');
    if (!['scene', 'breadboard'].includes(b.dataset.view)) requestAnimationFrame(fit);
  }));
  document.querySelectorAll('#dock-tabs button').forEach((b) => (b.onclick = () => showDock(b.dataset.dock)));
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); save(false); }
    if (e.key === 'F5') { e.preventDefault(); if (!$('btn-run').disabled) run(); }
    if (e.key === 'Escape') $('modal').classList.add('hidden');
  });
  window.addEventListener('hashchange', () => {
    const path = new URLSearchParams(location.hash.slice(1)).get('open');
    if (path && path !== state.graph?.path) openPath(path);
  });
  window.addEventListener('beforeunload', (e) => { if (state.dirty) { e.preventDefault(); e.returnValue = ''; } });
  // Dock resize.
  const dock = $('dock'), layout = $('layout');
  const grip = document.createElement('div'); grip.className = 'resize'; dock.appendChild(grip);
  grip.addEventListener('mousedown', (e) => {
    const y0 = e.clientY, h0 = dock.offsetHeight;
    const move = (m) => { const hgt = Math.max(120, Math.min(window.innerHeight - 200, h0 - (m.clientY - y0))); dock.style.height = hgt + 'px'; layout.style.bottom = hgt + 'px'; $('toast').style.bottom = hgt + 16 + 'px'; frameDirty = true; };
    const up = () => { window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up); };
    window.addEventListener('mousemove', move); window.addEventListener('mouseup', up);
  });
  on('graph-changed', () => { frameDirty = true; renderInspector(); });
  on('graph-changed-soft', () => { renderSchematic(); renderInspector(); });
  on('selection', () => { renderInspector(); });
  on('node-open', (id) => { showDock('instruments'); reveal(id); });
  on('control', () => { frameDirty = true; });
  on('error', (error) => { toast(error.message || String(error), true); log(error.message || String(error), 'err'); });
  window.addEventListener('resize', () => { frameDirty = true; });
}

async function init() {
  bind();
  const info = await api('state');
  state.parts = info.parts;
  state.partsById = Object.fromEntries(info.parts.map((p) => [p.id, p]));
  renderLibrary();
  const list = $('example-list');
  for (const ex of info.examples) {
    const item = document.createElement('div');
    item.className = 'menu-item';
    item.innerHTML = `<b>${ex.file}</b><small>${ex.description || ex.name}</small>`;
    item.onclick = () => openPath(ex.path);
    list.appendChild(item);
  }
  if (!info.renode) log('RENODE_BIN is not set: Renode parts need it (or tick "mock hardware").', 'warn');
  const hash = new URLSearchParams(location.hash.slice(1));
  const startup = hash.get('open') || info.startup || localStorage.getItem('embeddyn-lab-last');
  if (startup) {
    try { setGraph(await api('open', { path: startup })); return; } catch (error) { log(`could not open ${startup}: ${error.message}`, 'warn'); }
  }
  setGraph(await api('new', { name: 'untitled' }));
}

init().catch((error) => { toast(error.message, true); log(error.stack || error.message, 'err'); });
requestAnimationFrame(frame);
rebuildInstruments();
