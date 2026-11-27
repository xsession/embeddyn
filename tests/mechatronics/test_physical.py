"""Physical layer: radio distance and link budgets, cables (length, twist, shield, CAN timing), I2C capacitance."""
import json
import threading
import urllib.request
from pathlib import Path

import pytest
import yaml

from embeddyn.mechatronics.comms import i2c
from embeddyn.mechatronics.comms.medium import Media
from embeddyn.mechatronics.comms.physical import (ber_waterfall, cable_effects, i2c_check, path_loss_db, radio_link,
                                                  wifi_freq)
from embeddyn.mechatronics.factory import build_adapters
from embeddyn.mechatronics.gui.server import serve
from embeddyn.mechatronics.model import MechatronicProject
from embeddyn.mechatronics.orchestrator import CoSimulationOrchestrator

ROOT = Path(__file__).resolve().parents[2]
CABLE_LAB = ROOT / "examples/mechatronics/cable-lab.yaml"
RF_LAB = ROOT / "examples/mechatronics/rf-lab.yaml"


def test_path_loss_and_waterfall():
    assert path_loss_db(1.0, 2440.0) == pytest.approx(40.2, abs=0.1)          # free space at 1 m, 2.4 GHz
    assert path_loss_db(10.0, 2440.0, 3.0) - path_loss_db(1.0, 2440.0, 3.0) == pytest.approx(30.0)
    assert wifi_freq(6) == 2437 and wifi_freq(36) == 5180
    assert ber_waterfall(-94, -94) == pytest.approx(1e-3)
    assert ber_waterfall(-80, -94) == 1e-9 and ber_waterfall(-110, -94) == 0.5


def test_rssi_and_errors_grow_with_distance():
    rssi = [radio_link({"proto": "ble"}, {"distance_m": d, "exponent": 2.7})["rssi"] for d in (1, 10, 100)]
    assert rssi[0] > rssi[1] > rssi[2] and rssi[0] - rssi[1] == pytest.approx(27.0, abs=0.1)
    bers = [radio_link({"proto": "ble"}, {"distance_m": d, "exponent": 2.7})["ber"] for d in (10, 80, 120)]
    assert bers[0] < bers[1] < bers[2]
    assert radio_link({"proto": "ble"}, {"distance_m": 200, "exponent": 2.7})["lost"]
    # LoRa: a higher spreading factor reaches further
    far = {"distance_m": 8000, "exponent": 2.9}
    assert radio_link({"proto": "lora", "sf": 7, "freq": 868.1}, far)["lost"]
    assert not radio_link({"proto": "lora", "sf": 12, "freq": 868.1}, far)["lost"]
    # near field: NFC works at 2 cm, not at 8 cm; UHF tags stop answering past ~10 m
    assert not radio_link({"proto": "nfc"}, {"distance_m": 0.02})["lost"] and radio_link({"proto": "nfc"}, {"distance_m": 0.08})["lost"]
    tag = {"proto": "rfid", "air": "gen2", "dir": "tag"}
    assert not radio_link(tag, {"distance_m": 4})["lost"] and radio_link(tag, {"distance_m": 14})["lost"]
    # per-sender distances and walls
    link = {"distance_m": 2, "sources": {"far": {"distance_m": 50}}}
    assert radio_link({"proto": "ble", "src": "far"}, link)["rssi"] < radio_link({"proto": "ble", "src": "near"}, link)["rssi"]
    assert radio_link({"proto": "wifi", "channel": 6}, {"distance_m": 5, "walls_db": 10})["rssi"] == \
        pytest.approx(radio_link({"proto": "wifi", "channel": 6}, {"distance_m": 5})["rssi"] - 10)


def test_cable_twist_shield_and_can_length_limit():
    plain = cable_effects({"length_m": 20, "type": "untwisted"})
    twisted = cable_effects({"length_m": 20, "type": "twisted"})
    shielded = cable_effects({"length_m": 20, "type": "twisted", "shielded": True})
    assert plain["emi_ber"] > twisted["emi_ber"] > shielded["emi_ber"] and plain["emi_ber"] == pytest.approx(20 * twisted["emi_ber"])
    assert cable_effects({"length_m": 200})["delay_s"] == pytest.approx(1e-6)
    limits = {rate: cable_effects({"length_m": 1}, "can", rate)["max_length_m"] for rate in (1e6, 5e5, 125e3)}
    assert 25 <= limits[1e6] <= 45 and 90 <= limits[5e5] <= 110 and 450 <= limits[125e3] <= 550    # the textbook table
    assert cable_effects({"length_m": 80}, "can", 5e5)["timing_error"] == 0
    assert cable_effects({"length_m": 130}, "can", 5e5)["timing_error"] > 0.2


def test_i2c_capacitance_rise_time_and_pullups():
    ok = i2c_check({"length_m": 0.3, "type": "twisted", "mode": "fast", "pullup_ohm": 2200})
    assert ok["ok"] and ok["bus_pf"] == pytest.approx(0.3 * 52 + 30) and ok["ber"] == 0
    shielded = i2c_check({"length_m": 2, "type": "untwisted", "shielded": True, "mode": "fast", "pullup_ohm": 4700})
    assert shielded["bus_pf"] == pytest.approx(230) and shielded["rise_ns"] == pytest.approx(0.8473 * 4700 * 230e-3, rel=1e-3)
    assert not shielded["ok"] and shielded["ber"] > 1e-3 and "967-1539" in shielded["advice"]
    slow = i2c_check({"length_m": 2, "type": "untwisted", "shielded": True, "mode": "standard", "pullup_ohm": 3300})
    assert slow["ok"]                                             # the same cable is fine at 100 kHz
    huge = i2c_check({"length_m": 6, "type": "coax", "mode": "fast", "pullup_ohm": 1000})
    assert "bus capacitance" in " ".join(huge["problems"]) and "no pull-up fits" in huge["advice"]


def test_i2c_decoder_and_boards():
    ctx = {}
    config = {"devices": {"0x48": {"name": "TMP102", "registers": {"0": {"name": "temperature", "shift": 4, "scale": 0.0625, "unit": "degC"}}}}}
    assert i2c.decode(bytes([0x90, 0x00]), "m2s", config, ctx)["summary"] == "write TMP102 reg 0x00"
    assert i2c.decode(bytes([0x91]), "m2s", config, ctx)["type"] == "read"
    assert i2c.decode(bytes([0x19, 0x00]), "s2m", config, ctx)["summary"].endswith("(temperature 25 degC)")
    sensor = i2c.Tmp102()
    sensor.advance(0, 0.001, {"temperature": -10.5})
    sensor.receive(0.001, "rx", {"proto": "i2c", "dir": "m2s", "data": "91"})
    ((_, _, reply),) = sensor.pop_events()
    master = i2c.I2cMaster()
    master.advance(0, 0.006, {})
    master.receive(0.006, "rx", reply)
    assert master.output(0, [], {})["temperature"] == pytest.approx(-10.5)
    sensor.receive(0.01, "rx", {"proto": "i2c", "dir": "m2s", "data": "93"})      # another address: silence
    assert sensor.pop_events() == []


def _run(path, override=None):
    raw = yaml.safe_load(path.read_text())
    for topic, spec in (override or {}).items():
        raw["media"][topic] = spec
    project = MechatronicProject.from_mapping(raw, path.parent)
    orchestrator = CoSimulationOrchestrator(project, build_adapters(project))
    return orchestrator.run(), orchestrator.media.specs()


def test_cable_lab_fails_as_wired_and_works_when_fixed():
    rows, media = _run(CABLE_LAB)
    assert media["can"]["derived"]["cable"]["timing_margin_ns"] < 0 and media["can"]["stats"]["errors"] > 50
    check = media["i2c"]["derived"]["cable"]["i2c"]
    assert not check["ok"] and media["i2c"]["stats"]["corrupted"] > 10
    errors = [abs(r["mcu.temperature"] - r["env.temperature"]) for r in rows[50:]]
    assert max(errors) > 5                                            # flipped bits: wrong temperatures
    assert rows[-1]["mcu.i2c_nacks"] > 0                              # corrupted addresses: NACK
    rows, media = _run(CABLE_LAB, {"can": {"kind": "wired", "bitrate": 500000, "cable": {"length_m": 40, "type": "twisted"}},
                                   "i2c": {"kind": "wired", "cable": {"length_m": 2, "type": "untwisted", "shielded": True,
                                                                      "i2c": {"mode": "fast", "pullup_ohm": 1200}}}})
    assert media["can"]["stats"]["errors"] == 0 and media["i2c"]["stats"]["corrupted"] == 0
    assert media["i2c"]["derived"]["cable"]["i2c"]["ok"] and rows[-1]["mcu.i2c_nacks"] == 0
    assert max(abs(r["mcu.temperature"] - r["env.temperature"]) for r in rows[50:]) < 0.5
    assert rows[-1]["master.nodes_operational"] == 2


def test_rf_lab_moves_a_device_out_of_range():
    rows, media = _run(RF_LAB, {"nfc": {"link": {"distance_m": 0.09}}, "lora": {"link": {"distance_m": 9000, "exponent": 2.9}}})
    assert rows[-1]["nfc.tags_read"] == 0 and media["nfc"]["stats"]["lost"] > 0
    assert rows[-1]["lora.uplinks"] < rows[-1]["lora.fcnt"]           # SF9 does not make 9 km
    assert media["lora"]["derived"]["links"]["lora node"]["rssi"] < -129
    rows, media = _run(RF_LAB)
    assert media["ble"]["derived"]["links"]["beacon"]["rssi"] < media["ble"]["derived"]["links"]["sensor"]["rssi"]


def test_medium_link_sets_rssi_in_packets():
    media = Media({"ble": {"link": {"distance_m": 10, "exponent": 2.0}}})
    ((_, out),) = media.apply(0.0, "ble", {"proto": "ble", "rssi": -40, "data": "00" * 20, "src": "x"})
    assert out["rssi"] == pytest.approx(-60.2, abs=0.1) and out["link"]["distance_m"] == 10


@pytest.fixture
def lab():
    server = serve(None, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.lab_state.stop_all()
    server.server_close()


def test_medium_check_endpoint(lab):
    def post(body):
        request = urllib.request.Request(lab + "/api/analyzer/medium_check", data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())["derived"]
    i2c_result = post({"topic": "i2c", "spec": {"cable": {"length_m": 2, "type": "untwisted", "shielded": True,
                                                          "i2c": {"mode": "fast", "pullup_ohm": 4700}}}})
    assert not i2c_result["cable"]["i2c"]["ok"]
    can = post({"topic": "can", "spec": {"bitrate": 1000000, "cable": {"length_m": 60}}})
    assert can["cable"]["max_length_m"] < 60
    lora_links = post({"topic": "lora", "spec": {"link": {"distance_m": 8000, "exponent": 2.9}}})["links"]
    assert lora_links["SF7"]["lost"] and not lora_links["SF12"]["lost"]
