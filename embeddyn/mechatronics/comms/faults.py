"""Wired bus faults: what really goes wrong on UART, RS-485, SPI, I2C and Ethernet.

A medium's ``faults:`` section (``media:`` in the project, or the Lab's Medium panel)
switches them on; ``fault_bursts:`` turns some on only for a while (``start``/``end``,
``period``/``duty``, like the medium's bursts). The medium's own bit error rate, loss,
crosstalk and ``cable:`` checks still apply on top.

```yaml
media:
  uart:
    faults:
      baud_error_pct: 4.5        # receiver clock off: the sample point drifts, framing errors, garbage
      rx_baud: 9600              # or a wrong baud rate altogether
      rx_format: 8E1             # receiver expects parity the sender does not send (parity errors)
      glitches_per_s: 200        # noise spikes on the line (glitch_us wide): noise flags, false start bits
      breaks: [{start: 0.4, end: 0.45}]          # line held low: break condition
      overrun: {busy_us: 800, period_ms: 10, fifo: 1}   # ISR blocked (flash write...): bytes lost
      char_gap_us: 0             # sender pauses between characters
      disconnected: [{start: 0.7, end: 0.8}]
  rs485:
    cable: {length_m: 800, termination: false, unit_loads: 8}  # physical.rs485_check
    faults:
      bias: false                # no fail-safe bias: the idle bus floats, receivers decode noise
      de_late_us: 30             # driver enabled after the UART started: first bits never on the bus
      de_hold_us: 3000           # driver released late: the slave's answer collides with it
      char_gap_us: 1200          # gaps > 1.5 characters split a Modbus RTU frame
      ground_offset_v: 9         # outside −7…+12 V common mode: nothing gets through
  spi:
    cable: {length_m: 0.5}       # physical.spi_check: MISO timing, SCK ringing
    faults:
      slave_mode: 1              # slave samples on the other clock edge: every bit one clock off
      double_clock: 0.05         # SCK ringing: an extra clock edge (bit slip) in 5 % of the transfers
      cs_glitch: 0.02            # chip select drops mid-transfer: the slave resets, MISO floats
      slave_off: [{start: 0.5, end: 0.6}]        # slave unpowered: MISO reads 0xFF
  i2c:
    faults:
      stuck: [{start: 0.3, end: 0.35}]           # a slave holds SDA low (reset mid-read)
      nack: 0.05                 # device busy (EEPROM write cycle): address NACKed
      stretch_timeout: 0.02      # clock stretching beyond the master's timeout
      address_conflict: 0x5A     # a second device at the same address: wired-AND of both answers
      scl_glitch: 0.01           # extra SCL edge: bits shifted
  eth:
    cable: {length_m: 130, category: 5e}        # physical.ethernet_check
    faults:
      duplex_mismatch: plc       # this node is half duplex, its peer full: late collisions
      half_duplex: false         # all half duplex (a hub): collisions, jam, backoff, retransmission
      link_down: [{start: 0.5, end: 0.6}]
      mtu: 1500                  # larger frames are dropped
      duplicate: 0.0             # probability a frame is delivered twice (a loop, a mirror port)
      reorder: 0.0               # probability a frame is held back reorder_us
```

Every fault leaves its trace in the event (``"medium": {"faults": [...], "original": ...}``,
UART ``"errors"``), in the medium's statistics (``framing``, ``parity``, ``noise``,
``break``, ``overrun``, ``collisions``, ``late_collisions``, ``fcs_errors`` ...) and in
the analyzer row, so a capture shows what the bus did and why.
"""
from __future__ import annotations

import math
import random
from typing import Any, Callable, Mapping

from . import serial
from .spi import sample_edge, shift_bits

WIRED = ("uart", "rs485", "spi", "i2c", "eth")
_WINDOW_KEYS = ("start", "end", "period", "duty")


def window(spec: Mapping[str, Any], t: float) -> bool:
    start, end = float(spec.get("start", 0.0)), spec.get("end")
    if t < start or (end is not None and t > float(end)):
        return False
    period = spec.get("period")
    if period:
        return ((t - start) % float(period)) < float(spec.get("duty", 0.5)) * float(period)
    return True


def active(spec: Mapping[str, Any], t: float) -> dict[str, Any]:
    """The faults in force at t: the base ``faults`` (in their window) and the ``fault_bursts`` on top."""
    base = spec.get("faults") or {}
    out = {k: v for k, v in base.items() if k not in _WINDOW_KEYS} if window(base, t) else {}
    for burst in spec.get("fault_bursts") or []:
        if window(burst, t):
            out.update({k: v for k, v in burst.items() if k not in _WINDOW_KEYS})
    return out


def _hit(windows: Any, t: float) -> bool:
    return any(window(w, t) for w in windows or [])


def _count(medium: Any, key: str, n: int = 1) -> None:
    medium.stats[key] = medium.stats.get(key, 0) + n


def _poisson(mean: float, rng: random.Random) -> int:
    if mean <= 0:
        return 0
    if mean > 30:
        return max(0, int(round(rng.gauss(mean, math.sqrt(mean)))))
    k, p, cumulative, u = 0, math.exp(-mean), math.exp(-mean), rng.random()
    while u > cumulative:
        k += 1
        p *= mean / k
        cumulative += p
    return k


def _positions(count: int, ber: float, rng: random.Random) -> set[int]:
    n = _poisson(count * min(max(ber, 0.0), 1.0), rng) if ber > 0 else 0
    return set(rng.sample(range(count), min(n, count))) if n and count else set()


def _random_levels(t0: float, t1: float, mean: float, rng: random.Random) -> list[tuple[float, float, int]]:
    """A floating or fought-over line: random levels between t0 and t1, each lasting ~`mean`."""
    out, t = [], t0
    while t < t1:
        span = min(t1 - t, rng.expovariate(1.0 / max(mean, 1e-9)))
        out.append((t, t + span, rng.randint(0, 1)))
        t += span
    return out


Flip = Callable[[Any, float, int, random.Random], list]
Mark = Callable[[Any, Mapping[str, Any]], Any]


def apply(medium: Any, t: float, payload: dict[str, Any], ber: float, notes: dict[str, Any], rng: random.Random,
          flip: Flip, mark: Mark) -> list[tuple[float, Any]]:
    """The events one wired frame turns into (the medium calls this instead of its generic bit flips)."""
    faults = active(medium.spec, t)
    proto = payload.get("proto")
    if proto in ("uart", "rs485"):
        return _line(medium, t, payload, ber, notes, rng, faults, mark)
    handler = {"spi": _spi, "i2c": _i2c, "eth": _eth}[str(proto)]
    return handler(medium, t, payload, ber, notes, rng, faults, flip, mark)


def _lost(medium: Any, t: float, payload: dict[str, Any], notes: dict[str, Any], why: str, mark: Mark) -> list[tuple[float, Any]]:
    notes["lost"] = why
    medium.stats["lost"] += 1
    return [(t, mark(payload, notes))]


def _finish(medium: Any, payload: dict[str, Any], notes: dict[str, Any], problems: list[str], original: str,
            mark: Mark) -> Any:
    if problems:
        notes["faults"] = problems
    if payload.get("data") != original:
        notes.setdefault("original", original)
        medium.stats["corrupted"] += 1
    return mark(payload, notes) if notes else payload


# -- UART and RS-485 ----------------------------------------------------------------------------------------


def _line(medium: Any, t: float, payload: dict[str, Any], ber: float, notes: dict[str, Any], rng: random.Random,
          f: Mapping[str, Any], mark: Mark) -> list[tuple[float, Any]]:
    proto = payload["proto"]
    data = bytes.fromhex(str(payload.get("data", "")))
    baud = float(payload.get("baud") or medium.spec.get("bitrate") or (19200 if proto == "rs485" else 115200))
    fmt = str(payload.get("format") or "8N1")
    src = str(payload.get("src"))
    cell, ctime = 1.0 / baud, serial.char_time(baud, fmt)
    problems: list[str] = []
    cable = medium.cable(proto, baud)
    check = (cable or {}).get(proto)
    if check:
        ber += check["ber"]
        problems += [p for p in check["problems"] if check["ber"] > 0]
    if proto == "rs485" and f.get("ground_offset_v") is not None and not -7.0 <= float(f["ground_offset_v"]) <= 12.0:
        return _lost(medium, t, payload, notes, f"ground offset {float(f['ground_offset_v']):g} V is outside the "
                                                 "−7…+12 V common-mode range: the receivers see nothing", mark)
    if _hit(f.get("disconnected"), t):
        return _lost(medium, t, payload, notes, "cable disconnected", mark)
    rx_baud = float(f.get("rx_baud", baud)) * (1 + float(f.get("baud_error_pct", 0.0)) / 100)
    rx_fmt = str(f.get("rx_format", fmt))
    if abs(rx_baud - baud) > 1e-9:
        problems.append(f"receiver at {rx_baud:.0f} bit/s, sender at {baud:.0f} ({(rx_baud / baud - 1) * 100:+.1f} %)")
    if rx_fmt != fmt:
        problems.append(f"receiver set to {rx_fmt}, sender sends {fmt}")
    gaps = None
    duration = len(data) * ctime
    if f.get("char_gap_us"):
        gaps = [0.0] + [rng.uniform(0.0, float(f["char_gap_us"]) * 1e-6) for _ in data[1:]]
        duration += sum(gaps)
    windows: list[tuple[float, float, int]] = []
    pre = 0.0
    biased = bool(f.get("bias", True)) or bool(f.get("failsafe", False))
    if proto == "rs485":
        bus = medium.state.get("rs485_bus")
        if bus and bus["src"] != src and t < bus["until"]:
            overlap = bus["until"] - t
            windows += _random_levels(0.0, min(overlap, duration), cell, rng)
            problems.append(f"bus contention: {bus['src']} still drives the bus for {overlap * 1e6:.0f} µs"
                            + (" (its driver enable is held after the frame)" if t >= bus["end"] else ""))
            _count(medium, "collisions")
        if not biased and rng.random() < float(f.get("floating_noise", 0.5)):
            pre = 2 * ctime
            windows += _random_levels(-pre, -0.3 * ctime, cell * 2, rng)
            problems.append("idle bus floating (no fail-safe bias): receivers decode noise before the frame")
        de_late = float(f.get("de_late_us", 0.0)) * 1e-6
        if de_late > 0:
            windows += [(0.0, de_late, 1)] if biased else _random_levels(0.0, de_late, cell, rng)
            problems.append(f"driver enabled {de_late * 1e6:.0f} µs late: the start of the frame never reaches the bus")
        medium.state["rs485_bus"] = {"src": src, "end": t + duration,
                                     "until": t + duration + float(f.get("de_hold_us", 0.0)) * 1e-6}
    rate = float(f.get("glitches_per_s", 0.0))
    if rate:
        width = float(f.get("glitch_us", 1.0)) * 1e-6
        spikes = _poisson(rate * (duration + pre), rng)
        for _ in range(spikes):
            at = rng.uniform(-pre, duration)
            windows.append((at, at + width, 0 if rng.random() < 0.7 else 1))
        if spikes:
            problems.append(f"{spikes} noise spike{'s' if spikes > 1 else ''} ({width * 1e6:g} µs)")
    for brk in f.get("breaks") or []:
        b0, b1 = float(brk.get("start", 0.0)) - t, float(brk.get("end", 1e9)) - t
        if b0 < duration and b1 > -pre:
            windows.append((max(b0, -pre), min(b1, duration + ctime), 0))
            problems.append("line held low (break)")
    flips = _positions(int(len(data) * serial.char_bits(fmt)), ber, rng)
    if flips:
        problems.append(f"{len(flips)} bit error{'s' if len(flips) > 1 else ''} on the line")
    original = payload.get("data")
    if not (windows or flips or rx_baud != baud or rx_fmt != fmt or f.get("overrun") or gaps):
        return [(t, _finish(medium, payload, notes, problems, str(original), mark) if notes or problems else payload)]
    line, _ = serial.transmit(data, baud, fmt, gaps, flips, start=pre)
    line = line.overlay([(a + pre, b + pre, level) for a, b, level in windows])
    chars = serial.receive(line, rx_baud, rx_fmt, phase=rng.random())
    if f.get("overrun"):
        chars = _overrun(medium, t - pre, chars, f["overrun"], problems)
    pieces = [chars] if chars else []
    if proto == "rs485" and f.get("modbus", True) and len(chars) > 1:      # RTU: > 1.5 character idle ends a frame
        limit = 1.5 * serial.char_time(rx_baud, rx_fmt)
        pieces, current = [], [chars[0]]
        for previous, char in zip(chars, chars[1:]):
            if char["t"] - (previous["end"] + 0.5 / rx_baud) > limit:
                pieces.append(current)
                current = []
            current.append(char)
        pieces.append(current)
        if len(pieces) > 1:
            problems.append(f"idle > 1.5 characters inside the frame: received as {len(pieces)} frames")
    if not pieces:
        return _lost(medium, t, payload, {**notes, "faults": problems}, "nothing on the line a UART could read", mark)
    out = []
    for piece in pieces:
        received = bytes(c["byte"] for c in piece)
        errors = [{"i": i, "kind": kind} for i, c in enumerate(piece) for kind in c["errors"]]
        for error in errors:
            _count(medium, error["kind"])
        event = {**payload, "data": received.hex()}
        if errors:
            event["errors"] = errors
            counts: dict[str, int] = {}
            for error in errors:
                counts[error["kind"]] = counts.get(error["kind"], 0) + 1
            summary = ", ".join(f"{n} {kind}" for kind, n in counts.items())
        else:
            summary = ""
        piece_notes = dict(notes)
        piece_problems = problems + ([f"UART flags: {summary}"] if summary else [])
        out.append((max(t, t + piece[0]["t"] - pre), _finish(medium, event, piece_notes, piece_problems, str(original), mark)))
    return out


def _overrun(medium: Any, t0: float, chars: list[dict[str, Any]], spec: Mapping[str, Any], problems: list[str]) -> list[dict[str, Any]]:
    """Bytes that arrive while the CPU cannot read the UART (an ISR or a flash write blocks it) and the
    receive FIFO is full are lost; the next byte read carries the overrun flag."""
    period = float(spec.get("period_ms", 10.0)) * 1e-3
    busy = float(spec.get("busy_us", 500.0)) * 1e-6
    fifo = int(spec.get("fifo", 1))
    state = medium.state.setdefault("overrun", {"window": None, "count": 0})
    kept, dropped, flag = [], 0, False
    for char in chars:
        at = t0 + char["end"]
        k = math.floor(at / period)
        if at - k * period < busy:
            if state["window"] != k:
                state["window"], state["count"] = k, 0
            state["count"] += 1
            if state["count"] > fifo:
                dropped += 1
                flag = True
                continue
        if flag:
            char = {**char, "errors": [*char["errors"], "overrun"]}
            flag = False
        kept.append(char)
    if dropped:
        problems.append(f"receiver overrun: {dropped} byte{'s' if dropped > 1 else ''} lost while the CPU was busy "
                        f"({busy * 1e6:.0f} µs every {period * 1e3:g} ms, FIFO {fifo})")
        _count(medium, "overrun_lost", dropped)
    return kept


# -- SPI ----------------------------------------------------------------------------------------------------


def _spi(medium: Any, t: float, payload: dict[str, Any], ber: float, notes: dict[str, Any], rng: random.Random,
         f: Mapping[str, Any], flip: Flip, mark: Mark) -> list[tuple[float, Any]]:
    direction = str(payload.get("dir", "mosi"))
    data = bytes.fromhex(str(payload.get("data", "")))
    original = str(payload.get("data", ""))
    mode = int(payload.get("mode", 0))
    state = medium.state.setdefault("spi", {})
    last = state.get("last") or {}
    same = bool(last) and abs(last["t"] - t) < 1e-6                 # the MOSI half of this transfer
    hz = float(payload.get("hz") or (last.get("hz") if same and direction == "miso" else None) or medium.spec.get("bitrate") or 1e6)
    problems: list[str] = []
    check = (medium.cable("spi", hz) or {}).get("spi")
    if _hit(f.get("slave_off"), t):
        return _lost(medium, t, payload, notes, "slave unpowered: it ignores the transfer, MISO floats (reads 0xFF)", mark)
    if direction == "mosi":
        transfer: dict[str, Any] = {"t": t, "cut": None, "slip": None, "edge": False, "hz": hz}
        slave_mode = f.get("slave_mode")
        if slave_mode is not None and sample_edge(int(slave_mode)) != sample_edge(mode) and data:
            data = shift_bits(data, 0, -1)
            transfer["edge"] = True
            problems.append(f"slave samples on the {sample_edge(int(slave_mode))} edge (mode {int(slave_mode)}), master is "
                            f"mode {mode}: the slave reads every bit one clock early")
        ring = float(f.get("double_clock", check["double_clock"] if check else 0.0))
        if ring and data and rng.random() < ring:
            at = rng.randrange(len(data) * 8)
            data = shift_bits(data, at, +1)
            transfer["slip"] = at
            problems.append(f"SCK ringing: an extra clock at bit {at} (byte {at // 8}), the slave is one bit behind from there")
            _count(medium, "bit_slips")
        if len(data) > 1 and rng.random() < float(f.get("cs_glitch", 0.0)):
            k = rng.randrange(1, len(data))
            data = data[:k]
            transfer["cut"] = k
            problems.append(f"chip select glitch after byte {k}: the slave reset and ignored the rest")
            _count(medium, "cs_glitches")
        state["last"] = transfer
    else:
        transfer = last
        if same:
            if transfer.get("edge"):
                data = shift_bits(data, 0, +1)
                problems.append("clock mode mismatch: the master reads MISO one clock late")
            if transfer.get("slip") is not None:
                data = shift_bits(data, transfer["slip"], -1)
                problems.append(f"the extra clock at bit {transfer['slip']} advanced the slave's shift register")
            if transfer.get("cut") is not None:
                cut = int(transfer["cut"])
                data = data[:cut] + b"\xff" * max(0, len(data) - cut)
                problems.append(f"MISO floats after the chip select glitch (byte {cut} on reads 0xFF)")
        if check and check["late"]:
            data = shift_bits(data, 0, +1)
            problems.append(f"MISO valid {check['miso_ns']} ns after the clock edge, the master samples at "
                            f"{check['half_period_ns']} ns: read one bit late (max {check['max_hz'] / 1e6:.1f} MHz)")
        elif check:
            ber += check["ber"]
    payload = {**payload, "data": data.hex()}
    flipped = flip(payload, ber, int(notes.get("extra_bits", 0)), rng)
    if flipped:
        notes["flipped"] = flipped
        medium.stats["flipped_bits"] += len(flipped)
    return [(t, _finish(medium, payload, notes, problems, original, mark))]


# -- I2C ----------------------------------------------------------------------------------------------------


def _i2c(medium: Any, t: float, payload: dict[str, Any], ber: float, notes: dict[str, Any], rng: random.Random,
         f: Mapping[str, Any], flip: Flip, mark: Mark) -> list[tuple[float, Any]]:
    direction = str(payload.get("dir", "m2s"))
    original = str(payload.get("data", ""))
    data = bytes.fromhex(original)
    problems: list[str] = []
    if _hit(f.get("stuck"), t):
        _count(medium, "bus_stuck")
        return _lost(medium, t, payload, notes, "SDA held low by a slave: bus stuck (recover with 9 SCL pulses + STOP)", mark)
    if direction == "m2s" and rng.random() < float(f.get("nack", 0.0)):
        _count(medium, "nacks")
        return _lost(medium, t, payload, notes, "address NACKed: device busy", mark)
    if direction == "s2m" and rng.random() < float(f.get("stretch_timeout", 0.0)):
        _count(medium, "stretch_timeouts")
        return _lost(medium, t, payload, notes, "slave stretched SCL past the master's timeout", mark)
    if direction == "s2m" and f.get("address_conflict") is not None and data:
        value = f["address_conflict"]
        other = bytes((int(value, 0) if isinstance(value, str) else int(value)) & 0xFF for _ in data) \
            if not isinstance(value, bool) else bytes(rng.randrange(256) for _ in data)
        data = bytes(a & b for a, b in zip(data, other))
        problems.append("two devices answer at this address: the open-drain bus ANDs their bits")
    if data and rng.random() < float(f.get("scl_glitch", 0.0)):
        at = rng.randrange(len(data) * 8)
        data = shift_bits(data, at, +1)
        problems.append(f"SCL glitch: an extra clock at bit {at}, the rest is shifted")
        _count(medium, "bit_slips")
    payload = {**payload, "data": data.hex()}
    flipped = flip(payload, ber, int(notes.get("extra_bits", 0)), rng)
    if flipped:
        notes["flipped"] = flipped
        medium.stats["flipped_bits"] += len(flipped)
    return [(t, _finish(medium, payload, notes, problems, original, mark))]


# -- Ethernet -----------------------------------------------------------------------------------------------


def _eth(medium: Any, t: float, payload: dict[str, Any], ber: float, notes: dict[str, Any], rng: random.Random,
         f: Mapping[str, Any], flip: Flip, mark: Mark) -> list[tuple[float, Any]]:
    from .wifi import fcs
    original = str(payload.get("data", ""))
    data = bytes.fromhex(original)
    speed = float(payload.get("speed") or medium.spec.get("bitrate") or 100e6)
    src = str(payload.get("src"))
    duration = (len(data) + 8) * 8 / speed
    problems: list[str] = []
    if _hit(f.get("link_down"), t):
        _count(medium, "link_down")
        return _lost(medium, t, payload, notes, "link down: no carrier", mark)
    check = (medium.cable("eth", speed) or {}).get("eth")
    if check:
        ber += check["ber"]
    mtu = f.get("mtu")
    if mtu and len(data) - 18 > int(mtu):
        _count(medium, "giants")
        return _lost(medium, t, payload, notes, f"{len(data) - 18}-byte payload > MTU {int(mtu)}: dropped (giant)", mark)
    state = medium.state.setdefault("eth", {"frames": []})
    frames = [x for x in state["frames"] if x["end"] > t - 0.01]
    half_all = bool(f.get("half_duplex"))
    mismatch = f.get("duplex_mismatch")
    half = half_all or (mismatch is not None and mismatch is not False and (mismatch is True or str(mismatch) == src))
    other = next((x for x in frames if x["src"] != src and x["start"] <= t < x["end"]), None)
    if other and half:
        if t - other["start"] <= float(f.get("collision_window_us", 1.0)) * 1e-6:   # both started together
            state["frames"] = frames + [{"src": src, "start": t, "end": t + duration, "half": True}]
            return _eth_backoff(medium, t, payload, notes, data[:8], other, speed, rng, mark)
        wait = other["end"] + 96 / speed - t                    # carrier sense: wait for the line, then the gap
        t += wait
        problems.append(f"deferred {wait * 1e6:.1f} µs: {other['src']} was transmitting (half duplex carrier sense)")
        _count(medium, "deferred")
    elif other and other.get("half") and not half_all:          # full duplex talks over a half-duplex frame
        cut = int((t - other["start"]) * speed / 8)
        late = cut >= 64
        _count(medium, "late_collisions" if late else "collisions")
        state["frames"] = frames + [{"src": src, "start": t, "end": t + duration}]
        problems.append(f"duplex mismatch: {other['src']} is half duplex and was {cut} bytes into a frame; it sees a "
                        f"{'late ' if late else ''}collision, aborts{' without retransmitting' if late else ''} and drops this one")
        return _lost(medium, t, payload, {**notes, "faults": problems}, f"{'late ' if late else ''}collision (duplex mismatch)", mark)
    state["frames"] = frames + [{"src": src, "start": t, "end": t + duration, "half": half}]
    out = []
    if rng.random() < float(f.get("reorder", 0.0)):
        t += float(f.get("reorder_us", 500.0)) * 1e-6
        problems.append("held back in a queue (reordered)")
        _count(medium, "reordered")
    payload = {**payload, "data": data.hex()}
    flipped = flip(payload, ber, int(notes.get("extra_bits", 0)), rng)
    if flipped:
        notes["flipped"] = flipped
        medium.stats["flipped_bits"] += len(flipped)
        received = bytes.fromhex(payload["data"])
        if fcs(received[:-4]) != received[-4:]:
            notes["lost"] = "FCS error: dropped by the receiving MAC"
            medium.stats["lost"] += 1
            _count(medium, "fcs_errors")
    out.append((t, _finish(medium, payload, notes, problems, original, mark)))
    if "lost" not in notes and rng.random() < float(f.get("duplicate", 0.0)):
        _count(medium, "duplicates")
        out.append((t + duration, mark(dict(payload), {**notes, "faults": [*problems, "duplicate (a loop or a mirror port)"]})))
    return out


def _eth_backoff(medium: Any, t: float, payload: dict[str, Any], notes: dict[str, Any], fragment: bytes, other: Mapping[str, Any],
                 speed: float, rng: random.Random, mark: Mark) -> list[tuple[float, Any]]:
    """Half duplex: a collision in the first 64 bytes, a jam, binary exponential backoff and a new attempt."""
    _count(medium, "collisions")
    slot = 512 / speed
    out = [(t, {**mark({**payload, "data": fragment.hex()}, {**notes, "faults": [f"collision with {other['src']}: jam"]}),
                "error": "collision: jam, backoff, retransmission"})]
    attempts = int(payload.get("retry", 0)) + 1
    wait = rng.randrange(2 ** min(attempts, 10)) * slot
    medium.stats["retransmissions"] += 1
    again = {**payload, "retry": attempts}
    return out + [(t + len(fragment) * 8 / speed + 32 / speed + 96 / speed + wait + max(0.0, other["end"] - t), again)]
