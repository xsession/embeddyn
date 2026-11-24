"""Whole-system loop: STM32L476 firmware (Renode >= 1.17) + H-bridge + DC motor + MuJoCo arm."""
import dataclasses
import os
import socket
import statistics
from pathlib import Path

import pytest

pytest.importorskip("mujoco")

from embeddyn.mechatronics.adapters.renode import renode_version  # noqa: E402
from embeddyn.mechatronics.factory import build_adapters  # noqa: E402
from embeddyn.mechatronics.metrics import compute_metrics, parse_metric_specs  # noqa: E402
from embeddyn.mechatronics.model import MechatronicProject  # noqa: E402
from embeddyn.mechatronics.orchestrator import CoSimulationOrchestrator  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
PROJECT = ROOT / "examples/mechatronics/mujoco-arm-firmware.yaml"
FIRMWARE = ROOT / "examples/mechatronics/firmware/arm-joint-l476.elf"
RENODE = os.environ.get("RENODE_BIN")
pytestmark = pytest.mark.skipif(not RENODE or not FIRMWARE.exists() or (renode_version(RENODE) or (0,)) < (1, 17, 0),
                                reason="needs Renode >= 1.17 (STM32_Timer encoder mode) and the arm-joint firmware")


def test_firmware_position_loop_through_encoder_and_h_bridge(tmp_path):
    project = MechatronicProject.load(PROJECT)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        project.instances["mcu"].options.update(port=probe.getsockname()[1], executable=RENODE,
                                                log=str(tmp_path / "renode.log"))
    rows = CoSimulationOrchestrator(project, build_adapters(project)).run()
    metrics = compute_metrics(rows, parse_metric_specs(project.metrics))
    assert metrics["lift.final_value"] == pytest.approx(1.2, abs=0.02)
    assert metrics["lift.overshoot"] < 5.0 and metrics["lift.settling_time"] < 1.0
    # The firmware's encoder-derived angle tracks the physics within counts quantisation (+ one step).
    assert max(abs(r["mcu.angle"] - r["arm.angle"]) for r in rows) < 0.005
    # The bridge duty measured on the PWM pins is the duty the firmware commanded (one step later).
    lagged = [abs(rows[i - 1]["mcu.duty"] - rows[i]["bridge.duty"]) for i in range(20, len(rows))]
    assert statistics.median(lagged) < 0.01
    assert metrics["current.peak"] < 12.0 + 1e-6


def test_project_parameters_reach_the_firmware_calibration(tmp_path):
    """firmware.kp/ki/kd are written into `arm_cal` in MCU RAM, after the reset handler's copy."""
    import struct
    project = MechatronicProject.load(PROJECT)
    project.duration_seconds = 0.005
    project.apply_parameter_overrides({"firmware.kp": 12.5, "firmware.ki": 0.75, "firmware.kd": 0.0625})
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        project.instances["mcu"].options.update(port=probe.getsockname()[1], executable=RENODE,
                                                log=str(tmp_path / "renode.log"))
    adapters = build_adapters(project)
    mcu = adapters["mcu"]
    steps = CoSimulationOrchestrator(project, adapters).iterate()
    try:
        for _ in steps:                 # run to the end but read RAM before the adapters stop
            if mcu.client is not None and mcu.client.get_time_us() >= 4000:
                break
        read = lambda address: struct.unpack("<f", struct.pack("<I", mcu.client.bus_read("l476", address)))[0]
        assert mcu.client.bus_read("l476", 0x20000000) == 0x41524D4A      # 'ARMJ': the struct is where we write
        assert (read(0x20000004), read(0x20000008), read(0x2000000C)) == (12.5, 0.75, 0.0625)
    finally:
        steps.close()
    # Stopping kills the whole launcher process group: nothing listens on the port any more, so the
    # next run (a tuning evaluation) can't connect to a Renode that is still shutting down.
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", project.instances["mcu"].options["port"]), timeout=1).close()


def _run(tmp_path, duration, edit=None):
    project = MechatronicProject.load(PROJECT)
    project.duration_seconds = duration
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        project.instances["mcu"].options.update(port=probe.getsockname()[1], executable=RENODE,
                                                log=str(tmp_path / "renode.log"))
    if edit:
        edit(project)
    return CoSimulationOrchestrator(project, build_adapters(project)).run()


def test_homes_the_encoder_to_the_absolute_spi_sensor(tmp_path):
    """The arm starts parked at 0.6 rad. The incremental encoder knows nothing of that; the firmware
    homes it to the AS5047P (SPI, modelled in embeddyn) after the sensor's 10 ms power-up."""
    def park(project):
        project.instances["arm"].options["initial"] = {"qpos:shoulder": 0.6}
        project.signals["arm.angle"] = dataclasses.replace(project.signals["arm.angle"], initial=0.6)
    rows = _run(tmp_path, 0.15, park)
    final = rows[-1]
    assert final["mcu.home_angle"] == pytest.approx(0.6, abs=0.003)
    assert abs(final["mcu.angle"] - final["arm.angle"]) < 0.005          # encoder-derived angle is absolute
    assert abs(final["mcu.abs_angle"] - final["arm.angle"]) < 0.01       # one loop of pipeline lag
    assert final["mcu.abs_frames"] >= 135 and final["mcu.abs_errors"] == 0 and final["mcu.fault"] == 0


def test_encoder_disagreeing_with_the_absolute_sensor_trips_the_drive(tmp_path):
    """A wrong encoder scale (here 20 % too many counts, like a wrong gear ratio) makes the
    encoder angle drift from the absolute angle as the arm moves; after 20 ms the firmware faults
    and switches the H-bridge off."""
    def miscount(project):
        index = next(i for i, c in enumerate(project.couplings) if c.target == "arm.encoder_counts")
        coupling = project.couplings[index]
        project.couplings[index] = dataclasses.replace(
            coupling, transform=dataclasses.replace(coupling.transform, gain=coupling.transform.gain * 1.2))
    rows = _run(tmp_path, 0.6, miscount)
    tripped = next(r for r in rows if int(r["mcu.fault"]) & 1)
    assert 0.1 < tripped["time"] < 0.6
    after = [r for r in rows if r["time"] > tripped["time"] + 0.002]
    assert after and all(r["mcu.duty"] == 0.0 and r["bridge.duty"] == 0.0 for r in after)


def test_hot_motor_is_derated_through_the_i2c_temperature_sensor(tmp_path):
    """Winding at 90 degC: the AS6221 (Renode's I2C model, fed through the sensor bridge) reports it,
    and the firmware limits the duty to (110 - 90) / (110 - 70) = 0.5."""
    def hot(project):
        project.instances["winding"].options["parameters"]["initial_temperature"] = 90.0
        project.signals["motor.winding_temperature"] = dataclasses.replace(
            project.signals["motor.winding_temperature"], initial=90.0)
    rows = _run(tmp_path, 0.3, hot)
    late = [r for r in rows if r["time"] > 0.15]
    assert late[-1]["mcu.motor_temperature"] == pytest.approx(late[-1]["motor.winding_temperature"], abs=1.0)
    limit = late[-1]["mcu.duty_limit"]
    assert 0.42 < limit < 0.5
    assert max(abs(r["mcu.duty"]) for r in late) <= max(r["mcu.duty_limit"] for r in late) + 1e-6
    assert max(abs(r["bridge.duty"]) for r in late) < 0.51 and int(late[-1]["mcu.fault"]) == 0


def test_parallel_tuning_runs_renode_instances_side_by_side(tmp_path):
    """Three firmware-in-the-loop evaluations at once: each launched Renode gets its own port and log."""
    from embeddyn.mechatronics.tuning import load_tuning_spec, tune
    spec = load_tuning_spec(ROOT / "examples/mechatronics/tuning/arm-firmware.yaml")
    spec.duration, spec.max_evaluations, spec.workers = 2.05, 4, 3      # just into the `lift` window (2-3 s)
    result = tune(spec)
    assert result["runs"] >= 4 and not [entry["error"] for entry in result["history"] if "error" in entry]
