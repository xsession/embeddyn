"""Physical layer: radio link budgets with distance, cables (length, twist, shield) and I2C bus capacitance.

Used by `comms.medium` when a topic's medium has a ``link:`` (radio) or ``cable:``
(wired) section; also usable alone (`radio_link`, `cable_effects`, `i2c_check`).

**Radio.** Log-distance path loss from the free-space loss at 1 m:

    PL(d) = 20·log10(f / MHz) − 27.55 + 10·n·log10(d / 1 m) + walls

RSSI = Ptx + antenna gains − PL (+ fading, in the medium). The bit error rate follows a
waterfall around the receiver sensitivity (the RSSI where BER = 1e-3):
log10(BER) = −3 − (RSSI − sensitivity) / `db_per_decade`, between 1e-9 and 0.5. Packets
more than `capture_margin` dB below the sensitivity are not even detected (lost).
Defaults per radio:

| radio | f | Ptx | sensitivity | notes |
|---|---|---|---|---|
| BLE 1M | 2440 MHz | 0 dBm | −94 dBm | |
| Wi-Fi 6 Mb/s | 2437 MHz | 18 dBm | −82 dBm | channel number gives the frequency |
| LoRa BW125 | 868 MHz | 14 dBm | −123 … −137 dBm | SF7 … SF12 from the packet |
| NFC 13.56 MHz | near field | | | inductive: 60 dB/decade, works to ~5 cm |
| RFID 125 kHz | near field | | | inductive, works to ~8 cm |
| RFID UHF Gen2 | 868 MHz | 33 dBm EIRP | tag −18 dBm | forward link powers the tag: beyond ~10 m it stays silent |

**Cable.** Propagation 5 ns/m (velocity factor ~0.66). EMI pickup grows with length and
falls with twisting (pair: ×0.05, about −26 dB) and shielding (×0.1):
extra BER = `emi_ber_per_m` × length × pickup × `environment`. CAN adds the
bit-timing limit: the round trip 2·L·5 ns plus transceiver and controller delays must
fit in ~70 % of a bit (the rest is phase segments and oscillator tolerance): 40 m at
1 Mbit/s, 100 m at 500 kbit/s, 500 m at 125 kbit/s. Past it, arbitration and ACK fail
and frames turn into error frames.

**I2C.** Bus capacitance = cable (pF/m by construction) + 10 pF per device + `extra_pf`;
rise time t_r = 0.8473 · Rp · Cb (30 % → 70 % of VDD) checked against the
specification (UM10204): 1000 ns / 300 ns / 120 ns and 400 pF (550 pF in Fast-mode
Plus). Too slow an edge makes SDA/SCL miss V_IH before the clock samples: bit errors
that grow as t_r approaches the SCL high time.

**RS-485** (`rs485_check`): the rule of thumb bit rate × length ≤ 10⁸ (bit/s·m) and
1200 m at most; without termination the reflections take about three round trips
(6 · L · 5 ns) to settle, harmless while that is under a quarter of a bit, sampled as
wrong bits once it reaches the middle of the bit; more than 32 unit loads overload the
drivers. **RS-232** (`uart_check`): 2500 pF of cable at up to 20 kbit/s (TIA-232), less
at higher rates where the slew-rate-limited drivers no longer settle. **SPI**
(`spi_check`): the master samples MISO half a clock after it shifted MOSI, so the round
trip over the cable plus the slave's output delay must fit in half a period (else
MISO arrives one bit late); fast edges on a long unterminated SCK ring, and a ring
that crosses the threshold twice is an extra clock (bit slip). **Ethernet**
(`ethernet_check`): 100 m per segment (BER climbs beyond it), Cat3 only for 10BASE-T.
"""
from __future__ import annotations

import math
from typing import Any, Mapping

RADIO = {
    "ble": {"freq_mhz": 2440.0, "tx_dbm": 0.0, "sensitivity_dbm": -94.0},
    "wifi": {"freq_mhz": 2437.0, "tx_dbm": 18.0, "sensitivity_dbm": -82.0},
    "lora": {"freq_mhz": 868.1, "tx_dbm": 14.0, "sensitivity_dbm": -129.0},
    "nfc": {"near_field": True, "range_m": 0.05},
    "rfid125": {"near_field": True, "range_m": 0.08},
    "gen2": {"freq_mhz": 866.0, "tx_dbm": 33.0, "tag_threshold_dbm": -18.0, "sensitivity_dbm": -75.0},
}
LORA_SENSITIVITY = {7: -123.0, 8: -126.0, 9: -129.0, 10: -132.0, 11: -134.5, 12: -137.0}   # BW125, typical SX127x
CABLE_PF_PER_M = {"twisted": 52.0, "untwisted": 40.0, "ribbon": 45.0, "coax": 100.0}
PICKUP = {"twisted": 0.05, "untwisted": 1.0, "ribbon": 0.5, "coax": 0.02}
I2C_MODES = {  # f_scl, t_r max, Cb max, sink current at VOL 0.4 V
    "standard": {"hz": 100e3, "tr_ns": 1000.0, "cb_pf": 400.0, "iol_ma": 3.0},
    "fast": {"hz": 400e3, "tr_ns": 300.0, "cb_pf": 400.0, "iol_ma": 3.0},
    "fast-plus": {"hz": 1e6, "tr_ns": 120.0, "cb_pf": 550.0, "iol_ma": 20.0},
}


def wifi_freq(channel: Any) -> float | None:
    try:
        channel = int(channel)
    except (TypeError, ValueError):
        return None
    if 1 <= channel <= 13:
        return 2407.0 + 5 * channel
    if channel == 14:
        return 2484.0
    if 32 <= channel <= 177:
        return 5000.0 + 5 * channel
    return None


def path_loss_db(distance_m: float, freq_mhz: float, exponent: float = 2.0, walls_db: float = 0.0) -> float:
    d = max(distance_m, 0.01)
    return 20 * math.log10(freq_mhz) - 27.55 + 10 * exponent * math.log10(d) + walls_db


def ber_waterfall(rssi: float, sensitivity: float, db_per_decade: float = 1.5) -> float:
    exponent = -3.0 - (rssi - sensitivity) / max(db_per_decade, 0.1)
    return min(0.5, max(1e-9, 10 ** exponent))


def radio_kind(payload: Mapping[str, Any]) -> str:
    proto = str(payload.get("proto", ""))
    if proto == "rfid":
        return "gen2" if payload.get("air") == "gen2" else "rfid125"
    return proto


def radio_link(payload: Mapping[str, Any], link: Mapping[str, Any], fade_db: float = 0.0) -> dict[str, Any]:
    """RSSI, SNR, BER and detection of one packet over `link` (distance_m, tx_dbm, exponent, walls_db, ...);
    `fade_db` is this packet's fading (added to the received level)."""
    kind = radio_kind(payload)
    base = dict(RADIO.get(kind, RADIO["ble"]))
    sources = link.get("sources") or {}
    per = sources.get(str(payload.get("src"))) or {}
    spec = {**base, **{k: v for k, v in link.items() if k != "sources"}, **per}
    distance = float(spec.get("distance_m", 1.0))
    if base.get("near_field"):
        reach = float(spec.get("range_m", base["range_m"]))
        # Inductive coupling falls with d^3 in field strength: 60 dB per decade; the reach is the -3 dB point
        margin = -60.0 * math.log10(max(distance, 1e-3) / reach) + fade_db
        ber = ber_waterfall(margin, 0.0, float(spec.get("db_per_decade", 1.5)))
        return {"kind": kind, "distance_m": distance, "margin_db": round(margin, 1), "ber": ber,
                "lost": margin < -float(spec.get("capture_margin", 3.0)), "why": f"out of reach ({distance * 100:.1f} cm)"}
    freq = float(spec.get("freq_mhz", base.get("freq_mhz", 2440.0)))
    if kind == "wifi" and "freq_mhz" not in link and wifi_freq(payload.get("channel")):
        freq = wifi_freq(payload.get("channel")) or freq
    if kind == "lora" and payload.get("freq") and "freq_mhz" not in link:
        freq = float(payload["freq"])
    loss = path_loss_db(distance, freq, float(spec.get("exponent", 2.0)), float(spec.get("walls_db", 0.0)))
    gains = float(spec.get("antenna_gain_db", 0.0))
    if kind == "gen2":
        tag_power = float(spec["tx_dbm"]) - loss + gains + fade_db
        rssi = tag_power - loss - 10.0 + gains                  # backscatter: second pass, modulation loss
        powered = payload.get("dir") == "reader" or tag_power >= float(spec["tag_threshold_dbm"])
        ber = ber_waterfall(rssi, float(spec["sensitivity_dbm"]), float(spec.get("db_per_decade", 1.5))) \
            if payload.get("dir") != "reader" else ber_waterfall(tag_power, float(spec["tag_threshold_dbm"]) + 3, 1.5)
        return {"kind": kind, "distance_m": distance, "path_loss_db": round(loss, 1), "tag_power_dbm": round(tag_power, 1),
                "rssi": round(rssi, 1), "ber": ber, "lost": not powered,
                "why": f"tag not powered ({tag_power:.1f} dBm < {spec['tag_threshold_dbm']} dBm)"}
    sensitivity = float(spec.get("sensitivity_dbm", base["sensitivity_dbm"]))
    if kind == "lora" and "sensitivity_dbm" not in link and payload.get("sf"):
        sensitivity = LORA_SENSITIVITY.get(int(payload["sf"]), sensitivity)
    rssi = float(spec.get("tx_dbm", base["tx_dbm"])) - loss + gains + fade_db
    noise = -174.0 + 10 * math.log10({"ble": 1e6, "wifi": 20e6, "lora": 125e3}.get(kind, 1e6)) + float(spec.get("noise_figure_db", 6.0))
    ber = ber_waterfall(rssi, sensitivity, float(spec.get("db_per_decade", 1.5)))
    return {"kind": kind, "distance_m": distance, "freq_mhz": freq, "path_loss_db": round(loss, 1), "rssi": round(rssi, 1),
            "snr": round(rssi - noise, 1), "sensitivity_dbm": sensitivity, "ber": ber,
            "lost": rssi < sensitivity - float(spec.get("capture_margin", 3.0)),
            "why": f"below sensitivity at {distance:g} m ({rssi:.1f} dBm < {sensitivity:g} dBm)"}


def cable_effects(cable: Mapping[str, Any], proto: str | None = None, bitrate: float | None = None) -> dict[str, Any]:
    """Delay, EMI bit error rate and (CAN) bit-timing check of a cable."""
    length = float(cable.get("length_m", 1.0))
    kind = str(cable.get("type", "twisted"))
    shielded = bool(cable.get("shielded", False))
    pickup = PICKUP.get(kind, 1.0) * (0.1 if shielded else 1.0)
    delay = length * float(cable.get("ns_per_m", 5.0)) * 1e-9
    # Ethernet's transformer-coupled pairs and line codes shrug off what upsets a CAN or UART wire
    default = {"eth": 1e-12, "rs485": 2e-8}.get(str(proto), 2e-6)
    ber = float(cable.get("emi_ber_per_m", default)) * length * pickup * float(cable.get("environment", 1.0))
    out: dict[str, Any] = {"length_m": length, "type": kind, "shielded": shielded, "pickup": pickup, "delay_s": delay,
                           "emi_ber": ber, "timing_error": 0.0}
    if proto == "can":
        # A CAN node must see the farthest node's bit (and its ACK) back within the propagation part of the
        # bit time: 2 · (cable + transceiver + controller) ≤ ~70 % of a bit (the rest: phase segments,
        # oscillator tolerance). That gives the textbook 40 m at 1 Mbit/s, 100 m at 500 kbit/s, 500 m at 125.
        rate = float(bitrate or cable.get("bitrate", 500_000))
        bit = 1.0 / rate
        fixed = float(cable.get("transceiver_ns", 150.0)) * 1e-9 + float(cable.get("controller_ns", 50.0)) * 1e-9
        budget = float(cable.get("prop_fraction", 0.7)) * bit
        need = 2 * (delay + fixed)
        margin = budget - need
        max_length = max(0.0, (budget - 2 * fixed) / (2 * float(cable.get("ns_per_m", 5.0)) * 1e-9))
        out.update(bitrate=rate, loop_delay_ns=round(need * 1e9), timing_margin_ns=round(margin * 1e9),
                   max_length_m=round(max_length, 1))
        if margin < 0:                                          # probability a frame's arbitration/ACK fails
            out["timing_error"] = min(1.0, 2.0 * -margin / budget)
    return out


def i2c_check(cable: Mapping[str, Any]) -> dict[str, Any]:
    """Bus capacitance, rise time and pull-up range of an I2C bus over a cable (UM10204 limits)."""
    length = float(cable.get("length_m", 0.1))
    kind = str(cable.get("type", "untwisted"))
    shielded = bool(cable.get("shielded", False))
    per_m = float(cable.get("pf_per_m", CABLE_PF_PER_M.get(kind, 50.0) + (60.0 if shielded and kind != "coax" else 0.0)))
    devices = int(cable.get("devices", 2))
    cb = per_m * length + 10.0 * devices + float(cable.get("extra_pf", 10.0))
    mode = str(cable.get("mode", "standard"))
    limits = I2C_MODES.get(mode, I2C_MODES["standard"])
    vdd = float(cable.get("vdd", 3.3))
    rp = float(cable.get("pullup_ohm", 4700.0))
    tr = 0.8473 * rp * cb * 1e-12 * 1e9                          # ns
    rp_min = (vdd - 0.4) / (limits["iol_ma"] * 1e-3)
    rp_max = limits["tr_ns"] * 1e-9 / (0.8473 * cb * 1e-12)
    high_ns = 0.5e9 / limits["hz"]
    problems = []
    if cb > limits["cb_pf"]:
        problems.append(f"bus capacitance {cb:.0f} pF > {limits['cb_pf']:.0f} pF")
    if tr > limits["tr_ns"]:
        problems.append(f"rise time {tr:.0f} ns > {limits['tr_ns']:.0f} ns")
    if rp < rp_min:
        problems.append(f"pull-up {rp:.0f} Ω < {rp_min:.0f} Ω (VOL > 0.4 V)")
    # Bit errors: none while the edge meets the spec; then growing as the rise eats the SCL high time
    ratio = tr / high_ns
    ber = 0.0 if tr <= limits["tr_ns"] else min(0.5, 10 ** (-6 + 8 * (ratio - limits["tr_ns"] / high_ns)))
    advice = (f"use {rp_min:.0f}-{rp_max:.0f} Ω pull-ups" if rp_min <= rp_max else
              f"no pull-up fits: {cb:.0f} pF is too much for {mode} mode (slow the clock, shorten or buffer the bus)")
    return {"mode": mode, "scl_hz": limits["hz"], "length_m": length, "type": kind, "shielded": shielded,
            "pf_per_m": per_m, "bus_pf": round(cb, 1), "cb_max_pf": limits["cb_pf"], "pullup_ohm": rp,
            "rise_ns": round(tr, 1), "rise_max_ns": limits["tr_ns"], "rp_min_ohm": round(rp_min), "rp_max_ohm": round(rp_max),
            "ok": not problems, "problems": problems, "advice": advice, "ber": ber}


def _ramp(x: float, x0: float, x1: float, low: float = 1e-7, high: float = 0.05) -> float:
    """0 below x0, then log-linear from `low` at x0 to `high` at x1 (and beyond)."""
    if x <= x0:
        return 0.0
    f = min(1.0, (x - x0) / max(x1 - x0, 1e-12))
    return 10 ** (math.log10(low) + f * (math.log10(high) - math.log10(low)))


def rs485_check(cable: Mapping[str, Any], baud: float | None = None) -> dict[str, Any]:
    """Length rule, termination and loading of an RS-485 bus."""
    length = float(cable.get("length_m", 10.0))
    rate = float(baud or cable.get("baud", 19200))
    ns_per_m = float(cable.get("ns_per_m", 5.0))
    terminated = bool(cable.get("termination", True))
    loads = float(cable.get("unit_loads", 2))
    bit = 1.0 / rate
    settle = 6 * length * ns_per_m * 1e-9
    problems, ber = [], 0.0
    if length * rate > 1e8:
        problems.append(f"{length:g} m × {rate:g} bit/s exceeds 10⁸ (max {1e8 / rate:.0f} m at this rate)")
        ber += _ramp(length * rate / 1e8, 1.0, 3.0)
    if length > 1200:
        problems.append(f"{length:g} m > 1200 m")
        ber += _ramp(length, 1200, 2400)
    if not terminated:
        ratio = settle / bit
        ber += _ramp(ratio, 0.25, 0.5, 1e-6, 0.1)
        if ratio > 0.25:
            problems.append(f"no termination: reflections settle in {settle * 1e9:.0f} ns, {ratio:.0%} of a bit")
    if loads > 32:
        problems.append(f"{loads:g} unit loads > 32")
        ber += _ramp(loads, 32, 64, 1e-6, 0.01)
    return {"length_m": length, "baud": rate, "terminated": terminated, "settle_ns": round(settle * 1e9),
            "max_length_m": round(min(1200.0, 1e8 / rate)), "unit_loads": loads, "ber": min(0.5, ber),
            "ok": not problems, "problems": problems,
            "advice": "terminate both ends with 120 Ω" if not terminated and problems else
                      ("lower the bit rate or shorten the bus" if problems else "")}


def uart_check(cable: Mapping[str, Any], baud: float | None = None) -> dict[str, Any]:
    """RS-232 cable capacitance against TIA-232 (2500 pF up to 20 kbit/s)."""
    length = float(cable.get("length_m", 2.0))
    rate = float(baud or cable.get("baud", 115200))
    kind = str(cable.get("type", "untwisted"))
    pf = float(cable.get("pf_per_m", CABLE_PF_PER_M.get(kind, 50.0) + (60.0 if cable.get("shielded") and kind != "coax" else 0.0))) * length
    limit = 2500.0 * min(1.0, 20000.0 / rate)
    ber = _ramp(pf / limit, 1.0, 2.0, 1e-6, 0.05)
    problems = [f"cable {pf:.0f} pF > {limit:.0f} pF allowed at {rate:g} bit/s"] if pf > limit else []
    return {"length_m": length, "baud": rate, "cable_pf": round(pf), "max_pf": round(limit),
            "max_length_m": round(limit / (pf / max(length, 1e-9)), 1), "ber": ber, "ok": not problems, "problems": problems}


def spi_check(cable: Mapping[str, Any], hz: float | None = None) -> dict[str, Any]:
    """MISO timing over a cable and SCK ringing (double clocking)."""
    length = float(cable.get("length_m", 0.1))
    clock = float(hz or cable.get("hz", 1e6))
    ns_per_m = float(cable.get("ns_per_m", 5.0))
    output_ns = float(cable.get("slave_output_ns", 40.0))      # SCK edge → MISO valid
    setup_ns = float(cable.get("master_setup_ns", 10.0))
    half = 0.5e9 / clock
    need = 2 * length * ns_per_m + output_ns + setup_ns
    ratio = need / half
    series = float(cable.get("series_ohm", 0.0))
    ring = 0.0 if length < 0.2 or series >= 22 else min(0.3, 0.02 * length / 0.2)
    problems = []
    if ratio > 1:
        problems.append(f"MISO valid {need:.0f} ns after the edge > half a clock ({half:.0f} ns): read one bit late")
    elif ratio > 0.8:
        problems.append(f"MISO timing margin {half - need:.0f} ns: marginal")
    if ring:
        problems.append(f"{length:g} m of SCK without series termination rings: extra clocks")
    max_hz = 0.5e9 / need
    return {"length_m": length, "hz": clock, "miso_ns": round(need), "half_period_ns": round(half), "max_hz": round(max_hz),
            "late": ratio > 1, "ber": _ramp(ratio, 0.8, 1.0, 1e-6, 0.05) if ratio <= 1 else 0.0, "double_clock": ring,
            "ok": not problems, "problems": problems,
            "advice": f"stay below {max_hz / 1e6:.1f} MHz" + (", add 22-33 Ω series resistors at the drivers" if ring else "")}


def ethernet_check(cable: Mapping[str, Any], speed: float | None = None) -> dict[str, Any]:
    """Segment length and cable category of twisted-pair Ethernet."""
    length = float(cable.get("length_m", 10.0))
    rate = float(speed or cable.get("speed", 100e6))
    category = str(cable.get("category", "5e"))
    problems, ber = [], 0.0
    if length > 100:
        problems.append(f"{length:g} m > 100 m per segment")
        ber += _ramp(length, 100, 160, 1e-9, 0.01)
    if category == "3" and rate > 10e6:
        problems.append(f"Cat3 is for 10BASE-T only ({rate / 1e6:g} Mbit/s)")
        ber += 1e-4
    if category in ("5", "5e") and rate > 1e9:
        problems.append(f"Cat{category} is rated to 1 Gbit/s")
        ber += 1e-6
    return {"length_m": length, "speed": rate, "category": category, "ber": min(0.5, ber), "ok": not problems,
            "problems": problems, "advice": "add a switch (each port is a new 100 m segment)" if length > 100 else ""}
