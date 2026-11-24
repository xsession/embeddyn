import math
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

from embeddyn.mechatronics.adapters.fmu import FmuAdapter, FmuError
from embeddyn.mechatronics.factory import build_adapters
from embeddyn.mechatronics.model import Domain, MechatronicProject
from embeddyn.mechatronics.orchestrator import CoSimulationOrchestrator
from embeddyn.mechatronics.runtime import InstanceMap, ScheduleEntry, build_scheduler
from embeddyn.mechatronics.model import SchedulerConfig

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "examples/mechatronics/fmus/rotational_load/build_fmu.py"
TWO_WHEEL = ROOT / "examples/mechatronics/two-wheel-drive.yaml"
HAVE_CC = shutil.which("cc") is not None or shutil.which("gcc") is not None
needs_cc = pytest.mark.skipif(not HAVE_CC or not sys.platform.startswith(("linux", "darwin")),
                              reason="needs a C compiler to build the example FMU")


@pytest.fixture(scope="module")
def fmus(tmp_path_factory):
    if not HAVE_CC:
        pytest.skip("no C compiler")
    out = tmp_path_factory.mktemp("fmus")
    subprocess.run([sys.executable, str(BUILD), str(out)], check=True, capture_output=True)
    return {"2.0": out / "rotational_load.fmu", "3.0": out / "rotational_load_fmi3.fmu"}


def _project(tmp_path, instances, signals, **extra) -> Path:
    data = {"name": "t", "step_seconds": 0.001, "duration_seconds": 0.01, "instances": instances,
            "signals": signals, **extra}
    path = tmp_path / "p.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


# -- instances -----------------------------------------------------------------

def test_instance_parsing_and_validation(tmp_path):
    decay = {"domain": "mechanical", "model": "embeddyn.mechatronics.plants:ThermalRC", "options": {"backend": "python"}}
    project = MechatronicProject.load(_project(tmp_path, {"a": decay, "b": {**decay, "step": 0.0005}}, {}))
    assert list(project.simulator_instances()) == ["a", "b"]      # no default core instances
    assert project.simulator_instances()["b"].step_seconds == 0.0005
    assert project.scheduler.algorithm == "multirate-fixed-step"
    for bad, message in [({"1x": decay}, "invalid simulator instance name"),
                         ({"a": {"model": "x"}}, "needs a domain"),
                         ({"a": {**decay, "step": 0.0007}}, "must divide")]:
        with pytest.raises(ValueError, match=message):
            MechatronicProject.load(_project(tmp_path, bad, {}))
    with pytest.raises(ValueError, match="duplicate"):
        path = _project(tmp_path, {"mechanical": decay}, {}, simulators={"mechanical": {"options": {"backend": "mock"}}})
        MechatronicProject.load(path)


def test_legacy_project_keeps_domain_keys():
    project = MechatronicProject.load(ROOT / "examples/mechatronics/dc-motor.yaml")
    adapters = build_adapters(project, mock=True)
    assert set(adapters) == {"firmware", "electrical", "mechanical"}
    assert adapters[Domain.FIRMWARE] is adapters["firmware"] and Domain.MECHANICAL in adapters
    project.duration_seconds = 0.01
    runtime = CoSimulationOrchestrator(project, {Domain.FIRMWARE: adapters[Domain.FIRMWARE],
                                                 Domain.ELECTRICAL: adapters["electrical"],
                                                 "mechanical": adapters[Domain.MECHANICAL]})
    runtime.run()
    assert runtime.activation_count[Domain.FIRMWARE] == runtime.activation_count["mechanical"] == 20


def test_instance_map_normalises_domain_keys():
    mapping = InstanceMap({Domain.THERMAL: 1})
    mapping["left"] = 2
    assert mapping["thermal"] == 1 and mapping.get(Domain.THERMAL) == 1 and "left" in mapping
    assert list(mapping) == ["thermal", "left"]


def test_scheduler_orders_instances_by_domain_then_declaration():
    entries = [ScheduleEntry("wheel", Domain.MECHANICAL, 0.0005), ScheduleEntry("ctrl", Domain.FIRMWARE),
               ScheduleEntry("motor_b", Domain.ELECTRICAL), ScheduleEntry("motor_a", Domain.ELECTRICAL)]
    scheduler = build_scheduler(SchedulerConfig(algorithm="multirate-fixed-step", macro_step_seconds=0.001), entries)
    plan = [(round(a.time_seconds, 6), a.instance) for a in scheduler.activations(0.0, 0.001)]
    assert plan == [(0.0005, "wheel"), (0.001, "ctrl"), (0.001, "motor_b"), (0.001, "motor_a"), (0.001, "wheel")]


# -- FMU -------------------------------------------------------------------------

@needs_cc
@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_fmu_matches_analytic_response(fmus, version):
    adapter = FmuAdapter(str(fmus[version]), parameters={"J": 0.5, "b": 0.1}, inputs={"tau": "u"},
                         outputs={"w": "y", "theta": "th"})
    assert adapter.description.version == version
    adapter.start({})
    try:
        for k in range(1, 101):
            out = adapter.step(k * 0.01, 0.01, {"u": 1.0}).outputs
        assert out["y"] == pytest.approx(10 * (1 - math.exp(-0.2)), rel=1e-10)
        # theta = (tau/b) * (t - (J/b) * (1 - exp(-b t / J))), J/b = 5
        assert out["th"] == pytest.approx(10 * (1 - 5 * (1 - math.exp(-0.2))), rel=1e-9)
    finally:
        adapter.stop()


@needs_cc
def test_fmu_errors(fmus, tmp_path):
    with pytest.raises(FmuError, match="no real variables named nope"):
        FmuAdapter(str(fmus["2.0"]), outputs={"nope": "x"})
    adapter = FmuAdapter(str(fmus["2.0"]), parameters={"J": -1.0})
    with pytest.raises(FmuError, match="SetReal"):
        adapter.start({})
    # A token that doesn't match the binary: instantiation must fail.
    tampered = tmp_path / "bad.fmu"
    with zipfile.ZipFile(fmus["2.0"]) as source, zipfile.ZipFile(tampered, "w") as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == "modelDescription.xml":
                data = data.replace(b"{8c4e810f", b"{00000000")
            target.writestr(item, data)
    with pytest.raises(FmuError, match="instantiation failed"):
        FmuAdapter(str(tampered)).start({})
    adapter = FmuAdapter(str(fmus["3.0"]))
    with pytest.raises(FmuError, match="no FMU state"):        # checkpoint taken before start()
        adapter.restore(adapter.checkpoint())


@needs_cc
def test_two_wheel_example_with_instances(fmus, tmp_path):
    fmu_dir = ROOT / "examples/mechatronics/fmus"
    target = fmu_dir / "rotational_load.fmu"
    if not target.exists():
        shutil.copy(fmus["2.0"], target)
    project = MechatronicProject.load(TWO_WHEEL)
    project.duration_seconds = 0.4
    adapters = build_adapters(project)
    assert sorted(adapters) == ["ctrl_left", "ctrl_right", "drive_left", "drive_right", "wheel_left", "wheel_right"]
    runtime = CoSimulationOrchestrator(project, adapters)
    rows = runtime.run()
    assert runtime.activation_count["ctrl_left"] == 400 and runtime.activation_count["wheel_left"] == 800
    last = rows[-1]
    assert last["left.wheel_speed"] == pytest.approx(10.0, abs=0.5)
    assert last["right.wheel_speed"] == pytest.approx(6.0, abs=0.6)
    assert last["right.encoder"] > 0
