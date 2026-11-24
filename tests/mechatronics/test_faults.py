"""Fault injection: signal faults in project files, scenarios and checkpoints."""
import csv
import json
import statistics
from pathlib import Path

import pytest
import yaml

from embeddyn.mechatronics.compiler import ProjectCompiler
from embeddyn.mechatronics.factory import build_adapters
from embeddyn.mechatronics.faults import FaultInjector, parse_faults
from embeddyn.mechatronics.model import MechatronicProject
from embeddyn.mechatronics.orchestrator import CoSimulationOrchestrator
from embeddyn.mechatronics.runtime import CheckpointManager
from embeddyn.mechatronics.verification import scenarios

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples" / "mechatronics"
SENSOR = "firmware.measured_velocity"     # what the controller sees of mechanical.velocity


def dc_motor_raw():
    raw = yaml.safe_load((EXAMPLES / "dc-motor.yaml").read_text())
    raw["duration_seconds"] = 1.0
    return raw


def dc_motor(*faults):
    raw = dc_motor_raw()
    raw["faults"] = list(faults)
    return MechatronicProject.from_mapping(raw, EXAMPLES)


def run(project):
    runtime = CoSimulationOrchestrator(project, build_adapters(project, mock=True))
    return runtime.run(), runtime


def at(rows, time):
    return min(rows, key=lambda row: abs(row["time"] - time))


def test_no_faults_changes_nothing():
    plain, _ = run(dc_motor())
    raw = dc_motor_raw()
    assert "faults" not in raw and plain == run(MechatronicProject.from_mapping(raw, EXAMPLES))[0]


def test_stuck_sensor_holds_its_value_only_inside_the_window():
    rows, _ = run(dc_motor({"name": "enc", "signal": SENSOR, "type": "stuck", "start": "0.1 s", "end": "0.3 s"}))
    window = [row[SENSOR] for row in rows if 0.1 <= row["time"] < 0.3 - 1e-9]
    assert len(set(window)) == 1 and window[0] > 0                    # held at the value it had at 0.1 s
    late = at(rows, 0.6)
    assert late[SENSOR] == pytest.approx(at(rows, 0.599)["mechanical.velocity"], rel=0.05)  # follows again
    events = [event for row in rows for event in row.get("__events__", [])]
    assert [(e["topic"], e["payload"]["state"]) for e in events] == [("fault.enc", "start"), ("fault.enc", "end")]
    assert events[0]["time_seconds"] == pytest.approx(0.1) and events[1]["time_seconds"] == pytest.approx(0.3)


def test_stuck_at_a_value_and_offset_reach_the_controller():
    nominal, _ = run(dc_motor())
    stuck, _ = run(dc_motor({"signal": SENSOR, "type": "stuck", "value": "0 rpm", "start": 0.2}))
    assert all(row[SENSOR] == 0.0 for row in stuck if row["time"] >= 0.2)
    # The controller never sees the motor move, so it drives harder than nominal.
    assert stuck[-1]["mechanical.velocity"] > nominal[-1]["mechanical.velocity"] + 1.0
    offset, _ = run(dc_motor({"signal": SENSOR, "type": "offset", "offset": "1 rad/s"}))
    # The loop regulates the faulty reading, so the true speed settles about 1 rad/s low.
    assert offset[-1]["mechanical.velocity"] == pytest.approx(nominal[-1]["mechanical.velocity"] - 1.0, abs=0.15)


def test_faults_on_signals_nothing_writes():
    rows, _ = run(dc_motor({"signal": "electrical.supply_voltage", "type": "gain", "gain": 0.5, "start": 0.5, "end": 0.7}))
    assert at(rows, 0.4)["electrical.supply_voltage"] == 24.0
    assert at(rows, 0.6)["electrical.supply_voltage"] == 12.0
    assert at(rows, 0.8)["electrical.supply_voltage"] == 24.0


def test_noise_drift_dropout_and_intermittent():
    noisy, _ = run(dc_motor({"signal": "mechanical.load_torque", "type": "noise", "std": "5 mN*m", "seed": 3}))
    again, _ = run(dc_motor({"signal": "mechanical.load_torque", "type": "noise", "std": "5 mN*m", "seed": 3}))
    values = [row["mechanical.load_torque"] for row in noisy]
    assert values == [row["mechanical.load_torque"] for row in again]          # seeded: reproducible
    assert 0.003 < statistics.pstdev(values) < 0.007
    drift, _ = run(dc_motor({"signal": "electrical.supply_voltage", "type": "drift", "rate": "-2 V/s", "start": 0.5}))
    assert at(drift, 0.75)["electrical.supply_voltage"] == pytest.approx(23.5)
    lost, _ = run(dc_motor({"signal": SENSOR, "type": "dropout", "probability": 1.0, "start": 0.2, "end": 0.25}))
    assert len({row[SENSOR] for row in lost if 0.2 <= row["time"] < 0.25 - 1e-9}) == 1
    blink, _ = run(dc_motor({"signal": "electrical.supply_voltage", "type": "stuck", "value": 0,
                             "start": 0.5, "period": "100 ms", "duty": 0.25}))
    assert [at(blink, t)["electrical.supply_voltage"] for t in (0.51, 0.56, 0.61, 0.66)] == [0.0, 24.0, 0.0, 24.0]


def test_fault_values_are_clamped_to_the_signal_range():
    rows, _ = run(dc_motor({"signal": "firmware.pwm_duty", "type": "offset", "offset": 5}))
    assert max(row["firmware.pwm_duty"] for row in rows) == 1.0


@pytest.mark.parametrize("fault, message", [
    ({"signal": "nope", "type": "stuck"}, "unknown signal nope"),
    ({"signal": SENSOR, "type": "melt"}, "unknown type melt"),
    ({"signal": SENSOR, "type": "offset"}, "needs offset"),
    ({"signal": SENSOR, "type": "offset", "offset": "3 V"}, "not a quantity in rad/s"),
    ({"signal": SENSOR, "type": "stuck", "start": 0.5, "end": 0.2}, "start < end"),
    ({"signal": SENSOR, "type": "dropout", "probability": 2}, "within 0..1"),
    ({"signal": SENSOR, "type": "stuck", "period": 0.1, "duty": 0}, "0 < duty"),
])
def test_invalid_faults_are_rejected(fault, message):
    with pytest.raises(ValueError, match=message):
        dc_motor(fault)
    with pytest.raises(ValueError, match="duplicate fault name"):
        dc_motor({"name": "a", "signal": SENSOR, "type": "stuck"}, {"name": "a", "signal": SENSOR, "type": "stuck"})


def test_scenarios_add_faults():
    raw = dc_motor_raw()
    project_file = EXAMPLES / "_faults_scenario_test.yaml"
    raw["verification"] = {"scenarios": [
        {"name": "nominal", "expect": [{"metric": "speed.final_value", "max": 8.5}]},
        {"name": "sensor-stuck-at-zero", "faults": [{"signal": SENSOR, "type": "stuck", "value": 0, "start": 0.2}],
         "expect": [{"metric": "speed.final_value", "max": 8.5}]},
    ]}
    project_file.write_text(yaml.safe_dump(raw))
    try:
        result = scenarios(project_file, raw["verification"]["scenarios"], mock=True)
    finally:
        project_file.unlink()
    assert [item["passed"] for item in result["scenarios"]] == [True, False]   # the overspeed is caught


def test_checkpoint_resume_keeps_fault_state(tmp_path):
    fault = {"signal": "mechanical.load_torque", "type": "noise", "std": 0.005, "seed": 1, "start": 0.2}
    stuck = {"signal": SENSOR, "type": "stuck", "start": 0.3, "end": 0.7}
    full, _ = run(dc_motor(fault, stuck))
    project = dc_motor(fault, stuck)
    manager = CheckpointManager(tmp_path)
    CoSimulationOrchestrator(project, build_adapters(project, mock=True)).run(
        checkpoint_manager=manager, checkpoint_interval_seconds=0.5)
    checkpoint = sorted(tmp_path.glob("*.json"))[0]
    project = dc_motor(fault, stuck)
    resumed = CoSimulationOrchestrator(project, build_adapters(project, mock=True)).run(resume_from=checkpoint)
    tail = [row for row in full if row["time"] >= resumed[0]["time"] - 1e-12]
    for a, b in zip(tail, resumed):
        assert a["mechanical.load_torque"] == pytest.approx(b["mechanical.load_torque"])
        assert a[SENSOR] == pytest.approx(b[SENSOR])


def test_compiled_snapshot_records_faults(tmp_path):
    project = dc_motor({"name": "enc", "signal": SENSOR, "type": "offset", "offset": "60 rpm", "start": "100 ms"})
    compiled = ProjectCompiler().compile(project, tmp_path)
    snapshot = json.loads((compiled.directory / "project.normalized.json").read_text())
    (fault,) = snapshot["faults"]
    assert fault["name"] == "enc" and fault["start"] == pytest.approx(0.1) and fault["offset"] == pytest.approx(6.283185, rel=1e-6)


def test_injector_alone():
    project = dc_motor()
    (fault,) = parse_faults([{"signal": SENSOR, "type": "stuck"}], project.signals)
    injector = FaultInjector([fault])
    assert injector.apply(SENSOR, 5.0, 2.0, 0.0) == 2.0 and injector.apply(SENSOR, 7.0, 2.0, 0.1) == 2.0
    assert injector.apply("other", 7.0, 2.0, 0.1) == 7.0


def test_threshold_metric():
    from embeddyn.mechatronics.metrics import compute_metrics, parse_metric_specs
    rows = [{"time": t / 10, "led": 1.0 if 3 <= t < 6 else 0.0} for t in range(10)]
    figures = compute_metrics(rows, parse_metric_specs({"trip": {"type": "threshold", "signal": "led", "start": 0.1}}))
    assert figures["trip.time"] == pytest.approx(0.25) and figures["trip.delay"] == pytest.approx(0.15)
    assert figures["trip.crossings"] == 1.0
    either = compute_metrics(rows, parse_metric_specs({"t": {"type": "threshold", "signal": "led", "direction": "either"}}))
    assert either["t.crossings"] == 2.0
    never = compute_metrics(rows, parse_metric_specs({"t": {"type": "threshold", "signal": "led", "threshold": 2}}))
    assert never["t.time"] is None and never["t.crossings"] == 0.0
    with pytest.raises(ValueError, match="direction"):
        parse_metric_specs({"t": {"type": "threshold", "signal": "led", "direction": "up"}})


def test_lab_documents_keep_faults():
    from embeddyn.mechatronics.gui.document import graph_from_raw, project_from_graph, raw_from_graph
    raw = dc_motor_raw()
    raw["faults"] = [{"name": "enc", "signal": SENSOR, "type": "stuck", "start": "0.1 s"}]
    graph = graph_from_raw(raw, EXAMPLES)
    assert raw_from_graph(graph)["faults"] == raw["faults"]
    assert [fault.name for fault in project_from_graph(graph).faults] == ["enc"]


def test_run_command_takes_faults(tmp_path):
    from typer.testing import CliRunner
    from embeddyn.__main__ import app
    result = CliRunner().invoke(app, ["mechatronics", "run", str(EXAMPLES / "dc-motor.yaml"), "--mock",
                                      "--output-root", str(tmp_path), "-o", str(tmp_path / "out.csv"),
                                      "--fault", "{signal: electrical.supply_voltage, type: stuck, value: 0 V}"])
    assert result.exit_code == 0, result.output
    rows = list(csv.DictReader((tmp_path / "out.csv").open()))
    assert {float(row["electrical.supply_voltage"]) for row in rows} == {0.0}
    bad = CliRunner().invoke(app, ["mechatronics", "run", str(EXAMPLES / "dc-motor.yaml"), "--mock",
                                   "--output-root", str(tmp_path), "--fault", "{signal: nope, type: stuck}"])
    assert bad.exit_code != 0 and "unknown signal nope" in bad.output


# ---------------------------------------------------------------- frame faults (bus traffic)

SERVO = EXAMPLES / "rc-servo-l476.yaml"
SERVO_CAN = EXAMPLES / "rc-servo-l476-can.yaml"


def servo_faults(*items, example=SERVO_CAN):
    project = MechatronicProject.load(example)
    return parse_faults(list(items), project.signals)


def test_frame_faults_apply_to_event_signals_only():
    (drop,) = servo_faults({"signal": "mcu.can", "type": "dropout", "id": "0x101", "direction": "output"})
    assert drop.frames and drop.ids == (0x101,) and drop.to_dict()["id"] == [0x101]
    for fault, message in [
        ({"signal": "mcu.can", "type": "stuck"}, "does not apply to mcu.can, a event signal"),
        ({"signal": "servo.feedback_voltage", "type": "corrupt"}, "does not apply to servo.feedback_voltage"),
        ({"signal": "servo.feedback_voltage", "type": "dropout", "id": 3}, "id only apply to faults on event"),
        ({"signal": "mcu.can", "type": "corrupt", "direction": "sideways"}, "direction must be one of"),
        ({"signal": "mcu.can", "type": "corrupt", "bits": 0}, "bits must be at least 1"),
        ({"signal": "mcu.can", "type": "dropout", "id": "x1"}, "invalid id"),
    ]:
        with pytest.raises(ValueError, match=message):
            servo_faults(fault)


def test_injector_drops_and_corrupts_frames():
    faults = servo_faults(
        {"name": "lost", "signal": "mcu.can", "type": "dropout", "id": 0x101, "direction": "output", "start": 1, "end": 2},
        {"name": "flip", "signal": "mcu.can", "type": "corrupt", "bits": 3, "direction": "input", "seed": 5})
    injector = FaultInjector(faults)
    assert injector.signals() == [] and injector.frame_signals() == ["mcu.can"]
    data = bytes(8)
    assert injector.frame("mcu.can", data, 0.5, direction="output", frame_id=0x101) == data     # before the window
    assert injector.frame("mcu.can", data, 1.5, direction="output", frame_id=0x101) is None     # lost
    assert injector.frame("mcu.can", data, 1.5, direction="output", frame_id=0x102) == data     # other id
    flipped = injector.frame("mcu.can", data, 1.5, direction="input", frame_id=0x200)
    assert sum(bin(byte).count("1") for byte in flipped) == 3                                  # 3 bits flipped
    assert injector.frame("mcu.can", b"", 1.5, direction="input") == b""                       # nothing to flip
    assert injector.frame_counts == {"lost": {"dropped": 1, "corrupted": 0}, "flip": {"dropped": 0, "corrupted": 1}}
    # Seeded and checkpointed: a restored injector continues the same random stream.
    state = json.loads(json.dumps(injector.snapshot()))
    restored = FaultInjector(faults)
    restored.restore(state)
    assert restored.frame_counts == injector.frame_counts
    assert restored.frame("mcu.can", data, 3, direction="input") == injector.frame("mcu.can", data, 3, direction="input")


def test_probability_drops_a_share_of_frames():
    (fault,) = servo_faults({"signal": "mcu.can", "type": "dropout", "probability": 0.25, "seed": 2})
    injector = FaultInjector([fault])
    kept = sum(injector.frame("mcu.can", b"x", 0.0, direction="output") is not None for _ in range(4000))
    assert 2850 < kept < 3150


def test_renode_adapter_faults_uart_bytes_and_can_frames():
    import socket
    from embeddyn.mechatronics.adapters.renode import RenodeAdapter
    from embeddyn.mechatronics.adapters.renode_api_v2 import CanFrame
    project = MechatronicProject.load(SERVO_CAN)
    project.faults = parse_faults([
        {"signal": "mcu.console", "type": "dropout", "direction": "output", "start": 0.15, "end": 0.25},
        {"signal": "mcu.console", "type": "corrupt", "direction": "input"},
        {"signal": "mcu.can", "type": "dropout", "id": 0x101, "start": 0.15, "end": 0.25}], project.signals)
    orchestrator = CoSimulationOrchestrator(project, build_adapters(project))
    adapter = orchestrator.adapters["mcu"]
    assert isinstance(adapter, RenodeAdapter) and adapter._faults is orchestrator.faults      # attached
    ours, renode_side = socket.socketpair()
    ours.setblocking(False)
    adapter.uarts[0].sock = ours
    renode_side.sendall(b"one\r\n")
    assert [e["payload"] for e in adapter._drain_uarts(0.1)] == ["one"]
    renode_side.sendall(b"two\r\n")
    assert adapter._drain_uarts(0.2) == []                                     # every byte lost
    renode_side.sendall(b"three\r\n")
    assert [e["payload"] for e in adapter._drain_uarts(0.3)] == ["three"]
    assert adapter.uart_write("console", "R") == 1 and renode_side.recv(8) != b"R"   # corrupted on the way in
    for time_ns, frame_id in [(100_000_000, 0x101), (200_000_000, 0x101), (200_000_000, 0x102)]:
        adapter._on_can_frame(CanFrame(time_ns, frame_id, b"\x10\x00" + bytes(6)))
    assert [(f.time_ns, f.frame_id) for f in adapter._can_frames] == [(100_000_000, 0x101), (200_000_000, 0x102)]
    adapter.stop()
    renode_side.close()
