"""NgspiceSharedAdapter: parsing/maths always; simulation when libngspice is available.

The simulation tests run when libngspice is found (system library or
NGSPICE_LIBRARY) and are skipped otherwise.
"""
import csv
import math
from pathlib import Path

import pytest

from embeddyn.mechatronics.adapters.ngspice_shared import (
    NgspiceError,
    NgspiceSharedAdapter,
    _reduce,
    find_library,
    load_ngspice_bindings,
    prepare_netlist,
)
from embeddyn.mechatronics.factory import build_adapters
from embeddyn.mechatronics.model import Domain, MechatronicProject
from embeddyn.mechatronics.orchestrator import CoSimulationOrchestrator

ROOT = Path(__file__).resolve().parents[2]
HBRIDGE = ROOT / "examples/mechatronics/dc-motor-ngspice.yaml"

try:
    LIBRARY = find_library()
except NgspiceError:
    LIBRARY = None
needs_ngspice = pytest.mark.skipif(LIBRARY is None, reason="libngspice not found (install libngspice0 or set NGSPICE_LIBRARY)")


def test_prepare_netlist_substitutes_and_replaces_analyses():
    text = """* {{not.a.signal}} in a comment is left alone
R1 a b {{electrical.r}}
.tran 1u 1m
.control
run
.endc
.end
"""
    lines = prepare_netlist(text, {"electrical.r": 2.5}, ".tran 1e-6 1 0 1e-6 uic")
    assert "R1 a b 2.5" in lines
    assert lines[-2:] == [".tran 1e-6 1 0 1e-6 uic", ".end"]
    assert not any(line.strip().lower() in {"run", ".control", ".endc", ".tran 1u 1m"} for line in lines)
    with pytest.raises(ValueError, match="unknown"):
        prepare_netlist("R1 a b {{unknown}}\n", {}, ".tran 1 1")


def test_bindings_validation():
    bindings = load_ngspice_bindings(inline=[
        {"signal": "e.duty", "source": "Vd", "min": 0, "max": 1},
        {"signal": "e.r", "alter": "Rmotor"},
        {"signal": "e.i", "vector": "i(Vsense)", "mode": "rms"},
    ])
    assert [b.kind for b in bindings.inputs] == ["source", "alter"]
    assert bindings.inputs[0].value(1.7) == 1.0
    assert bindings.outputs[0].mode == "rms"
    with pytest.raises(ValueError, match="exactly one"):
        load_ngspice_bindings(inline=[{"signal": "x", "direction": "input"}])
    with pytest.raises(ValueError, match="mode"):
        load_ngspice_bindings(inline=[{"signal": "x", "vector": "V(a)", "mode": "median"}])


def test_reduce_modes():
    times, values = [0.0, 1.0, 2.0], [0.0, 2.0, 0.0]
    assert _reduce(times, values, "mean") == pytest.approx(1.0)
    assert _reduce(times, values, "last") == 0.0
    assert _reduce(times, values, "max") == 2.0
    assert _reduce(times, [3.0, 3.0, 3.0], "rms") == pytest.approx(3.0)


def test_factory_builds_shared_adapter():
    project = MechatronicProject.load(HBRIDGE)
    adapter = build_adapters(project)[Domain.ELECTRICAL]
    assert isinstance(adapter, NgspiceSharedAdapter)
    assert adapter.max_step == pytest.approx(1e-6)
    assert {b.target for b in adapter.bindings.inputs} >= {"Vdutyp", "Vdutyn", "Vbus", "Vbemf", "Rmotor"}


RC = """* RC low-pass driven by an external source
Vin in 0 dc 0 external
R1 in out 1k
C1 out 0 1u ic=0
"""


def _rc_adapter(tmp_path, **extra):
    netlist = tmp_path / "rc.cir"
    netlist.write_text(RC, encoding="utf-8")
    return NgspiceSharedAdapter(netlist, inline_bindings=[
        {"signal": "e.vin", "source": "Vin"},
        {"signal": "e.r", "alter": "R1"},
        {"signal": "e.vout", "vector": "V(out)", "mode": "last"},
        {"signal": "e.vout_mean", "vector": "V(out)", "mode": "mean"},
        {"signal": "e.i", "vector": "I(Vin)", "mode": "last", "scale": -1.0},
    ], max_step=1e-5, duration=0.01, **extra)


@needs_ngspice
def test_state_carries_across_steps(tmp_path):
    """A 1 ms RC charged in 0.5 ms steps follows the analytic curve exactly at each step."""
    adapter = _rc_adapter(tmp_path)
    adapter.start({"e.vin": 1.0, "e.r": 1000.0})
    try:
        for k in range(1, 7):
            out = adapter.step(k * 5e-4, 5e-4, {"e.vin": 1.0, "e.r": 1000.0}).outputs
            assert out["e.vout"] == pytest.approx(1 - math.exp(-k * 0.5), abs=2e-4)
            assert out["e.i"] == pytest.approx(math.exp(-k * 0.5) / 1000, rel=2e-3)
        assert adapter._time == pytest.approx(3e-3, abs=1e-12)
    finally:
        adapter.stop()


@needs_ngspice
def test_input_hold_alter_and_restart(tmp_path):
    adapter = _rc_adapter(tmp_path)
    adapter.start({"e.vin": 1.0, "e.r": 1000.0})
    try:
        adapter.step(5e-3, 5e-3, {"e.vin": 1.0, "e.r": 1000.0})       # ~fully charged
        out = adapter.step(5.5e-3, 5e-4, {"e.vin": 0.0, "e.r": 100.0}).outputs
        # Discharge with tau = 0.1 ms after `alter R1 = 100`: 0.5 ms -> e^-5.
        assert out["e.vout"] == pytest.approx(math.exp(-5) * (1 - math.exp(-5)), abs=2e-3)
        assert 0.0 < out["e.vout_mean"] < 0.3
    finally:
        adapter.stop()
    # The library can be reused by a new adapter after stop().
    again = _rc_adapter(tmp_path)
    again.start({"e.vin": 1.0, "e.r": 1000.0})
    try:
        assert again.step(5e-4, 5e-4, {"e.vin": 1.0}).outputs["e.vout"] == pytest.approx(1 - math.exp(-0.5), abs=2e-4)
    finally:
        again.stop()


@needs_ngspice
def test_one_owner_per_process(tmp_path):
    first, second = _rc_adapter(tmp_path), _rc_adapter(tmp_path)
    first.start({"e.vin": 1.0, "e.r": 1000.0})
    try:
        with pytest.raises(NgspiceError, match="isolate"):
            second.start({"e.vin": 1.0, "e.r": 1000.0})
    finally:
        first.stop()


@needs_ngspice
def test_hbridge_example_closed_loop(tmp_path):
    """Switching H-bridge in the loop: same trend as the mock RL model, with real
    conduction and supply losses (lower bridge voltage at full duty)."""
    project = MechatronicProject.load(HBRIDGE)
    project.duration_seconds = 0.05
    rows = CoSimulationOrchestrator(project, build_adapters(project)).run(tmp_path / "run.csv")
    last = rows[-1]
    assert last["firmware.pwm_duty"] == pytest.approx(1.0)
    # Mock: 24 V at full duty. Here: minus Rsrc (0.05 Ω) and two switches (2 x 0.02 Ω).
    expected_voltage = 24.0 - last["electrical.motor_current"] * (0.05 + 0.04)
    assert last["electrical.motor_voltage"] == pytest.approx(expected_voltage, abs=0.1)
    assert 10.0 < last["electrical.motor_current"] < 12.5
    assert last["electrical.input_power"] == pytest.approx(24.0 * last["electrical.motor_current"], rel=0.03)
    assert 1.5 < last["mechanical.velocity"] < 2.5
    with (tmp_path / "run.csv").open() as handle:
        assert len(list(csv.DictReader(handle))) == len(rows)


def test_saved_vectors_cover_the_output_bindings():
    from embeddyn.mechatronics.adapters.ngspice_shared import saved_vectors
    outputs = load_ngspice_bindings(inline=[
        {"signal": "a", "vector": "V(a,b)"}, {"signal": "b", "vector": "I(Vsense)"},
        {"signal": "c", "vector": "v(a)"}, {"signal": "d", "vector": "@m1[id]"}]).outputs
    assert saved_vectors(outputs) == ["a", "b", "vsense#branch", "@m1[id]"]
    with pytest.raises(ValueError, match="save must be"):
        NgspiceSharedAdapter("x.cir", save="some")


@needs_ngspice
def test_save_bound_keeps_only_bound_vectors(tmp_path):
    """`save: bound` (default) gives the same outputs as `save: all` with fewer vectors kept."""
    results = {}
    for mode in ("bound", "all"):
        adapter = _rc_adapter(tmp_path, save=mode)
        adapter.start({"e.vin": 1.0, "e.r": 1000.0})
        try:
            results[mode] = [adapter.step(k * 5e-4, 5e-4, {"e.vin": 1.0, "e.r": 1000.0}).outputs for k in range(1, 4)]
            names = {name.lower() for name in adapter.spice.vector_names()}
            assert adapter.health()["vectors"] == len(names)
        finally:
            adapter.stop()
        if mode == "bound":
            assert "in" not in names and {"out", "vin#branch", "time"} <= names
        else:
            assert "in" in names
    assert results["bound"] == results["all"]


PULSED = """* RC driven by a pulse whose period boundaries fall on the co-simulation grid
Vp in 0 PULSE(0 1 0 24.995u 24.995u 10n 50u)
R1 in out 1k
C1 out 0 10n
"""


@needs_ngspice
def test_steps_ending_on_a_source_breakpoint(tmp_path):
    """Regression: the step's stop time used to sit 1e-16 s before a PULSE breakpoint that
    coincides with the grid (every 0.5 ms here), and the next step failed with
    'timestep too small'."""
    netlist = tmp_path / "pulsed.cir"
    netlist.write_text(PULSED, encoding="utf-8")
    adapter = NgspiceSharedAdapter(netlist, inline_bindings=[{"signal": "e.vout", "vector": "V(out)", "mode": "mean"}],
                                   max_step=2e-7, duration=0.003)
    adapter.start({})
    try:
        means = [adapter.step(k * 5e-4, 5e-4, {}).outputs["e.vout"] for k in range(1, 7)]
    finally:
        adapter.stop()
    assert all(m == pytest.approx(0.5, abs=0.02) for m in means)


MOSFET = ROOT / "examples/mechatronics/dc-motor-mosfet.yaml"


def test_mosfet_example_wiring():
    project = MechatronicProject.load(MOSFET)
    adapter = build_adapters(project)[Domain.ELECTRICAL]
    assert adapter.save == "bound" and adapter.spice_options == {"method": "gear"}
    assert "electrical.bridge_loss" in {b.signal for b in adapter.bindings.outputs}
    assert project.parameters["bridge.dead_time"].value == pytest.approx(2e-7)


def _mosfet_run(dead_time: float):
    project = MechatronicProject.load(MOSFET)
    project.duration_seconds = 0.025
    project.apply_parameter_overrides({"bridge.dead_time": dead_time})
    return CoSimulationOrchestrator(project, build_adapters(project)).run()


@needs_ngspice
def test_mosfet_bridge_dead_time_prevents_shoot_through():
    """MOSFET H-bridge in the loop: the dead time is a project parameter that reaches the
    netlist. Without it both switches of a leg conduct at every edge (shoot-through)."""
    blanked, shorted = _mosfet_run(2e-7), _mosfet_run(0.0)
    late = [row for row in blanked if row["time"] >= 0.02]
    assert all(0.3 < row["firmware.pwm_duty"] < 0.9 for row in late)          # switching, not saturated
    loss = sum(row["electrical.bridge_loss"] for row in late) / len(late)
    loss_shorted = sum(row["electrical.bridge_loss"] for row in shorted if row["time"] >= 0.02) / len(late)
    assert 0.3 < loss < 2.0 and loss_shorted > 5 * loss
    # Supply power covers the motor's electrical power plus the bridge losses (within the
    # small source-resistance and bleed losses).
    last = late[-1]
    motor = last["electrical.motor_voltage"] * last["electrical.motor_current"]
    assert last["electrical.input_power"] == pytest.approx(motor + last["electrical.bridge_loss"], rel=0.05)
