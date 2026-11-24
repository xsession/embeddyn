import json
import math
from pathlib import Path

import pytest
import yaml

from embeddyn.mechatronics.adapters.python_model import PythonModelAdapter, load_plant_class
from embeddyn.mechatronics.distributed.agent import _run_metrics
from embeddyn.mechatronics.factory import build_adapters
from embeddyn.mechatronics.metrics import compute_metrics, parse_metric_specs, select_metrics, step_response
from embeddyn.mechatronics.model import Domain, MechatronicProject
from embeddyn.mechatronics.orchestrator import CoSimulationOrchestrator
from embeddyn.mechatronics.plants import GearedDrivetrain, ThermalRC
from embeddyn.mechatronics.tuning import cmaes, load_tuning_spec, nelder_mead, score, tune

ROOT = Path(__file__).resolve().parents[2]
SERVO = ROOT / "examples/mechatronics/servo-drivetrain.yaml"


# -- metrics -----------------------------------------------------------------

def _second_order(zeta: float, wn: float, n: int = 4001, end: float = 4.0):
    """Exact unit-step response of a second-order system (underdamped)."""
    wd = wn * math.sqrt(1 - zeta ** 2)
    times = [end * i / (n - 1) for i in range(n)]
    values = [1 - math.exp(-zeta * wn * t) * (math.cos(wd * t) + zeta / math.sqrt(1 - zeta ** 2) * math.sin(wd * t))
              for t in times]
    return times, values


def test_step_response_matches_second_order_theory():
    zeta, wn = 0.3, 10.0
    times, values = _second_order(zeta, wn)
    figures = step_response(times, values, 1.0, settling_band=0.02)
    assert figures["overshoot"] == pytest.approx(100 * math.exp(-zeta * math.pi / math.sqrt(1 - zeta ** 2)), rel=1e-3)
    assert figures["peak_time"] == pytest.approx(math.pi / (wn * math.sqrt(1 - zeta ** 2)), abs=2e-3)
    # 2 % settling of the envelope bound: 4/(zeta*wn) is the textbook estimate.
    assert 0.8 * 4 / (zeta * wn) < figures["settling_time"] < 1.2 * 4 / (zeta * wn)
    assert figures["steady_state_error"] == pytest.approx(0.0, abs=1e-3)
    assert figures["rise_time"] > 0


def test_unsettled_response_reports_none():
    times = [0.0, 0.5, 1.0]
    figures = step_response(times, [0.0, 0.5, 0.6], 1.0)
    assert figures["settling_time"] is None and figures["rise_time"] is None


def test_compute_and_select_metrics():
    rows = [{"time": t / 10, "y": min(1.0, t / 5), "r": 1.0, "p": 2.0} for t in range(11)]
    specs = parse_metric_specs({"resp": {"type": "step_response", "signal": "y", "target": "r"},
                                "power": {"type": "integral", "signal": "p"},
                                "y": {"type": "stats", "signal": "y"}})
    metrics = compute_metrics(rows, specs)
    assert metrics["power.integral"] == pytest.approx(2.0)
    assert metrics["y.peak"] == 1.0
    assert select_metrics(metrics, ["integral", "resp.overshoot"]) == {"integral": 2.0, "resp.overshoot": 0.0}
    with pytest.raises(KeyError, match="ambiguous"):
        select_metrics({"a.peak": 1, "b.peak": 2}, ["peak"])
    with pytest.raises(ValueError, match="target"):
        parse_metric_specs({"x": {"type": "step_response", "signal": "y"}})


def test_project_rejects_metric_on_unknown_signal(tmp_path):
    data = yaml.safe_load(SERVO.read_text())
    data["metrics"] = {"bad": {"type": "stats", "signal": "nope.signal"}}
    path = tmp_path / "p.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="unknown signal"):
        MechatronicProject.load(path)


def test_agent_extracts_metrics_from_run_summary():
    stdout = "some log line\n" + json.dumps({"project": "p", "metrics": {"speed.overshoot": 3.0, "current.peak": 9.0}}, indent=2)
    assert _run_metrics(stdout, ["overshoot"]) == {"overshoot": 3.0}
    assert _run_metrics(stdout, None)["current.peak"] == 9.0
    assert _run_metrics(stdout, ["missing"])["missing"] is None


# -- tuning ------------------------------------------------------------------

def test_nelder_mead_finds_bounded_minimum():
    point, value = nelder_mead(lambda x: (x[0] - 0.3) ** 2 + (x[1] - 0.8) ** 2, [0.9, 0.1], 200, 1e-7)
    assert point == pytest.approx([0.3, 0.8], abs=1e-3) and value < 1e-6
    point, _ = nelder_mead(lambda x: -x[0], [0.5], 60)        # optimum on the bound
    assert point[0] == pytest.approx(1.0)


def _spec_file(tmp_path, **extra) -> Path:
    spec = {"project": str(ROOT / "examples/mechatronics/dc-motor.yaml"), "mock": True, "duration": 0.6,
            "parameters": {"controller.kp": {"initial": 0.12, "min": 0.01, "max": 2.0, "scale": "log"},
                           "controller.ki": {"initial": 0.8, "min": 0.05, "max": 20.0, "scale": "log"}},
            "objective": [{"metric": "speed.itae"}],
            "constraints": [{"metric": "speed.overshoot", "max": 5.0}],
            "max_evaluations": 12, **extra}
    path = tmp_path / "tune.yaml"
    path.write_text(yaml.safe_dump(spec))
    return path


def test_tuning_improves_objective_on_mock_system(tmp_path):
    spec = load_tuning_spec(_spec_file(tmp_path))
    result = tune(spec)
    assert result["evaluations"] == 12
    assert result["best"]["objective"] < result["initial"]["objective"]
    assert result["best"]["parameters"]["controller.kp"] <= 2.0


def test_batched_nelder_mead_takes_the_sequential_path():
    """With a batch evaluator the counted evaluations (and the simplex path) are exactly the
    sequential ones; the batch only adds speculative points."""
    rosenbrock = lambda x: (1 - 2 * x[0]) ** 2 + 10 * (2 * x[1] - 4 * x[0] ** 2) ** 2
    sequential_used, batched_used, batches = [], [], []

    def batch(points):
        batches.append(len(points))
        return [rosenbrock(point) for point in points]

    sequential = nelder_mead(rosenbrock, [0.1, 0.9], 60, 1e-6, used=sequential_used)
    batched = nelder_mead(rosenbrock, [0.1, 0.9], 60, 1e-6, batch=batch, used=batched_used)
    assert batched == sequential and batched_used == sequential_used and len(batched_used) == 60
    assert batches[0] == 3 and max(batches) == 3                 # initial simplex, then r/e/k per iteration
    assert sum(batches) > len(set(sequential_used))              # some speculative points


def test_parallel_tuning_counts_the_same_evaluations(tmp_path):
    spec = load_tuning_spec(_spec_file(tmp_path))
    sequential = tune(spec)
    spec.workers = 3
    parallel = tune(spec)
    counted = lambda result: [entry["parameters"] for entry in result["history"] if entry["counted"]]
    assert parallel["workers"] == 3 and parallel["evaluations"] == sequential["evaluations"] == 12
    assert set(map(lambda p: tuple(sorted(p.items())), counted(parallel))) == \
        set(map(lambda p: tuple(sorted(p.items())), counted(sequential)))
    assert parallel["runs"] == len(parallel["history"]) > len(counted(parallel))
    assert parallel["speculative_runs"] == parallel["runs"] - len(counted(parallel))
    assert parallel["best"]["objective"] <= sequential["best"]["objective"]
    assert not any("error" in entry for entry in parallel["history"])


def test_private_endpoints_only_touch_launched_renode(tmp_path):
    from embeddyn.mechatronics.factory import use_private_endpoints
    project = MechatronicProject.load(ROOT / "examples/mechatronics/mujoco-arm-firmware.yaml")
    use_private_endpoints(project)
    options = project.instances["mcu"].options
    assert options["port"] != 21260
    assert options["log"].endswith(f"renode-arm-joint.{__import__('os').getpid()}.log")
    assert project.instances["arm"].options.get("port") is None


def _batch_of(function, batches=None):
    def batch(points):
        if batches is not None:
            batches.append(len(points))
        return [function(point) for point in points]
    return batch


def test_cmaes_solves_a_curved_valley_inside_the_box():
    rosenbrock = lambda x: (1 - 2 * x[0]) ** 2 + 100 * (2 * x[1] - 4 * x[0] ** 2) ** 2   # minimum at (0.5, 0.5)
    batches, used = [], []
    point, value = cmaes(_batch_of(rosenbrock, batches), [0.1, 0.9], 1200, sigma=0.3, seed=3, tolerance=1e-9, used=used)
    assert point == pytest.approx([0.5, 0.5], abs=2e-3) and value < 1e-5
    assert set(batches) == {6} and len(used) == sum(batches) <= 1200        # 4 + 3 ln 2 = 6 per generation
    assert all(0.0 <= u <= 1.0 for candidate in used for u in candidate)   # mirrored into the box


def test_cmaes_reaches_an_optimum_on_the_bound_and_is_reproducible():
    slope = lambda x: x[0] + (x[1] - 0.3) ** 2
    first = cmaes(_batch_of(slope), [0.8, 0.8], 300, seed=7)
    again = cmaes(_batch_of(slope), [0.8, 0.8], 300, seed=7)
    assert first == again
    assert first[0][0] < 0.01 and first[0][1] == pytest.approx(0.3, abs=0.01)
    other = cmaes(_batch_of(slope), [0.8, 0.8], 300, seed=8)
    assert other[0] != first[0]


def test_cmaes_tuning_does_not_depend_on_the_worker_count(tmp_path):
    spec = load_tuning_spec(_spec_file(tmp_path, method="cma-es", population=4, max_evaluations=13))
    sequential = tune(spec)
    spec.workers = 4
    parallel = tune(spec)
    points = lambda result: [entry["parameters"] for entry in result["history"]]
    assert sequential["method"] == "cmaes" and points(parallel) == points(sequential)
    assert sequential["evaluations"] == sequential["runs"] == 13 and sequential["speculative_runs"] == 0
    assert sequential["initial"]["parameters"] == pytest.approx({"controller.kp": 0.12, "controller.ki": 0.8})
    assert sequential["best"]["objective"] < sequential["initial"]["objective"]
    with pytest.raises(ValueError, match="unknown tuning method"):
        load_tuning_spec(_spec_file(tmp_path, method="annealing"))


def test_score_penalises_violations_and_missing(tmp_path):
    spec = load_tuning_spec(_spec_file(tmp_path, constraints=[{"metric": "speed.settling_time", "max": 0.1}]))
    value, violations = score(spec, {"speed.itae": 0.2, "speed.settling_time": None})
    assert value > 1e5 and violations == ["speed.settling_time not available"]
    value, violations = score(spec, {"speed.itae": 0.2, "speed.settling_time": 0.05})
    assert value == pytest.approx(0.2) and not violations


# -- plants / python backend -------------------------------------------------

def test_drivetrain_backlash_and_gear_ratio():
    plant = GearedDrivetrain(gear_ratio=10, backlash=0.01, shaft_stiffness=1000, shaft_damping=0, load_inertia=1.0)
    inside = [0.04, 0.0, 0.0, 0.0]           # twist 0.004 < half gap 0.005: no torque
    assert plant.output(0, inside, {})["shaft_torque"] == 0.0
    outside = [0.08, 0.0, 0.0, 0.0]          # twist 0.008 -> spring compresses 0.003
    assert plant.output(0, outside, {})["shaft_torque"] == pytest.approx(3.0)
    assert plant.output(0, [0, 0, 2 * math.pi, 0], {})["encoder_count"] == 0.0
    encoder = GearedDrivetrain(encoder_cpr=4096)
    assert encoder.output(0, [0, 0, math.pi, 0], {})["encoder_count"] == 2048.0


def test_python_adapter_conserves_steady_state_and_checkpoints():
    """Rigid, frictionless drivetrain under constant torque: speed = T*t/(Jm + Jl/N^2)."""
    adapter = PythonModelAdapter("embeddyn.mechatronics.plants:GearedDrivetrain",
                                 parameters={"gear_ratio": 5, "motor_inertia": 0.01, "load_inertia": 0.25,
                                             "motor_damping": 0, "load_damping": 0, "shaft_stiffness": 1e5,
                                             "shaft_damping": 50},
                                 inputs={"motor_torque": "tm"}, outputs={"motor_speed": "w"}, max_step=1e-5)
    adapter.start({})
    for k in range(1, 11):
        out = adapter.step(k * 0.01, 0.01, {"tm": 0.2}).outputs
    assert out["w"] == pytest.approx(0.2 * 0.1 / (0.01 + 0.25 / 25), rel=2e-3)
    saved = adapter.checkpoint()
    adapter.step(0.2, 0.1, {"tm": 0.2})
    adapter.restore(saved)
    assert adapter.time == pytest.approx(0.1)


def test_thermal_rc_reaches_steady_state():
    adapter = PythonModelAdapter("embeddyn.mechatronics.plants:ThermalRC",
                                 parameters={"thermal_resistance": 2.0, "thermal_capacitance": 1.0, "alpha": 0.0},
                                 inputs={"current": "i", "ambient": "amb"},
                                 outputs={"temperature": "T", "resistance": "R"}, max_step=1e-2)
    adapter.start({})
    out = adapter.step(40.0, 40.0, {"i": 3.0, "amb": 20.0}).outputs  # 20 time constants
    assert out["T"] == pytest.approx(20.0 + 9.0 * 2.0 * 2.0, rel=1e-6)  # P = I^2 R = 18 W, x R_th


def test_plant_from_file_and_validation(tmp_path):
    source = tmp_path / "plant.py"
    source.write_text("""
class Decay:
    inputs = ()
    outputs = ("x",)
    states = ("x",)
    def __init__(self, rate=1.0): self.rate = rate
    def initial_state(self): return [1.0]
    def derivatives(self, t, x, u): return [-self.rate * x[0]]
    def output(self, t, x, u): return {"x": x[0]}
""")
    assert load_plant_class("plant.py:Decay", tmp_path).__name__ == "Decay"
    adapter = PythonModelAdapter("plant.py:Decay", root=str(tmp_path), parameters={"rate": "@k"},
                                 outputs={"x": "s.x"}, max_step=1e-3)
    adapter.start({"k": 2.0})
    assert adapter.step(1.0, 1.0, {}).outputs["s.x"] == pytest.approx(math.exp(-2.0), rel=1e-9)
    with pytest.raises(ValueError, match="unknown plant inputs"):
        PythonModelAdapter("plant.py:Decay", root=str(tmp_path), inputs={"nope": "a"})
    with pytest.raises(ValueError, match="unknown project parameter"):
        PythonModelAdapter("plant.py:Decay", root=str(tmp_path), parameters={"rate": "@missing"}).start({})


def test_servo_example_runs_all_four_domains():
    project = MechatronicProject.load(SERVO)
    project.duration_seconds = 0.2
    adapters = build_adapters(project)
    assert set(adapters) == {Domain.FIRMWARE, Domain.ELECTRICAL, Domain.MECHANICAL, Domain.THERMAL}
    runtime = CoSimulationOrchestrator(project, adapters)
    rows = runtime.run()
    last = rows[-1]
    assert last["mechanical.load_velocity"] == pytest.approx(last["mechanical.velocity"] / 10, abs=0.2)
    assert last["thermal.winding_temperature"] > 25.0
    assert last["electrical.motor_resistance"] > 2.0               # thermal feedback reached the electrical model
    assert all(runtime.activation_count[d] == 400 for d in adapters)
