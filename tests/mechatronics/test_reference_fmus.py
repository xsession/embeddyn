"""embeddyn's FMU importer against the Modelica Association Reference-FMUs, with FMPy as the oracle.

Opt-in: python tools/fmi/fetch_reference_fmus.py DIR && EMBEDDYN_REFERENCE_FMUS=DIR pytest ...
"""
import os
from pathlib import Path

import pytest

from embeddyn.mechatronics.adapters.fmu import FmuAdapter

ROOT = os.environ.get("EMBEDDYN_REFERENCE_FMUS")
pytestmark = pytest.mark.skipif(not ROOT, reason="set EMBEDDYN_REFERENCE_FMUS (tools/fmi/fetch_reference_fmus.py)")
fmpy = pytest.importorskip("fmpy")

CASES = {"Dahlquist": ["x"], "VanDerPol": ["x0", "x1"], "BouncingBall": ["h", "v"]}
STEP, STEPS = 0.01, 100


def _path(version: str, name: str) -> Path:
    path = Path(ROOT) / version / f"{name}.fmu"
    if not path.exists():
        pytest.skip(f"{path} missing")
    return path


def _run(adapter, first=1, last=STEPS, inputs=None):
    return [adapter.step(k * STEP, STEP, inputs(k) if inputs else {}).outputs for k in range(first, last + 1)]


@pytest.mark.parametrize("version", ["2.0", "3.0"])
@pytest.mark.parametrize("name", sorted(CASES))
def test_matches_fmpy_exactly(version, name):
    path, outputs = _path(version, name), CASES[name]
    adapter = FmuAdapter(str(path), outputs={o: o for o in outputs})
    adapter.start({})
    rows = _run(adapter)
    adapter.stop()
    reference = fmpy.simulate_fmu(str(path), stop_time=STEPS * STEP, output_interval=STEP,
                                  fmi_type="CoSimulation", output=outputs)
    for output in outputs:                                   # FMPy row 0 is t = 0
        assert [row[output] for row in rows] == list(reference[output][1:STEPS + 1])


@pytest.mark.parametrize("version", ["2.0", "3.0"])
@pytest.mark.parametrize("name", sorted(CASES))
def test_state_round_trip(version, name):
    path, outputs = _path(version, name), CASES[name]
    adapter = FmuAdapter(str(path), outputs={o: o for o in outputs})
    adapter.start({})
    assert adapter.restorable
    _run(adapter, 1, 50)
    saved = adapter.checkpoint()
    tail = _run(adapter, 51, STEPS)
    adapter.restore(saved)
    assert _run(adapter, 51, STEPS) == tail
    adapter.stop()


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_inputs_reach_the_fmu(version):
    path = _path(version, "Feedthrough")
    adapter = FmuAdapter(str(path), inputs={"Float64_continuous_input": "u"},
                         outputs={"Float64_continuous_output": "y"})
    adapter.start({"u": 0.0})
    rows = _run(adapter, inputs=lambda k: {"u": 0.5 * k})
    assert [row["y"] for row in rows] == [0.5 * k for k in range(1, STEPS + 1)]
    adapter.stop()
