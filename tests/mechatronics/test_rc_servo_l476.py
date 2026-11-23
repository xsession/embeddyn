"""RC servo driven by STM32L476 firmware in Renode (examples/mechatronics/rc-servo-l476.yaml).

The plant and pulse-decoder tests always run. The closed-loop tests are opt-in:
set RENODE_BIN to the `renode` launcher and build the firmware with
`make -C examples/mechatronics/firmware/rc-servo-l476`.
"""
import math
import os
import socket
from dataclasses import replace
from pathlib import Path

import pytest

from embeddyn.mechatronics.adapters.renode import RenodeAdapter, _PulseTracker, renode_version
from embeddyn.mechatronics.adapters.renode_api import GpioEvent
from embeddyn.mechatronics.factory import build_adapters
from embeddyn.mechatronics.metrics import compute_metrics, parse_metric_specs
from embeddyn.mechatronics.model import MechatronicProject
from embeddyn.mechatronics.orchestrator import CoSimulationOrchestrator
from embeddyn.mechatronics.plants import RCServo

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples/mechatronics/rc-servo-l476.yaml"
CAN_EXAMPLE = ROOT / "examples/mechatronics/rc-servo-l476-can.yaml"
FIRMWARE = ROOT / "examples/mechatronics/firmware/rc-servo-l476.elf"
RENODE = os.environ.get("RENODE_BIN")
CAL_TRIM_KI = 0x2000001C      # servo_cal.trim_ki (float32)

needs_renode = pytest.mark.skipif(not RENODE or not FIRMWARE.exists(),
                                  reason="set RENODE_BIN and build the rc-servo-l476 firmware")


def _simulate(servo: RCServo, u: dict, seconds: float, x=None, dt: float = 1e-4):
    x = servo.initial_state() if x is None else x
    for _ in range(round(seconds / dt)):
        k1 = servo.derivatives(0, x, u)
        k2 = servo.derivatives(0, [a + dt / 2 * b for a, b in zip(x, k1)], u)
        k3 = servo.derivatives(0, [a + dt / 2 * b for a, b in zip(x, k2)], u)
        k4 = servo.derivatives(0, [a + dt * b for a, b in zip(x, k3)], u)
        x = [a + dt / 6 * (b + 2 * c + 2 * d + e) for a, b, c, d, e in zip(x, k1, k2, k3, k4)]
    return x


def test_rc_servo_plant_follows_pulse_width():
    servo = RCServo()
    x = _simulate(servo, {"pulse_width": 2.0e-3}, 0.5)          # 2.0 ms -> +45 deg
    assert math.degrees(x[0]) == pytest.approx(45.0, abs=1.0)
    out = servo.output(0, x, {"pulse_width": 2.0e-3})
    assert out["feedback_voltage"] == pytest.approx(1.65 + 2.7 / 180 * 45, abs=0.02)
    assert out["signal_valid"] == 1.0
    # A load makes the proportional servo droop; it holds against less than stall torque.
    loaded = _simulate(servo, {"pulse_width": 2.0e-3, "load_torque": 0.3}, 0.5, x)
    assert 1.0 < 45.0 - math.degrees(loaded[0]) < 4.0
    # No pulse -> amplifier off, the load back-drives the output.
    limp = _simulate(servo, {"pulse_width": 0.0, "load_torque": 0.3}, 0.3, loaded)
    assert math.degrees(limp[0]) < 0.0
    assert servo.output(0, limp, {"pulse_width": 0.0})["current"] == 0.0


def test_pulse_tracker_measures_last_complete_pulse():
    tracker = _PulseTracker()
    for stamp, state in [(0, True), (1500, False), (20000, True), (21800, False), (40000, True)]:
        tracker.on_event(GpioEvent(0, stamp, state))
    assert tracker.pulses == 2
    assert tracker.value_us(40500, None) == 1800.0          # held while the next pulse is high
    assert tracker.value_us(40500, 50000.0) == 1800.0
    assert tracker.value_us(80000, 50000.0) == 0.0          # > 50 ms since the last pulse ended


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _project(tmp_path, profile: str, duration: float, trim_ki: float | None = None) -> MechatronicProject:
    project = MechatronicProject.load(EXAMPLE)
    project.duration_seconds = duration
    options = project.instances["mcu"].options
    options.update(port=_free_port(), executable=RENODE, log=str(tmp_path / "renode.log"))
    csv_file = tmp_path / "profile.csv"
    csv_file.write_text("time,command_voltage,load_torque\n" + profile)
    project.instances["operator"].model = str(csv_file)
    if trim_ki is not None:      # calibration written into firmware RAM over the system bus
        options["inline_bindings"] = [{"signal": "mcu.trim_ki", "direction": "input", "property": "sysbus",
                                       "address": CAL_TRIM_KI, "float32": True}]
        template = project.signals["mcu.trim_deg"]
        project.signals["mcu.trim_ki"] = replace(template, name="mcu.trim_ki", unit="1/s", initial=trim_ki)
    return project


def _run(project: MechatronicProject):
    return CoSimulationOrchestrator(project, build_adapters(project)).run()


@needs_renode
def test_rc_servo_closed_loop_in_renode(tmp_path):
    profile = "0.0,1.65,0.0\n0.1,2.475,0.0\n0.6,0.825,0.0\n1.1,2.2,0.0\n1.5,2.2,0.3\n"
    project = _project(tmp_path, profile, 2.5)
    rows = _run(project)
    metrics = compute_metrics(rows, parse_metric_specs(project.metrics))
    last = rows[-1]
    # Moves: +45 deg then -45 deg, slew-limited to 400 deg/s, settle like a real servo.
    for move, target in (("move_up", 45.0), ("move_down", -45.0)):
        assert metrics[f"{move}.final_value"] == pytest.approx(target, abs=1.0)
        assert metrics[f"{move}.overshoot"] < 10.0
        assert metrics[f"{move}.settling_time"] < 0.35
    # Under 0.3 N*m the firmware's trim integrator removes the droop.
    assert last["mcu.setpoint_deg"] == pytest.approx(30.0, abs=0.05)
    assert metrics["loaded_hold.mean"] == pytest.approx(30.0, abs=0.6)
    assert 0.5 < last["mcu.trim_deg"] < 4.0
    # The measured pulse is what the firmware computed: centre + (set-point + trim) * 11.11 us/deg.
    expected_us = 1500 + (last["mcu.setpoint_deg"] + last["mcu.trim_deg"]) * 2000 / 180
    assert last["mcu.servo_pulse"] * 1e6 == pytest.approx(expected_us, abs=1.5)
    assert last["mcu.fault_led"] == 0.0
    # The first pulse completes within the second frame; from then on the signal never drops.
    valid_from = next(row["time"] for row in rows if row["servo.signal_valid"] == 1.0)
    assert valid_from <= 0.041
    assert all(row["servo.signal_valid"] == 1.0 for row in rows if row["time"] > valid_from)


@needs_renode
def test_rc_servo_without_trim_droops(tmp_path):
    """Same load, trim integrator switched off by writing servo_cal.trim_ki = 0 over the bus."""
    rows = _run(_project(tmp_path, "0.0,2.2,0.3\n", 0.8, trim_ki=0.0))
    # The first write lands before boot and the startup code's .data copy
    # restores the flash default (2.0) for the first frame; every later step rewrites 0.
    assert abs(rows[-1]["mcu.trim_deg"]) < 0.01
    droop = 30.0 - rows[-1]["servo.angle_deg"]
    assert 1.0 < droop < 4.0


@needs_renode
def test_rc_servo_stall_trips_fault_and_releases(tmp_path):
    """A load above the stall torque pushes the horn away: after 0.5 s of large
    error the firmware latches the fault, lights the LED and stops the pulses."""
    rows = _run(_project(tmp_path, "0.0,2.2,0.0\n0.3,2.2,1.0\n", 1.2))
    fault_at = next(row["time"] for row in rows if row["mcu.fault_led"] == 1.0)
    assert 0.6 < fault_at < 1.0
    last = rows[-1]
    assert last["mcu.servo_pulse"] == 0.0 and last["servo.signal_valid"] == 0.0
    assert last["servo.current"] == 0.0


def test_uart_lines_are_drained_per_step():
    """The adapter splits what a UART socket delivered into complete lines (no Renode needed)."""
    adapter = RenodeAdapter(uarts=[{"name": "console", "peripheral": "sysbus.usart2", "signal": "mcu.console",
                                    "port": 1}])
    ours, renode_side = socket.socketpair()
    ours.setblocking(False)
    adapter.uarts[0].sock = ours
    renode_side.sendall(b"ready\r\nt=100 sp=")
    assert adapter._drain_uarts(0.1) == [{"time_seconds": 0.1, "topic": "mcu.console", "payload": "ready"}]
    renode_side.sendall(b"2\r\n")
    assert [e["payload"] for e in adapter._drain_uarts(0.2)] == ["t=100 sp=2"]
    assert adapter.uart_write("console", "R") == 1 and renode_side.recv(8) == b"R"
    with pytest.raises(KeyError):
        adapter.uart_write("nope", "x")
    adapter.stop()
    renode_side.close()
    assert "CreateServerSocketTerminal" not in " ".join(RenodeAdapter(script="x.resc").launch_command())


def _console(rows):
    return [(round(e["time_seconds"], 6), e["payload"]) for row in rows for e in row.get("__events__", [])
            if e["topic"] == "mcu.console"]


@needs_renode
def test_console_telemetry_is_deterministic(tmp_path):
    """USART2 prints a telemetry line every 100 ms; two runs give the same lines at the same times."""
    profile = "0.0,1.65,0.0\n0.1,2.475,0.0\n"
    first = _console(_run(_project(tmp_path, profile, 0.5)))
    second = _console(_run(_project(tmp_path, profile, 0.5)))
    assert first == second
    assert first[0][1] == "rc-servo-l476 ready"
    telemetry = [line for _, line in first if line.startswith("t=")]
    assert len(telemetry) == 5 and telemetry[-1].endswith("fault=0")
    fields = dict(item.split("=") for item in telemetry[-1].split())
    assert int(fields["pulse"]) == pytest.approx(2000, abs=3)            # +45 deg -> 2.0 ms


@needs_renode
def test_console_command_clears_a_latched_fault(tmp_path):
    """After a stall latches the fault, 'R' over the UART clears it (a logged, asynchronous input)."""
    project = _project(tmp_path, "0.0,2.2,0.0\n0.3,2.2,1.0\n1.0,2.2,0.0\n", 1.6)
    orchestrator = CoSimulationOrchestrator(project, build_adapters(project))
    sent_at, rows = None, []
    for row in orchestrator.iterate():
        rows.append(row)
        if sent_at is None and row["mcu.fault_led"] == 1.0 and row["time"] > 1.1:
            orchestrator.adapters["mcu"].uart_write("console", "R")
            sent_at = row["time"]
    lines = _console(rows)
    assert sent_at is not None
    cleared = [t for t, line in lines if line == "fault cleared"]
    assert cleared and sent_at < cleared[0] < sent_at + 0.05            # handled within the next frames
    assert rows[-1]["mcu.fault_led"] == 0.0 and rows[-1]["servo.signal_valid"] == 1.0


needs_renode_117 = pytest.mark.skipif(not RENODE or not FIRMWARE.exists() or (renode_version(RENODE) or (0,)) < (1, 17, 0),
                                      reason="needs Renode >= 1.17 (External Control CAN) and the firmware")


def _can_project(tmp_path, profile: str, duration: float) -> MechatronicProject:
    project = _project(tmp_path, profile, duration)
    can = MechatronicProject.load(CAN_EXAMPLE)
    mcu = project.instances["mcu"]
    for key in ("script", "api", "can"):
        mcu.options[key] = can.instances["mcu"].options[key]
    mcu.options["script"] = str((CAN_EXAMPLE.parent / mcu.options["script"]).resolve())
    project.signals.update({k: v for k, v in can.signals.items() if k not in project.signals})
    return project


@needs_renode_117
def test_can_status_frames_reach_embeddyn(tmp_path):
    """bxCAN status frame 0x101 every 100 ms -> CAN hub -> External Control CAN -> decoded signals."""
    rows = _run(_can_project(tmp_path, "0.0,1.65,0.0\n0.1,2.475,0.0\n", 0.5))
    frames = [e["payload"] for row in rows for e in row.get("__events__", []) if e["topic"] == "mcu.can"]
    assert len(frames) == 5 and {f["id"] for f in frames} == {0x101}
    last = rows[-1]
    assert last["mcu.can_setpoint_deg"] == pytest.approx(last["mcu.setpoint_deg"], abs=0.01)
    assert last["mcu.can_pulse_us"] == pytest.approx(2000, abs=3) and last["mcu.can_fault"] == 0.0


@needs_renode_117
def test_can_command_clears_a_latched_fault(tmp_path):
    project = _can_project(tmp_path, "0.0,2.2,0.0\n0.3,2.2,1.0\n1.0,2.2,0.0\n", 1.6)
    orchestrator = CoSimulationOrchestrator(project, build_adapters(project))
    sent_at, rows = None, []
    for row in orchestrator.iterate():
        rows.append(row)
        if sent_at is None and row["mcu.fault_led"] == 1.0 and row["time"] > 1.1:
            orchestrator.adapters["mcu"].can_write(0x200, b"\x01")
            sent_at = row["time"]
    assert sent_at is not None
    cleared = [t for t, line in _console(rows) if line == "fault cleared (CAN)"]
    assert cleared and sent_at < cleared[0] < sent_at + 0.05
    assert rows[-1]["mcu.fault_led"] == 0.0 and rows[-1]["mcu.can_fault"] == 0.0
