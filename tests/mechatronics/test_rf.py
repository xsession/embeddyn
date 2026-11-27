"""NFC, RFID, LoRaWAN, BLE/Wi-Fi decoder details, the RF lab example and impaired media."""
import json
import struct
import threading
import time
import urllib.request
from pathlib import Path

import pytest
import yaml

from embeddyn.mechatronics.comms import ble, lora, nfc, rfid, wifi
from embeddyn.mechatronics.comms.analyzer import decode_events
from embeddyn.mechatronics.comms.medium import Media, Medium, _modulation
from embeddyn.mechatronics.factory import build_adapters
from embeddyn.mechatronics.gui.document import load_graph
from embeddyn.mechatronics.gui.server import serve
from embeddyn.mechatronics.model import MechatronicProject
from embeddyn.mechatronics.orchestrator import CoSimulationOrchestrator

ROOT = Path(__file__).resolve().parents[2]
RF_LAB = ROOT / "examples/mechatronics/rf-lab.yaml"
MULTIBOARD = ROOT / "examples/mechatronics/multiboard-canopen-ble.yaml"


def _run(path, media=None, duration=None):
    raw = yaml.safe_load(path.read_text())
    if media is not None:
        raw["media"] = media
    if duration:
        raw["duration_seconds"] = duration
    project = MechatronicProject.from_mapping(raw, path.parent)
    orchestrator = CoSimulationOrchestrator(project, build_adapters(project))
    rows = orchestrator.run()
    config = raw["metadata"]["analyzer"]
    events = sorted((e for r in rows for e in r.get("__events__", []) if e["topic"] in config["topics"]),
                    key=lambda e: e["time_seconds"])
    return rows, decode_events(events, config), orchestrator


# -- codecs against published values ---------------------------------------------------------------------------
def test_nfc_crc_ndef_and_exchange():
    assert nfc.crc_a(b"\x50\x00") == b"\x57\xcd"                    # HLTA, ISO 14443-3 example
    uid = bytes.fromhex("04A2244A8B3C80")
    memory = nfc.type2_memory(uid, nfc.ndef_uri("https://embeddyn.dev/x") + b"")
    (record,) = nfc.ndef_from_memory(memory)
    assert record["value"] == "https://embeddyn.dev/x"
    assert nfc.parse_ndef(nfc.ndef_text("hello", "hu"))[0] == {"tnf": 1, "type": "T", "payload": nfc.ndef_text("hello", "hu")[4:].hex(),
                                                               "lang": "hu", "value": "hello"}
    ctx = {}
    assert nfc.decode(b"\x26", "pcd", 7, ctx)["type"] == "REQA"
    assert "double" in nfc.decode(b"\x44\x00", "picc", None, ctx)["summary"]
    nfc.decode(nfc.with_crc(b"\x30\x04"), "pcd", None, ctx)
    page4 = nfc.decode(nfc.with_crc(memory[16:32]), "picc", None, ctx)
    assert page4["type"] == "READ data" and page4["crc_ok"]


def test_rfid_em4100_and_gen2():
    bits = rfid.em4100_bits(0x1A, 0x00B4C7E2)
    decoded = rfid.decode_em4100(bits)
    assert decoded["ok"] and decoded["id"] == "1A00B4C7E2" and "(180,51170)" in decoded["summary"]
    broken = bits[:20] + ("0" if bits[20] == "1" else "1") + bits[21:]
    assert not rfid.decode_em4100(broken)["ok"]
    assert rfid.decode_em4100("0" * 13 + bits)["ok"]                  # finds the header in a stream
    query = rfid.gen2_query(3, session=1)
    assert len(query) == 22 and rfid.decode_gen2(query, "reader")["summary"].startswith("Query Q=3 (8 slots) S1")
    reply = rfid.gen2_epc_reply(bytes.fromhex("E28011606000020A1B2C3D01"))
    assert rfid.decode_gen2(reply, "tag", {"last": "ACK"})["epc"] == "E28011606000020A1B2C3D01"
    assert not rfid.decode_gen2(reply[:-1] + ("0" if reply[-1] == "1" else "1"), "tag", {"last": "ACK"})["ok"]


def test_lorawan_reference_frame_and_airtime():
    # Published example (lora-packet): "test" on port 1, NwkSKey/AppSKey below.
    frame = bytes.fromhex("40F17DBE4900020001954378762B11FF0D")
    keys = {"devices": {"49BE7DF1": {"nwkskey": "44024241ed4ce9a68c6a8bc055233fd3",
                                     "appskey": "ec925802ae430ca77fd3dd73cb2cc588"}}}
    decoded = lora.decode(frame, {}, keys)
    assert decoded["ok"] and decoded["summary"].startswith("Unconfirmed Data Up 49BE7DF1 FCnt 2 port 1")
    payload = next(f for f in decoded["fields"] if f["label"] == "FRMPayload")["children"][0]["value"]
    assert bytes.fromhex(payload.replace(" ", "")) == b"test"
    wrong = {"devices": {"49BE7DF1": {"nwkskey": "00" * 16, "appskey": "00" * 16}}}
    assert not lora.decode(frame, {}, wrong)["ok"]
    assert lora.time_on_air(13, 7, 125) == pytest.approx(0.046336, abs=1e-5)
    assert lora.time_on_air(13, 12, 125) > 1.0
    # our own frames round-trip
    nwk, app = bytes(range(16)), bytes(range(16, 32))
    built = lora.data_frame(0x26011BDA, 7, 2, b"\x01\x02", nwk, app, confirmed=True, fopts=b"\x03\x07")
    again = lora.decode(built, {}, {"devices": {"26011BDA": {"nwkskey": nwk.hex(), "appskey": app.hex()}}})
    assert again["ok"] and again["type"] == "Confirmed Data Up"


def test_ble_and_wifi_details():
    connect = ble.decode(_connect_ind())
    assert connect["type"] == "CONNECT_IND" and "interval 30 ms" in connect["summary"] and connect["crc_ok"]
    ibeacon = ble.ad(0x01, b"\x06") + ble.ad(0xFF, b"\x4c\x00\x02\x15" + bytes(16) + struct.pack(">HHb", 7, 42, -59))
    assert "iBeacon major 7 minor 42" in ble.decode(ble.adv_packet("D4:CA:6E:00:00:B1", ibeacon))["summary"]
    tlm = ble.ad(0x03, b"\xaa\xfe") + ble.ad(0x16, b"\xaa\xfe\x20\x00" + struct.pack(">HhII", 2950, 23 * 256, 5, 100))
    assert "Eddystone-TLM battery 2950 mV, 23.0 degC" in ble.decode(ble.adv_packet("D4:CA:6E:00:00:B1", tlm))["summary"]
    for message in (1, 2, 3, 4):
        frame = wifi.frame(2, 8, "3C:71:BF:00:00:77", "02:11:22:33:44:01", "02:11:22:33:44:01",
                           b"\x00\x00" + wifi.llc_snap(0x888E, wifi.eapol_key(message, message, bytes([message]) * 32 if message != 4 else bytes(32))),
                           from_ds=True)
        assert f"EAPOL-Key message {message}/4" in wifi.decode(frame)["summary"]
    deauth = wifi.frame(0, 12, "3C:71:BF:00:00:77", "02:11:22:33:44:01", "02:11:22:33:44:01", b"\x07\x00")
    assert "reason 7" in wifi.decode(deauth)["summary"]
    rsn = wifi.beacon("02:11:22:33:44:01", "lab", 11, 1, 0, rsn=bytes.fromhex("0100000fac040100000fac040100000fac020000"))
    assert "AKM PSK" in wifi.decode(rsn)["summary"]


def _connect_ind():
    ll = struct.pack("<I", 0x50654C5A) + b"\x55\x55\x55" + bytes([2]) + struct.pack("<HHHH", 4, 24, 0, 200) + b"\xff" * 4 + b"\x1f" + b"\xa5"
    body = ble.mac_bytes("5A:3B:00:00:00:F0") + ble.mac_bytes("C0:98:E5:00:00:5E") + ll
    pdu = bytes([0x05 | 0x40 | 0x80, len(body)]) + body
    return struct.pack("<I", ble.ADV_ACCESS_ADDRESS) + pdu + ble.crc24(pdu)


# -- the RF lab ---------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def rf_run():
    return _run(RF_LAB)


def test_rf_lab_every_radio_works(rf_run):
    rows, packets, _ = rf_run
    last = rows[-1]
    assert last["nfc.tags_read"] >= 1 and last["rfid.uhf_tags_seen"] == 3 and last["rfid.em4100_frames"] > 5
    assert last["lora.uplinks"] == last["lora.fcnt"] and last["lora.acks"] >= 1
    assert last["ble.sensor_connected"] == 1 and last["ble.scan_responses"] >= 3
    assert last["wifi.joins"] == 2 and last["wifi.handshakes"] == 2 and last["wifi.deauth_sent"] == 1
    summaries = [p["summary"] for p in packets]
    assert any("NDEF https://embeddyn.dev/rf-lab" in s for s in summaries)
    assert any(s.startswith("EM4100 card 0x1A00B4C7E2") for s in summaries)
    assert {p.get("epc") for p in packets} >= set() and sum(1 for p in packets if p["type"] == "EPC") >= 3
    assert any(p["type"] == "collision" for p in packets)
    assert any("temperature=" in s and "battery=" in s for s in summaries)          # LoRaWAN decrypted with the keys
    assert any("LinkADRReq" in json.dumps(p["fields"]) for p in packets if p["proto"] == "LoRa")
    assert {f"EAPOL-Key message {m}/4" for m in (1, 2, 3, 4)} <= {s.split("QoS Data ")[-1].split(" (")[0] for s in summaries}
    assert any("Deauthentication reason 7" in s for s in summaries)
    assert any(p["type"] == "CONNECT_IND" for p in packets) and any("iBeacon" in s for s in summaries)
    assert all(p["ok"] for p in packets)


# -- impaired media ---------------------------------------------------------------------------------------------------
def test_medium_strength_and_bit_errors():
    medium = Medium("can", {"bit_error_rate": 1e-3, "loss": 0.2, "bursts": [{"start": 1.0, "end": 2.0, "bit_error_rate": 0.1}],
                            "modulation": {"shape": "square", "frequency": 1.0, "depth": 1.0}})
    assert medium.strength(0.25) == (pytest.approx(1e-3), pytest.approx(0.2))         # square high
    assert medium.strength(0.75) == (0.0, 0.0)                                          # square low
    assert medium.strength(1.25)[0] == pytest.approx(0.1)
    assert _modulation({"shape": "sine", "frequency": 1, "depth": 0.5}, 0.25) == pytest.approx(1.0)
    media = Media({"x": {"bit_error_rate": 0.01, "seed": 3}})
    flipped = 0
    for i in range(2000):
        ((_, out),) = media.apply(i * 1e-3, "x", {"proto": "ble", "data": "00" * 32})
        flipped += len((out.get("medium") or {}).get("flipped", []))
    assert 0.007 < flipped / (2000 * 256) < 0.013                                     # ~1 % of the bits
    assert media.apply(0, "other", {"data": "00"}) == [(0, {"data": "00"})]           # untouched topic


def test_medium_radio_loss_crosstalk_and_can_retransmission():
    values = {"motor.current": 0.0}
    media = Media({"ble": {"rssi_offset": -30, "sensitivity": -90, "seed": 1},
                   "can": {"crosstalk": [{"signal": "motor.current", "ber_per_unit": 0.01}], "seed": 2}},
                  lambda name: values[name])
    ((_, weak),) = media.apply(0.1, "ble", {"proto": "ble", "rssi": -65, "data": "00" * 10})
    assert weak["medium"]["lost"].startswith("below sensitivity") and weak["rssi"] == -95
    frame = {"proto": "can", "id": 0x182, "data": "0011223344556677", "bits": 130}
    assert media.apply(0.2, "can", frame) == [(0.2, frame)]                           # no current, no coupling
    values["motor.current"] = 0.3                                                     # 0.3 % BER on 64 payload bits
    for k in range(200):
        out = media.apply(0.3 + k * 1e-3, "can", frame)
        if len(out) > 1:
            break
    assert out[0][1]["error"]
    good = [p for _, p in out if not p.get("error")]
    assert good and good[0]["data"] == frame["data"] and good[0]["retry"] >= 1         # retransmitted intact
    assert out[-1][0] > out[0][0] + 130 / 500_000                                     # after the error frame and a retry
    values["motor.current"] = 20.0                                                    # 20 % BER: every retry fails
    out = media.apply(1.0, "can", frame)
    assert all(p.get("error") for _, p in out) and media.specs()["can"]["stats"]["abandoned"] == 1
    topic_media = Media({"b": {"crosstalk": [{"topic": "a", "window_us": 100, "probability": 1.0, "bits": 3}]}})
    topic_media.apply(1.0, "a", {"data": "00"})
    ((_, hit),) = topic_media.apply(1.00005, "b", {"data": "0000"})
    assert hit["medium"]["crosstalk"] == "a" and len(hit["medium"]["flipped"]) == 3


def test_noisy_can_bus_keeps_the_network_running():
    rows, packets, orchestrator = _run(MULTIBOARD, {"can": {"bit_error_rate": 1e-3, "seed": 3},
                                                     "ble": {"rssi_offset": -20, "fading_std": 6, "sensitivity": -90, "seed": 2}})
    stats = orchestrator.media.specs()
    assert stats["can"]["stats"]["errors"] > 5 and stats["can"]["stats"]["retransmissions"] >= stats["can"]["stats"]["errors"] - 1
    assert stats["ble"]["stats"]["lost"] > 3
    assert rows[-1]["master.nodes_operational"] == 3                                 # CAN error handling hides the noise
    assert any(p["type"] == "error frame" for p in packets) and any(p.get("impaired") for p in packets if p["proto"] == "BLE")
    lost = [p for p in packets if p["proto"] == "BLE" and not p["ok"]]
    assert rows[-1]["bridge.ble_seen"] <= sum(1 for p in packets if p["proto"] == "BLE") - len(lost)


def test_lost_frames_reach_no_receiver():
    rows, _, orchestrator = _run(RF_LAB, {"lora": {"loss": 1.0, "seed": 1}}, duration=1.0)
    assert rows[-1]["lora.uplinks"] == 0 and rows[-1]["lora.fcnt"] > 0
    assert orchestrator.media.specs()["lora"]["stats"]["lost"] == rows[-1]["lora.fcnt"]


# -- live medium in the Lab ------------------------------------------------------------------------------------------
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


def test_http_live_medium(lab):
    info = _post(lab, "run", {"graph": load_graph(RF_LAB), "duration": 1.0, "realtime": True, "speed": 2.0})
    for _ in range(100):
        status = json.loads(urllib.request.urlopen(f"{lab}/api/session?session={info['session']}", timeout=10).read())
        if status["status"] == "running":
            break
        time.sleep(0.05)
    result = _post(lab, "analyzer/medium", {"session": info["session"], "topic": "rfid", "spec": {"loss": 1.0}})
    assert result["media"]["rfid"]["loss"] == 1.0
    for _ in range(200):
        status = json.loads(urllib.request.urlopen(f"{lab}/api/session?session={info['session']}", timeout=10).read())
        if status["status"] in ("done", "error"):
            break
        time.sleep(0.1)
    packets = json.loads(urllib.request.urlopen(f"{lab}/api/analyzer?session={info['session']}&from=0", timeout=10).read())
    assert packets["media"]["rfid"]["stats"]["lost"] > 0
    assert any(p.get("impaired") and p["proto"] == "RFID" for p in packets["packets"])


# -- pcap export: every link type round-trips, and Wireshark agrees (opt-in) --------------------------------------
import os
import shutil
import subprocess

from embeddyn.mechatronics.comms.analyzer import write_pcap
from embeddyn.mechatronics.comms.captures import read_capture

CABLE_LAB = ROOT / "examples/mechatronics/cable-lab.yaml"
TSHARK = os.environ.get("TSHARK") or shutil.which("tshark")


@pytest.fixture(scope="module")
def captures(tmp_path_factory, rf_run):
    out = tmp_path_factory.mktemp("pcap")
    rows, _, _ = rf_run
    raw = yaml.safe_load(RF_LAB.read_text())
    events = [e for r in rows for e in r.get("__events__", []) if e["topic"] in raw["metadata"]["analyzer"]["topics"]]
    cable_rows, _, _ = _run(CABLE_LAB, {"i2c": {"kind": "wired"}, "can": {"kind": "wired"}})
    events += [e for r in cable_rows for e in r.get("__events__", []) if e["topic"] in ("i2c", "can")]
    files = {proto: out / f"{proto}.pcap" for proto in ("can", "ble", "wifi", "lora", "nfc", "i2c")}
    counts = {proto: write_pcap(events, path, proto) for proto, path in files.items()}
    return events, files, counts


def test_pcap_round_trip_every_link_type(captures):
    events, files, counts = captures
    assert all(counts[p] > 5 for p in counts), counts
    for proto, path in files.items():
        back = read_capture(path)
        original = sorted((e for e in events if e["payload"].get("proto") == proto), key=lambda e: e["time_seconds"])
        if proto == "i2c":                                   # a read is one record: request and data
            assert [e["payload"]["data"] for e in back] == [e["payload"]["data"] for e in original]
            continue
        assert [e["payload"]["data"] for e in back] == [e["payload"]["data"] for e in original], proto
        if proto == "ble":
            assert {e["payload"]["channel"] for e in back} == {37, 38, 39}
        if proto in ("wifi", "lora"):
            assert all(e["payload"]["rssi"] is not None for e in back)
        if proto == "nfc":
            assert [e["payload"]["dir"] for e in back] == [e["payload"]["dir"] for e in original]


@pytest.mark.skipif(not TSHARK, reason="set TSHARK (or put tshark on PATH) to check the captures with Wireshark")
def test_wireshark_reads_every_capture(captures):
    _, files, counts = captures
    for proto, path in files.items():
        listing = subprocess.run([TSHARK, "-r", str(path)], capture_output=True, text=True, timeout=120)
        assert listing.returncode == 0 and len(listing.stdout.splitlines()) >= counts[proto] * (0.5 if proto == "i2c" else 1)
        malformed = subprocess.run([TSHARK, "-r", str(path), "-Y", "_ws.malformed"], capture_output=True, text=True, timeout=120)
        assert malformed.stdout.strip() == "", f"{proto}: {malformed.stdout[:300]}"
    fcs = subprocess.run([TSHARK, "-r", str(files["wifi"]), "-o", "wlan.check_checksum:TRUE", "-T", "fields", "-e", "wlan.fcs.status"],
                         capture_output=True, text=True, timeout=120).stdout.split()
    assert fcs and set(fcs) == {"1"}                         # every FCS good
    nfc = subprocess.run([TSHARK, "-r", str(files["nfc"]), "-V"], capture_output=True, text=True, timeout=120).stdout
    assert "[CRC Status: Good]" in nfc and "[CRC Status: Bad]" not in nfc
    lora = subprocess.run([TSHARK, "-r", str(files["lora"]), "-T", "fields", "-e", "lorawan.fhdr.devaddr"],
                          capture_output=True, text=True, timeout=120).stdout.split()
    assert lora and set(lora) == {"0x26011bda"}
