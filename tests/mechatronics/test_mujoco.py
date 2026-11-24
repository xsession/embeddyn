"""MuJoCo multibody backend (examples/mechatronics/mujoco-arm.yaml)."""
from pathlib import Path

import pytest

mujoco = pytest.importorskip("mujoco")

from embeddyn.mechatronics.adapters.mujoco_adapter import MujocoAdapter  # noqa: E402
from embeddyn.mechatronics.factory import build_adapters  # noqa: E402
from embeddyn.mechatronics.gui.document import load_graph  # noqa: E402
from embeddyn.mechatronics.metrics import compute_metrics, parse_metric_specs  # noqa: E402
from embeddyn.mechatronics.model import MechatronicProject  # noqa: E402
from embeddyn.mechatronics.orchestrator import CoSimulationOrchestrator  # noqa: E402
from embeddyn.mechatronics.runtime import CheckpointManager  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
PROJECT = ROOT / "examples/mechatronics/mujoco-arm.yaml"
MODEL = ROOT / "examples/mechatronics/mujoco/arm-joint.xml"


def _project(duration=None):
    project = MechatronicProject.load(PROJECT)
    if duration:
        project.duration_seconds = duration
    return project


def test_arm_joint_closed_loop():
    project = _project()
    rows = CoSimulationOrchestrator(project, build_adapters(project)).run()
    metrics = compute_metrics(rows, parse_metric_specs(project.metrics))
    assert metrics["lift.final_value"] == pytest.approx(1.2, abs=0.01)
    assert metrics["lift.overshoot"] < 5.0 and metrics["lift.settling_time"] < 1.0
    # Holding the arm horizontal needs a steady joint torque against gravity: (0.3*0.125 + 0.2*0.25) * 9.81,
    # delivered by the motor through the 20:1 gear.
    hold = rows[2000]
    assert hold["arm.angle"] == pytest.approx(0.0, abs=0.02)
    assert hold["arm.joint_torque"] == pytest.approx((0.3 * 0.125 + 0.2 * 0.25) * 9.81, rel=0.03)
    assert hold["arm.joint_torque"] == pytest.approx(20 * hold["motor.torque"], rel=1e-6)
    # The joint speed reaches the motor's back-EMF through the 20:1 coupling, one macro step later
    # (the electrical instance runs before the mechanical one inside each step).
    i = max(range(1, len(rows)), key=lambda k: abs(rows[k]["arm.speed"]))
    assert rows[i + 1]["motor.shaft_speed"] == pytest.approx(20 * rows[i]["arm.speed"], rel=1e-12)
    assert metrics["current.peak"] < 12.0 + 1e-6                      # 24 V / 2 ohm stall current


def test_disturbance_torque_is_rejected_by_the_controller():
    def run(disturbance):
        project = _project(2.0)
        signal = project.signals["load.value"]
        project.signals["load.value"] = signal.__class__(**{**signal.__dict__, "initial": disturbance})
        return CoSimulationOrchestrator(project, build_adapters(project)).run()
    free, pushed = run(0.0), run(-1.0)
    # The arm holds the same angle; the motor supplies the extra 1 N*m at the joint.
    assert pushed[-1]["arm.angle"] == pytest.approx(free[-1]["arm.angle"], abs=0.02)
    assert pushed[-1]["arm.joint_torque"] - free[-1]["arm.joint_torque"] == pytest.approx(1.0, abs=0.05)


def test_runs_are_bit_identical_and_resume_from_checkpoint_exactly(tmp_path):
    first = CoSimulationOrchestrator(_project(3.0), build_adapters(_project(3.0))).run(
        checkpoint_manager=CheckpointManager(tmp_path), checkpoint_interval_seconds=1.0)
    second = CoSimulationOrchestrator(_project(3.0), build_adapters(_project(3.0))).run()
    assert first == second                                            # deterministic across runs
    checkpoint = sorted(tmp_path.glob("checkpoint-*.json"))[1]       # about t = 2 s
    saved_at = CheckpointManager.load(checkpoint)["time_seconds"]
    resumed_project = _project(3.0)
    resumed = CoSimulationOrchestrator(resumed_project, build_adapters(resumed_project)).run(resume_from=checkpoint)
    assert resumed[0]["time"] == saved_at and 1.9 < saved_at < 2.1
    # Every instance (python plants, MuJoCo) restores exactly: the tail is bit-identical.
    assert resumed[-1] == first[-1]
    assert resumed[1:] == first[-len(resumed) + 1:]


def test_adapter_addresses_and_step_validation():
    adapter = MujocoAdapter(str(MODEL), inputs={"ctrl:shoulder_motor": "u"},
                            outputs={"qpos:shoulder": "q", "sensor:tip_acc[2]": "az", "xpos:tip[2]": "z"})
    adapter.start({})
    out = adapter.step(0.01, 0.01, {"u": 0.0}).outputs
    assert out["q"] < 0 and out["z"] < 0                               # unpowered arm falls under gravity
    state = adapter.checkpoint()
    after = adapter.step(0.02, 0.01, {"u": 0.1}).outputs
    adapter.restore(state)
    assert adapter.step(0.02, 0.01, {"u": 0.1}).outputs == after
    with pytest.raises(ValueError, match="not a multiple"):
        adapter.step(0.02025, 0.00025, {"u": 0.0})
    for bad, message in [({"qpos:elbow": "x"}, "no joint named"), ({"sensor:tip_acc[3]": "x"}, "3 components"),
                         ({"bogus": "x"}, "kind:name")]:
        with pytest.raises(ValueError, match=message):
            MujocoAdapter(str(MODEL), outputs=bad)
    with pytest.raises(ValueError, match="inputs must be"):
        MujocoAdapter(str(MODEL), inputs={"qpos:shoulder": "x"})


def test_lab_derives_pins_from_the_model():
    graph = load_graph(PROJECT)
    arm = next(n for n in graph["nodes"] if n["id"] == "arm")
    pins = {(p["name"], p["direction"]): p["signal"] for p in arm["ports"]}
    assert pins[("ctrl:shoulder_motor", "in")] == "motor.torque"
    assert pins[("qfrc:shoulder", "in")] == "load.value"
    assert pins[("sensor:tip_acc[0]", "out")] is None                 # offered by the model, not mapped
    assert ("sensor:joint_torque", "out") in pins
    nets = {(w["signal"], w["target"]["node"]) for w in graph["wires"] if w["kind"] == "net"}
    assert ("motor.torque", "arm") in nets and ("arm.angle", "controller") in nets
