"""Wired buses: UART line model, NMEA, Modbus RTU and TCP, SPI, Ethernet, their physical checks and faults."""
import os
import shutil
import struct
import subprocess
from pathlib import Path

import pytest
import yaml

from embeddyn.mechatronics.comms import ethernet, serial, spi
from embeddyn.mechatronics.comms.analyzer import decode_events, write_pcap
from embeddyn.mechatronics.comms.captures import read_capture
from embeddyn.mechatronics.comms.medium import Media
from embeddyn.mechatronics.comms.physical import ethernet_check, rs485_check, spi_check, uart_check
from embeddyn.mechatronics.factory import build_adapters
from embeddyn.mechatronics.model import MechatronicProject
from embeddyn.mechatronics.orchestrator import CoSimulationOrchestrator

ROOT = Path(__file__).resolve().parents[2]
WIRED_LAB = ROOT / "examples/mechatronics/wired-lab.yaml"
TSHARK = os.environ.get("TSHARK") or shutil.which("tshark")
SENTENCE = serial.nmea("GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,")


def _receive(data, baud, fmt="8N1", rx_baud=None, rx_fmt=None, windows=()):
    line, _ = serial.transmit(data, baud, fmt)
    line = line.overlay(list(windows))
    return serial.receive(line, rx_baud or baud, rx_fmt or fmt)


# -- UART line model ----------------------------------------------------------------------------------------
def test_uart_round_trip_and_baud_tolerance():
    for fmt in ("8N1", "8E1", "7O2", "8N2"):
        chars = _receive(SENTENCE, 115200, fmt)
        assert bytes(c["byte"] & 0x7F if fmt == "7O2" else c["byte"] for c in chars) == \
            (bytes(b & 0x7F for b in SENTENCE) if fmt == "7O2" else SENTENCE)
        assert not any(c["errors"] for c in chars)
    # a few percent of clock error is fine (the sample point drifts less than half a bit over 10 bits) ...
    assert bytes(c["byte"] for c in _receive(SENTENCE, 115200, rx_baud=115200 * 1.03)) == SENTENCE
    # ... past ~5 % the stop bit is sampled in the next character: framing errors and garbage
    bad = _receive(SENTENCE, 115200, rx_baud=115200 * 1.07)
    assert bytes(c["byte"] for c in bad) != SENTENCE and any("framing" in c["errors"] for c in bad)
    # a wrong baud rate altogether
    assert bytes(c["byte"] for c in _receive(b"hello", 115200, rx_baud=9600)) != b"hello"


def test_uart_parity_break_and_glitches():
    chars = _receive(b"A", 9600, "8N1", rx_fmt="8E1")             # 'A' has even parity bit 0; the stop bit (1) is read as parity
    assert "parity" in chars[0]["errors"]
    bit = 1 / 9600
    broken = _receive(b"\x00\x00", 9600, windows=[(0.0, 25 * bit, 0)])
    assert broken[0]["errors"] == ["break"]
    # a short spike on an idle line is not a start bit (the middle of the start bit is checked again)
    line, _ = serial.transmit(b"Z", 9600, start=10 * bit)
    assert [c["byte"] for c in serial.receive(line.overlay([(2 * bit, 2.1 * bit, 0)]), 9600)] == [ord("Z")]
    # a spike in the middle of a bit is outvoted by the 3-sample majority but raises the noise flag
    line, _ = serial.transmit(b"\xff", 9600)
    spiked = serial.receive(line.overlay([(4.5 * bit - 1e-6, 4.5 * bit + 1e-6, 0)]), 9600)
    assert spiked[0]["byte"] == 0xFF and spiked[0]["errors"] == ["noise"]


def test_nmea_checksum_and_values():
    decoded = serial.decode_nmea(SENTENCE.decode())
    assert decoded["ok"] and decoded["type"] == "GGA"
    assert decoded["values"]["lat"] == pytest.approx(48.1173, abs=1e-4) and decoded["values"]["sats"] == 8
    bad = SENTENCE.replace(b"545.4", b"545.5").decode()
    assert not serial.decode_nmea(bad)["ok"]


# -- Modbus -------------------------------------------------------------------------------------------------
def test_modbus_crc_and_request_response():
    assert serial.crc16(bytes.fromhex("01030000000A")) == bytes.fromhex("C5CD")   # the specification's example
    context = {}
    request = serial.rtu(1, bytes([3, 0, 0, 0, 2]))
    config = {"units": {1: {"name": "drive", "registers": {0: {"name": "temperature", "scale": 0.1, "unit": "degC"}}}}}
    row = serial.decode_rtu(request, config, context, "plc")
    assert row["ok"] and not row["response"] and row["type"] == "FC3 request"
    response = serial.rtu(1, bytes([3, 4, 0x01, 0x2C, 0, 7]))
    row = serial.decode_rtu(response, config, context, "drive")
    assert row["response"] and "temperature = 30 degC" in row["summary"]
    exception = serial.decode_rtu(serial.rtu(1, bytes([0x83, 2])), config, {}, "drive")
    assert exception["type"] == "exception" and "illegal data address" in exception["summary"]
    corrupted = bytearray(request)
    corrupted[3] ^= 0x10
    assert not serial.decode_rtu(bytes(corrupted), config, {}, "plc")["ok"]


# -- SPI ----------------------------------------------------------------------------------------------------
LIS3DH = {"devices": {"acc": {"name": "LIS3DH", "read_bit": "0x80", "inc_bit": "0x40",
                              "registers": {"0x0F": {"name": "WHO_AM_I", "expect": "0x33"},
                                            "0x28": {"name": ["x", "y", "z"], "format": "i16 LE", "shift": 4, "scale": 0.001, "unit": "g"}}}}}


def test_spi_register_decoder_and_bit_shifts():
    context = {}
    assert spi.decode(bytes([0x8F, 0]), "mosi", LIS3DH, context, "acc")["summary"] == "LIS3DH: read 0x0F WHO_AM_I (1 bytes)"
    assert spi.decode(bytes([0xFF, 0x33]), "miso", LIS3DH, context, "acc")["ok"]
    assert not spi.decode(bytes([0xFF, 0x66]), "miso", LIS3DH, context, "acc")["ok"]         # one bit off
    spi.decode(bytes([0xE8]) + bytes(6), "mosi", LIS3DH, context, "acc")
    xyz = struct.pack("<hhh", 100 << 4, -250 << 4, 1000 << 4)
    assert "x=0.1 g y=-0.25 g z=1 g" in spi.decode(b"\xff" + xyz, "miso", LIS3DH, context, "acc")["summary"]
    assert spi.shift_bits(b"\x33", 0, +1) == b"\x19" and spi.shift_bits(b"\x33\x80", 0, -1, fill=0) == b"\x67\x00"
    assert spi.sample_edge(0) == spi.sample_edge(3) != spi.sample_edge(1)


# -- Ethernet -----------------------------------------------------------------------------------------------
def test_ethernet_frames_checksums_and_modbus_tcp():
    hosts = {"hosts": {"192.168.0.10": "plc", "192.168.0.20": "io"}}
    udp = ethernet.frame(ethernet.BROADCAST, "02:00:00:00:00:20", 0x0800, ethernet.udp("192.168.0.20", "192.168.0.255", 5005, 5005, b'{"n": 1}'))
    assert len(udp) >= 64
    row = ethernet.decode(udp, hosts, {})
    assert row["ok"] and row["type"] == "UDP" and row["source"] == "io" and "checksum ok" in str(row["fields"])
    request = ethernet.frame("02:00:00:00:00:20", "02:00:00:00:00:10", 0x0800,
                             ethernet.tcp("192.168.0.10", "192.168.0.20", 49152, 502, 1, 1, 0x18, ethernet.mbap(7, 1, bytes([3, 0, 0, 0, 2]))))
    context = {}
    row = ethernet.decode(request, hosts, context)
    assert row["type"] == "Modbus/TCP" and "#7 FC3 Read Holding Registers 0..1" in row["summary"] and row["target"] == "io"
    reply = ethernet.frame("02:00:00:00:00:10", "02:00:00:00:00:20", 0x0800,
                           ethernet.tcp("192.168.0.20", "192.168.0.10", 502, 49152, 1, 13, 0x18, ethernet.mbap(7, 1, bytes([3, 4, 0, 5, 0, 6]))))
    assert "FC3 → 5 (0x0005), 6 (0x0006)" in ethernet.decode(reply, hosts, context)["summary"]
    arp = ethernet.decode(ethernet.frame(ethernet.BROADCAST, "02:00:00:00:00:10", 0x0806,
                                         ethernet.arp(1, "02:00:00:00:00:10", "192.168.0.10", "00:00:00:00:00:00", "192.168.0.20")), hosts, {})
    assert arp["summary"] == "who has 192.168.0.20? tell 192.168.0.10"
    bad = bytearray(udp)
    bad[30] ^= 1
    broken = ethernet.decode(bytes(bad), hosts, {})
    assert not broken["ok"] and "FCS error" in broken["problems"]
    runt = ethernet.decode(udp[:40] + ethernet.fcs(udp[:40]), hosts, {})
    assert any("runt" in p for p in runt["problems"])


# -- physical checks ----------------------------------------------------------------------------------------
def test_wired_physical_checks():
    assert rs485_check({"length_m": 1000}, 19200)["ok"]
    assert not rs485_check({"length_m": 1000}, 1e6)["ok"]                        # 10^8 rule: 100 m at 1 Mbit/s
    unterminated = rs485_check({"length_m": 300, "termination": False}, 115200)
    assert not unterminated["ok"] and unterminated["ber"] > 0
    assert rs485_check({"length_m": 5, "termination": False}, 9600)["ok"]        # short and slow: no reflections to speak of
    assert uart_check({"length_m": 15}, 19200)["ok"] and not uart_check({"length_m": 15}, 115200)["ok"]
    fast = spi_check({"length_m": 1.5}, 10e6)
    assert fast["late"] and fast["max_hz"] < 10e6 and not spi_check({"length_m": 0.1}, 4e6)["late"]
    assert spi_check({"length_m": 1.0}, 1e6)["double_clock"] > 0 == spi_check({"length_m": 1.0, "series_ohm": 33}, 1e6)["double_clock"]
    assert ethernet_check({"length_m": 90})["ok"] and not ethernet_check({"length_m": 150})["ok"]
    assert not ethernet_check({"length_m": 10, "category": "3"}, 100e6)["ok"]


# -- faults through the medium ------------------------------------------------------------------------------
def _through(spec, topic, *events):
    media = Media({topic: spec})
    out = []
    for t, payload in events:
        out += media.apply(t, topic, payload)
    return out, media.specs()[topic]["stats"]


def test_uart_and_rs485_faults():
    payload = {"proto": "uart", "data": SENTENCE.hex(), "baud": 115200, "src": "gps"}
    clean, _ = _through({"kind": "wired"}, "uart", (0.0, payload))
    assert clean[0][1] == payload
    (_, got), = _through({"faults": {"baud_error_pct": 6}}, "uart", (0.0, payload))[0]
    assert got["data"] != payload["data"] and got["errors"] and got["medium"]["original"] == payload["data"]
    out, stats = _through({"faults": {"overrun": {"busy_us": 2000, "period_ms": 10, "fifo": 1}}}, "uart", (0.0, payload))
    assert len(bytes.fromhex(out[0][1]["data"])) < len(SENTENCE) and stats["overrun_lost"] > 0
    request = {"proto": "rs485", "data": serial.rtu(1, bytes([3, 0, 0, 0, 4])).hex(), "baud": 19200, "format": "8E1", "src": "plc"}
    answer = {"proto": "rs485", "data": serial.rtu(1, bytes([3, 8]) + bytes(8)).hex(), "baud": 19200, "format": "8E1", "src": "drive"}
    after = 8 * serial.char_time(19200, "8E1") + 0.0025
    out, stats = _through({"faults": {"de_hold_us": 3000}}, "rs485", (0.0, request), (after, answer))
    assert stats["collisions"] == 1 and out[-1][1]["data"] != answer["data"]
    out, _ = _through({"faults": {"de_hold_us": 1000}}, "rs485", (0.0, request), (after, answer))
    assert out[-1][1] == answer                                                   # released in time
    out, _ = _through({"faults": {"char_gap_us": 4000}}, "rs485", (0.0, request))
    assert len(out) > 1                                                           # RTU: idle > 1.5 characters splits the frame
    out, stats = _through({"faults": {"ground_offset_v": 15}}, "rs485", (0.0, request))
    assert out[0][1]["medium"]["lost"].startswith("ground offset") and stats["lost"] == 1


def test_spi_i2c_and_ethernet_faults():
    mosi = {"proto": "spi", "dir": "mosi", "data": "8f00", "cs": "acc", "mode": 0, "hz": 4e6, "src": "mcu"}
    miso = {"proto": "spi", "dir": "miso", "data": "ff33", "cs": "acc", "mode": 0, "src": "acc"}
    out, _ = _through({"faults": {"slave_mode": 1}}, "spi", (0.0, mosi), (0.0, miso))
    assert out[0][1]["data"] == "1e01" and out[1][1]["data"] == "ff99"           # every bit one clock off
    out, _ = _through({"faults": {"slave_mode": 3}}, "spi", (0.0, mosi), (0.0, miso))
    assert out[1][1] == miso                                                      # modes 0 and 3 sample on the same edge
    out, _ = _through({"cable": {"length_m": 10.0, "series_ohm": 33}}, "spi", (0.0, mosi), (0.0, miso))
    assert out[1][1]["data"] == "ff99"                                            # MISO too late over 10 m at 4 MHz
    out, _ = _through({"faults": {"stuck": [{"start": 0.1, "end": 0.2}]}}, "i2c",
                      (0.05, {"proto": "i2c", "dir": "m2s", "data": "9100"}), (0.15, {"proto": "i2c", "dir": "m2s", "data": "9100"}))
    assert "medium" not in out[0][1] and "bus stuck" in out[1][1]["medium"]["lost"]
    out, _ = _through({"faults": {"address_conflict": "0x0F"}}, "i2c", (0.0, {"proto": "i2c", "dir": "s2m", "data": "19f0"}))
    assert out[0][1]["data"] == "0900"
    frame = ethernet.frame(ethernet.BROADCAST, "02:00:00:00:00:20", 0x0800, ethernet.udp("192.168.0.20", "192.168.0.255", 1, 1, bytes(200))).hex()
    duration = (len(frame) // 2 + 8) * 8 / 10e6
    plc = {"proto": "eth", "data": frame, "src": "plc", "speed": 10e6}
    io = {"proto": "eth", "data": frame, "src": "io", "speed": 10e6}
    out, stats = _through({"faults": {"duplex_mismatch": "plc"}}, "eth", (0.0, plc), (duration / 2, io))
    assert "late collision" in out[1][1]["medium"]["lost"] and stats["late_collisions"] == 1
    out, stats = _through({"faults": {"duplex_mismatch": "plc"}}, "eth", (0.0, io), (duration / 2, plc))
    assert out[1][0] == pytest.approx(duration + 96 / 10e6) and stats["deferred"] == 1   # the half side waits for the line
    out, _ = _through({"faults": {"link_down": [{"start": 0.0, "end": 1.0}]}}, "eth", (0.5, plc))
    assert out[0][1]["medium"]["lost"] == "link down: no carrier"
    out, stats = _through({"bit_error_rate": 1e-3, "seed": 3}, "eth", *[(i * 0.001, plc) for i in range(20)])
    assert stats["fcs_errors"] > 0 and all("FCS" in o[1]["medium"]["lost"] for o in out if o[1].get("medium", {}).get("flipped"))


# -- the wired lab ------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def wired_run():
    raw = yaml.safe_load(WIRED_LAB.read_text())
    project = MechatronicProject.from_mapping(raw, WIRED_LAB.parent)
    orchestrator = CoSimulationOrchestrator(project, build_adapters(project))
    rows = orchestrator.run()
    config = raw["metadata"]["analyzer"]
    events = sorted((e for r in rows for e in r.get("__events__", []) if e["topic"] in config["topics"]), key=lambda e: e["time_seconds"])
    return rows, events, decode_events(events, config), orchestrator


def test_wired_lab_breaks_each_bus_inside_its_fault_window(wired_run):
    rows, _, packets, orchestrator = wired_run
    last = rows[-1]
    assert last["nav.good"] > 10 and last["nav.checksum_errors"] > 0 and last["nav.uart_errors"] > 0
    assert last["plc.good"] > 10 and last["plc.timeouts"] > 0 and last["drive.rx_crc_errors"] > 0
    assert last["mcu.polls"] > 100 and last["mcu.id_errors"] > 0 and last["mcu.no_answer"] > 0
    assert last["i2c.reads"] > 100 and last["i2c.nacks"] > 0
    assert last["eth.connected"] == 1 and last["eth.good"] > 100 and last["eth.retransmissions"] > 0
    assert abs(last["plc.temperature"] - 45.0) < 20 and abs(last["eth.value"] - 3.0) < 2
    for proto in ("UART", "Modbus", "SPI", "I2C"):                   # clean before the first fault window
        assert all(p["ok"] for p in packets if p["proto"] == proto and p["time"] < 0.25), proto
        assert any(not p["ok"] or p.get("impaired") for p in packets if p["proto"] == proto and p["time"] >= 0.25), proto
    stats = {topic: m["stats"] for topic, m in orchestrator.media.specs().items()}
    assert stats["uart"]["framing"] > 0 and stats["uart"]["overrun_lost"] > 0
    assert stats["rs485"]["collisions"] > 0 and stats["i2c"]["bus_stuck"] > 0
    assert stats["eth"]["link_down"] > 0 and stats["eth"]["late_collisions"] + stats["eth"]["collisions"] > 0
    kinds = {p["proto"]: set() for p in packets}
    for p in packets:
        kinds[p["proto"]].add(p["type"])
    assert {"ARP", "UDP", "TCP", "Modbus/TCP"} <= kinds["Ethernet"] and {"GGA", "RMC"} <= kinds["UART"]
    assert {"FC3 request", "FC3 response", "FC6 request"} <= kinds["Modbus"]


@pytest.fixture(scope="module")
def wired_captures(tmp_path_factory, wired_run):
    _, events, _, _ = wired_run
    out = tmp_path_factory.mktemp("wired-pcap")
    files = {proto: out / f"{proto}.pcap" for proto in ("eth", "uart", "spi", "rs485")}
    counts = {proto: write_pcap(events, path, proto) for proto, path in files.items()}
    return events, files, counts


def test_wired_pcap_round_trip(wired_captures):
    events, files, counts = wired_captures
    assert all(n > 10 for n in counts.values()), counts
    for proto, path in files.items():
        back = read_capture(path)
        original = sorted((e for e in events if e["payload"].get("proto") == proto), key=lambda e: e["time_seconds"])
        assert [e["payload"]["data"] for e in back] == [e["payload"]["data"] for e in original], proto
        if proto == "spi":
            assert [e["payload"]["dir"] for e in back] == [e["payload"]["dir"] for e in original]


@pytest.mark.skipif(not TSHARK, reason="set TSHARK (or put tshark on PATH) to check the captures with Wireshark")
def test_wireshark_agrees_on_wired_checksums(wired_captures):
    events, files, counts = wired_captures
    for proto, path in files.items():
        malformed = subprocess.run([TSHARK, "-r", str(path), "-Y", "_ws.malformed"], capture_output=True, text=True, timeout=120)
        assert malformed.returncode == 0 and malformed.stdout.strip() == "", f"{proto}: {malformed.stdout[:300]}"
    checks = ["-o", "eth.check_fcs:TRUE", "-o", "ip.check_checksum:TRUE", "-o", "udp.check_checksum:TRUE", "-o", "tcp.check_checksum:TRUE"]
    fields = subprocess.run([TSHARK, "-r", str(files["eth"]), *checks, "-T", "fields", "-e", "eth.fcs.status", "-e", "ip.checksum.status",
                             "-e", "udp.checksum.status", "-e", "tcp.checksum.status"], capture_output=True, text=True, timeout=120).stdout
    statuses = [line.split("\t") for line in fields.splitlines()]
    assert len(statuses) == counts["eth"]
    ours = [e for e in sorted(events, key=lambda e: e["time_seconds"]) if e["payload"].get("proto") == "eth"]
    for status, event in zip(statuses, ours):                       # Wireshark's FCS verdict is ours
        data = bytes.fromhex(event["payload"]["data"])
        assert status[0] == ("1" if ethernet.fcs(data[:-4]) == data[-4:] else "0")
        if status[0] == "1":
            assert all(s in ("", "1") for s in status[1:])          # good IP, UDP and TCP checksums
    user = 'uat:user_dlts:"User 3 (DLT=150)","mbrtu","0","","0",""'
    crc = subprocess.run([TSHARK, "-r", str(files["rs485"]), "-o", user, "-o", "mbrtu.crc_verification:TRUE", "-T", "fields",
                          "-e", "mbrtu.crc16.status"], capture_output=True, text=True, timeout=120).stdout.splitlines()
    ours = [e for e in sorted(events, key=lambda e: e["time_seconds"]) if e["payload"].get("proto") == "rs485"]
    compared = 0
    for status, event in zip(crc, ours):
        data = bytes.fromhex(event["payload"]["data"])
        if status in ("0", "1") and len(data) >= 4:
            assert status == ("1" if serial.crc16(data[:-2]) == data[-2:] else "0"), data.hex()
            compared += 1
    assert compared > 50 and "1" in crc and "0" in crc


# -- capture reader fixes -----------------------------------------------------------------------------------
def _pcap(link, *packets):
    out = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, link)
    for i, data in enumerate(packets):
        out += struct.pack("<IIII", 1, i, len(data), len(data)) + data
    return out


def test_capture_reader_fixes(tmp_path):
    from embeddyn.mechatronics.comms import nfc, wifi
    from embeddyn.mechatronics.comms.captures import read_candump, read_pcap
    fd = read_candump("(1.000000) can0 123##1DEADBEEF\n  can0  1F334455  [12]  00 01 02 03 04 05 06 07 08 09 0A 0B\n")
    assert fd[0]["payload"] == {"proto": "can", "id": 0x123, "data": "deadbeef", "src": "can0", "fd": True}
    assert fd[1]["payload"]["data"] == bytes(range(12)).hex() and fd[1]["payload"]["fd"] and fd[1]["payload"]["ext"]
    phdr = lambda rf, flags: struct.pack("<BbbBIH", rf, -60, -100, 0, 0x8E89BED6, flags) + b"\xd6\xbe\x89\x8e\x00\x00"  # noqa: E731
    ble = read_pcap(_pcap(256, phdr(5, 0x0001), phdr(12, 0x0003), phdr(20, 0x0003)))
    assert [e["payload"]["channel"] for e in ble] == [4, 38, 18] and [e["payload"]["rssi"] for e in ble] == [None, -60, -60]
    body = wifi.frame(0, 8, wifi.BROADCAST, "02:00:00:00:00:01", "02:00:00:00:00:01", bytes(12))
    bare = body[:-4] if wifi.fcs(body[:-4]) == body[-4:] else body
    radiotap = struct.pack("<BBHI", 0, 0, 8, 0)                                    # no flags field: FCS unknown
    frames = read_pcap(_pcap(127, radiotap + bare, radiotap + bare + wifi.fcs(bare)))
    assert frames[0]["payload"]["data"] == frames[1]["payload"]["data"] == (bare + wifi.fcs(bare)).hex()
    assert read_pcap(_pcap(105, bare))[0]["payload"]["data"] == (bare + wifi.fcs(bare)).hex()
    dropped = read_pcap(_pcap(264, struct.pack(">BBH", 0, 0xFD, 4) + b"\x04\xa2\x24\x4a", struct.pack(">BBH", 0, 0xFC, 1) + b"\x26"))
    assert dropped[0]["payload"] == {"proto": "nfc", "dir": "picc", "data": nfc.with_crc(b"\x04\xa2\x24\x4a").hex(), "src": "capture"}
    assert dropped[1]["payload"]["bits"] == 7 and dropped[1]["payload"]["data"] == "26"
