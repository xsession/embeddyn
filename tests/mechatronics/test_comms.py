"""Multi-board communication: CAN bus, CANopen stack, BLE and 802.11 frames, the analyzer and event delivery."""
import io
import json
import struct
import threading
import time
import urllib.request
from pathlib import Path

import pytest
import yaml

from embeddyn.mechatronics.comms import ble, can, canopen, wifi
from embeddyn.mechatronics.comms.analyzer import decode_event, decode_events, write_pcap
from embeddyn.mechatronics.comms.boards import AxisNode, CanopenMaster, SensorNode, status_record
from embeddyn.mechatronics.factory import build_adapters
from embeddyn.mechatronics.gui.document import load_graph
from embeddyn.mechatronics.gui.server import serve
from embeddyn.mechatronics.model import MechatronicProject
from embeddyn.mechatronics.orchestrator import CoSimulationOrchestrator

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples/mechatronics/multiboard-canopen-ble.yaml"


# -- CAN -------------------------------------------------------------------------------------------
def test_frame_bits_and_crc():
    # 0 data bytes, id 0: 19 bits before CRC, CRC 15, + stuff bits, + 13 trailer bits + 3 IFS
    assert can.frame_bits(0x7FF, b"") > can.frame_bits(0x555, b"") - 5
    eight = can.frame_bits(0x123, bytes(8))
    assert 111 + 3 <= eight <= 135 + 3                     # the classic 8-byte frame range, intermission included
    assert can.frame_bits(0x123, bytes(8), extended=True) > eight
    assert can.crc15([1, 0, 1, 1]) != can.crc15([1, 0, 1, 0])


def test_bus_arbitrates_lowest_id_first_and_serialises():
    bus = can.CanBus(bitrate=500_000)
    for frame_id in (0x300, 0x080, 0x182):
        bus.receive(0.0, "tx", {"proto": "can", "id": frame_id, "data": "0011223344556677", "src": "x"})
    bus.advance(0.0, 0.001, {})
    wire = bus.pop_events()
    assert [p["id"] for _, _, p in wire] == [0x080, 0x182, 0x300]
    ends = [t for t, _, _ in wire]
    starts = [p["start"] for _, _, p in wire]
    assert starts[1] == pytest.approx(ends[0]) and starts[2] == pytest.approx(ends[1])
    assert bus.output(0.001, [], {})["load"] == pytest.approx(ends[-1] / 0.001)


# -- CANopen ---------------------------------------------------------------------------------------
def _frames(node, port="can_tx"):
    return [p for _, name, p in node.pop_events() if name == port]


def test_canopen_node_boot_nmt_sdo_pdo():
    axis = AxisNode(node_id=2, boot_delay=0.001)
    axis.advance(0.0, 0.002, {})
    (boot,) = _frames(axis)
    assert boot["id"] == 0x702 and boot["data"] == "00"
    # expedited SDO upload of the device type
    axis.receive(0.003, "can_rx", {"id": 0x602, "data": "4000100000000000"})
    (reply,) = _frames(axis)
    assert reply["id"] == 0x582 and bytes.fromhex(reply["data"])[0] == 0x43
    assert struct.unpack_from("<I", bytes.fromhex(reply["data"]), 4)[0] == AxisNode.device_type
    # segmented upload of the device name
    axis.receive(0.004, "can_rx", {"id": 0x602, "data": "4008100000000000"})
    (initiate,) = _frames(axis)
    size = struct.unpack_from("<I", bytes.fromhex(initiate["data"]), 4)[0]
    text, toggle = b"", 0
    while True:
        axis.receive(0.005, "can_rx", {"id": 0x602, "data": bytes([0x60 | toggle << 4]).hex() + "00" * 7})
        (segment,) = _frames(axis)
        data = bytes.fromhex(segment["data"])
        text += data[1:1 + 7 - ((data[0] >> 1) & 7)]
        toggle ^= 1
        if data[0] & 1:
            break
    assert text.decode() == AxisNode.device_name and len(text) == size
    # unknown object -> abort
    axis.receive(0.006, "can_rx", {"id": 0x602, "data": "4034120000000000"})
    (abort,) = _frames(axis)
    assert bytes.fromhex(abort["data"])[0] == 0x80 and struct.unpack_from("<I", bytes.fromhex(abort["data"]), 4)[0] == 0x06020000
    # NMT start, RPDO set-point, SYNC -> TPDO1 with status and position
    axis.receive(0.007, "can_rx", {"id": 0x000, "data": "0102"})
    assert axis.state == 0x05
    axis.receive(0.008, "can_rx", {"id": 0x202, "data": struct.pack("<iH", 10_000, 0x0F).hex()})
    axis.advance(0.008, 0.3, {})
    _frames(axis)
    axis.receive(0.3, "can_rx", {"id": 0x080, "data": ""})
    tpdo = [f for f in _frames(axis) if f["id"] == 0x182]
    status, position = struct.unpack("<Hi", bytes.fromhex(tpdo[0]["data"]))
    assert status & 0x0400 and position == pytest.approx(10_000, abs=50)


def test_canopen_decoder():
    assert canopen.decode(0x000, bytes([1, 0]))["summary"] == "NMT start → all nodes"
    assert canopen.decode(0x703, b"\x05", {"nodes": {3: "sensor"}})["summary"] == "sensor: heartbeat operational"
    emcy = canopen.decode(0x083, bytes.fromhex("1042090000000000"))
    assert emcy["type"] == "EMCY" and "over temperature" in emcy["summary"]
    sdo = canopen.decode(0x602, bytes.fromhex("2b17100064000000"))
    assert sdo["summary"].startswith("SDO write 0x1017:00 = 100")
    config = {"pdos": {"0x182": {"name": "axis TPDO1", "fields": [{"name": "status", "format": "u16"},
                                                                  {"name": "position", "format": "i32", "scale": 0.001, "unit": "mm"}]}}}
    pdo = canopen.decode(0x182, struct.pack("<Hi", 0x237, 12_500), config)
    assert pdo["fields"][1]["value"] == "12.5 mm" and pdo["fields"][1]["bytes"] == [2, 6]


def test_master_brings_up_a_network_over_the_bus():
    bus = can.CanBus()
    master, axis, sensor = CanopenMaster(nodes=(2, 3)), AxisNode(), SensorNode()
    boards = [master, axis, sensor]
    t, h = 0.0, 0.0005
    for _ in range(400):                                    # 0.2 s, the bus delivers one step later
        for board in boards:
            board.advance(t, t + h, {"target_mm": 50.0, "distance_mm": 300.0})
        bus_in = [p for b in boards for _, _, p in b.pop_events()]
        for frame in bus_in:
            bus.receive(t + h, "tx", frame)
        bus.advance(t + h, t + 2 * h, {})
        for when, _, frame in bus.pop_events():
            for board in boards:
                board.receive(when, "can_rx", frame)
        t += h
    assert master.phase == "run" and axis.state == 0x05 and sensor.state == 0x05
    assert master.info[2]["0x1008"] == AxisNode.device_name and master.info[3]["0x1000"] == SensorNode.device_type
    assert master.output(t, [], {})["nodes_operational"] == 2
    assert master.position_mm > 1.0                         # the axis follows the set-point it got over RPDO


# -- BLE and 802.11 -----------------------------------------------------------------------------------
def test_ble_packet_round_trip_and_crc():
    adv = ble.ad(0x01, b"\x06") + ble.ad(0x09, b"EMB-GW") + ble.ad(0xFF, status_record(1, 0x0F, -1234, 655, 400, 1, 7))
    packet = ble.adv_packet("C0:FF:EE:00:00:04", adv)
    assert packet[:4] == struct.pack("<I", 0x8E89BED6) and packet[5] == 6 + len(adv)
    config = {"manufacturer": {"0xFFFF": {"name": "status", "fields": [
        {"name": "version", "format": "u8"}, {"name": "nodes", "format": "u8"},
        {"name": "position", "format": "i32", "scale": 0.001, "unit": "mm"}, {"name": "temperature", "format": "i16", "scale": 0.1}]}}}
    decoded = ble.decode(packet, config)
    assert decoded["crc_ok"] and decoded["source"] == "C0:FF:EE:00:00:04" and "EMB-GW" in decoded["summary"]
    assert "position=-1.234 mm" in decoded["summary"] and "temperature=65.5" in decoded["summary"]
    corrupt = bytearray(packet)
    corrupt[10] ^= 0x01
    assert not ble.decode(bytes(corrupt))["crc_ok"]
    with pytest.raises(ValueError):
        ble.adv_packet("C0:FF:EE:00:00:04", bytes(32))


def test_wifi_frames_decode():
    beacon = wifi.beacon("02:00:00:00:00:01", "lab", 6, 3, 102400)
    decoded = wifi.decode(beacon, {"stations": {"02:00:00:00:00:01": "ap"}})
    assert decoded["fcs_ok"] and decoded["type"] == "Beacon" and "SSID='lab'" in decoded["summary"] and decoded["source"] == "ap"
    payload = json.dumps({"temp_c": 61.5}).encode()
    data = wifi.frame(2, 0, "02:00:00:00:00:01", "24:0A:C4:00:00:05", "02:00:00:00:00:01",
                      wifi.llc_snap(0x0800, wifi.ipv4_udp("192.168.4.2", "192.168.4.1", 40000, 5000, payload)), to_ds=True)
    decoded = wifi.decode(data)
    assert "192.168.4.2 → 192.168.4.1 UDP 40000→5000 temp_c=61.5" in decoded["summary"]
    ip = next(f for f in decoded["fields"] if f["label"] == "IPv4")
    assert "ok" in ip["children"][2]["value"]
    assert wifi.decode(wifi.frame(1, 13, "24:0A:C4:00:00:05"))["type"] == "ACK"
    assert not wifi.decode(data[:-1] + bytes([data[-1] ^ 1]))["fcs_ok"]


# -- the example ----------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def run():
    project = MechatronicProject.load(EXAMPLE)
    rows = CoSimulationOrchestrator(project, build_adapters(project)).run()
    config = yaml.safe_load(EXAMPLE.read_text())["metadata"]["analyzer"]
    events = [e for r in rows for e in r.get("__events__", []) if e["topic"] in config["topics"]]
    return rows, events, config


def test_example_network_comes_up_and_reports(run):
    rows, events, config = run
    last = rows[-1]
    assert last["master.nmt_state"] == 5 and last["master.nodes_operational"] == 3
    assert last["sensor.overtemp"] == 1.0
    assert abs(last["master.axis_position_mm"] - last["axis.position_mm"]) < 5.0     # the master sees the axis over PDO
    assert last["bridge.wifi_state"] == 4 and last["ap.stations"] == 1 and last["bridge.udp_sent"] >= 5
    assert last["bridge.temperature_c"] == pytest.approx(last["sensor.temperature_c"], abs=1.0)  # BLE -> Wi-Fi carries it
    assert 0.01 < max(r["can.load_avg"] for r in rows[200:]) < 0.2


def test_example_traffic_decodes(run):
    _, events, config = run
    packets = decode_events(events, config)
    kinds = {(p["proto"], p["type"]) for p in packets}
    assert {("CANopen", "NMT"), ("CANopen", "SYNC"), ("CANopen", "SDO"), ("CANopen", "EMCY"), ("CANopen", "TPDO1"),
            ("CANopen", "heartbeat"), ("BLE", "ADV_NONCONN_IND"), ("802.11", "Beacon"), ("802.11", "Probe Request"),
            ("802.11", "Association Response"), ("802.11", "Data"), ("802.11", "ACK")} <= kinds
    assert all(p["ok"] for p in packets)
    assert {p["channel"] for p in packets if p["proto"] == "BLE"} == {37, 38, 39}
    names = [p["summary"] for p in packets if p["type"] == "SDO" and "segment" in p["summary"] and "(last)" in p["summary"]]
    assert len(names) == 3                                 # one segmented device-name upload per node
    response = next(p for p in packets if p["type"] == "SDO" and p["source"] == "axis")
    assert response["target"] == "master"


def test_pcap_files(run, tmp_path):
    _, events, _ = run
    counts = {proto: write_pcap(events, tmp_path / f"{proto}.pcap", proto) for proto in ("can", "ble", "wifi")}
    assert counts["can"] > 500 and counts["ble"] > 50 and counts["wifi"] > 20
    data = (tmp_path / "can.pcap").read_bytes()
    magic, major, minor, _, _, snap, link = struct.unpack_from("<IHHiIII", data)
    assert (magic, major, minor, link) == (0xA1B23C4D, 2, 4, 227)
    seconds, nanos, caplen, length = struct.unpack_from("<IIII", data, 24)
    assert caplen == length == 16                          # SocketCAN can_frame
    buffer = io.BytesIO()
    assert write_pcap(events, buffer, "ble") == counts["ble"]
    assert struct.unpack_from("<I", buffer.getvalue(), 20)[0] == 251
    with pytest.raises(ValueError):
        write_pcap(events, buffer, "usb")


def test_renode_style_can_events_decode():
    row = decode_event({"time_seconds": 0.1, "topic": "mcu.can", "payload": {"id": 0x101, "data": "0102"}})
    assert row["proto"] == "CAN" and row["summary"] == "0x101 [2] 01 02"


# -- event delivery ------------------------------------------------------------------------------------
def test_instances_receive_only_others_events_on_subscribed_topics(run):
    rows, events, _ = run
    sources = {e["topic"]: set() for e in events}
    for e in events:
        sources[e["topic"]].add(e["source"])
    assert set(sources) == {"can", "ble", "wifi"}


# -- HTTP API --------------------------------------------------------------------------------------------
@pytest.fixture
def lab():
    server = serve(None, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.lab_state.stop_all()
    server.server_close()


def _post(base, path, body):
    request = urllib.request.Request(base + "/api/" + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def test_http_analyzer_and_pcap(lab):
    graph = load_graph(EXAMPLE)
    info = _post(lab, "run", {"graph": graph, "duration": 0.3})
    for _ in range(200):
        status = json.loads(urllib.request.urlopen(f"{lab}/api/session?session={info['session']}", timeout=10).read())
        if status["status"] in ("done", "error"):
            break
        time.sleep(0.1)
    assert status["status"] == "done"
    first = json.loads(urllib.request.urlopen(f"{lab}/api/analyzer?session={info['session']}&from=0", timeout=10).read())
    assert first["total"] == len(first["packets"]) > 100 and first["pending"] == 0
    times = [p["time"] for p in first["packets"]]
    assert times == sorted(times) and [p["no"] for p in first["packets"]] == list(range(1, len(times) + 1))
    more = json.loads(urllib.request.urlopen(f"{lab}/api/analyzer?session={info['session']}&from=50", timeout=10).read())
    assert more["packets"][0]["no"] == 51
    pcap = urllib.request.urlopen(f"{lab}/api/pcap?session={info['session']}&proto=wifi", timeout=10).read()
    assert struct.unpack_from("<I", pcap, 20)[0] == 105 and len(pcap) > 24
