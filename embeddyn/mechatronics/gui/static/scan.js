// embeddyn Lab: Scan view. A printed schematic (PDF, PNG, JPEG ...) or photos of a board read into a netlist: the
// overlay of what was read, the parts and nets found (click one to point at it on the page), markings typed in by
// hand, an SMD marking decoder, and import as an emulated board.
import { api, emit, log, state, toast } from './store.js';

const $ = (id) => document.getElementById(id);
const SVG = 'http://www.w3.org/2000/svg';
const sc = { result: null, page: 1, uploads: [], zoom: 1, x: 0, y: 0, selected: null, list: 'parts', bound: false,
  mode: 'drawing' };

function esc(text) {
  return String(text ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function readFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve({ name: file.name, data: String(reader.result).split(',', 2)[1] || '' });
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

async function addFiles(files) {
  const accepted = [...files].filter((f) => /\.(pdf|png|jpe?g|tiff?|bmp|gif|webp)$/i.test(f.name));
  if (!accepted.length) { toast('Drop a PDF or an image', true); return; }
  const tooBig = accepted.find((f) => f.size > 24 * 1024 * 1024);
  if (tooBig) { toast(`${tooBig.name} is larger than 24 MB: give its path instead`, true); return; }
  sc.uploads = await Promise.all(accepted.map(readFile));
  $('sc-files').textContent = sc.uploads.map((u) => u.name).join(', ');
  $('sc-files').title = 'These files are sent to the Lab server and scanned';
  if (sc.mode === 'board') {
    // first file: top, second: bottom, the rest: close-ups (edit the fields to change)
    const names = sc.uploads.map((u) => u.name);
    $('sc-top').value = names[0] || '';
    $('sc-bottom').value = names[1] || '';
    $('sc-closeups').value = names.slice(2).join(', ');
  } else {
    $('sc-path').value = '';
  }
}

function setMode(mode) {
  sc.mode = mode;
  document.querySelectorAll('#sc-toolbar .sc-drawing').forEach((el) => el.classList.toggle('hidden', mode !== 'drawing'));
  document.querySelectorAll('#sc-toolbar .sc-board').forEach((el) => el.classList.toggle('hidden', mode !== 'board'));
  $('sc-input').accept = mode === 'board' ? 'image/*' : '.pdf,image/*';
  $('sc-choose').title = mode === 'board'
    ? 'Photos of the board: the top side, the bottom side, then close-ups (in that order)'
    : 'A PDF (vector or scanned) or pictures of the schematic; several files are the pages of one design';
}

async function runBoard() {
  const top = $('sc-top').value.trim();
  if (!top) { toast('Choose the board photos, or type the path of the top photo', true); return; }
  const names = new Set(sc.uploads.map((u) => u.name));
  const closeups = $('sc-closeups').value.split(/[,;\n]/).map((p) => p.trim()).filter(Boolean);
  const wanted = [top, $('sc-bottom').value.trim(), ...closeups].filter((n) => names.has(n));
  const body = {
    root: state.graph?.root, top, bottom: $('sc-bottom').value.trim() || null, closeups,
    board_mm: $('sc-boardmm').value.trim() || null, out: $('sc-out').value.trim() || null,
    uploads: sc.uploads.filter((u) => wanted.includes(u.name)),
  };
  const button = $('sc-run');
  button.disabled = true;
  status('reading the board… (a few minutes: every photo is straightened, segmented and read)');
  const t0 = performance.now();
  try {
    sc.result = await api('scan/board', body);
    sc.page = 1;
    sc.selected = null;
    $('sc-out').placeholder = sc.result.out;
    const r = sc.result;
    const marked = r.parts.filter((p) => p.marking).length;
    status(`${r.parts.length} parts (${marked} with a marking), ${r.nets.length} nets, ${r.board_mm.join(' x ')} mm in ${((performance.now() - t0) / 1000).toFixed(0)} s`);
    log(`board photos: ${r.parts.length} parts, ${r.nets.length} nets → ${r.netlist}`, 'ok');
    for (const c of r.closeups || []) log(`close-up ${c.photo.split('/').pop()}: ${c.placed ? `placed, ${c.markings} markings` : (c.note || 'not placed')}`, c.placed ? '' : 'warn');
    if (!$('sc-title').value) $('sc-title').value = r.out.split('/').pop();
    render();
    requestAnimationFrame(fitPage);
  } catch (error) {
    status('');
    toast(error.message, true); log(error.message, 'err');
  } finally { button.disabled = false; }
}

async function runScan() {
  if (sc.mode === 'board') return runBoard();
  const paths = $('sc-path').value.split(/[,;\n]/).map((p) => p.trim()).filter(Boolean);
  if (!paths.length && !sc.uploads.length) { toast('Choose or drop a PDF/image, or type its path', true); return; }
  const body = {
    root: state.graph?.root, paths, uploads: paths.length ? [] : sc.uploads,
    pages: $('sc-pages').value.trim(), dpi: Number($('sc-dpi').value) || null,
    reference: $('sc-ref').value.trim() || null, out: $('sc-out').value.trim() || null,
  };
  const button = $('sc-run');
  button.disabled = true;
  status('scanning… (pictures take a while: OCR)');
  const t0 = performance.now();
  try {
    sc.result = await api('scan/run', body);
    sc.page = sc.result.pages[0]?.index || 1;
    sc.selected = null;
    $('sc-out').placeholder = sc.result.out;
    const r = sc.result;
    status(`${r.parts.length} parts, ${r.nets.length} nets from ${r.pages.length} page(s) in ${((performance.now() - t0) / 1000).toFixed(1)} s`);
    log(`schematic scan: ${r.parts.length} parts, ${r.nets.length} nets → ${r.netlist}`, 'ok');
    if (!$('sc-title').value) $('sc-title').value = r.out.split('/').pop();
    render();
    requestAnimationFrame(fitPage);
  } catch (error) {
    status('');
    toast(error.message, true); log(error.message, 'err');
  } finally { button.disabled = false; }
}

function status(text) { $('sc-status').textContent = text; }

// -- page with what was read ------------------------------------------------------------------------------
function currentPage() { return sc.result?.pages.find((p) => p.index === sc.page); }

function renderPage() {
  const page = currentPage();
  const stage = $('sc-stage');
  const tabs = $('sc-pagetabs');
  tabs.innerHTML = '';
  for (const p of sc.result?.pages || []) {
    const b = document.createElement('button');
    b.textContent = p.name || `p${p.index}`;
    b.title = `${p.source.split('/').pop()} · ${p.parts} parts${p.unnamed ? `, ${p.unnamed} symbols without a reference` : ''}${p.raster ? ' · read from pixels' : ' · vector'}`;
    b.classList.toggle('active', p.index === sc.page);
    b.onclick = () => { sc.page = p.index; renderPage(); fitPage(); };
    tabs.appendChild(b);
  }
  stage.innerHTML = '';
  if (!page) {
    $('sc-empty').classList.remove('hidden');
    return;
  }
  $('sc-empty').classList.add('hidden');
  stage.style.width = page.width + 'px';
  stage.style.height = page.height + 'px';
  if (page.overlay) {
    const img = document.createElement('img');
    img.src = '/api/scan/image?path=' + encodeURIComponent(page.overlay) + '&v=' + (sc.stamp || 0);
    img.draggable = false;
    stage.appendChild(img);
  }
  const svg = document.createElementNS(SVG, 'svg');
  svg.setAttribute('viewBox', `0 0 ${page.width} ${page.height}`);
  svg.id = 'sc-marks';
  stage.appendChild(svg);
  drawMarks();
  applyView();
}

function drawMarks() {
  const svg = $('sc-marks');
  if (!svg || !sc.result) return;
  svg.innerHTML = '';
  const sel = sc.selected;
  if (!sel) return;
  const r = sc.result;
  const mark = (tag, attrs) => {
    const el = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    svg.appendChild(el);
    return el;
  };
  if (sel.kind === 'part') {
    const box = r.boxes[sel.ref];
    if (box && box[0] === sc.page) {
      const pad = 3;
      mark('rect', { x: box[1] - pad, y: box[2] - pad, width: box[3] - box[1] + 2 * pad, height: box[4] - box[2] + 2 * pad, class: 'sc-hit' });
    }
    for (const [key, pos] of Object.entries(r.positions)) {
      if (key.startsWith(sel.ref + '.') && pos[0] === sc.page) mark('circle', { cx: pos[1], cy: pos[2], r: 2.2, class: 'sc-pin' });
    }
  } else {
    const net = r.nets[sel.index];
    for (const n of net?.nodes || []) {
      const pos = r.positions[`${n.ref}.${n.pin}`];
      if (!pos || pos[0] !== sc.page) continue;
      mark('circle', { cx: pos[1], cy: pos[2], r: 4, class: 'sc-pin' + (n.guessed ? ' guessed' : '') });
      const t = mark('text', { x: pos[1] + 5, y: pos[2] - 4, class: 'sc-label' });
      t.textContent = `${n.ref}.${n.pin}`;
    }
  }
}

function applyView() {
  $('sc-stage').style.transform = `translate(${sc.x}px, ${sc.y}px) scale(${sc.zoom})`;
}

function fitPage() {
  const page = currentPage();
  const wrap = $('sc-viewport');
  if (!page || !wrap.clientWidth) return;
  sc.zoom = Math.min(wrap.clientWidth / page.width, wrap.clientHeight / page.height) * 0.97;
  sc.x = (wrap.clientWidth - page.width * sc.zoom) / 2;
  sc.y = (wrap.clientHeight - page.height * sc.zoom) / 2;
  applyView();
}

function centreOn(pageIndex, x, y) {
  if (pageIndex !== sc.page) { sc.page = pageIndex; renderPage(); fitPage(); }
  const wrap = $('sc-viewport');
  sc.zoom = Math.max(sc.zoom, 2);
  sc.x = wrap.clientWidth / 2 - x * sc.zoom;
  sc.y = wrap.clientHeight / 2 - y * sc.zoom;
  applyView();
}

// -- lists --------------------------------------------------------------------------------------------------
function renderLists() {
  const r = sc.result;
  const summary = $('sc-summary');
  if (!r) { summary.innerHTML = '<div class="small">Nothing scanned yet.</div>'; $('sc-list').innerHTML = ''; return; }
  const guessed = r.guessed.length;
  summary.innerHTML = `
    <div><b>${r.parts.length}</b> parts · <b>${r.nets.length}</b> nets · <b>${r.pages.length}</b> page(s)</div>
    ${guessed ? `<div class="small" title="Pins the drawing gives no number for: numbered 1, 2 left to right / top to bottom. Check diodes and polarised capacitors.">${guessed} pin numbers guessed (red)</div>` : ''}
    ${r.mode === 'board' ? `<div class="small">board ${r.board_mm.join(' x ')} mm at ${r.px_per_mm} px/mm · ${r.parts.filter((p) => p.marking).length} markings read: click a part to type one</div>` : ''}
    ${r.pages.some((p) => p.raster) ? '<div class="small warn">Read from pixels: check the overlay before trusting the netlist.</div>' : ''}
    ${r.score ? `<div class="sc-score">${esc(r.score.summary)}</div>` : ''}
    ${r.notes.length ? `<details><summary class="small">${r.notes.length} note(s)</summary>${r.notes.map((n) => `<div class="small">${esc(n)}</div>`).join('')}</details>` : ''}
    <div class="small">netlist: <code>${esc(r.netlist)}</code></div>`;
  const query = $('sc-filter').value.trim().toLowerCase();
  const list = $('sc-list');
  list.innerHTML = '';
  document.querySelectorAll('#sc-listtabs button').forEach((b) => b.classList.toggle('active', b.dataset.list === sc.list));
  if (sc.list === 'parts') {
    for (const part of r.parts) {
      if (query && !`${part.ref} ${part.value} ${part.package || ''} ${part.marking || ''}`.toLowerCase().includes(query)) continue;
      const row = document.createElement('div');
      row.className = 'sc-row' + (sc.selected?.kind === 'part' && sc.selected.ref === part.ref ? ' sel' : '');
      const where = r.mode === 'board' ? esc(part.package || part.kind || '') : `p${part.page ?? '?'}`;
      row.innerHTML = `<b>${esc(part.ref)}</b><span>${esc(part.value) || '<i class="small">no value</i>'}</span><span class="small">${part.pins} pins · ${where}</span>`;
      row.onclick = () => {
        sc.selected = { kind: 'part', ref: part.ref };
        const box = r.boxes[part.ref];
        if (box) centreOn(box[0], (box[1] + box[3]) / 2, (box[2] + box[4]) / 2);
        drawMarks(); renderLists(); renderPart();
      };
      list.appendChild(row);
    }
  } else {
    r.nets.forEach((net, index) => {
      const text = net.nodes.map((n) => `${n.ref}.${n.pin}${n.name ? ` (${n.name})` : ''}`).join(' ');
      if (query && !`${net.name} ${text}`.toLowerCase().includes(query)) return;
      const row = document.createElement('div');
      row.className = 'sc-row net' + (sc.selected?.kind === 'net' && sc.selected.index === index ? ' sel' : '');
      row.innerHTML = `<b>${esc(net.name)}</b><span class="small">${net.nodes.map((n) =>
        `<span class="${n.guessed ? 'guessed' : ''}" title="${esc(n.name)}">${esc(n.ref)}.${esc(n.pin)}</span>`).join(' ')}</span>`;
      row.onclick = () => {
        sc.selected = { kind: 'net', index };
        renderPart();
        const pages = net.nodes.map((n) => r.positions[`${n.ref}.${n.pin}`]).filter(Boolean);
        if (pages.length && !pages.some((p) => p[0] === sc.page)) { sc.page = pages[0][0]; renderPage(); fitPage(); }
        drawMarks(); renderLists();
      };
      list.appendChild(row);
    });
  }
}

// -- a board part: what was read, and the marking typed in by hand -----------------------------------------
function readingLine(r) {
  const flag = r.confidence === 'check' ? ' <span class="check">check</span>' : '';
  return `<div class="sc-reading"><b>${esc(r.part)}</b>${r.package ? ` · ${esc(r.package)}` : ''}${r.detail ? ` — ${esc(r.detail)}` : ''}${r.pins ? ` · pins ${esc(r.pins)}` : ''}${flag} <span class="small">${Math.round((r.score || 0) * 100)}%</span></div>`;
}

function renderPart() {
  const panel = $('sc-part');
  const r = sc.result;
  const part = r && r.mode === 'board' && sc.selected?.kind === 'part' ? r.parts.find((p) => p.ref === sc.selected.ref) : null;
  if (!part) { panel.classList.add('hidden'); return; }
  panel.classList.remove('hidden');
  panel.innerHTML = `
    <div class="sc-part-head"><b>${esc(part.ref)}</b><span>${esc(part.value || '')}</span><span class="small">${esc(part.kind || '')}</span></div>
    <div class="small">${esc(part.package || '?')} (body ${part.size_mm.join(' x ')} mm, ${part.pins} pads)${part.candidates?.length > 1 ? ` · or ${esc(part.candidates.slice(1).join(', '))}` : ''}</div>
    ${part.marking ? `<div class="small">marking: <code>${esc(part.marking.replace(/\n/g, ' / '))}</code></div>` : '<div class="small">no marking read</div>'}
    ${(part.readings || []).map(readingLine).join('')}
    <div class="sc-mark-row"><input id="sc-mark" placeholder="type the marking (lines with /)" value="${esc((part.marking || '').replace(/\n/g, ' / '))}">
      <select id="sc-pin1" title="How the marking sits in the picture (sets pin 1: counted counter-clockwise from the lower left of the text)">
        <option value="auto">text: as read</option><option value="upright">text upright</option><option value="upside_down">upside down</option>
        <option value="reads_up">reads upward</option><option value="reads_down">reads downward</option></select>
      <button id="sc-mark-go" class="primary">Apply</button></div>
    ${(part.notes || []).map((n) => `<div class="small">${esc(n)}</div>`).join('')}`;
  $('sc-mark-go').onclick = () => applyMarking(part.ref);
  $('sc-mark').addEventListener('keydown', (e) => { if (e.key === 'Enter') applyMarking(part.ref); });
}

async function applyMarking(ref) {
  const text = $('sc-mark').value.split('/').map((t) => t.trim()).join('\n');
  try {
    const page = sc.page;
    sc.result = await api('scan/marking', { out: sc.result.out, ref, text, pin1: $('sc-pin1').value });
    sc.page = page;
    log(`${ref}: marking "${text.replace(/\n/g, ' / ')}" → ${sc.result.parts.find((p) => p.ref === ref)?.value || 'no reading'}`, 'ok');
    const keep = { zoom: sc.zoom, x: sc.x, y: sc.y };
    render();
    Object.assign(sc, keep); applyView();
  } catch (error) { toast(error.message, true); }
}

async function decodeSmd() {
  try {
    const answer = await api('smd/decode', { text: $('smd-text').value, package: $('smd-pkg').value.trim() || null,
      size: $('smd-size').value.trim() || null });
    const out = [];
    if (answer.decoded) {
      out.push(...answer.decoded.readings.slice(0, 6).map(readingLine));
      out.push(...answer.decoded.notes.map((n) => `<div class="small">${esc(n)}</div>`));
      if (!answer.decoded.readings.length && !answer.decoded.notes.length) out.push('<div class="small">nothing known</div>');
    }
    if (answer.packages) {
      out.push('<div class="small">packages for that body:</div>');
      out.push(...answer.packages.map((m) => `<div class="sc-reading"><span class="score">${Math.round(m.score * 100)}%</span> <b>${esc(m.package)}</b> · ${m.leads} leads · ${esc(m.kinds.join(', '))}</div>`));
    }
    $('smd-out').innerHTML = out.join('');
  } catch (error) { toast(error.message, true); }
}

function render() {
  sc.stamp = Date.now();                     // overlays are rewritten after a marking edit
  renderPage();
  renderLists();
  renderPart();
  $('sc-import').disabled = !sc.result;
}

// -- import as a board ---------------------------------------------------------------------------------------
async function importBoard() {
  if (!sc.result) return;
  const button = $('sc-import');
  button.disabled = true;
  try {
    const result = await api('scan/import', {
      out: sc.result.out, title: $('sc-title').value.trim(), substitute: $('sc-subst').value,
      firmware: $('sc-fw').value.trim(),
    });
    const lines = [result.mcu ? `MCU ${result.mcu} (${result.platform})` : 'no supported MCU found',
      ...result.devices, ...result.findings];
    $('sc-import-result').innerHTML = lines.map((l) => `<div class="small">${esc(l)}</div>`).join('');
    log(`imported board from the scan → ${result.project}`, 'ok');
    for (const finding of result.findings) log(finding, finding.startsWith('error') ? 'err' : finding.startsWith('warning') ? 'warn' : '');
    if (!result.mcu) { toast('No supported microcontroller in the scanned netlist: give it with REF=PART', true); return; }
    emit('open-project', result.project);
  } catch (error) {
    toast(error.message, true); log(error.message, 'err');
  } finally { button.disabled = !sc.result; }
}

// -- wiring -------------------------------------------------------------------------------------------------
function bind() {
  if (sc.bound) return;
  sc.bound = true;
  $('sc-choose').onclick = () => $('sc-input').click();
  $('sc-mode').onchange = (e) => setMode(e.target.value);
  $('smd-go').onclick = decodeSmd;
  ['smd-text', 'smd-pkg', 'smd-size'].forEach((id) => $(id).addEventListener('keydown', (e) => { if (e.key === 'Enter') decodeSmd(); }));
  $('sc-input').onchange = (e) => addFiles(e.target.files);
  $('sc-run').onclick = runScan;
  $('sc-path').addEventListener('keydown', (e) => { if (e.key === 'Enter') runScan(); });
  $('sc-path').addEventListener('input', () => { if ($('sc-path').value.trim()) { sc.uploads = []; $('sc-files').textContent = ''; } });
  $('sc-filter').oninput = renderLists;
  $('sc-import').onclick = importBoard;
  document.querySelectorAll('#sc-listtabs button').forEach((b) => (b.onclick = () => { sc.list = b.dataset.list; renderLists(); }));
  const wrap = $('scan-wrap');
  wrap.addEventListener('dragover', (e) => { e.preventDefault(); wrap.classList.add('drop'); });
  wrap.addEventListener('dragleave', (e) => { if (e.target === wrap || !wrap.contains(e.relatedTarget)) wrap.classList.remove('drop'); });
  wrap.addEventListener('drop', (e) => {
    e.preventDefault(); wrap.classList.remove('drop');
    if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files).then(() => sc.uploads.length && sc.mode === 'drawing' && runScan());
  });
  const vp = $('sc-viewport');
  vp.addEventListener('wheel', (e) => {
    e.preventDefault();
    const rect = vp.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    const factor = Math.exp(-e.deltaY * 0.0015);
    const zoom = Math.min(40, Math.max(0.05, sc.zoom * factor));
    sc.x = mx - (mx - sc.x) * (zoom / sc.zoom);
    sc.y = my - (my - sc.y) * (zoom / sc.zoom);
    sc.zoom = zoom;
    applyView();
  }, { passive: false });
  vp.addEventListener('mousedown', (e) => {
    const x0 = e.clientX - sc.x, y0 = e.clientY - sc.y;
    vp.classList.add('panning');
    const move = (m) => { sc.x = m.clientX - x0; sc.y = m.clientY - y0; applyView(); };
    const up = () => { vp.classList.remove('panning'); window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up); };
    window.addEventListener('mousemove', move); window.addEventListener('mouseup', up);
  });
  vp.addEventListener('dblclick', fitPage);
}

export function setActive(active) {
  document.getElementById('layout').classList.toggle('scanning', active);   // the part library and inspector rest
  if (!active) return;
  bind();
  render();
  requestAnimationFrame(fitPage);
}

export const fit = fitPage;
export { sc };
