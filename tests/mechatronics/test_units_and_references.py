"""Unit conversion in project files and parameter references in couplings."""
import copy
import json
import math
from pathlib import Path

import pytest
import yaml

from embeddyn.mechatronics.compiler import ProjectCompiler
from embeddyn.mechatronics.factory import build_adapters
from embeddyn.mechatronics.model import MechatronicProject
from embeddyn.mechatronics.orchestrator import CoSimulationOrchestrator
from embeddyn.mechatronics.quantities import Quantity, convert, parse_unit

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples" / "mechatronics"


@pytest.mark.parametrize("raw, unit, expected", [
    ("0.5 ms", "s", (0.0005, "s")),
    ("1500 us", "s", (0.0015, "s")),
    ("3 mH", "H", (0.003, "H")),
    ("5 mN*m", "N*m", (0.005, "N*m")),
    ("1 Nm", "N*m", (1.0, "N*m")),
    ("90 deg", "rad", (math.pi / 2, "rad")),
    ("60 rpm", "rad/s", (2 * math.pi, "rad/s")),
    ("300 K", "degC", (26.85, "degC")),
    ("2 kohm", "", (2000.0, "ohm")),
    ("500 g", "", (0.5, "kg")),
    ("2 kg*cm^2", "", (2e-4, "kg*m^2")),
    ("5 min", "", (300.0, "s")),
    ("25 degC", "", (25.0, "degC")),                 # chosen units are kept
    ("10 deg", "", (10.0, "deg")),
    ("0.08 N*m*s/rad", "", (0.08, "N*m*s/rad")),
    ("12 counts", "", (12.0, "counts")),             # unknown: kept as written
    (24, "V", (24.0, "V")),
    ({"value": 3, "unit": "mA"}, "A", (0.003, "A")),
])
def test_quantities_convert_to_the_expected_unit(raw, unit, expected):
    quantity = Quantity.parse(raw, unit)
    assert quantity.value == pytest.approx(expected[0]) and quantity.unit == expected[1]


def test_incompatible_units_are_rejected():
    with pytest.raises(ValueError, match="not a quantity in s"):
        Quantity.parse("3 V", "s")
    with pytest.raises(ValueError, match="different dimensions"):
        convert(1.0, "rad/s", "Hz")                  # angle is a dimension: no silent 2*pi errors
    assert parse_unit("furlongs") is None
    assert Quantity.parse("2 kohm").to("ohm") == pytest.approx(2000.0)


def project(raw):
    return MechatronicProject.from_mapping(raw, EXAMPLES)


def dc_motor():
    return yaml.safe_load((EXAMPLES / "dc-motor.yaml").read_text())


def test_steps_and_signal_values_with_units():
    raw = dc_motor()
    raw["step_seconds"] = "0.5 ms"
    raw["signals"]["firmware.target_velocity"]["initial"] = "60 rpm"
    raw["signals"]["electrical.motor_inductance"]["initial"] = "3 mH"
    p = project(raw)
    assert p.step_seconds == pytest.approx(0.0005)
    assert p.signals["firmware.target_velocity"].initial == pytest.approx(2 * math.pi)
    assert p.signals["electrical.motor_inductance"].initial == pytest.approx(0.003)
    raw["signals"]["electrical.motor_inductance"]["initial"] = "3 ms"
    with pytest.raises(ValueError, match="not a quantity in H"):
        project(raw)


def test_parameter_override_in_other_units():
    p = project(dc_motor())
    p.apply_parameter_overrides({"motor.k": "80 mN*m/A"})
    assert p.parameters["motor.k"].value == pytest.approx(0.08) and p.parameters["motor.k"].unit == "N*m/A"


def rows(p):
    return CoSimulationOrchestrator(p, build_adapters(p, mock=True)).run()


def test_coupling_gain_from_a_parameter_matches_the_number():
    raw = dc_motor()
    assert raw["couplings"][1]["gain"] == "@motor.k"           # the example declares Kt = Ke once
    numeric = copy.deepcopy(raw)
    numeric["couplings"][1]["gain"] = numeric["couplings"][2]["gain"] = 0.08
    assert rows(project(raw)) == rows(project(numeric))


def test_parameter_overrides_reach_couplings():
    p = project(dc_motor())
    baseline = rows(p)[-1]["mechanical.velocity"]
    p.apply_parameter_overrides({"motor.k": 0.1})
    assert [c.transform.gain for c in p.resolved_couplings()[1:3]] == [0.1, 0.1]
    assert rows(p)[-1]["mechanical.velocity"] != baseline


def test_reference_forms_and_errors():
    raw = dc_motor()
    raw["parameters"]["gear"] = 4.0
    raw["couplings"][0]["gain"] = "1/@gear"
    raw["couplings"][0]["offset"] = "-@gear"
    first = project(raw).resolved_couplings()[0].transform
    assert (first.gain, first.offset) == (0.25, -4.0)
    raw["couplings"][0]["gain"] = "@missing"
    with pytest.raises(ValueError, match="unknown parameter missing"):
        project(raw)
    raw["couplings"][0]["gain"] = "2*@gear"
    with pytest.raises(ValueError, match="invalid gain"):
        project(raw)
    raw["couplings"][0]["gain"] = "0.08 V*s/rad"                 # a number with a unit is fine too
    assert project(raw).resolved_couplings()[0].transform.gain == pytest.approx(0.08)


def test_compiled_plan_records_resolved_gain_and_reference(tmp_path):
    compiled = ProjectCompiler().compile(project(dc_motor()), tmp_path)
    snapshot = json.loads((compiled.directory / "project.normalized.json").read_text())
    back_emf = snapshot["couplings"][1]
    assert back_emf["transform"]["gain"] == 0.08 and back_emf["parameters"] == {"gain": "@motor.k"}
