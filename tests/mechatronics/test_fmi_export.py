"""FMU state in checkpoints, FMU export of python plants, FMPy checks."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from embeddyn.mechatronics.adapters.fmu import FmuAdapter, FmuError
from embeddyn.mechatronics.factory import build_adapters
from embeddyn.mechatronics.model import MechatronicProject
from embeddyn.mechatronics.orchestrator import CoSimulationOrchestrator
from embeddyn.mechatronics.runtime import CheckpointManager

ROOT = Path(__file__).resolve().parents[2]
FMU_SOURCE = ROOT / "examples/mechatronics/fmus/rotational_load"
LAB = ROOT / "examples/mechatronics/rc-servo-lab.yaml"
needs_cc = pytest.mark.skipif(shutil.which("cc") is None and shutil.which("gcc") is None, reason="needs a C compiler")


@pytest.fixture(scope="module")
def c_fmus(tmp_path_factory):
    out = tmp_path_factory.mktemp("cfmu")
    subprocess.run([sys.executable, str(FMU_SOURCE / "build_fmu.py"), str(out)], check=True, capture_output=True)
    return {"2.0": out / "rotational_load.fmu", "3.0": out / "rotational_load_fmi3.fmu"}


@needs_cc
@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_fmu_state_round_trip(c_fmus, version):
    def fresh():
        adapter = FmuAdapter(str(c_fmus[version]), parameters={"J": 0.02, "b": 0.1},
                             inputs={"tau": "tau"}, outputs={"w": "w", "theta": "theta"})
        adapter.start({})
        return adapter
    adapter = fresh()
    assert adapter.restorable and adapter.capabilities()["checkpoint"]
    for k in range(1, 51):
        adapter.step(k * 0.001, 0.001, {"tau": 0.5})
    saved = adapter.checkpoint()
    assert saved["restorable"] and saved["state"] and saved["time"] == pytest.approx(0.05)
    tail = [adapter.step(k * 0.001, 0.001, {"tau": 0.5 - k * 1e-3}).outputs for k in range(51, 101)]
    adapter.restore(saved)                                        # roll back in the same instance
    assert [adapter.step(k * 0.001, 0.001, {"tau": 0.5 - k * 1e-3}).outputs for k in range(51, 101)] == tail
    other = fresh()                                               # and into a new instance
    other.restore(saved)
    assert [other.step(k * 0.001, 0.001, {"tau": 0.5 - k * 1e-3}).outputs for k in range(51, 101)] == tail
    adapter.stop(), other.stop()


@needs_cc
def test_fmu_without_state_flags_refuses_resume(c_fmus, tmp_path):
    import zipfile
    stateless = tmp_path / "stateless.fmu"
    with zipfile.ZipFile(c_fmus["2.0"]) as source, zipfile.ZipFile(stateless, "w") as target:
        for item in source.infolist():
            data = source.read(item)
            if item.filename == "modelDescription.xml":
                data = data.replace(b'canGetAndSetFMUstate="true" canSerializeFMUstate="true"', b"")
            target.writestr(item, data)
    adapter = FmuAdapter(str(stateless), outputs={"w": "w"})
    adapter.start({})
    record = adapter.checkpoint()
    assert record == {"fmu": "stateless.fmu", "time": 0.0, "restorable": False}
    with pytest.raises(FmuError, match="cannot save its state"):
        adapter.restore(record)
    adapter.stop()


pythonfmu3 = pytest.importorskip("pythonfmu3")


@pytest.fixture(scope="module")
def servo_fmu(tmp_path_factory):
    from embeddyn.mechatronics.fmi_export import export_plant
    return export_plant("embeddyn.mechatronics.plants:RCServo", tmp_path_factory.mktemp("export"),
                        parameters={"load_inertia": 2e-4, "coulomb_friction": 0.01})


def test_plant_description_and_name_clashes():
    from embeddyn.mechatronics.fmi_export import describe_plant
    info = describe_plant("embeddyn.mechatronics.plants:RCServo", input_start={"load_torque": 0.1})
    assert info["parameters"]["p_supply"] == ("supply", 6.0)       # clashes with the `supply` input
    assert info["parameters"]["p_angle"] == ("angle", 0.0)         # clashes with the `angle` output
    assert info["fixed"]["pulse_valid"] == [4.0e-4, 2.7e-3]        # sequences are fixed at export
    assert info["input_start"] == {"pulse_width": 0.0, "load_torque": 0.1, "supply": 6.0}
    with pytest.raises(ValueError, match="no inputs"):
        describe_plant("embeddyn.mechatronics.plants:RCServo", input_start={"bogus": 1})
    with pytest.raises(ValueError, match="no parameters"):
        describe_plant("embeddyn.mechatronics.plants:RCServo", parameters={"bogus": 1})


def _lab(duration, fmu=None):
    project = MechatronicProject.load(LAB)
    project.duration_seconds = duration
    if fmu is not None:
        servo = project.instances["servo"]
        servo.model = str(fmu)
        for key in ("parameters", "max_step"):
            servo.options.pop(key)
        servo.options["backend"] = "fmu"
    return project


def test_exported_fmu_reproduces_the_python_plant_exactly(servo_fmu):
    python_rows = CoSimulationOrchestrator(_lab(1.0), build_adapters(_lab(1.0))).run()
    fmu_project = _lab(1.0, servo_fmu)
    fmu_rows = CoSimulationOrchestrator(fmu_project, build_adapters(fmu_project)).run()
    assert fmu_rows == python_rows                     # same RK4 sub-stepping inside the FMU: bit-identical


def test_exported_fmu_resumes_from_a_project_checkpoint(servo_fmu, tmp_path):
    full = CoSimulationOrchestrator(_lab(1.5, servo_fmu), build_adapters(_lab(1.5, servo_fmu))).run(
        checkpoint_manager=CheckpointManager(tmp_path), checkpoint_interval_seconds=0.5)
    checkpoint = sorted(tmp_path.glob("checkpoint-*.json"))[1]       # t = 1.0 s
    resumed = CoSimulationOrchestrator(_lab(1.5, servo_fmu), build_adapters(_lab(1.5, servo_fmu))).run(
        resume_from=checkpoint)
    assert resumed[0]["time"] == 1.0 and resumed == full[1000:]


fmpy = pytest.importorskip("fmpy")


def test_fmpy_validates_and_simulates_the_export(servo_fmu):
    from embeddyn.mechatronics.fmi_export import check_fmu
    report = check_fmu(servo_fmu)
    assert report["valid"], report["problems"]
    assert report["fmi_version"] == "3.0" and report["can_get_set_state"] and report["can_serialize_state"]
    assert report["variables"]["pulse_width"] == "input" and report["variables"]["angle_deg"] == "output"
    assert report["variables"]["time"] == "independent"


@needs_cc
def test_fmpy_checks_the_c_example(c_fmus):
    from embeddyn.mechatronics.fmi_export import check_fmu
    for path in c_fmus.values():
        report = check_fmu(path)
        assert report["simulated"], report["problems"]
