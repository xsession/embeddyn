// "Changes" view of the analyzer: one row per message, changed bytes highlighted (cansniffer style).
//
// Messages are grouped by key (CAN id; BLE sender + PDU type; Wi-Fi sender, receiver and frame
// type). For every key the view keeps, up to the run's time cursor: the latest bytes, how often
// each byte and each bit changed and when a byte last changed. Bytes that changed in the latest
// packet glow, earlier changes fade over the fade window, bytes that never changed are dim, so
// counters, set-points and flags stand out from constants. Selecting a row shows its bits as a
// flip-count heat map; selecting bytes (shift-click for a range) shows them read as integers of
// every width and byte order, and plots the chosen reading over the run.

export function messageKey(p) {
  if (p.proto === 'CANopen' || p.proto === 'CAN') return `CAN 0x${Number(p.id).toString(16).toUpperCase().padStart(3, '0')}`;
  if (p.proto === 'BLE') return `BLE ${p.source || '?'} ${p.type}`;
  if (['NFC', 'RFID', 'LoRa', 'I2C', 'SPI', 'UART', 'Modbus'].includes(p.proto)) return `${p.proto} ${p.source || p.target || '?'} ${p.type}`;
  if (p.proto === 'Ethernet') return `Ethernet ${p.source || '?'} → ${p.target || '?'} ${p.type}`;
  return `Wi-Fi ${p.source || '?'} → ${p.target || '?'} ${p.type}`;
}

// Bytes that always change and say nothing about the application: BLE CRC, 802.11 sequence and FCS,
// Modbus CRC, Ethernet FCS and the IPv4 identification and checksums.
export function noiseMask(p, length) {
  const mask = new Set();
  if (p.proto === 'BLE') for (let i = Math.max(0, length - 3); i < length; i++) mask.add(i);
  if (p.proto === 'Modbus') for (let i = Math.max(0, length - 2); i < length; i++) mask.add(i);
  if (p.proto === 'Ethernet') {
    for (let i = Math.max(0, length - 4); i < length; i++) mask.add(i);
    if (length >= 34) for (const i of [18, 19, 24, 25]) mask.add(i);                // IPv4 identification, header checksum
    if (p.type === 'UDP' && length >= 42) { mask.add(40); mask.add(41); }
    if ((p.type === 'TCP' || p.type === 'Modbus/TCP') && length >= 54) for (const i of [38, 39, 40, 41, 42, 43, 44, 45, 50, 51]) mask.add(i);   // seq, ack, checksum
  }
  if (p.proto === '802.11') {
    for (let i = Math.max(0, length - 4); i < length; i++) mask.add(i);
    if (length >= 24) { mask.add(22); mask.add(23); }
    if (p.type === 'Beacon' || p.type === 'Probe Response') for (let i = 24; i < 32; i++) mask.add(i);   // TSF timestamp
  }
  return mask;
}

const bytesOf = (p) => (p.hex.match(/../g) || []).map((h) => parseInt(h, 16));

// Per-key statistics over the packets with time <= until.
export function buildStats(packets, until = Infinity, maskNoise = true) {
  const stats = new Map();
  for (const p of packets) {
    if (p.time > until + 1e-12) break;
    const key = messageKey(p);
    const bytes = bytesOf(p);
    let s = stats.get(key);
    if (!s) {
      s = { key, proto: p.proto, type: p.type, count: 0, first: p.time, last: p.time, bytes: [], prev: null,
        changes: [], flips: [], lastChange: [], changedNow: [], lengths: new Set(), mask: new Set(), latest: p, history: [],
        before: null };
      stats.set(key, s);
    }
    s.count += 1;
    s.lengths.add(bytes.length);
    if (maskNoise) s.mask = noiseMask(p, bytes.length);
    s.changedNow = bytes.map(() => false);
    if (s.prev) {
      for (let i = 0; i < bytes.length; i++) {
        const before = s.prev[i];
        if (before === undefined || before === bytes[i]) continue;
        s.changes[i] = (s.changes[i] || 0) + 1;
        s.lastChange[i] = p.time;
        s.changedNow[i] = true;
        const flips = (s.flips[i] = s.flips[i] || [0, 0, 0, 0, 0, 0, 0, 0]);
        const diff = before ^ bytes[i];
        for (let b = 0; b < 8; b++) if (diff >> b & 1) flips[b] += 1;
      }
    }
    s.before = s.prev;
    s.prev = bytes;
    s.bytes = bytes;
    s.last = p.time;
    s.type = p.type;
    s.latest = p;
    s.history.push(p);
  }
  for (const s of stats.values()) {
    s.period = s.count > 1 ? (s.last - s.first) / (s.count - 1) : null;
    s.dynamic = s.changes.some((c, i) => c && !s.mask.has(i));
  }
  return stats;
}

// Every integer reading of bytes [start, end) (end - start <= 4).
export function readings(bytes, start, end) {
  const slice = bytes.slice(start, end);
  const n = slice.length;
  const out = [];
  if (!n || slice.some((b) => b === undefined)) return out;
  const le = slice.reduce((acc, b, i) => acc + b * 2 ** (8 * i), 0);
  const be = slice.reduce((acc, b) => acc * 256 + b, 0);
  const signed = (v) => (v >= 2 ** (8 * n - 1) ? v - 2 ** (8 * n) : v);
  const bits = 8 * n;
  if (n === 1) out.push(['u8', le], ['i8', signed(le)]);
  else {
    out.push([`u${bits} LE`, le], [`i${bits} LE`, signed(le)]);
    out.push([`u${bits} BE`, be], [`i${bits} BE`, signed(be)]);
  }
  return out;
}

export function readingOf(bytes, start, end, kind) {
  const found = readings(bytes, start, end).find(([name]) => name === kind);
  return found ? found[1] : null;
}

// Byte ranges whose integer reading follows a signal: Pearson r over the packets, and the linear
// fit signal = scale * reading + offset. `signalAt(t)` gives the signal at a packet's time.
export function correlate(packets, signalAt, maskNoise = true, limit = 12) {
  const stats = buildStats(packets, Infinity, maskNoise);
  const results = [];
  for (const s of stats.values()) {
    if (s.history.length < 5) continue;
    const ys = s.history.map((p) => signalAt(p.time));
    if (ys.some((y) => y === null || !Number.isFinite(y))) continue;
    const rows = s.history.map((p) => (p.hex.match(/../g) || []).map((x) => parseInt(x, 16)));
    const width = Math.min(...rows.map((r) => r.length));
    for (let start = 0; start < width; start++) {
      if (s.mask.has(start)) continue;
      for (const size of [1, 2, 4]) {
        const span = [...Array(size).keys()].map((k) => start + k);
        if (start + size > width || span.some((i) => s.mask.has(i)) || !span.some((i) => s.changes[i])) continue;
        for (const [kind] of readings(rows[0], start, start + size)) {
          const xs = rows.map((r) => readingOf(r, start, start + size, kind));
          const fit = linearFit(xs, ys);
          if (fit) results.push({ key: s.key, start, end: start + size, kind, ...fit });
        }
      }
    }
  }
  results.sort((a, b) => Math.abs(b.r) - Math.abs(a.r) || (a.end - a.start) - (b.end - b.start));
  const seen = new Set(), best = [];
  for (const item of results) {                    // one result per key and start byte
    const id = `${item.key}|${item.start}`;
    if (seen.has(id)) continue;
    seen.add(id);
    best.push(item);
    if (best.length >= limit) break;
  }
  return best;
}

export function linearFit(xs, ys) {
  const n = xs.length;
  if (n < 3) return null;
  const mx = xs.reduce((a, b) => a + b, 0) / n, my = ys.reduce((a, b) => a + b, 0) / n;
  let sxx = 0, syy = 0, sxy = 0;
  for (let i = 0; i < n; i++) { const dx = xs[i] - mx, dy = ys[i] - my; sxx += dx * dx; syy += dy * dy; sxy += dx * dy; }
  if (sxx === 0 || syy === 0) return null;
  const scale = sxy / sxx;
  return { r: sxy / Math.sqrt(sxx * syy), scale, offset: my - scale * mx, n };
}
