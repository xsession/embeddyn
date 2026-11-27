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
  return `Wi-Fi ${p.source || '?'} → ${p.target || '?'} ${p.type}`;
}

// Bytes that always change and say nothing about the application: BLE CRC, 802.11 sequence and FCS.
export function noiseMask(p, length) {
  const mask = new Set();
  if (p.proto === 'BLE') for (let i = Math.max(0, length - 3); i < length; i++) mask.add(i);
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
