"""Lab breadboard view: netlist parsing, automatic layout, the netlist check, export and the HTTP API."""
import copy
import json
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from embeddyn.mechatronics.gui.breadboard import (auto_layout, board_nets, canonical_net, check_board,
                                                  export_netlist, load_board, name_nets, parse_netlist,
                                                  parse_value, part_type, save_board, sidecar_path)
from embeddyn.mechatronics.gui.document import load_graph
from embeddyn.mechatronics.gui.server import serve
from embeddyn.mechatronics.lightcurtain.beam import IrBeamPath

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples/mechatronics"
NETLISTS = sorted(EXAMPLES.glob("**/*.cir"))
CHANNEL = EXAMPLES / "circuits/ir-curtain-channel.cir"
PROJECT = EXAMPLES / "ir-curtain-channel.yaml"


def _connectivity(netlist):
    return {e.ref: [canonical_net(n) for n in e.nodes] for e in netlist.elements}


def test_parser_handles_models_continuations_and_subcircuits():
    text = """* test circuit
.model nfet VDMOS(Vto=3 Kp=60)
.model QN NPN(Bf=100
+ Vaf=50)
.subckt amp in out vcc
R1 in out 1k
.ends amp
Vcc vcc 0 dc 5 ; supply
R1 vcc a 4.7k
C1 a 0 22u ic=0
M1 d g s nfet
M2 d g s b nmosx W=1u
Q1 c b 0 QN
Q2 c b 0 sub QN
X1 a out vcc amp gain=2
S1 a 0 c 0 sw
D1 a k
+ LEDRED
.control
R9 x y 1
.endc
.end
"""
    netlist = parse_netlist(text)
    by = {e.ref: e for e in netlist.elements}
    assert "R1" in by and by["R1"].nodes == ["vcc", "a"] and by["R1"].value == "4.7k"   # .subckt body skipped
    assert "R9" not in by                                                               # .control skipped
    assert by["M1"].nodes == ["d", "g", "s"] and by["M1"].model == "nfet"
    assert by["M2"].nodes == ["d", "g", "s", "b"] and by["M2"].model == "nmosx"
    assert by["Q1"].nodes == ["c", "b", "0"] and by["Q2"].nodes == ["c", "b", "0", "sub"]
    assert by["X1"].nodes == ["a", "out", "vcc"] and by["X1"].model == "amp" and by["X1"].value == "gain=2"
    assert by["S1"].nodes == ["a", "0", "c", "0"] and by["S1"].model == "sw"
    assert by["D1"].model == "LEDRED" and by["D1"].span == 2
    assert netlist.models["qn"][0] == "npn"
    assert [part_type(by[r], netlist) for r in ("Vcc", "C1", "M1", "Q1", "X1", "S1", "D1")] == \
        ["supply", "electrolytic", "nmos_power", "npn", "ic", "switch", "led"]


def test_parse_value():
    assert parse_value("4.7k") == pytest.approx(4700)
    assert parse_value("1meg") == pytest.approx(1e6)
    assert parse_value("22u") == pytest.approx(22e-6)
    assert parse_value("{rled}") is None


@pytest.mark.parametrize("path", NETLISTS, ids=lambda p: p.name)
def test_automatic_layout_matches_every_example_netlist(path):
    netlist = parse_netlist(path.read_text())
    board = auto_layout(netlist)
    check = check_board(board)
    assert check["ok"], check["issues"]
    assert not [i for i in check["issues"] if i["kind"] == "unplaced"]
    # Every lead has its own hole: no two leads or wire ends share one.
    holes = [pin["hole"] for part in board["parts"] if not part.get("module") for pin in part["pins"]]
    holes += [end for wire in board["wires"] for end in (wire["a"], wire["b"]) if not end.startswith("m:")]
    assert len(holes) == len(set(holes))
    # The netlist written from the board connects the same pins.
    again = parse_netlist(export_netlist(board, path.read_text()))
    assert _connectivity(again) == _connectivity(netlist)


def test_channel_layout_parts_and_modules():
    netlist = parse_netlist(CHANNEL.read_text())
    board = auto_layout(netlist)
    parts = {p["id"]: p for p in board["parts"]}
    assert board["supply"] == "vcc"
    assert {k for k, p in parts.items() if p.get("module")} == {"Vcc", "Vgpio", "Iph"}
    assert parts["D1"]["type"] == "led" and parts["Q2"]["type"] == "phototransistor"
    assert parts["Vsled"]["type"] == "ammeter" and parts["Bcmp"]["type"] == "behavioral"
    assert parts["Rled"]["pins"][0]["hole"].startswith("tp")          # resistor from the + rail
    names = name_nets(board)
    groups = board_nets(board)
    assert names[groups[("Q1", "E")]] == "0" and names[groups[("Rload", "2")]] == "rx"


def test_check_finds_shorts_opens_and_unplaced_legs():
    board = auto_layout(parse_netlist(CHANNEL.read_text()))
    parts = {p["id"]: p for p in board["parts"]}
    shorted = copy.deepcopy(board)
    q1 = next(p for p in shorted["parts"] if p["id"] == "Q1")
    q2 = next(p for p in shorted["parts"] if p["id"] == "Q2")
    column = lambda hole: int(hole[1:])                                   # noqa: E731
    shorted["wires"].append({"id": "x", "a": f"a{column(q1['pins'][0]['hole'])}", "b": f"a{column(q2['pins'][0]['hole'])}"})
    result = check_board(shorted)
    assert not result["ok"] and any(i["kind"] == "short" and set(i["nets"]) == {"qc", "rx"} for i in result["issues"])

    opened = copy.deepcopy(board)
    rload = next(p for p in opened["parts"] if p["id"] == "Rload")
    rload["pins"][1]["hole"] = "j29"                                       # an empty strip
    result = check_board(opened)
    assert any(i["kind"] == "open" and i["net"] == "rx" for i in result["issues"])

    loose = copy.deepcopy(board)
    next(p for p in loose["parts"] if p["id"] == "Rout")["pins"][0]["hole"] = None
    result = check_board(loose)
    assert any(i["kind"] == "unplaced" for i in result["issues"])
    assert parts["Rout"]["pins"][0]["hole"]                                # the original is untouched


def test_export_keeps_lines_rewrites_values_and_appends_added_parts():
    text = CHANNEL.read_text()
    board = auto_layout(parse_netlist(text))
    next(p for p in board["parts"] if p["id"] == "Rbase")["value"] = "2.2k"
    board["parts"] = [p for p in board["parts"] if p["id"] != "Cfilt"]
    c = next(int(p["pins"][0]["hole"][1:]) for p in board["parts"] if p["id"] == "Rout")
    rail = c + 1 if (c + 1) % 6 else c + 2
    board["parts"].append({"id": "C9", "ref": "C9", "type": "capacitor", "value": "100n", "added": True,
                           "palette": "capacitor", "spice": {"letter": "C", "nodes": [], "model": None},
                           "pins": [{"name": "1", "hole": f"b{c}"}, {"name": "2", "hole": f"tn{rail}"}]})
    board["parts"].append({"id": "D7", "ref": "D7", "type": "diode", "value": "", "added": True, "palette": "diode",
                           "spice": {"letter": "D", "nodes": [], "model": "D1N4148"},
                           "pins": [{"name": "A", "hole": "j29"}, {"name": "K", "hole": "tn28"}]})
    out = export_netlist(board, text)
    assert "Rbase gpio base 2.2k" in out
    assert "* removed on the breadboard: Cfilt rx 0 1n" in out
    assert "C9 cmp 0 100n" in out
    assert any(line.startswith("D7 N") and line.endswith(" 0 D1N4148") for line in out.splitlines())
    assert ".model D1N4148" in out and out.rstrip().endswith(".end")
    assert ".param rled={{curtain.led_resistor}}" in out                   # untouched lines stay
    check = check_board(board)
    assert check["ok"]                                                     # added parts carry no expectations


def test_sidecar_round_trip(tmp_path):
    netlist = tmp_path / "chan.cir"
    shutil.copy(CHANNEL, netlist)
    board = load_board(netlist)
    assert board["saved"] is False
    board["wires"][0]["color"] = "#123456"
    path = save_board(netlist, board)
    assert path == sidecar_path(netlist) and path.name == "chan.cir.breadboard.yaml"
    again = load_board(netlist)
    assert again["saved"] is True and again["wires"][0]["color"] == "#123456" and again["netlist"] == "chan.cir"


def test_ir_beam_path():
    beam = IrBeamPath(coupling=1e-3, dark_current=0.0)
    assert beam.output(0, [], {"led_current": 0.02, "transmission": 0.5})["photo_current"] == pytest.approx(1e-5)
    assert beam.output(0, [], {"led_current": -0.02, "transmission": 2.0})["photo_current"] == 0.0
    with pytest.raises(ValueError):
        IrBeamPath(coupling=-1)


# -- HTTP API -----------------------------------------------------------------------------------
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


def test_http_breadboard_endpoints(lab, tmp_path):
    graph = load_graph(PROJECT)
    result = _post(lab, "breadboard/circuits", {"graph": graph})
    (circuit,) = result["circuits"]
    assert circuit["instance"] == "electrical" and circuit["netlist"] == str(CHANNEL)
    inputs = {b["target"]: b for b in circuit["inputs"]}
    assert inputs["Vgpio"]["from_node"] == "strobe" and inputs["Vgpio"]["from_domain"] == "firmware"
    assert inputs["Iph"]["kind"] == "current_source" and inputs["Iph"]["from_signal"] == "optics.photo_current"
    outputs = {b["vector"]: b for b in circuit["outputs"]}
    assert outputs["i(Vsled)"]["to_nodes"] == ["beam"]
    assert {p["id"] for p in result["palette"]} >= {"resistor", "led_ir", "npn", "phototransistor"}

    netlist = tmp_path / "chan.cir"
    shutil.copy(CHANNEL, netlist)
    opened = _post(lab, "breadboard/open", {"path": str(netlist)})
    assert opened["check"]["ok"] and opened["models"]["led_ir940"] == "d"
    board = opened["board"]
    assert _post(lab, "breadboard/check", {"board": board})["ok"]
    saved = _post(lab, "breadboard/save", {"path": str(netlist), "board": board})
    assert Path(saved["sidecar"]).exists()
    assert _post(lab, "breadboard/open", {"path": "chan.cir", "root": str(tmp_path)})["board"]["saved"] is True
    text = _post(lab, "breadboard/netlist", {"path": str(netlist), "board": board})["text"]
    assert _connectivity(parse_netlist(text)) == _connectivity(parse_netlist(CHANNEL.read_text()))
    written = _post(lab, "breadboard/export", {"path": str(netlist), "board": board, "target": "out.cir"})
    assert Path(written["path"]) == tmp_path / "out.cir" and (tmp_path / "out.cir").exists()
    fresh = _post(lab, "breadboard/layout", {"path": str(netlist)})
    assert fresh["board"]["saved"] is False and fresh["check"]["ok"]
    with pytest.raises(urllib.error.HTTPError) as error:
        _post(lab, "breadboard/open", {"path": str(tmp_path / "x.sh")})
    assert error.value.code == 400


# -- the example runs in ngspice ------------------------------------------------------------------
def _ngspice():
    try:
        from embeddyn.mechatronics.adapters.ngspice_shared import find_library
        return find_library()
    except Exception:                                    # noqa: BLE001
        return None


@pytest.mark.skipif(_ngspice() is None, reason="libngspice not found (set NGSPICE_LIBRARY)")
def test_ir_curtain_channel_runs():
    from embeddyn.mechatronics.factory import build_adapters
    from embeddyn.mechatronics.model import MechatronicProject
    from embeddyn.mechatronics.orchestrator import CoSimulationOrchestrator

    project = MechatronicProject.load(PROJECT)
    rows = CoSimulationOrchestrator(project, build_adapters(project)).run()
    led = [r["electrical.led_current"] for r in rows]
    seen = [r["electrical.rx_out"] for r in rows if r["optics.transmission"] > 0.5 and r["electrical.gpio"] > 3
            and r["optics.led_current"] > 0.01]
    blocked = [r["electrical.rx_out"] for r in rows if r["optics.transmission"] < 0.5]
    assert 0.018 < max(led) < 0.03                                        # ~20 mA through the IR LED
    # Beam clear and lit: comparator high; blocked: low (edges settle within a few steps).
    assert seen and sum(v > 3.0 for v in seen) > 0.8 * len(seen)
    assert blocked and sum(v < 0.5 for v in blocked) > 0.9 * len(blocked)
