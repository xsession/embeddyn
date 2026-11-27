"""embeddyn Lab (embeddyn/mechatronics/gui): document <-> graph, parts, run sessions, HTTP API."""
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from embeddyn.mechatronics.factory import build_adapters
from embeddyn.mechatronics.gui.document import graph_from_raw, load_graph, project_from_graph, raw_from_graph
from embeddyn.mechatronics.gui.parts import instantiate_part, part_library
from embeddyn.mechatronics.gui.server import serve
from embeddyn.mechatronics.gui.session import RunSession
from embeddyn.mechatronics.gui.sources import FunctionGenerator
from embeddyn.mechatronics.model import MechatronicProject
from embeddyn.mechatronics.orchestrator import CoSimulationOrchestrator

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = sorted((ROOT / "examples/mechatronics").glob("*.yaml"))
LAB = ROOT / "examples/mechatronics/rc-servo-lab.yaml"
EXAMPLE_DIR = ROOT / "examples/mechatronics"


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_examples_round_trip_through_the_graph(path):
    graph = load_graph(path)
    rebuilt = project_from_graph(graph)
    original = MechatronicProject.load(path)
    assert set(rebuilt.signals) == set(original.signals)
    assert [(c.source, c.target, c.transform) for c in rebuilt.couplings] == \
           [(c.source, c.target, c.transform) for c in original.couplings]
    assert set(rebuilt.simulator_instances()) == set(original.simulator_instances())
    assert rebuilt.metrics == original.metrics and rebuilt.verification == original.verification
    # Every coupling is drawn as a wire between two pins.
    assert sum(w["kind"] == "coupling" for w in graph["wires"]) == len(original.couplings)
    # Saving and loading again is stable (layout included).
    again = graph_from_raw(raw_from_graph(graph), Path(graph["root"]), path)
    assert [(n["id"], n["x"], n["y"]) for n in again["nodes"]] == [(n["id"], n["x"], n["y"]) for n in graph["nodes"]]


def test_lab_example_has_instruments_and_wires():
    graph = load_graph(LAB)
    kinds = {n["id"]: n["kind"] for n in graph["nodes"]}
    assert kinds == {"command": "instance", "controller": "instance", "servo": "instance", "load": "source",
                     "scope": "instrument", "dmm": "instrument", "gauge": "instrument", "arm": "scene"}
    wires = {(w["kind"], w["signal"], w["target"]["node"]) for w in graph["wires"]}
    assert ("net", "load.value", "servo") in wires                      # slider drives the plant input
    assert ("probe", "servo.angle_deg", "arm") in wires                 # 3-D body follows the angle
    servo = next(n for n in graph["nodes"] if n["id"] == "servo")
    supply = next(p for p in servo["ports"] if p["name"] == "supply")
    assert supply["signal"] is None and supply["mappable"]             # unconnected: plant default


def test_renode_ports_come_from_the_binding_file():
    graph = load_graph(ROOT / "examples/mechatronics/rc-servo-l476.yaml")
    mcu = next(n for n in graph["nodes"] if n["id"] == "mcu")
    ports = {(p["signal"], p["direction"]): p["label"] for p in mcu["ports"]}
    assert ports[("mcu.servo_pulse", "out")] == "timer2 gpio_pulse_width pin 0"
    assert ports[("operator.command_voltage", "in")] == "adc1 adc channel 1"
    assert ports[("mcu.console", "out")] == "usart2 console"                       # UART pin from options.uarts
    assert ("probe", "mcu.console", "console") in {(w["kind"], w["signal"], w["target"]["node"]) for w in graph["wires"]}
    assert {w["signal"] for w in graph["wires"] if w["kind"] == "net"} == {
        "mcu.servo_pulse", "operator.command_voltage", "arm.load_torque", "servo.feedback_voltage"}


def test_every_part_instantiates(tmp_path):
    parts = part_library()
    assert {"Controllers", "Electrical", "Mechanical", "Sources", "Instruments", "3-D bodies"} <= {p["category"] for p in parts}
    for part in parts:
        if part["kind"] == "bus":                       # the Lab adds buses itself (topic = name)
            continue
        result = instantiate_part(part["id"], "x1", tmp_path)
        node = result["node"]
        assert node["kind"] == part["kind"] and node["ports"] is not None
        if part["kind"] == "instance" and part["template"]["options"]["backend"] == "python":
            outputs = [p for p in node["ports"] if p["direction"] == "out" and p.get("kind") != "event"]
            events = [p for p in node["ports"] if p.get("kind") == "event"]
            assert outputs or events
            assert all(p["signal"] == f"x1.{p['name']}" for p in outputs)
            assert set(result["signals"]) == {p["signal"] for p in outputs}
            assert all(p["signal"] for p in events), part["id"]   # boards join their usual bus
    servo_fw = instantiate_part("stm32l476_servo_fw", "mcu", tmp_path)["node"]
    assert not Path(servo_fw["spec"]["options"]["bindings"]).is_absolute()     # relative to the project
    with pytest.raises(ValueError, match="invalid part name"):
        instantiate_part("slider", "1bad", tmp_path)


def _built_graph(tmp_path):
    """What the GUI builds by dropping parts and wiring them: generator -> pulse generator -> servo."""
    graph = graph_from_raw({"name": "built", "step_seconds": 0.001, "duration_seconds": 0.6, "signals": {}}, tmp_path)
    for part, name in [("function_generator", "fgen"), ("servo_pulse_gen", "ctrl"), ("rc_servo", "servo"),
                       ("slider", "load"), ("oscilloscope", "scope")]:
        made = instantiate_part(part, name, tmp_path, graph["signals"])
        graph["signals"].update(made["signals"])
        graph["nodes"].append(made["node"])
    nodes = {n["id"]: n for n in graph["nodes"]}
    nodes["fgen"]["spec"]["options"]["parameters"] = {"waveform": "step", "amplitude": 30.0, "delay": 0.05}

    def net(src, out, dst, port):
        signal = f"{src}.{out}"
        nodes[dst]["spec"]["options"]["inputs"][port] = signal
        graph["wires"].append({"id": f"w{len(graph['wires'])}", "kind": "net", "signal": signal,
                               "source": {"node": src, "port": out}, "target": {"node": dst, "port": port}})

    net("fgen", "value", "ctrl", "angle_deg")
    net("ctrl", "pulse_width", "servo", "pulse_width")
    net("load", "value", "servo", "load_torque")
    nodes["scope"]["ports"][0]["signal"] = "servo.angle_deg"
    return graph


def test_graph_built_in_the_gui_runs_and_saves(tmp_path):
    graph = _built_graph(tmp_path)
    project = project_from_graph(graph)
    rows = CoSimulationOrchestrator(project, build_adapters(project)).run()
    assert rows[-1]["servo.angle_deg"] == pytest.approx(30.0, abs=1.5)
    raw = raw_from_graph(graph)
    assert raw["metadata"]["gui"]["nodes"][1]["channels"] == {"A": "servo.angle_deg"}
    assert "scope" not in raw["instances"] and "load" not in raw["instances"]


def test_forced_signals_and_iterate():
    project = MechatronicProject.load(LAB)
    project.duration_seconds = 0.5
    orchestrator = CoSimulationOrchestrator(project, build_adapters(project))
    orchestrator.forced["load.value"] = 0.25
    rows = list(orchestrator.iterate())
    assert len(rows) == 501 and all(row["load.value"] == 0.25 for row in rows)
    # Without forcing the slider signal keeps its initial value (0) and the servo holds better.
    free = CoSimulationOrchestrator(project, build_adapters(project)).run()
    assert free[-1]["load.value"] == 0.0
    assert abs(free[-1]["servo.angle_deg"] - 45.0) < abs(rows[-1]["servo.angle_deg"] - 45.0)


def test_run_session_live_control_pause_and_stop():
    graph = load_graph(LAB)
    graph["duration_seconds"] = 60.0
    session = RunSession(graph, realtime=True, speed=1.0).start()
    deadline = time.monotonic() + 30
    while session.status != "running" and time.monotonic() < deadline:
        time.sleep(0.02)
    session.force("load.value", 0.3)
    time.sleep(0.3)
    session.pause()
    paused_rows = len(session.rows)
    time.sleep(0.3)
    assert session.status == "paused" and len(session.rows) - paused_rows <= 2
    session.resume()
    time.sleep(0.2)
    session.stop()
    session.join(20)
    assert session.status == "stopped" and session.error is None
    index = session.names.index("load.value") + 1
    assert session.rows[-1][index] == 0.3
    assert 0 < session.rows[-1][0] < 5                      # real-time pacing: nowhere near 60 s
    assert session.csv().splitlines()[0].startswith("time,command.value")


def test_function_generator_waveforms():
    gen = lambda **kw: FunctionGenerator(**kw)
    assert gen(waveform="sine", frequency=1).output(0.25, [], {})["value"] == pytest.approx(1.0)
    assert gen(waveform="square", frequency=1).output(0.75, [], {})["value"] == -1.0
    assert gen(waveform="step", delay=0.1, amplitude=2, offset=1).output(0.05, [], {})["value"] == 1.0
    assert gen(waveform="pulse", duty=0.2, frequency=10).output(0.01, [], {})["value"] == 1.0
    assert gen(waveform="triangle", frequency=1).output(0.25, [], {})["value"] == pytest.approx(0.0)
    assert gen(waveform="sine", frequency=1).output(0.25, [], {"gate": 0.0})["value"] == 0.0
    with pytest.raises(ValueError):
        gen(waveform="noise")


# -- HTTP API ------------------------------------------------------------------------------
@pytest.fixture
def lab():
    server = serve(None, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.lab_state.stop_all()
    server.server_close()


def _post(base, path, body, headers=None):
    request = urllib.request.Request(base + "/api/" + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def _events(base, session):
    events = []
    with urllib.request.urlopen(f"{base}/api/stream?session={session}", timeout=60) as response:
        name = None
        for raw in response:
            line = raw.decode().rstrip("\n")
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                events.append((name, json.loads(line[6:])))
                if name == "end":
                    return events
    return events


def test_http_api_open_run_stream_save(lab, tmp_path):
    with urllib.request.urlopen(lab + "/", timeout=10) as response:
        assert b"embeddyn" in response.read()
    state = json.loads(urllib.request.urlopen(lab + "/api/state", timeout=10).read())
    assert any(e["file"] == "rc-servo-lab.yaml" for e in state["examples"]) and len(state["parts"]) > 20
    graph = _post(lab, "open", {"path": str(LAB)})
    assert _post(lab, "validate", {"graph": graph})["ok"]
    info = _post(lab, "run", {"graph": graph, "duration": 0.3})
    events = _events(lab, info["session"])
    rows = [row for name, payload in events if name == "rows" for row in payload["rows"]]
    assert len(rows) == 301 and rows[-1][0] == pytest.approx(0.3)
    final = [payload for name, payload in events if name == "status"][-1]
    assert final["status"] == "done" and "current.rms" in final["metrics"]
    csv = urllib.request.urlopen(f"{lab}/api/csv?session={info['session']}", timeout=10).read().decode()
    assert len(csv.splitlines()) == 302
    # Save elsewhere: relative paths are rebased, layout and instruments survive.
    graph["nodes"][0]["x"] = 123.0
    saved = _post(lab, "save", {"graph": graph, "path": str(tmp_path / "sub" / "copy.yaml")})
    reopened = load_graph(Path(saved["path"]))
    assert reopened["nodes"][0]["x"] == 123.0
    assert {n["id"] for n in reopened["nodes"]} == {n["id"] for n in graph["nodes"]}
    MechatronicProject.load(Path(saved["path"]))
    text = _post(lab, "yaml", {"graph": graph})["yaml"]
    assert "metadata:" in text and "gui:" in text


def test_http_api_refuses_cross_origin_and_traversal(lab):
    with pytest.raises(urllib.error.HTTPError) as error:
        _post(lab, "open", {"path": str(LAB)}, {"Origin": "http://evil.example"})
    assert error.value.code == 403
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(lab + "/../../server.py", timeout=10)
    assert error.value.code == 404
    with pytest.raises(urllib.error.HTTPError) as error:
        _post(lab, "save", {"graph": load_graph(LAB), "path": "/tmp/evil.sh"})
    assert error.value.code == 400


def test_ports_endpoint_follows_option_edits(lab, tmp_path):
    spec = {"domain": "mechanical", "model": "embeddyn.mechatronics.plants:DCMotorRL",
            "options": {"backend": "python", "inputs": {"duty": "a.duty"}, "outputs": {"current": "m.current"}}}
    ports = _post(lab, "ports", {"spec": spec, "root": str(tmp_path), "signals": {}})["ports"]
    by_name = {p["name"]: p for p in ports}
    assert by_name["duty"]["signal"] == "a.duty" and by_name["supply"]["signal"] is None
    assert by_name["current"]["unit"] == "A"


# -- buses, pinouts, wiring check -----------------------------------------------------------------------------
def test_buses_are_pins_nodes_and_wires():
    from embeddyn.mechatronics.gui.document import graph_from_raw, raw_from_graph
    graph = load_graph(EXAMPLE_DIR / "multiboard-canopen-ble.yaml")
    buses = {n["topic"]: n for n in graph["nodes"] if n["kind"] == "bus"}
    assert set(buses) == {"can.tx", "can", "ble", "wifi"} and buses["can"]["protocol"] == "CAN"
    bus_wires = [w for w in graph["wires"] if w["kind"] == "bus"]
    into_can = {w["source"]["node"] for w in bus_wires if w["target"]["node"] == "bus_can"}
    out_of_can = {w["target"]["node"] for w in bus_wires if w["source"]["node"] == "bus_can"}
    assert into_can == {"can"} and {"master", "axis", "sensor", "gateway"} <= out_of_can     # CanBus puts frames on can
    assert not any(w["kind"] == "net" and w["signal"] in buses for w in graph["wires"])   # events are not nets
    raw = raw_from_graph(graph)
    assert "buses" not in raw["metadata"]["gui"] and "bus_can" in raw["metadata"]["gui"]["layout"]
    # a bus without boards survives a save
    graph["nodes"].append({"id": "bus_spare", "kind": "bus", "label": "spare", "topic": "spare", "ports": []})
    again = graph_from_raw(raw_from_graph(graph), EXAMPLE_DIR)
    assert any(n["kind"] == "bus" and n["topic"] == "spare" for n in again["nodes"])
    renode = load_graph(EXAMPLE_DIR / "rc-servo-l476-canbus.yaml")
    mcu_pins = {p["name"]: p["signal"] for n in renode["nodes"] if n["id"] == "mcu" for p in n["ports"] if p.get("kind") == "event"}
    assert mcu_pins.get("can:tx") and mcu_pins.get("can:rx")


def test_wiring_check_finds_units_drivers_and_lonely_buses():
    from embeddyn.mechatronics.gui.document import unit_conversion, wiring_issues
    assert unit_conversion("rad", "deg")[0] == pytest.approx(57.29578) and unit_conversion("deg", "rad")[0] == pytest.approx(0.0174533)
    assert unit_conversion("degC", "K") == pytest.approx((1.0, 273.15)) and unit_conversion("V", "A") is None
    graph = {"signals": {"a": {"initial": 2.5}}, "nodes": [
        {"id": "p", "kind": "instance", "ports": [{"name": "angle", "direction": "out", "signal": "s", "unit": "rad"},
                                                   {"name": "tx", "direction": "out", "signal": "x", "unit": ""},
                                                   {"name": "event:tx", "direction": "out", "signal": "lonely", "kind": "event"}]},
        {"id": "q", "kind": "instance", "ports": [{"name": "deg", "direction": "in", "signal": "s", "unit": "deg"},
                                                   {"name": "x", "direction": "out", "signal": "x"},
                                                   {"name": "a", "direction": "in", "signal": "a"},
                                                   {"name": "event:rx", "direction": "in", "signal": None, "kind": "event"}]},
        {"id": "bus_lonely", "kind": "bus", "topic": "lonely", "label": "lonely", "ports": []}],
        "wires": [{"id": "w0", "kind": "net", "signal": "s", "source": {"node": "p", "port": "angle"}, "target": {"node": "q", "port": "deg"}},
                  {"id": "w1", "kind": "bus", "signal": "lonely", "source": {"node": "p", "port": "event:tx"}, "target": {"node": "bus_lonely", "port": "tx"}}]}
    issues = {i["kind"]: i for i in wiring_issues(graph)}
    assert "gain 57.2958" in issues["units"]["text"]
    assert issues["drivers"]["severity"] == "error" and "x is written by 2 outputs" in issues["drivers"]["text"]
    assert "stays at 2.5" in issues["undriven"]["text"]
    assert "nobody subscribes" in issues["bus"]["text"] and "q.event:rx is on no bus" in issues["event"]["text"]


def test_transistor_legs_follow_the_package():
    from embeddyn.mechatronics.gui import breadboard as bb
    text = """* pinout
V1 vcc 0 dc 5
R1 vcc c 1k
R2 vcc b 100k
Q1 c b 0 Q2N3904
M1 d g 0 N2N7000
M2 d2 g 0 NPOWER
R3 vcc d 1k
R4 vcc d2 1k
R5 vcc g 10k
.model Q2N3904 NPN(Is=6.734f Bf=416.4)
.model N2N7000 VDMOS(Vto=2.1 Kp=0.3)
.model NPOWER VDMOS(Vto=3 Kp=60)
.end
"""
    board = bb.auto_layout(bb.parse_netlist(text), "pinout")
    order = {}
    for part in board["parts"]:
        if part["type"] in bb.TRANSISTOR_TYPES:
            assert [p["name"] for p in part["pins"]] == bb.PIN_NAMES[part["type"]][:3]          # pin i is still node i
            order[part["ref"]] = [p["name"] for p in sorted(part["pins"], key=lambda p: int(p["hole"][1:]))]
    assert order == {"Q1": ["E", "B", "C"], "M1": ["S", "G", "D"], "M2": ["G", "D", "S"]}
    assert bb.check_board(board)["ok"]


def test_bus_activity_counts_frames_rate_and_damage():
    from embeddyn.mechatronics.gui.server import _bus_activity

    class Session:
        def __init__(self):
            self.events = []

        def events_since(self, cursor):
            return self.events[cursor:], len(self.events)

        def info(self):
            return {"status": "running"}

    session = Session()
    session.events += [{"topic": "can", "time_seconds": i * 0.001, "payload": {"proto": "can", "id": 1, "data": ""}} for i in range(200)]
    session.events.append({"topic": "can", "time_seconds": 0.2, "payload": {"proto": "can", "medium": {"lost": "noise"}}})
    session.events.append({"topic": "uart", "time_seconds": 0.2, "payload": {"proto": "uart", "medium": {"faults": ["x"]}}})
    first = _bus_activity(session)["topics"]
    assert first["can"]["frames"] == 201 and first["can"]["lost"] == 1 and first["can"]["rate_hz"] == pytest.approx(1000, rel=0.02)
    assert first["uart"]["impaired"] == 1
    session.events.append({"topic": "can", "time_seconds": 0.5, "payload": {"proto": "can", "error": "bit error"}})
    again = _bus_activity(session)["topics"]                 # incremental: only the new event is read
    assert again["can"]["frames"] == 202 and again["can"]["errors"] == 1 and again["can"]["rate_hz"] == 10.0
