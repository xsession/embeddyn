"""Whole-system loop: STM32L476 firmware (Renode >= 1.17) + H-bridge + DC motor + MuJoCo arm."""
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
