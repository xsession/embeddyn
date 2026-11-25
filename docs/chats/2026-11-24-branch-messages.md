# embeddyn development chat: messages only in the earlier branch transcript

Source transcript: `0444761b-2a19-464e-af51-e9dde79af500.jsonl`. Hidden reasoning, harness records and system reminders are omitted; e-mail addresses are redacted; tool inputs over 8000 and tool outputs over 4000 characters are shortened.

### Assistant — 2026-11-24 20:20:59

No response requested.

## User — 2026-11-24 20:20:59

Task (from the session that started this one):
Continue the task you were in the middle of in this conversation - it was handed to you so the user could keep talking in the original session. Pick up exactly where the work stopped and finish it. If this session runs in its own fresh worktree it was normally cut from the original session's branch tip (check `git log -1` if unsure); anything the original session left uncommitted is not there — re-create it from the remaining plan. Remaining plan:

Continue roadmap item 11 in the embeddyn monorepo copy: /media/riko/model_store/GIT_/codelayer_forge/engines/embeddyn (both repos are on `main` after the merge of fault injection; branch first before committing, e.g. `embeddyn-power-electronics`; nothing of this work is committed yet; don't commit .vscode/).

Done so far (uncommitted):
- embeddyn/mechatronics/adapters/ngspice_shared.py: the step's stop condition is now `stop when time >= {target}` (was target - epsilon). ngspice turns `stop when` into a breakpoint; landing 2e-16 s before a source breakpoint (PULSE carrier at 0.5 ms) made the next step "timestep too small". Run tests/mechatronics/test_ngspice_shared.py and the dc-motor-ngspice tests to confirm nothing regressed; add a regression test (stop on a grid point that coincides with a PULSE breakpoint).
- examples/mechatronics/circuits/motor-hbridge-mosfet.cir: MOSFET H-bridge (generic VDMOS with body diodes), tanh gate drivers with dead time from parameter {{bridge.dead_time}} and gate resistor {{bridge.gate_resistance}}, idle legs held on at duty 0/1, outputs V(ploss) (device dissipation), i(Vsense), V(a,b), V(pin). Prototype (scratchpad proto.py: duty 0.6, 24 V, bemf 6 V, 20 ms): loss 5.9 W at 0 ns (shoot-through), 3.6-4.0 W at 50-100 ns, minimum 0.49 W at 200 ns, 0.56-0.63 W at 0.5-2 us with Vab falling 14.3 -> 13.3 V (dead-time distortion). Peak low-side current is dominated by reverse-recovery ringing: don't expose it. ~3.5 s per 20 ms simulated.

Remaining:
1. `.save` filtering in NgspiceSharedAdapter: option `save: bound | all` (default bound) emitting `.save` for the vectors the output bindings read (V(a,b) -> a b; I(Vx) -> vx#branch; raw names as-is); test that results equal `save: all` and an unbound node is absent; report the vector-count reduction.
2. Example project examples/mechatronics/dc-motor-mosfet.yaml (copy dc-motor-ngspice.yaml; parameters bridge.dead_time 200 ns, bridge.gate_resistance 10 ohm; binding file bindings/ngspice-hbridge-mosfet.yaml adding electrical.bridge_loss mean of V(ploss); metrics: bridge_loss stats + integral). Optional tuning spec examples/mechatronics/tuning/dead-time.yaml minimising bridge loss energy over bridge.dead_time.
3. Tests (needs NGSPICE_LIBRARY=/media/riko/model_store/ngspice-lib/root/usr/lib/x86_64-linux-gnu/libngspice.so.0, venv /media/riko/model_store/embeddyn-venv/bin/python, pytest --basetemp=/media/riko/model_store/tmp/...): loss at dead_time 0 >> at 200 ns via parameter override, short duration (~0.05 s).
4. Docs: docs/reference/ngspice.md (save option, MOSFET bridge, dead time), examples table, theory note on dead-time loss trade-off, roadmap item 11 state (KiCad round trip still open). mkdocs build --strict.
5. Run tests/mechatronics, then ask the user before committing; sync to the standalone repo /media/riko/model_store/GIT_/embeddyn via format-patch --relative=engines/embeddyn and check trees match.

**Tool: mcp__ccd_turn__update_plan**

```
{
 "items": [
  {
   "content": "Confirm ngspice stop-time fix, add regression test",
   "status": "in_progress"
  },
  {
   "content": "Add .save filtering to ngspice adapter",
   "status": "pending"
  },
  {
   "content": "Add MOSFET example project and bindings",
   "status": "pending"
  },
  {
   "content": "Add MOSFET bridge tests",
   "status": "pending"
  },
  {
   "content": "Update docs and roadmap",
   "status": "pending"
  },
  {
   "content": "Run tests and docs build",
   "status": "pending"
  }
 ],
 "scope": "first_task"
}
```

<details><summary>Result</summary>

```
Plan shown (6 steps).
```

</details>

**Tool: Bash** — Check notes, repo state and ngspice tests

```
{
 "command": "ls /home/riko/.config/Claude/side-session-notes/local_d764701e-2422-42c3-82c6-a9b83e96afc2 2>/dev/null; cd /media/riko/model_store/GIT_/codelayer_forge && git -c safe.directory='*' status --short; git -c safe.directory='*' branch --show-current; sed -n 1,60p engines/embeddyn/tests/mechatronics/test_ngspice_shared.py",
 "description": "Check notes, repo state and ngspice tests"
}
```

<details><summary>Result</summary>

```
M engines/embeddyn/embeddyn/mechatronics/adapters/ngspice_shared.py
?? .vscode/
?? engines/embeddyn/examples/mechatronics/circuits/motor-hbridge-mosfet.cir
main
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
Shell cwd was reset to /media/riko/model_store/GIT_/embeddyn
```

</details>

**Tool: Bash** — Read rest of ngspice tests

```
{
 "command": "cd /media/riko/model_store/GIT_/codelayer_forge/engines/embeddyn && sed -n 60,400p tests/mechatronics/test_ngspice_shared.py",
 "description": "Read rest of ngspice tests"
}
```

<details><summary>Result</summary>

```
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
    expec
… [549 more characters not shown]
```

</details>

### Assistant — 2026-11-24 20:21:29

Stop-condition fix is in place. Now `.save` filtering.

**Tool: Bash** — Add .save filtering to ngspice adapter

```
{
 "command": "cd /media/riko/model_store/GIT_/codelayer_forge/engines/embeddyn && python3 - <<'EOF'\nfrom pathlib import Path\np = Path(\"embeddyn/mechatronics/adapters/ngspice_shared.py\"); s = p.read_text()\ndef rep(old, new):\n    global s\n    assert s.count(old) == 1, old; s = s.replace(old, new)\nrep('''libngspice holds global state, so only one instance can run per process. Use\n`--isolate` (process-isolated adapters) to run several at once.\n''', '''libngspice holds global state, so only one instance can run per process. Use\n`--isolate` (process-isolated adapters) to run several at once.\n\nngspice keeps every saved vector for the whole run. With `save: bound` (the\ndefault) the netlist gets a `.save` card for just the vectors the output\nbindings read, so memory grows with the bound outputs, not with the size of\nthe circuit; `save: all` keeps ngspice's default (every node and branch).\n''')\nrep('''OUTPUT_MODES = (\"last\", \"mean\", \"rms\", \"min\", \"max\")\n''', '''OUTPUT_MODES = (\"last\", \"mean\", \"rms\", \"min\", \"max\")\nSAVE_MODES = (\"bound\", \"all\")\n''')\nrep('''def prepare_netlist(''', '''def saved_vectors(outputs: Iterable[NgspiceOutputBinding]) -> list[str]:\n    \"\"\"`.save` names for the vectors the output bindings read: V(a,b) -> a b, I(Vx) -> vx#branch.\"\"\"\n    names: list[str] = []\n    for binding in outputs:\n        match = _PROBE.fullmatch(binding.vector.strip())\n        if not match:\n            wanted = [binding.vector.strip()]\n        elif match.group(1).lower() == \"i\":\n            wanted = [f\"{match.group(2).strip()}#branch\"]\n        else:\n            wanted = [match.group(2).strip()] + ([match.group(3).strip()] if match.group(3) else [])\n        names.extend(name.lower() for name in wanted if name.lower() not in names)\n    return names\n\n\ndef prepare_netlist(''')\nrep('''                 max_step: float | None = None, duration: float | None = None,\n                 options: Mapping[str, Any] | None = None) -> None:''', '''                 max_step: float | None = None, duration: float | None = None,\n                 options: Mapping[str, Any] | None = None, save: str = \"bound\") -> None:''')\nrep('''        self.spice_options = dict(options or {})\n''', '''        self.spice_options = dict(options or {})\n        if save not in SAVE_MODES:\n            raise ValueError(f\"ngspice save must be one of {', '.join(SAVE_MODES)}\")\n        self.save = save\n''')\nrep('''            lines.insert(len(lines) - 2, f\".options {options}\")\n''', '''            lines.insert(len(lines) - 2, f\".options {options}\")\n        if self.save == \"bound\" and self.bindings.outputs:\n            lines.insert(len(lines) - 2, \".save \" + \" \".join(saved_vectors(self.bindings.outputs)))\n''')\np.write_text(s)\np = Path(\"embeddyn/mechatronics/factory.py\"); s = p.read_text()\nold = '''        \"options\": dict(options.get(\"spice_options\", {})),\n    }'''\nassert s.count(old) == 1\ns = s.replace(old, '''        \"options\": dict(options.get(\"spice_options\", {})),\n        \"save\": str(options.get(\"save\", \"bound\")),\n    }''')\np.write_text(s)\nEOF\ngrep -n \"NgspiceSharedAdapter(\" -r embeddyn | head",
 "description": "Add .save filtering to ngspice adapter"
}
```

<details><summary>Result</summary>

```
embeddyn/mechatronics/factory.py:122:            return NgspiceSharedAdapter(**ngspice_shared_kwargs(project, cfg))
embeddyn/mechatronics/adapters/ngspice_shared.py:305:class NgspiceSharedAdapter(SimulatorAdapter):
Shell cwd was reset to /media/riko/model_store/GIT_/embeddyn
```

</details>

**Tool: Bash** — Find ctypes declarations

```
{
 "command": "cd /media/riko/model_store/GIT_/codelayer_forge/engines/embeddyn && grep -n \"restype\\|argtypes\" embeddyn/mechatronics/adapters/ngspice_shared.py",
 "description": "Find ctypes declarations"
}
```

<details><summary>Result</summary>

```
96:        self.lib.ngSpice_Command.argtypes = [ctypes.c_char_p]
97:        self.lib.ngSpice_Circ.argtypes = [ctypes.POINTER(ctypes.c_char_p)]
98:        self.lib.ngGet_Vec_Info.argtypes = [ctypes.c_char_p]
99:        self.lib.ngGet_Vec_Info.restype = ctypes.POINTER(_VectorInfo)
Shell cwd was reset to /media/riko/model_store/GIT_/embeddyn
```

</details>

**Tool: Bash** — Add vector listing, save tests, regression test; run

```
{
 "command": "cd /media/riko/model_store/GIT_/codelayer_forge/engines/embeddyn && python3 - <<'EOF'\nfrom pathlib import Path\np = Path(\"embeddyn/mechatronics/adapters/ngspice_shared.py\"); s = p.read_text()\ndef rep(old, new):\n    global s\n    assert s.count(old) == 1, old; s = s.replace(old, new)\nrep('''        self.lib.ngGet_Vec_Info.restype = ctypes.POINTER(_VectorInfo)\n''', '''        self.lib.ngGet_Vec_Info.restype = ctypes.POINTER(_VectorInfo)\n        self.lib.ngSpice_CurPlot.restype = ctypes.c_char_p\n        self.lib.ngSpice_AllVecs.argtypes = [ctypes.c_char_p]\n        self.lib.ngSpice_AllVecs.restype = ctypes.POINTER(ctypes.c_char_p)\n''')\nrep('''    def vector(self, name: str, start: int = 0) -> list[float]:''', '''    def vector_names(self) -> list[str]:\n        \"\"\"Names of the vectors in the current plot (what ngspice keeps for the run).\"\"\"\n        names, array = [], self.lib.ngSpice_AllVecs(self.lib.ngSpice_CurPlot())\n        index = 0\n        while array and array[index]:\n            names.append(array[index].decode())\n            index += 1\n        return names\n\n    def vector(self, name: str, start: int = 0) -> list[float]:''')\nrep('''    def health(self) -> dict[str, object]:\n        return {**super().health(), \"time\": self._time, \"points\": self._index}''', '''    def health(self) -> dict[str, object]:\n        health: dict[str, object] = {**super().health(), \"time\": self._time, \"points\": self._index, \"save\": self.save}\n        if self.spice is not None and self._started:\n            health[\"vectors\"] = len(self.spice.vector_names())\n        return health''')\np.write_text(s)\nEOF\ncat >> tests/mechatronics/test_ngspice_shared.py <<'EOF'\n\n\ndef test_saved_vectors_cover_the_output_bindings():\n    from embeddyn.mechatronics.adapters.ngspice_shared import saved_vectors\n    outputs = load_ngspice_bindings(inline=[\n        {\"signal\": \"a\", \"vector\": \"V(a,b)\"}, {\"signal\": \"b\", \"vector\": \"I(Vsense)\"},\n        {\"signal\": \"c\", \"vector\": \"v(a)\"}, {\"signal\": \"d\", \"vector\": \"@m1[id]\"}]).outputs\n    assert saved_vectors(outputs) == [\"a\", \"b\", \"vsense#branch\", \"@m1[id]\"]\n    with pytest.raises(ValueError, match=\"save must be\"):\n        NgspiceSharedAdapter(\"x.cir\", save=\"some\")\n\n\n@needs_ngspice\ndef test_save_bound_keeps_only_bound_vectors(tmp_path):\n    \"\"\"`save: bound` (default) gives the same outputs as `save: all` with fewer vectors kept.\"\"\"\n    results = {}\n    for mode in (\"bound\", \"all\"):\n        adapter = _rc_adapter(tmp_path, save=mode)\n        adapter.start({\"e.vin\": 1.0, \"e.r\": 1000.0})\n        try:\n            results[mode] = [adapter.step(k * 5e-4, 5e-4, {\"e.vin\": 1.0, \"e.r\": 1000.0}).outputs for k in range(1, 4)]\n            names = {name.lower() for name in adapter.spice.vector_names()}\n            assert adapter.health()[\"vectors\"] == len(names)\n        finally:\n            adapter.stop()\n        if mode == \"bound\":\n            assert \"in\" not in names and {\"out\", \"vin#branch\", \"time\"} <= names\n        else:\n            assert \"in\" in names\n    assert results[\"bound\"] == results[\"all\"]\n\n\nPULSED = \"\"\"* RC driven by a pulse whose period boundaries fall on the co-simulation grid\nVp in 0 PULSE(0 1 0 24.995u 24.995u 10n 50u)\nR1 in out 1k\nC1 out 0 10n\n\"\"\"\n\n\n@needs_ngspice\ndef test_steps_ending_on_a_source_breakpoint(tmp_path):\n    \"\"\"Regression: the step's stop time used to sit 1e-16 s before a PULSE breakpoint that\n    coincides with the grid (every 0.5 ms here), and the next step failed with\n    'timestep too small'.\"\"\"\n    netlist = tmp_path / \"pulsed.cir\"\n    netlist.write_text(PULSED, encoding=\"utf-8\")\n    adapter = NgspiceSharedAdapter(netlist, inline_bindings=[{\"signal\": \"e.vout\", \"vector\": \"V(out)\", \"mode\": \"mean\"}],\n                                   max_step=2e-7, duration=0.003)\n    adapter.start({})\n    try:\n        means = [adapter.step(k * 5e-4, 5e-4, {}).outputs[\"e.vout\"] for k in range(1, 7)]\n    finally:\n        adapter.stop()\n    assert all(m == pytest.approx(0.5, abs=0.02) for m in means)\nEOF\nexport PYTHONPATH=$PWD NGSPICE_LIBRARY=/media/riko/model_store/ngspice-lib/root/usr/lib/x86_64-linux-gnu/libngspice.so.0 TMPDIR=/media/riko/model_store/tmp; timeout 600 /media/riko/model_store/embeddyn-venv/bin/python -m pytest -q -p no:cacheprovider tests/mechatronics/test_ngspice_shared.py --basetemp=/media/riko/model_store/tmp/embeddyn-pytest-ng 2>&1 | tail -15",
 "description": "Add vector listing, save tests, regression test; run",
 "timeout": 620000
}
```

<details><summary>Result</summary>

```
adapter.start({"e.vin": 1.0, "e.r": 1000.0})
            try:
                results[mode] = [adapter.step(k * 5e-4, 5e-4, {"e.vin": 1.0, "e.r": 1000.0}).outputs for k in range(1, 4)]
                names = {name.lower() for name in adapter.spice.vector_names()}
                assert adapter.health()["vectors"] == len(names)
            finally:
                adapter.stop()
            if mode == "bound":
>               assert "in" not in names and {"out", "vin#branch", "time"} <= names
E               AssertionError: assert ('in' not in {'in', 'out', 'time', 'vin#branch'})

tests/mechatronics/test_ngspice_shared.py:191: AssertionError
=========================== short test summary info ============================
FAILED tests/mechatronics/test_ngspice_shared.py::test_save_bound_keeps_only_bound_vectors
1 failed, 10 passed in 2.43s
Shell cwd was reset to /media/riko/model_store/GIT_/embeddyn
```

</details>

### Assistant — 2026-11-24 20:22:17

The regression test passes. `save: bound` still kept node `in` — checking what ngspice actually received.

**Tool: Bash** — Debug .save card and vector list

```
{
 "command": "cd /media/riko/model_store/GIT_/codelayer_forge/engines/embeddyn && export PYTHONPATH=$PWD NGSPICE_LIBRARY=/media/riko/model_store/ngspice-lib/root/usr/lib/x86_64-linux-gnu/libngspice.so.0; /media/riko/model_store/embeddyn-venv/bin/python - <<'EOF' 2>&1 | tail -20\nfrom pathlib import Path\nfrom embeddyn.mechatronics.adapters import ngspice_shared as ns\nd = Path(\"/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad\"); d.mkdir(parents=True, exist_ok=True)\n(d/\"rc.cir\").write_text(\"* rc\\nVin in 0 dc 0 external\\nR1 in out 1k\\nC1 out 0 1u ic=0\\n\")\norig = ns._Ngspice.load\nns._Ngspice.load = lambda self, lines: (print(\"\\n\".join(lines)), orig(self, lines))[1]\nfor save in (\"bound\", \"all\"):\n    a = ns.NgspiceSharedAdapter(d/\"rc.cir\", inline_bindings=[{\"signal\":\"x\",\"source\":\"Vin\"},{\"signal\":\"o\",\"vector\":\"V(out)\"},{\"signal\":\"i\",\"vector\":\"I(Vin)\"}], max_step=1e-5, duration=0.01, save=save)\n    a.start({\"x\":1}); a.step(5e-4,5e-4,{\"x\":1}); print(save, a.spice.vector_names()); a.stop()\nEOF",
 "description": "Debug .save card and vector list"
}
```

<details><summary>Result</summary>

```
* rc
Vin in 0 dc 0 external
R1 in out 1k
C1 out 0 1u ic=0
.save out vin#branch
.tran 1e-05 1.02 0 1e-05 uic
.end
bound ['vin#branch', 'out', 'in', 'time']
* rc
Vin in 0 dc 0 external
R1 in out 1k
C1 out 0 1u ic=0
.tran 1e-05 1.02 0 1e-05 uic
.end
all ['vin#branch', 'out', 'in', 'time']
Shell cwd was reset to /media/riko/model_store/GIT_/embeddyn
```

</details>

**Tool: Bash** — Try different .save syntaxes

```
{
 "command": "cd /media/riko/model_store/GIT_/codelayer_forge/engines/embeddyn && export PYTHONPATH=$PWD NGSPICE_LIBRARY=/media/riko/model_store/ngspice-lib/root/usr/lib/x86_64-linux-gnu/libngspice.so.0; /media/riko/model_store/embeddyn-venv/bin/python - <<'EOF' 2>&1 | grep -v \"^\\*\\|^\\.\\|^[VRC]\" | tail -20\nfrom pathlib import Path\nfrom embeddyn.mechatronics.adapters import ngspice_shared as ns\nd = Path(\"/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad\")\nfor card in (\".save v(out) i(vin)\", \".save out\", \".save v(out) vin#branch\", \".save all\"):\n    orig = ns.prepare_netlist\n    ns.prepare_netlist = lambda text, values, tran, card=card, orig=orig: orig(text, values, tran)[:-2] + [card] + orig(text, values, tran)[-2:]\n    a = ns.NgspiceSharedAdapter(d/\"rc.cir\", inline_bindings=[{\"signal\":\"x\",\"source\":\"Vin\"}], max_step=1e-5, duration=0.01, save=\"all\")\n    a.start({\"x\":1}); a.step(5e-4,5e-4,{\"x\":1}); print(card, \"->\", a.spice.vector_names()); a.stop()\n    ns.prepare_netlist = orig\nEOF",
 "description": "Try different .save syntaxes"
}
```

<details><summary>Result</summary>

```
Shell cwd was reset to /media/riko/model_store/GIT_/embeddyn
```

</details>

**Tool: Bash** — Try .save syntaxes with marker

```
{
 "command": "cd /media/riko/model_store/GIT_/codelayer_forge/engines/embeddyn && export PYTHONPATH=$PWD NGSPICE_LIBRARY=/media/riko/model_store/ngspice-lib/root/usr/lib/x86_64-linux-gnu/libngspice.so.0; /media/riko/model_store/embeddyn-venv/bin/python - <<'EOF' 2>&1 | grep RESULT\nfrom pathlib import Path\nfrom embeddyn.mechatronics.adapters import ngspice_shared as ns\nd = Path(\"/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad\")\norig = ns.prepare_netlist\nfor card in (\".save v(out) i(vin)\", \".save out\", \".save v(out) vin#branch\", \".save all\"):\n    ns.prepare_netlist = lambda text, values, tran, card=card: orig(text, values, tran)[:-2] + [card] + orig(text, values, tran)[-2:]\n    a = ns.NgspiceSharedAdapter(d/\"rc.cir\", inline_bindings=[{\"signal\":\"x\",\"source\":\"Vin\"}], max_step=1e-5, duration=0.01, save=\"all\")\n    a.start({\"x\":1}); a.step(5e-4,5e-4,{\"x\":1}); print(\"RESULT\", card, \"->\", a.spice.vector_names()); a.stop()\nEOF",
 "description": "Try .save syntaxes with marker"
}
```

<details><summary>Result</summary>

```
RESULT .save v(out) i(vin) -> ['vin#branch', 'out', 'in', 'time']
RESULT .save out -> ['vin#branch', 'out', 'in', 'time']
RESULT .save v(out) vin#branch -> ['vin#branch', 'out', 'in', 'time']
RESULT .save all -> ['vin#branch', 'out', 'in', 'time']
Shell cwd was reset to /media/riko/model_store/GIT_/embeddyn
```

</details>

### Assistant — 2026-11-24 20:22:42

ngspice ignores `.save` cards when loaded through the shared library (it applies them only in batch mode). Trying the interactive `save` command before `run`.

**Tool: Bash** — Try interactive save command

```
{
 "command": "cd /media/riko/model_store/GIT_/codelayer_forge/engines/embeddyn && export PYTHONPATH=$PWD NGSPICE_LIBRARY=/media/riko/model_store/ngspice-lib/root/usr/lib/x86_64-linux-gnu/libngspice.so.0; /media/riko/model_store/embeddyn-venv/bin/python - <<'EOF' 2>&1 | grep RESULT\nfrom pathlib import Path\nfrom embeddyn.mechatronics.adapters import ngspice_shared as ns\nd = Path(\"/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad\")\nfor cmd in (\"save out vin#branch\", \"save v(out) i(vin)\"):\n    a = ns.NgspiceSharedAdapter(d/\"rc.cir\", inline_bindings=[{\"signal\":\"x\",\"source\":\"Vin\"}], max_step=1e-5, duration=0.01, save=\"all\")\n    a.start({\"x\":1}); a.spice.command(cmd); a.step(5e-4,5e-4,{\"x\":1}); a.step(1e-3,5e-4,{\"x\":1}); print(\"RESULT\", cmd, \"->\", a.spice.vector_names(), len(a.spice.vector(\"out\"))); a.stop()\nEOF",
 "description": "Try interactive save command"
}
```

<details><summary>Result</summary>

```
RESULT save out vin#branch -> ['vin#branch', 'out', 'in', 'time'] 114
RESULT save v(out) i(vin) -> ['vin#branch', 'out', 'in', 'time'] 114
Shell cwd was reset to /media/riko/model_store/GIT_/embeddyn
```

</details>

**Tool: Bash** — Test save command on MOSFET circuit

```
{
 "command": "cd /media/riko/model_store/GIT_/codelayer_forge/engines/embeddyn && export PYTHONPATH=$PWD NGSPICE_LIBRARY=/media/riko/model_store/ngspice-lib/root/usr/lib/x86_64-linux-gnu/libngspice.so.0; /media/riko/model_store/embeddyn-venv/bin/python - <<'EOF' 2>&1 | grep RESULT\nfrom embeddyn.mechatronics.adapters import ngspice_shared as ns\nNET = \"examples/mechatronics/circuits/motor-hbridge-mosfet.cir\"\nB = [dict(signal=\"d\", source=\"Vdutyp\", min=0, max=1), dict(signal=\"vbus\", source=\"Vbus\"), dict(signal=\"i\", vector=\"i(Vsense)\"), dict(signal=\"loss\", vector=\"V(ploss)\")]\ninit = {\"d\": 0.6, \"vbus\": 24, \"electrical.motor_resistance\": 2.0, \"electrical.motor_inductance\": 0.003, \"bridge.dead_time\": 2e-7,\n        \"bridge.gate_resistance\": 10.0, \"electrical.supply_voltage\": 24, \"electrical.motor_current\": 0}\nfor cmd in (None, \"save ploss vsense#branch\"):\n    a = ns.NgspiceSharedAdapter(NET, inline_bindings=B, max_step=2e-7, duration=0.01, save=\"all\")\n    a.start(init)\n    if cmd: a.spice.command(cmd)\n    out = a.step(5e-4, 5e-4, init).outputs\n    print(\"RESULT\", cmd, len(a.spice.vector_names()), sorted(a.spice.vector_names())[:12], out); a.stop()\nEOF",
 "description": "Test save command on MOSFET circuit",
 "timeout": 300000
}
```

<details><summary>Result</summary>

```
RESULT None 50 ['a', 'b', 'bgha#branch', 'bghb#branch', 'bgla#branch', 'bglb#branch', 'bloss#branch', 'bpin#branch', 'd1', 'd2', 'd3', 'd4'] {'i': 1.0763918829033494, 'loss': 0.06054434519713382}
RESULT save ploss vsense#branch 50 ['a', 'b', 'bgha#branch', 'bghb#branch', 'bgla#branch', 'bglb#branch', 'bloss#branch', 'bpin#branch', 'd1', 'd2', 'd3', 'd4'] {'i': 1.0763918829033494, 'loss': 0.06054434519713382}
Shell cwd was reset to /media/riko/model_store/GIT_/embeddyn
```

</details>

**Tool: Bash** — Reissue save each step and rerun tests

```
{
 "command": "cd /media/riko/model_store/GIT_/codelayer_forge/engines/embeddyn && python3 - <<'EOF'\nfrom pathlib import Path\np = Path(\"embeddyn/mechatronics/adapters/ngspice_shared.py\"); s = p.read_text()\ndef rep(old, new):\n    global s\n    assert s.count(old) == 1, old; s = s.replace(old, new)\nrep('''ngspice keeps every saved vector for the whole run. With `save: bound` (the\ndefault) the netlist gets a `.save` card for just the vectors the output\nbindings read, so memory grows with the bound outputs, not with the size of\nthe circuit; `save: all` keeps ngspice's default (every node and branch).\n''', '''ngspice keeps every saved vector for the whole run. With `save: bound` (the\ndefault) only the vectors the output bindings read are saved, so memory grows\nwith the bound outputs, not with the size of the circuit; `save: all` keeps\nngspice's default (every node and branch). libngspice ignores `.save` cards\noutside batch mode, and `save` entries share the debug list with `stop`\nbreakpoints, so the adapter issues the `save` command after clearing that\nlist each step.\n''')\nrep('''        if self.save == \"bound\" and self.bindings.outputs:\n            lines.insert(len(lines) - 2, \".save \" + \" \".join(saved_vectors(self.bindings.outputs)))\n''', '''        self._save_command = (\"save \" + \" \".join([\"time\", *saved_vectors(self.bindings.outputs)])\n                              if self.save == \"bound\" and self.bindings.outputs else None)\n''')\nrep('''        self.spice.command(\"delete all\")\n        self.spice.command(f\"stop when''', '''        self.spice.command(\"delete all\")          # clears `stop` and `save` entries alike\n        if self._save_command:\n            self.spice.command(self._save_command)\n        self.spice.command(f\"stop when''')\nrep('''        self._altered: dict[str, float] = {}\n''', '''        self._altered: dict[str, float] = {}\n        self._save_command: str | None = None\n''')\np.write_text(s)\nEOF\ngrep -n \"_altered: dict\\|_save_command\" embeddyn/mechatronics/adapters/ngspice_shared.py; export PYTHONPATH=$PWD NGSPICE_LIBRARY=/media/riko/model_store/ngspice-lib/root/usr/lib/x86_64-linux-gnu/libngspice.so.0 TMPDIR=/media/riko/model_store/tmp; timeout 600 /media/riko/model_store/embeddyn-venv/bin/python -m pytest -q -p no:cacheprovider tests/mechatronics/test_ngspice_shared.py --basetemp=/media/riko/model_store/tmp/embeddyn-pytest-ng 2>&1 | tail -8",
 "description": "Reissue save each step and rerun tests",
 "timeout": 620000
}
```

<details><summary>Result</summary>

```
346:        self._altered: dict[str, float] = {}
347:        self._save_command: str | None = None
371:        self._save_command = ("save " + " ".join(["time", *saved_vectors(self.bindings.outputs)])
406:        if self._save_command:
407:            self.spice.command(self._save_command)
/media/riko/model_store/embeddyn-venv/lib/python3.12/site-packages/pytest_asyncio/plugin.py:306: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to "function" scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session". Set it to "fixture-scope" to keep the current behavior.

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))
...........                                                              [100%]
11 passed in 1.42s
Shell cwd was reset to /media/riko/model_store/GIT_/embeddyn
```

</details>

### Assistant — 2026-11-24 20:23:17

`save: bound` now works and gives identical outputs. Measuring the reduction on the MOSFET bridge.

**Tool: Bash** — Compare save all vs bound on MOSFET bridge

```
{
 "command": "cd /media/riko/model_store/GIT_/codelayer_forge/engines/embeddyn && export PYTHONPATH=$PWD NGSPICE_LIBRARY=/media/riko/model_store/ngspice-lib/root/usr/lib/x86_64-linux-gnu/libngspice.so.0; /media/riko/model_store/embeddyn-venv/bin/python - <<'EOF' 2>&1 | grep RESULT\nimport time, resource\nfrom embeddyn.mechatronics.adapters import ngspice_shared as ns\nNET = \"examples/mechatronics/circuits/motor-hbridge-mosfet.cir\"\nB = [dict(signal=\"d\", source=\"Vdutyp\", min=0, max=1), dict(signal=\"d\", source=\"Vdutyn\", scale=-1, min=0, max=1), dict(signal=\"vbus\", source=\"Vbus\"), dict(signal=\"bemf\", source=\"Vbemf\"),\n     dict(signal=\"i\", vector=\"i(Vsense)\"), dict(signal=\"vab\", vector=\"V(a,b)\"), dict(signal=\"pin\", vector=\"V(pin)\"), dict(signal=\"loss\", vector=\"V(ploss)\")]\ninit = {\"d\": 0.6, \"vbus\": 24, \"bemf\": 6, \"electrical.motor_resistance\": 2.0, \"electrical.motor_inductance\": 0.003, \"bridge.dead_time\": 2e-7,\n        \"bridge.gate_resistance\": 10.0, \"electrical.supply_voltage\": 24, \"electrical.motor_current\": 0}\nfor save in (\"all\", \"bound\"):\n    a = ns.NgspiceSharedAdapter(NET, inline_bindings=B, max_step=2e-7, duration=0.05, save=save)\n    t = time.time(); a.start(init)\n    for k in range(1, 41): out = a.step(k*5e-4, 5e-4, init).outputs\n    n = len(a.spice.vector_names()); pts = a._index\n    print(\"RESULT\", save, f\"{time.time()-t:.1f}s vectors={n} points={pts} MB~={n*pts*8/1e6:.0f}\", {k: round(v,4) for k,v in out.items()}); a.stop()\nEOF",
 "description": "Compare save all vs bound on MOSFET bridge",
 "timeout": 300000
}
```

<details><summary>Result</summary>

```
RESULT all 3.6s vectors=50 points=109364 MB~=44 {'i': 4.1303, 'vab': 14.2591, 'pin': 59.7199, 'loss': 0.4916}
RESULT bound 3.7s vectors=6 points=109364 MB~=5 {'i': 4.1303, 'vab': 14.2591, 'pin': 59.7199, 'loss': 0.4916}
Shell cwd was reset to /media/riko/model_store/GIT_/embeddyn
```

</details>

### Assistant — 2026-11-24 20:23:35

50 → 6 vectors kept (about 44 MB → 5 MB per 20 ms simulated), with identical outputs and the same run time. Now the example project and its bindings.

**Tool: mcp__ccd_turn__update_plan**

```
{
 "done": [
  1,
  2
 ],
 "start": 3
}
```

<details><summary>Result</summary>

```
Plan updated: step 3 of 6 is in progress.
```

</details>

**Tool: Bash** — Create MOSFET bindings and example project

```
{
 "command": "cd /media/riko/model_store/GIT_/codelayer_forge/engines/embeddyn/examples/mechatronics && cat > bindings/ngspice-hbridge-mosfet.yaml <<'EOF'\n# Signal bindings for circuits/motor-hbridge-mosfet.cir (backend: ngspice-shared).\n# Same inputs and outputs as ngspice-hbridge.yaml, plus the MOSFETs' dissipation.\nbindings:\n  - {signal: electrical.pwm_duty, direction: input, source: Vdutyp, min: 0.0, max: 1.0}\n  - {signal: electrical.pwm_duty, direction: input, source: Vdutyn, scale: -1.0, min: 0.0, max: 1.0}\n  - {signal: electrical.supply_voltage, direction: input, source: Vbus}\n  - {signal: electrical.back_emf, direction: input, source: Vbemf}\n\n  - {signal: electrical.motor_current, direction: output, vector: \"i(Vsense)\", mode: mean}\n  - {signal: electrical.motor_voltage, direction: output, vector: \"V(a,b)\", mode: mean}\n  - {signal: electrical.input_power, direction: output, vector: \"V(pin)\", mode: mean}\n  # Channel + body-diode dissipation of the four MOSFETs, averaged over each step.\n  - {signal: electrical.bridge_loss, direction: output, vector: \"V(ploss)\", mode: mean}\nEOF\npython3 - <<'EOF'\nfrom pathlib import Path\ns = Path(\"dc-motor-ngspice.yaml\").read_text()\ndef rep(old, new):\n    global s\n    assert s.count(old) == 1, old; s = s.replace(old, new)\nrep(\"name: dc-motor-hbridge\\n\", \"name: dc-motor-mosfet-bridge\\n\")\nrep(\"duration_seconds: 0.25\\n\", \"duration_seconds: 0.1\\n\")\nrep(\"  description: DC motor speed loop with a switching H-bridge simulated in ngspice (libngspice, state kept across steps).\\n\",\n    \"  description: >\\n    DC motor speed loop on a MOSFET H-bridge in ngspice: gate drivers with\\n    dead time, body diodes, device losses. Dead time and gate resistance are\\n    parameters, so `tune`, `tolerance` and `-p` overrides reach the circuit.\\n\")\nrep(\"  motor.k: {value: 0.08, unit: N*m/A}   # torque constant Kt = back-EMF constant Ke (V*s/rad), declared once\\n\",\n    \"  motor.k: {value: 0.08, unit: N*m/A}   # torque constant Kt = back-EMF constant Ke (V*s/rad), declared once\\n\"\n    \"  bridge.dead_time: {value: 2.0e-7, unit: s}        # {{bridge.dead_time}} in the netlist\\n\"\n    \"  bridge.gate_resistance: {value: 10.0, unit: ohm}  # external gate resistor per MOSFET\\n\")\nrep(\"    model: circuits/motor-hbridge.cir\\n\", \"    model: circuits/motor-hbridge-mosfet.cir\\n\")\nrep(\"    options: {backend: ngspice-shared, bindings: bindings/ngspice-hbridge.yaml, max_step: 1.0e-6}\\n\",\n    \"    # max_step 0.2 us resolves the dead time; ngspice refines further on the switching edges.\\n\"\n    \"    # save: bound (default) keeps only the vectors the bindings read.\\n\"\n    \"    options: {backend: ngspice-shared, bindings: bindings/ngspice-hbridge-mosfet.yaml, max_step: 2.0e-7}\\n\")\nrep(\"  electrical.input_power: {domain: electrical, unit: W, initial: 0.0}\\n\",\n    \"  electrical.input_power: {domain: electrical, unit: W, initial: 0.0}\\n  electrical.bridge_loss: {domain: electrical, unit: W, initial: 0.0}\\n\")\nrep(\"  energy: {type: integral, signal: electrical.input_power}\\n\",\n    \"  energy: {type: integral, signal: electrical.input_power}\\n\"\n    \"  bridge_loss: {type: stats, signal: electrical.bridge_loss}\\n\"\n    \"  loss_energy: {type: integral, signal: electrical.bridge_loss}\\n\")\nPath(\"dc-motor-mosfet.yaml\").write_text(s)\nEOF\ngrep -n \"motor_resistance\\|alter\" dc-motor-mosfet.yaml; cat tuning/pi-mock.yaml | sed -n 8,30p",
 "description": "Create MOSFET bindings and example project"
}
```

<details><summary>Result</summary>

```
35:  electrical.motor_resistance: {domain: electrical, unit: ohm, initial: 2.0}
  controller.ki: {initial: 0.8, min: 0.05, max: 20.0, scale: log}
objective:
  - {metric: speed.itae, weight: 1.0}
  - {metric: current.rms, weight: 0.001}
constraints:
  - {metric: speed.overshoot, max: 5.0}
  - {metric: speed.settling_time, max: 0.6}
max_evaluations: 40
Shell cwd was reset to /media/riko/model_store/GIT_/embeddyn
```

</details>

### Assistant — 2026-11-24 20:23:50

Running the example to see whether the duty leaves saturation (dead time only matters while switching).

**Tool: Bash** — Run MOSFET example at two dead times

```
{
 "command": "cd /media/riko/model_store/GIT_/codelayer_forge/engines/embeddyn && export PYTHONPATH=$PWD NGSPICE_LIBRARY=/media/riko/model_store/ngspice-lib/root/usr/lib/x86_64-linux-gnu/libngspice.so.0; timeout 600 /media/riko/model_store/embeddyn-venv/bin/python - <<'EOF' 2>&1 | grep -v Warn | tail -20\nimport time\nfrom embeddyn.mechatronics.model import MechatronicProject\nfrom embeddyn.mechatronics.factory import build_adapters\nfrom embeddyn.mechatronics.orchestrator import CoSimulationOrchestrator\nfrom embeddyn.mechatronics.metrics import compute_metrics\nfor dead in (2e-7, 0.0):\n    p = MechatronicProject.load(\"examples/mechatronics/dc-motor-mosfet.yaml\")\n    p.duration_seconds = 0.25\n    p.apply_parameter_overrides({\"bridge.dead_time\": dead})\n    t = time.time(); rows = CoSimulationOrchestrator(p, build_adapters(p)).run()\n    print(f\"dead={dead} {time.time()-t:.1f}s\")\n    for r in rows[::50]: print(f\"  t={r['time']:.3f} duty={r['firmware.pwm_duty']:.3f} v={r['mechanical.velocity']:.2f} i={r['electrical.motor_current']:.2f} loss={r['electrical.bridge_loss']:.3f} pin={r['electrical.input_power']:.1f}\")\n    m = compute_metrics(rows, p.metrics); print({k: round(v, 4) for k, v in m.items() if k.startswith((\"bridge_loss\", \"loss_energy\", \"energy\", \"speed.settl\"))})\nEOF",
 "description": "Run MOSFET example at two dead times",
 "timeout": 620000
}
```

<details><summary>Result</summary>

```
Traceback (most recent call last):
  File "<stdin>", line 7, in <module>
  File "/media/riko/model_store/GIT_/codelayer_forge/engines/embeddyn/embeddyn/mechatronics/model.py", line 214, in load
    path = path.resolve()
           ^^^^^^^^^^^^
AttributeError: 'str' object has no attribute 'resolve'
Shell cwd was reset to /media/riko/model_store/GIT_/embeddyn
```

</details>

**Tool: Bash** — Run MOSFET example at two dead times

```
{
 "command": "cd /media/riko/model_store/GIT_/codelayer_forge/engines/embeddyn && export PYTHONPATH=$PWD NGSPICE_LIBRARY=/media/riko/model_store/ngspice-lib/root/usr/lib/x86_64-linux-gnu/libngspice.so.0; timeout 600 /media/riko/model_store/embeddyn-venv/bin/python - <<'EOF' 2>&1 | grep -v Warn | tail -24\nimport time\nfrom pathlib import Path\nfrom embeddyn.mechatronics.model import MechatronicProject\nfrom embeddyn.mechatronics.factory import build_adapters\nfrom embeddyn.mechatronics.orchestrator import CoSimulationOrchestrator\nfrom embeddyn.mechatronics.metrics import compute_metrics\nfor dead in (2e-7, 0.0):\n    p = MechatronicProject.load(Path(\"examples/mechatronics/dc-motor-mosfet.yaml\"))\n    p.duration_seconds = 0.25\n    p.apply_parameter_overrides({\"bridge.dead_time\": dead})\n    t = time.time(); rows = CoSimulationOrchestrator(p, build_adapters(p)).run()\n    print(f\"dead={dead} {time.time()-t:.1f}s\")\n    for r in rows[::50]: print(f\"  t={r['time']:.3f} duty={r['firmware.pwm_duty']:.3f} v={r['mechanical.velocity']:.2f} i={r['electrical.motor_current']:.2f} loss={r['electrical.bridge_loss']:.3f} pin={r['electrical.input_power']:.1f}\")\n    m = compute_metrics(rows, p.metrics); print({k: round(v, 4) for k, v in m.items() if v is not None and k.startswith((\"bridge_loss\", \"loss_energy\", \"energy\", \"speed.settl\"))})\nEOF",
 "description": "Run MOSFET example at two dead times",
 "timeout": 620000
}
```

<details><summary>Result</summary>

```
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/media/riko/model_store/GIT_/codelayer_forge/engines/embeddyn/embeddyn/mechatronics/adapters/ngspice_shared.py", line 413, in step
    raise NgspiceError(f"ngspice stopped at {times[-1] if times else 'start'} before {target}:\n"
embeddyn.mechatronics.adapters.ngspice_shared.NgspiceError: ngspice stopped at 0.01830000009935165 before 0.0185:
simulation interrupted
Doing analysis at TEMP = 27.000000 and TNOM = 27.000000
 Reference value :  1.65007e-02
238: condition met: stop  when time >= 0.017
doAnalyses: pause requested
simulation interrupted
Doing analysis at TEMP = 27.000000 and TNOM = 27.000000
 Reference value :  1.70251e-02
245: condition met: stop  when time >= 0.0175
doAnalyses: pause requested
simulation interrupted
Doing analysis at TEMP = 27.000000 and TNOM = 27.000000
 Reference value :  1.75251e-02
252: condition met: stop  when time >= 0.018
doAnalyses: pause requested
simulation interrupted
Doing analysis at TEMP = 27.000000 and TNOM = 27.000000
 Reference value :  1.80600e-02
doAnalyses: TRAN:  Timestep too small; time = 0.0183, timestep = 2.5e-19: trouble with node "vd2#branch"
simulation aborted
Shell cwd was reset to /media/riko/model_store/GIT_/embeddyn
```

</details>

### Assistant — 2026-11-24 20:24:23

The closed loop hits "timestep too small" at 18.3 ms, mid-step, at a carrier corner while the duty is coming out of saturation. Trying solver options on the closed-loop case.

**Tool: Bash** — Try solver options on closed-loop run

```
{
 "command": "cd /media/riko/model_store/GIT_/codelayer_forge/engines/embeddyn && export PYTHONPATH=$PWD NGSPICE_LIBRARY=/media/riko/model_store/ngspice-lib/root/usr/lib/x86_64-linux-gnu/libngspice.so.0; cat > /tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad/loop.py <<'EOF'\nimport json, os, sys, time\nfrom pathlib import Path\nfrom embeddyn.mechatronics.model import MechatronicProject\nfrom embeddyn.mechatronics.factory import build_adapters\nfrom embeddyn.mechatronics.orchestrator import CoSimulationOrchestrator\nopts = json.loads(sys.argv[1]); dur = float(sys.argv[2]); dead = float(sys.argv[3]) if len(sys.argv) > 3 else 2e-7\np = MechatronicProject.load(Path(\"examples/mechatronics/dc-motor-mosfet.yaml\"))\np.duration_seconds = dur\np.apply_parameter_overrides({\"bridge.dead_time\": dead})\ninst = [c for c in p.simulator_instances().values() if c.options.get(\"backend\") == \"ngspice-shared\"][0]\ninst.options[\"spice_options\"] = opts\nt = time.time()\ntry:\n    rows = CoSimulationOrchestrator(p, build_adapters(p)).run()\n    r = rows[-1]; print(\"RESULT\", opts, dead, f\"ok {time.time()-t:.1f}s duty={r['firmware.pwm_duty']:.3f} v={r['mechanical.velocity']:.2f} loss={r['electrical.bridge_loss']:.3f}\")\nexcept Exception as e:\n    print(\"RESULT\", opts, dead, \"FAIL\", [l for l in str(e).splitlines() if \"Timestep\" in l or \"stopped\" in l][:2])\nEOF\nfor o in '{}' '{\"method\":\"gear\"}' '{\"reltol\":\"1e-3\",\"abstol\":\"1e-9\",\"method\":\"gear\"}' '{\"rshunt\":\"1e8\",\"method\":\"gear\"}'; do timeout 300 /media/riko/model_store/embeddyn-venv/bin/python /tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad/loop.py \"$o\" 0.03 2>&1 | grep RESULT; done",
 "description": "Try solver options on closed-loop run",
 "timeout": 1300000
}
```

<details><summary>Result</summary>

```
RESULT {} 2e-07 FAIL ['ngspice stopped at 0.01830000009935165 before 0.0185:', 'doAnalyses: TRAN:  Timestep too small; time = 0.0183, timestep = 2.5e-19: trouble with node "vd2#branch"']
RESULT {'method': 'gear'} 2e-07 ok 22.0s duty=0.999 v=1.20 loss=2.106
RESULT {'reltol': '1e-3', 'abstol': '1e-9', 'method': 'gear'} 2e-07 ok 18.0s duty=0.999 v=1.20 loss=2.106
RESULT {'rshunt': '1e8', 'method': 'gear'} 2e-07 ok 18.8s duty=0.999 v=1.20 loss=2.106
Shell cwd was reset to /media/riko/model_store/GIT_/embeddyn
```

</details>

**Tool: Bash** — Run full 0.25 s at two dead times

```
{
 "command": "cd /media/riko/model_store/GIT_/codelayer_forge/engines/embeddyn && export PYTHONPATH=$PWD NGSPICE_LIBRARY=/media/riko/model_store/ngspice-lib/root/usr/lib/x86_64-linux-gnu/libngspice.so.0; S=/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad; sed -i 's/    r = rows\\[-1\\]; print/    [print(\"ROW\", f\"{r[\\x27time\\x27]:.3f} duty={r[\\x27firmware.pwm_duty\\x27]:.3f} v={r[\\x27mechanical.velocity\\x27]:.2f} i={r[\\x27electrical.motor_current\\x27]:.2f} loss={r[\\x27electrical.bridge_loss\\x27]:.3f}\") for r in rows[::40]]\\n    r = rows[-1]; print/' $S/loop.py; (timeout 900 /media/riko/model_store/embeddyn-venv/bin/python $S/loop.py '{\"method\":\"gear\"}' 0.25 2e-7 > $S/run200.txt 2>&1 &); timeout 900 /media/riko/model_store/embeddyn-venv/bin/python $S/loop.py '{\"method\":\"gear\"}' 0.25 0 > $S/run0.txt 2>&1; sleep 5; grep -E \"ROW|RESULT\" $S/run200.txt $S/run0.txt",
 "description": "Run full 0.25 s at two dead times",
 "timeout": 960000
}
```

<details><summary>Result</summary>

```
/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad/run200.txt:ROW 0.000 duty=0.000 v=0.00 i=0.00 loss=0.000
/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad/run200.txt:ROW 0.020 duty=0.992 v=0.79 i=11.57 loss=2.307
/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad/run200.txt:ROW 0.040 duty=1.000 v=1.59 i=11.56 loss=2.100
/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad/run200.txt:ROW 0.060 duty=1.000 v=2.33 i=11.53 loss=2.090
/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad/run200.txt:ROW 0.080 duty=1.000 v=3.01 i=11.50 loss=2.080
/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad/run200.txt:ROW 0.100 duty=1.000 v=3.63 i=11.48 loss=2.072
/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad/run200.txt:ROW 0.120 duty=1.000 v=4.21 i=11.46 loss=2.063
/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad/run200.txt:ROW 0.140 duty=1.000 v=4.74 i=11.44 loss=2.056
/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad/run200.txt:ROW 0.160 duty=0.997 v=5.22 i=11.42 loss=2.049
/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad/run200.txt:ROW 0.180 duty=0.984 v=5.67 i=11.30 loss=2.366
/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad/run200.txt:ROW 0.200 duty=0.970 v=6.06 i=11.14 loss=2.263
/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad/run200.txt:ROW 0.220 duty=0.956 v=6.42 i=10.96 loss=2.187
/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad/run200.txt:ROW 0.240 duty=0.941 v=6.73 i=10.78 loss=2.131
/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad/run200.txt:RESULT {'method': 'gear'} 2e-07 ok 101.6s duty=0.934 v=6.87 loss=2.095
/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad/run0.txt:ROW 0.000 duty=0.000 v=0.00 i=0.00 loss=0.000
/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad/run0.txt:ROW 0.020 duty=0.992 v=0.79 i=11.59 loss=2.111
/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad/run0.txt:ROW 0.040 duty=1.000 v=1.60 i=11.56 loss=2.100
/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad/run0.txt:ROW 0.060 duty=1.000 v=2.33 i=11.53 loss=2.090
/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad/run0.txt:ROW 0.080 duty=1.000 v=3.01 i=11.50 loss=2.080
/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad/run0.txt:ROW 0.100 duty=1.000 v=3.63 i=11.48 loss=2.072
/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad/run0.txt:ROW 0.120 duty=1.000 v=4.21 i=11.46 loss=2.063
/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad/run0.txt:ROW 0.140 duty=1.000 v=4.74 i=11.44 loss=2.056
/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad/run0.txt:ROW 0.160 duty=0.996 v=5.22 i=11.42 loss=2.049
/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad/run0.txt:ROW 0.180 duty=0.984 v=5.67 i=11.33 loss=12.270
/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad/run0.txt:ROW 0.200 duty=0.970 
… [589 more characters not shown]
```

</details>

**Tool: Bash** — Run light-rotor example across dead times

```
{
 "command": "cd /media/riko/model_store/GIT_/codelayer_forge/engines/embeddyn && python3 - <<'EOF'\nfrom pathlib import Path\np = Path(\"examples/mechatronics/dc-motor-mosfet.yaml\"); s = p.read_text()\ndef rep(old, new):\n    global s\n    assert s.count(old) == 1, old; s = s.replace(old, new)\nrep(\"    options: {backend: ngspice-shared, bindings: bindings/ngspice-hbridge-mosfet.yaml, max_step: 2.0e-7}\\n\",\n    \"    # method=gear: trapezoidal integration rings on the stiff MOSFET edges (\\\"timestep too small\\\").\\n\"\n    \"    options: {backend: ngspice-shared, bindings: bindings/ngspice-hbridge-mosfet.yaml, max_step: 2.0e-7,\\n\"\n    \"              spice_options: {method: gear}}\\n\")\nrep(\"  mechanical.inertia: {domain: mechanical, unit: kg*m^2, initial: 0.02}\\n\",\n    \"  mechanical.inertia: {domain: mechanical, unit: kg*m^2, initial: 0.002}   # light rotor: settles in ~40 ms, then switches at ~75 % duty\\n\")\np.write_text(s)\nEOF\nexport PYTHONPATH=$PWD NGSPICE_LIBRARY=/media/riko/model_store/ngspice-lib/root/usr/lib/x86_64-linux-gnu/libngspice.so.0; S=/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad; sed -i 's/rows\\[::40\\]/rows[::20]/' $S/loop.py; for d in 0 1e-7 2e-7 5e-7 1e-6; do (timeout 900 /media/riko/model_store/embeddyn-venv/bin/python $S/loop.py '{\"method\":\"gear\"}' 0.1 $d > $S/r$d.txt 2>&1 &) ; done; sleep 100; while pgrep -f loop.py >/dev/null; do sleep 5; done; grep RESULT $S/r*.txt; grep ROW $S/r2e-7.txt",
 "description": "Run light-rotor example across dead times",
 "timeout": 960000
}
```

<details><summary>Result</summary>

```
Command did not complete within its 600s timeout and was moved to the background (ID: bvim3cznn). Output is being written to: /tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/tasks/bvim3cznn.output. You will be notified when it completes. It is running now: do not start a second copy while it runs. If it is no longer needed, stop it with TaskStop. No output had been read from it when it was moved. A program that buffers its output, or a pipe into one such as tail, can keep the output file empty until the command finishes. To check interim output, use Read on that file path.
Session cwd remains /media/riko/model_store/GIT_/embeddyn; directory changes made by the backgrounded command do not apply to subsequent commands.
```

</details>

### Assistant — 2026-11-24 20:39:17

While those five runs finish, I'll write the docs.

**Tool: Bash** — Read ngspice reference and examples table

```
{
 "command": "cd /media/riko/model_store/GIT_/codelayer_forge/engines/embeddyn && cat docs/reference/ngspice.md; sed -n 1,30p docs/reference/examples.md; ls docs/theory docs/tutorials",
 "description": "Read ngspice reference and examples table"
}
```

<details><summary>Result</summary>

````
# Electronics in the loop: ngspice shared library

The `ngspice-shared` electrical backend runs the circuit in **libngspice**
inside the co-simulation process. One transient analysis covers the whole run,
so inductor currents, capacitor voltages and switch states carry from one
co-simulation step to the next. Switching circuits (PWM bridges, converters)
are simulated with their real ripple; the adapter averages it per step for
the slower domains.

The older `ngspice` backend (one `ngspice -b` process per step, no carried
state) stays for batch use; use `ngspice-shared` for co-simulation.

## How a step works

1. **Inputs** are held constant for the step (zero-order hold). They reach the
   circuit through `external` voltage or current sources (libngspice's
   `GetVSRCData`/`GetISRCData` callbacks), or through `alter` when a device
   parameter changes (sent only when the value changes).
2. The adapter sets `stop when time >= t` and runs (first step) or resumes the
   analysis. ngspice's `GetSyncData` callback shortens the last internal
   timestep, so the analysis lands **exactly** on the co-simulation time.
3. **Outputs** are read from the result vectors over the step window: the last
   value, or the time-weighted `mean`, `rms`, `min` or `max`.

An RC test (`tests/mechatronics/test_ngspice_shared.py`) checks the stepped
response against the analytic curve within 2·10⁻⁴ V at every step.

## Configuration

```yaml
simulators:
  electrical:
    model: circuits/motor-hbridge.cir
    options:
      backend: ngspice-shared
      bindings: bindings/ngspice-hbridge.yaml
      max_step: 1.0e-6        # ngspice max timestep (default: step_seconds / 50)
      # library: /path/to/libngspice.so.0   (or NGSPICE_LIBRARY, or the system library)
      # spice_options: {reltol: 0.001}       (written as one .options card)
      # inline_bindings: [...]               (same format as the bindings file)
```

The netlist is a plain SPICE deck:

* `{{signal.name}}` placeholders are replaced by the signal's **initial**
  value at start (for fixed parameters and initial conditions). Placeholders
  in comment lines are left alone.
* Analysis cards (`.tran`, `.op`, `.ac`, `.dc`, …), `.control` blocks and
  `.end` are removed; the adapter appends its own `.tran … uic`.
* Sources to be driven from the co-simulation are written as
  `Vname n+ n- dc 0 external`.

### Bindings

| Direction | Keys | Meaning |
|-----------|------|---------|
| input | `source: Vname` | value of an `external` voltage source |
| input | `current_source: Iname` | value of an `external` current source |
| input | `alter: Rname` or `alter: "@Lname[inductance]"` | `alter` command when the value changes |
| output | `vector: "V(node)"`, `"V(n1,n2)"`, `"I(Vname)"` or a raw vector name | `mode`: `mean` (default), `last`, `rms`, `min`, `max` |

All bindings take `scale` and `offset`; inputs also take `min`/`max`
(applied after scaling). One signal can drive several sources, e.g. a signed
duty split into two legs with `scale: -1` and `min: 0`. Quote vector
expressions in YAML flow style (`vector: "V(a,b)"`), otherwise the comma
splits the mapping.

## Example: switching H-bridge

`examples/mechatronics/circuits/motor-hbridge.cir`
is a full H-bridge with 20 kHz sign-magnitude PWM (triangle carrier and
voltage-controlled switches), a supply with source resistance and DC-link
capacitor, and the motor's R, L and back-EMF. Bindings are in
`bindings/ngspice-hbridge.yaml` (`examples/mechatronics/bindings/ngspice-hbridge.yaml`).

* `examples/mechatronics/dc-motor-ngspice.yaml`: mock firmware + H-bridge +
  mock mechanics. 0.25 s (about 250 000 SPICE points) in about 5 s.
* `examples/mechatronics/dc-motor-full.yaml`: real STM32 firmware in Renode
  + H-bridge in ngspice + mock mechanics. 0.25 s in about 11 s.

Compared with the ideal RL mock, the bridge shows real losses: at full duty
and 11.4 A it delivers 22.97 V instead of 24 V (0.05 Ω source resistance plus
two 0.02 Ω switches).

Two netlist 
… [4187 more characters not shown]
````

</details>

**Tool: Bash** — Check dead-time sweep results

```
{
 "command": "sleep 60; S=/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad; pgrep -f loop.py | wc -l; grep -h RESULT $S/r*.txt; grep ROW $S/r2e-7.txt",
 "description": "Check dead-time sweep results",
 "timeout": 120000
}
```

<details><summary>Result</summary>

```
<tool_use_error>Blocked: sleep 60 followed by: S=/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad pgrep -f loop.py wc -l grep -h RESULT $S/r*.txt grep ROW $S/r2e-7.txt. To wait for a condition, use Monitor with an until-loop (e.g. `until <check>; do sleep 2; done`). To wait for a command you started, use run_in_background: true. Do not chain shorter sleeps to work around this block.</tool_use_error>
```

</details>

**Tool: Bash** — Check dead-time sweep results

```
{
 "command": "S=/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad; pgrep -f loop.py | wc -l; grep -h RESULT $S/r*.txt; grep ROW $S/r2e-7.txt",
 "description": "Check dead-time sweep results"
}
```

<details><summary>Result</summary>

```
2
RESULT {'method': 'gear'} 1e-06 ok 47.1s duty=0.565 v=5.61 loss=0.933
RESULT {'method': 'gear'} 2e-07 ok 42.0s duty=0.553 v=5.67 loss=0.820
RESULT {'method': 'gear'} 0.0 ok 181.5s duty=0.932 v=6.88 loss=12.196
RESULT {'method': 'gear'} 0.0 ok 37.9s duty=0.551 v=5.68 loss=11.207
RESULT {'method': 'gear'} 1e-07 ok 37.9s duty=0.552 v=5.67 loss=0.797
RESULT {'method': 'gear'} 2e-07 ok 101.6s duty=0.934 v=6.87 loss=2.095
RESULT {'method': 'gear'} 5e-07 ok 45.3s duty=0.558 v=5.64 loss=0.849
ROW 0.000 duty=0.000 v=0.00 i=0.00 loss=0.000
ROW 0.010 duty=0.706 v=2.80 i=8.69 loss=1.437
ROW 0.020 duty=0.559 v=4.18 i=6.58 loss=0.864
ROW 0.030 duty=0.518 v=4.71 i=5.94 loss=0.726
ROW 0.040 duty=0.511 v=4.97 i=5.81 loss=0.694
ROW 0.050 duty=0.515 v=5.13 i=5.84 loss=0.723
ROW 0.060 duty=0.522 v=5.25 i=5.92 loss=0.740
ROW 0.070 duty=0.530 v=5.36 i=6.01 loss=0.763
ROW 0.080 duty=0.538 v=5.47 i=6.10 loss=0.789
ROW 0.090 duty=0.546 v=5.57 i=6.19 loss=0.801
ROW 0.100 duty=0.553 v=5.67 i=6.27 loss=0.820
```

</details>

**Tool: Bash** — Check tuning spec format

```
{
 "command": "cd /media/riko/model_store/GIT_/codelayer_forge/engines/embeddyn && cat examples/mechatronics/tuning/servo-pi.yaml | head -30; grep -n \"def load_tuning\\|scale\\|\\\"duration\\\"\\|max_evaluations\\|method\" embeddyn/mechatronics/tuning.py | head -20",
 "description": "Check tuning spec format"
}
```

<details><summary>Result</summary>

```
# Tune the servo's PI gains with the geared, backlash-afflicted drivetrain in the loop.
# Run: embeddyn mechatronics tune examples/mechatronics/tuning/servo-pi.yaml
project: ../servo-drivetrain.yaml
parameters:
  controller.kp: {initial: 0.5, min: 0.02, max: 3.0, scale: log}
  controller.ki: {initial: 4.0, min: 0.1, max: 40.0, scale: log}
objective:
  - {metric: speed.itae, weight: 1.0}
  - {metric: shaft.peak, weight: 0.002}       # spare the gearbox
constraints:
  - {metric: speed.overshoot, max: 5.0}
  - {metric: speed.settling_time, max: 0.5}
max_evaluations: 30
8:  controller.kp: {initial: 0.12, min: 0.01, max: 2.0, scale: log}
9:  controller.ki: {initial: 0.8, min: 0.05, max: 20.0, scale: log}
15:max_evaluations: 40
17:method: nelder-mead             # or cmaes (population-based; keeps every worker busy)
24:parameter, logarithmic when `scale: log`). It is deterministic: the same file
101:    max_evaluations: int = 40
104:    method: str = "nelder-mead"
110:def load_tuning_spec(path: Path) -> TuningSpec:
122:        log = str(entry.get("scale", "linear")).lower() in {"log", "logarithmic"}
124:            raise ValueError(f"parameter {name}: need min < max (and min > 0 for log scale)")
138:                      None if raw.get("duration") is None else float(raw["duration"]),
139:                      int(raw.get("max_evaluations", 40)), float(raw.get("tolerance", 1e-4)),
140:                      max(1, int(raw.get("workers", 1))), _method(raw.get("method", "nelder-mead")),
145:def _method(value: Any) -> str:
146:    method = str(value).lower().replace("_", "-")
149:    if method not in aliases:
150:        raise ValueError(f"unknown tuning method {value!r} (nelder-mead or cmaes)")
151:    return aliases[method]
192:def nelder_mead(function: Callable[[list[float]], float], start: Sequence[float], max_evaluations: int,
203:    `max_evaluations`. So the simplex path, and the evaluations that count,
Shell cwd was reset to /media/riko/model_store/GIT_/embeddyn
```

</details>

**Tool: Bash** — Write dead-time tuning spec and check tune options

```
{
 "command": "cd /media/riko/model_store/GIT_/codelayer_forge/engines/embeddyn && cat > examples/mechatronics/tuning/dead-time.yaml <<'EOF'\n# Choose the H-bridge dead time that minimises MOSFET losses (ngspice, libngspice).\n# Too short: both switches of a leg conduct at once (shoot-through). Too long:\n# the body diodes carry the current for longer and the bridge voltage sags.\n#   embeddyn mechatronics tune examples/mechatronics/tuning/dead-time.yaml --workers 4\n# Each evaluation simulates 30 ms of switching (~12 s); every worker runs its own libngspice.\nproject: ../dc-motor-mosfet.yaml\nduration: 0.03                    # the loop leaves saturation at ~15 ms and switches at ~55 % duty\nparameters:\n  bridge.dead_time: {initial: 5.0e-7, min: 2.0e-8, max: 2.0e-6, scale: log}\nobjective:\n  - {metric: loss_energy.integral, weight: 1000.0}   # MOSFET losses over the run, mJ\nmax_evaluations: 12\nworkers: 4\nEOF\nexport PYTHONPATH=$PWD NGSPICE_LIBRARY=/media/riko/model_store/ngspice-lib/root/usr/lib/x86_64-linux-gnu/libngspice.so.0 TMPDIR=/media/riko/model_store/tmp; /media/riko/model_store/embeddyn-venv/bin/python -m embeddyn mechatronics tune --help 2>&1 | grep -E \"^\\s+(-|--)\" | head -20",
 "description": "Write dead-time tuning spec and check tune options"
}
```

<details><summary>Result</summary>

```
Shell cwd was reset to /media/riko/model_store/GIT_/embeddyn
```

</details>

**Tool: Bash** — Show tune command help

```
{
 "command": "cd /media/riko/model_store/GIT_/codelayer_forge/engines/embeddyn && export PYTHONPATH=$PWD; /media/riko/model_store/embeddyn-venv/bin/python -m embeddyn mechatronics tune --help 2>&1 | tail -25",
 "description": "Show tune command help"
}
```

<details><summary>Result</summary>

```
Usage: python -m embeddyn mechatronics tune [OPTIONS] {spec_file}              
                                                                                
 Tune project parameters by repeated co-simulation (bounded Nelder-Mead or      
 CMA-ES).                                                                       
                                                                                
╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    spec_file      <file>  Tuning spec (YAML). [required]                   │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --output           -o      <path>  [default: .embeddyn/tuning/result.json]   │
│ --max-evaluations          <int>                                             │
│ --workers                  <int>   Parallel evaluations (overrides the       │
│                                    spec).                                    │
│ --method                   <str>   nelder-mead or cmaes (overrides the       │
│                                    spec).                                    │
│ --help                     <bool>  Show this message and exit.               │
╰──────────────────────────────────────────────────────────────────────────────╯
Shell cwd was reset to /media/riko/model_store/GIT_/embeddyn
```

</details>

### Assistant — 2026-11-24 20:40:15

Running the tuning spec in the background while I write tests and docs.

**Tool: Bash** — Run dead-time tuning example

```
{
 "command": "cd /media/riko/model_store/GIT_/codelayer_forge/engines/embeddyn && export PYTHONPATH=$PWD NGSPICE_LIBRARY=/media/riko/model_store/ngspice-lib/root/usr/lib/x86_64-linux-gnu/libngspice.so.0 TMPDIR=/media/riko/model_store/tmp; S=/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad; timeout 1500 /media/riko/model_store/embeddyn-venv/bin/python -m embeddyn mechatronics tune examples/mechatronics/tuning/dead-time.yaml -o $S/dead-time.json > $S/tune.log 2>&1; echo exit=$?; tail -30 $S/tune.log",
 "description": "Run dead-time tuning example",
 "run_in_background": true
}
```

**Tool: Bash** — Add MOSFET bridge tests and run

```
{
 "command": "cd /media/riko/model_store/GIT_/codelayer_forge/engines/embeddyn && cat >> tests/mechatronics/test_ngspice_shared.py <<'EOF'\n\n\nMOSFET = ROOT / \"examples/mechatronics/dc-motor-mosfet.yaml\"\n\n\ndef test_mosfet_example_wiring():\n    project = MechatronicProject.load(MOSFET)\n    adapter = build_adapters(project)[Domain.ELECTRICAL]\n    assert adapter.save == \"bound\" and adapter.spice_options == {\"method\": \"gear\"}\n    assert \"electrical.bridge_loss\" in {b.signal for b in adapter.bindings.outputs}\n    assert project.parameters[\"bridge.dead_time\"].value == pytest.approx(2e-7)\n\n\ndef _mosfet_run(dead_time: float):\n    project = MechatronicProject.load(MOSFET)\n    project.duration_seconds = 0.025\n    project.apply_parameter_overrides({\"bridge.dead_time\": dead_time})\n    return CoSimulationOrchestrator(project, build_adapters(project)).run()\n\n\n@needs_ngspice\ndef test_mosfet_bridge_dead_time_prevents_shoot_through():\n    \"\"\"MOSFET H-bridge in the loop: the dead time is a project parameter that reaches the\n    netlist. Without it both switches of a leg conduct at every edge (shoot-through).\"\"\"\n    blanked, shorted = _mosfet_run(2e-7), _mosfet_run(0.0)\n    late = [row for row in blanked if row[\"time\"] >= 0.02]\n    assert all(0.3 < row[\"firmware.pwm_duty\"] < 0.9 for row in late)          # switching, not saturated\n    loss = sum(row[\"electrical.bridge_loss\"] for row in late) / len(late)\n    loss_shorted = sum(row[\"electrical.bridge_loss\"] for row in shorted if row[\"time\"] >= 0.02) / len(late)\n    assert 0.3 < loss < 2.0 and loss_shorted > 5 * loss\n    # Supply power covers the motor's electrical power plus the bridge losses (within the\n    # small source-resistance and bleed losses).\n    last = late[-1]\n    motor = last[\"electrical.motor_voltage\"] * last[\"electrical.motor_current\"]\n    assert last[\"electrical.input_power\"] == pytest.approx(motor + last[\"electrical.bridge_loss\"], rel=0.05)\nEOF\nexport PYTHONPATH=$PWD NGSPICE_LIBRARY=/media/riko/model_store/ngspice-lib/root/usr/lib/x86_64-linux-gnu/libngspice.so.0 TMPDIR=/media/riko/model_store/tmp; timeout 900 /media/riko/model_store/embeddyn-venv/bin/python -m pytest -q -p no:cacheprovider tests/mechatronics/test_ngspice_shared.py --basetemp=/media/riko/model_store/tmp/embeddyn-pytest-ng2 --durations=3 2>&1 | tail -15",
 "description": "Add MOSFET bridge tests and run",
 "timeout": 920000
}
```

<details><summary>Result</summary>

```
Command running in background with ID: bn6txquls. Output is being written to: /tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/tasks/bn6txquls.output. You will be notified when it completes — do not poll that file or sleep while waiting. Read the file only if the user asks for progress, or if you need a line it prints before you can go on, such as a server's ready line or port.
Session cwd remains /media/riko/model_store/GIT_/embeddyn; directory changes made by the backgrounded command do not apply to subsequent commands.
```

</details>

<details><summary>Result</summary>

```
/media/riko/model_store/embeddyn-venv/lib/python3.12/site-packages/pytest_asyncio/plugin.py:306: PytestDeprecationWarning: The configuration option "asyncio_default_fixture_loop_scope" is unset.
The event loop scope for asynchronous fixtures will default to the fixture scope. Future versions of pytest-asyncio will default the loop scope for asynchronous fixtures to "function" scope. Set the default fixture loop scope explicitly in order to avoid unexpected behavior in the future. Valid fixture loop scopes are: "function", "class", "module", "package", "session". Set it to "fixture-scope" to keep the current behavior.

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))
.............                                                            [100%]
============================= slowest 3 durations ==============================
21.22s call     tests/mechatronics/test_ngspice_shared.py::test_mosfet_bridge_dead_time_prevents_shoot_through
1.00s call     tests/mechatronics/test_ngspice_shared.py::test_hbridge_example_closed_loop
0.07s call     tests/mechatronics/test_ngspice_shared.py::test_steps_ending_on_a_source_breakpoint
13 passed in 22.89s
Shell cwd was reset to /media/riko/model_store/GIT_/embeddyn
```

</details>

### Assistant — 2026-11-24 20:40:54

All 13 ngspice tests pass, including the original H-bridge loop (no regression from the stop change); the MOSFET test takes 21 s. Now the docs.

**Tool: mcp__ccd_turn__update_plan**

```
{
 "done": [
  3,
  4
 ],
 "start": 5
}
```

<details><summary>Result</summary>

```
Plan updated: step 5 of 6 is in progress.
```

</details>

**Tool: Bash** — Check theory headings and roadmap lines

```
{
 "command": "cd /media/riko/model_store/GIT_/codelayer_forge/engines/embeddyn && grep -n \"^#\" docs/theory/electronics.md; grep -n \"ngspice\\|11 \" docs/project/roadmap.md | head; sed -n 30,45p docs/reference/examples.md",
 "description": "Check theory headings and roadmap lines"
}
```

<details><summary>Result</summary>

```
1:# Electronics
15:## Averaged models
25:## ngspice in the loop
29:### One electrical step
52:### Netlist preparation
56:### Why mean values are the right coupling
60:## Circuits from KiCad
64:## Limits
17:| **Electrical: ngspice (libngspice)** | 🟢 works with carried state | `ngspice-shared` backend, switching H-bridge example, closed loop with Renode firmware ([NGSPICE.md](../reference/ngspice.md)); LTspice and the per-step `ngspice` backend remain batch-only |
26:Firmware runs for real in Renode and electronics in ngspice; the mechanical domain still runs only as a built-in model.
87:## Phase R2: electronics with state (ngspice shared library)
91:- [x] `NgspiceSharedAdapter` (`backend: ngspice-shared`) on libngspice via
98:  H-bridge in ngspice.
155:- [ ] `embeddyn mechatronics doctor` checks the Renode build, ngspice library
157:- [ ] Docker image with Renode (custom-cores), ngspice and Webots for
181:| 11 | ngspice: gate drive/dead time/MOSFET models, `.save` filtering, KiCad round trip | R2; INTEGRATION_PLAN 6 | missing |
| `tolerance/servo.yaml` | `tolerance` | servo-drivetrain |
| `estimation/dc-motor.yaml` | `estimate` | dc-motor + `bench/dc-motor-step-test.csv` |

## Supporting files

| Directory | Contents |
|---|---|
| `firmware/` | C sources and Makefiles for the STM32L476 examples (`rc-servo-l476`, `arm-joint-l476`, `motor-speed-control`) |
| `renode/` | `.resc` scripts and `.repl` platform descriptions (incl. the SPI/I²C sensor platform) |
| `bindings/` | Renode, ngspice and Webots binding files |
| `circuits/` | SPICE netlists (`motor-hbridge.cir`) |
| `mujoco/` | MJCF models (`arm-joint.xml`) |
| `fmus/` | the rotational-load FMU (FMI 2 and 3) and its build script |
| `golden/` | golden waveforms for verification |
| `rc-servo/` | operator command profile (replay CSV) |
| `controllers/`, `worlds/` | Webots controller bridge and worlds |
Shell cwd was reset to /media/riko/model_store/GIT_/embeddyn
```

</details>

**Tool: Bash** — Read theory electronics tail

```
{
 "command": "cd /media/riko/model_store/GIT_/codelayer_forge/engines/embeddyn && sed -n 56,80p docs/theory/electronics.md",
 "description": "Read theory electronics tail"
}
```

<details><summary>Result</summary>

```
### Why mean values are the right coupling

The mechanical domain integrates torque over the macro step. Feeding it the step-mean current gives the correct impulse \(\int \tau\,dt = K_t \int i\,dt\) regardless of ripple phase; feeding it the end-of-step current would alias the 20 kHz ripple into a spurious low-frequency torque. The same argument applies to input power for energy balances.

## Circuits from KiCad

`embeddyn mechatronics kicad-export board.kicad_sch -o board.cir` calls `kicad-cli sch export netlist --format spice`. Mark the sources the co-simulation drives as `external` and map them in a bindings file; see [Export a KiCad schematic to SPICE](../guides/kicad.md).

## Limits

* libngspice is a process-global library: **one ngspice-shared instance per process** (use `--isolate-adapters` for more).
* All result points are kept in memory for the run.
* No checkpoint/restore of the SPICE state.
Shell cwd was reset to /media/riko/model_store/GIT_/embeddyn
```

</details>

**Tool: Bash** — Update ngspice, theory, examples, roadmap docs

```
{
 "command": "cd /media/riko/model_store/GIT_/codelayer_forge/engines/embeddyn && python3 - <<'EOF'\nfrom pathlib import Path\ndef edit(path, old, new):\n    p = Path(path); s = p.read_text()\n    assert s.count(old) == 1, (path, old[:70]); p.write_text(s.replace(old, new))\n\nR = \"docs/reference/ngspice.md\"\nedit(R, '''2. The adapter sets `stop when time >= t` and runs (first step) or resumes the\n   analysis. ngspice's `GetSyncData` callback shortens the last internal\n   timestep, so the analysis lands **exactly** on the co-simulation time.''', '''2. The adapter sets `stop when time >= t` and runs (first step) or resumes the\n   analysis. ngspice turns the condition into a breakpoint at `t`, and its\n   `GetSyncData` callback shortens the last internal timestep, so the\n   analysis lands **exactly** on the co-simulation time. (The stop used to be\n   set a hair before `t`; when a source breakpoint, such as a PWM carrier\n   corner, fell on the grid, the next step then had to be 10⁻¹⁶ s long and\n   failed with \"timestep too small\".)''')\nedit(R, '''      # spice_options: {reltol: 0.001}       (written as one .options card)\n''', '''      # spice_options: {reltol: 0.001}       (written as one .options card)\n      # save: bound                          (default) or all, see below\n''')\nedit(R, '''## Example: switching H-bridge\n''', '''### What ngspice keeps: `save`\n\nngspice keeps every saved vector for the whole run. With `save: bound` (the\ndefault) only `time` and the vectors the output bindings read are saved:\n`V(a,b)` saves nodes `a` and `b`, `I(Vx)` saves `vx#branch`, a raw vector\nname is saved as written. `save: all` keeps ngspice's default, every node and\nbranch current, which you need only to inspect other vectors interactively.\nOn the MOSFET bridge below this keeps 6 vectors instead of 50 (about 5 MB\ninstead of 44 MB per 20 ms simulated) with identical outputs.\n\nlibngspice ignores `.save` cards outside batch mode, and `save` entries share\nngspice's debug list with `stop` breakpoints, which the adapter clears every\nstep; so the adapter issues the `save` command itself before each run/resume.\nA `.save` card in your netlist has no effect.\n\n## Example: switching H-bridge\n''')\nedit(R, '''## Getting libngspice''', '''## Example: MOSFET H-bridge with dead time\n\n`examples/mechatronics/circuits/motor-hbridge-mosfet.cir` replaces the ideal\nswitches with four N-channel power MOSFETs (a generic 60 V, ~10 mΩ `VDMOS`\nmodel with its body diode, gate charge and junction capacitances) and adds\nthe gate drive:\n\n* **Dead time.** Each leg's high side is on while duty > carrier + δ and its\n  low side while duty < carrier − δ, with δ = `tdead` × carrier slope / 2, so\n  both are off for `tdead` around every edge. Duty 0 (below 0.5 %) and 1\n  (above 99.5 %) hold a switch on, so an idle leg is never blanked.\n* **Gate drivers**: a 12 V output with about 10 ns rise and fall (a `tanh`\n  edge, which ngspice integrates more easily than a step) through the gate\n  resistor; the high-side driver floats on its switch node (an ideal\n  bootstrap supply).\n* **Losses**: `V(ploss)` is the dissipation of the four devices,\n  V<sub>DS</sub>·I<sub>D</sub> summed over channel and body diode, measured\n  through 0 V sources in the drains.\n\n`{{bridge.dead_time}}` and `{{bridge.gate_resistance}}` are **project\nparameters** in `dc-motor-mosfet.yaml` (200 ns and 10 Ω), so `-p\nbridge.dead_time=100ns`, `tune` and `tolerance` reach the netlist like any\nparameter. The project uses a ten times lighter rotor than\n`dc-motor-ngspice.yaml`, so the speed loop leaves saturation after about\n15 ms and the bridge switches at about 55 % duty.\n\nBridge loss at 0.1 s (24 V, about 6 A), from real runs:\n\n| Dead time | 0 | 100 ns | 200 ns | 500 ns | 1 µs |\n|---|---|---|---|---|---|\n| MOSFET losses | 11.2 W | 0.80 W | 0.82 W | 0.85 W | 0.93 W |\n\nWithout dead time both switches of a leg conduct at every edge\n(shoot-through, current spikes of about 90 A). Above about 100 ns the\nlosses rise slowly, because the body diodes (about 0.8 V) carry the\ncurrent for longer than the channels (about 6 mΩ), and the mean bridge voltage\nsags with dead time. `tuning/dead-time.yaml` lets `tune` choose it.\nThe peak drain currents are dominated by body-diode reverse recovery and\nringing of this generic model, so they aren't bound as outputs.\n\nSolver settings: `spice_options: {method: gear}`. The default trapezoidal\nrule rings on the stiff switching edges and stopped one closed-loop run with\n\"timestep too small\" at a carrier corner; `max_step: 2.0e-7` resolves the\ndead time. 0.1 s takes about 40 s (vs about 2 s for the ideal-switch bridge),\nso use this circuit where the switching itself matters, and the ideal-switch\nbridge or the averaged mock elsewhere.\n\n## Getting libngspice''')\nedit(R, '''* All result points stay in memory for the whole run (about 250 000 points\n  per 0.25 s at a 1 µs max step). Very long runs need a larger max step or a\n  future `.save`-based vector filter.''', '''* All result points of the saved vectors stay in memory for the whole run\n  (about 250 000 points per 0.25 s at a 1 µs max step); `save: bound` keeps\n  that to the bound vectors. Very long runs also need a larger max step.''')\n\nT = \"docs/theory/electronics.md\"\nedit(T, '''## Circuits from KiCad''', '''### Dead time and switching losses\n\nA half-bridge leg must never have both switches on: the supply would be\nshorted through them (shoot-through). The gate driver therefore waits a\ndead time \\\\(t_d\\\\) after turning one switch off before turning the other on.\nDuring \\\\(t_d\\\\) the load current keeps flowing through a body diode, at\nabout 0.8 V instead of \\\\(I R_{DS(on)}\\\\), and the bridge output follows the\ncurrent direction instead of the duty, so the delivered voltage falls by\nabout \\\\(2 t_d f_{PWM} V_{bus}\\\\) per leg. The loss therefore has two\nregimes: below the switches' turn-off time it is dominated by shoot-through\n(here 11 W at \\\\(t_d = 0\\\\)); above it, it rises slowly with \\\\(t_d\\\\)\n(diode conduction). Because the dead time is a netlist placeholder bound to\na project parameter, `tune` can choose it against a loss metric\n([MOSFET bridge](../reference/ngspice.md#example-mosfet-h-bridge-with-dead-time)).\n\n## Circuits from KiCad''')\nedit(T, \"* All result points are kept in memory for the run.\", \"* The points of the saved vectors are kept in memory for the run (`save: bound`, the default, keeps only the vectors the bindings read).\")\n\nE = \"docs/reference/examples.md\"\nedit(E, '''| `dc-motor-renode.yaml` |''', '''| `dc-motor-mosfet.yaml` | mock firmware · **ngspice** MOSFET H-bridge with dead time and device losses · mock mechanics | libngspice | [ngspice adapter](ngspice.md#example-mosfet-h-bridge-with-dead-time) |\n| `dc-motor-renode.yaml` |''')\nedit(E, '''| `tolerance/servo.yaml` | `tolerance` | servo-drivetrain |''', '''| `tuning/dead-time.yaml` | `tune` (4 workers) | dc-motor-mosfet |\n| `tolerance/servo.yaml` | `tolerance` | servo-drivetrain |''')\nedit(E, \"| `circuits/` | SPICE netlists (`motor-hbridge.cir`) |\", \"| `circuits/` | SPICE netlists (`motor-hbridge.cir`, `motor-hbridge-mosfet.cir`) |\")\n\nedit(\"docs/project/roadmap.md\", \"| 11 | ngspice: gate drive/dead time/MOSFET models, `.save` filtering, KiCad round trip | R2; INTEGRATION_PLAN 6 | missing |\",\n     \"| 11 | ngspice: gate drive/dead time/MOSFET models, `.save` filtering, KiCad round trip | R2; INTEGRATION_PLAN 6 | partly: MOSFET H-bridge with gate drivers, dead time and loss output (`dc-motor-mosfet.yaml`, `tuning/dead-time.yaml`); `save: bound` vector filtering ([ngspice](../reference/ngspice.md)); KiCad round trip still missing |\")\nEOF\ngrep -rn \"kicad\" docs/project/roadmap.md | head -3",
 "description": "Update ngspice, theory, examples, roadmap docs"
}
```

<details><summary>Result</summary>

```
Shell cwd was reset to /media/riko/model_store/GIT_/embeddyn
```

</details>

### Assistant — 2026-11-24 20:41:34

Checking two claims against data: the prototype's Vab drop at 2 µs (14.30 → 13.32 V = 0.98 V) matches t_d·f·V_bus = 0.96 V, not 2·t_d·f·V. Correcting that and the channel resistance wording.

**Tool: Bash** — Fix voltage claim and build docs strictly

```
{
 "command": "cd /media/riko/model_store/GIT_/codelayer_forge/engines/embeddyn && python3 - <<'EOF'\nfrom pathlib import Path\ndef edit(path, old, new):\n    p = Path(path); s = p.read_text()\n    assert s.count(old) == 1, (path, old[:70]); p.write_text(s.replace(old, new))\nedit(\"docs/theory/electronics.md\", '''current direction instead of the duty, so the delivered voltage falls by\nabout \\\\(2 t_d f_{PWM} V_{bus}\\\\) per leg.''', '''current direction instead of the duty, so the switching leg delivers about\n\\\\(t_d f_{PWM} V_{bus}\\\\) less (0.96 V for 2 µs at 20 kHz and 24 V; the\nsimulated bridge loses 0.98 V).''')\nedit(\"docs/reference/ngspice.md\", \"current for longer than the channels (about 6 mΩ), and the mean bridge voltage\\nsags with dead time.\",\n     \"current for longer than the channels (a few mΩ), and the mean bridge voltage\\nsags by about t<sub>d</sub>·f<sub>PWM</sub>·V<sub>bus</sub> (0.98 V at 2 µs).\")\nEOF\nexport PYTHONPATH=$PWD; /media/riko/model_store/embeddyn-venv/bin/python -m mkdocs build --strict -d /media/riko/model_store/tmp/embeddyn-site 2>&1 | grep -iE \"warn|error|built\" | grep -v \"Zensical\\|NO_MKDOCS\" | tail -5",
 "description": "Fix voltage claim and build docs strictly"
}
```

<details><summary>Result</summary>

```
INFO    -  Documentation built in 4.57 seconds
Shell cwd was reset to /media/riko/model_store/GIT_/embeddyn
```

</details>

**Tool: Bash** — Read tuning result

```
{
 "command": "S=/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad; cat /tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/tasks/bn6txquls.output | tail -5; tail -25 $S/tune.log; python3 -c \"\nimport json; d=json.load(open('$S/dead-time.json')); print({k: d[k] for k in d if k not in ('evaluations','history')}); [print(e) for e in d.get('evaluations', d.get('history', []))]\" 2>&1 | head -30",
 "description": "Read tuning result"
}
```

<details><summary>Result</summary>

```
"violations": []
  }
}

[exited with code 0]
[  9] objective=33.3075  bridge.dead_time=1.7741e-07  (ok, 10.695 s)
[ 10] objective=32.0469  bridge.dead_time=8.8914e-08  (ok, 9.844 s)
[ 11] objective=33.0552  bridge.dead_time=1.4927e-07  (ok, 10.687 s)
[ 12] objective=44.9877  bridge.dead_time=4.4563e-08  (ok, 9.571 s)
[ 13] objective=32.4722  bridge.dead_time=1.0567e-07  (ok, 9.607 s)
{
  "output": "/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/scratchpad/dead-time.json",
  "evaluations": 12,
  "runs": 13,
  "wall_seconds": 71.577,
  "feasible": true,
  "initial": {
    "parameters": {
      "bridge.dead_time": 5.000000000000002e-07
    },
    "objective": 35.45104530985868
  },
  "best": {
    "parameters": {
      "bridge.dead_time": 8.891397050194602e-08
    },
    "objective": 32.04689134159681,
    "violations": []
  }
}
Traceback (most recent call last):
  File "<string>", line 2, in <module>
TypeError: 'int' object is not iterable
{'project': '/media/riko/model_store/GIT_/codelayer_forge/engines/embeddyn/examples/mechatronics/dc-motor-mosfet.yaml', 'method': 'nelder-mead', 'workers': 4, 'runs': 13, 'speculative_runs': 4, 'wall_seconds': 71.577, 'initial': {'evaluation': 1, 'parameters': {'bridge.dead_time': 5.000000000000002e-07}, 'objective': 35.45104530985868, 'violations': [], 'metrics': {'speed.final_value': 4.669618222985648, 'speed.steady_state_error': 3.3303817770143516, 'speed.iae': 0.14592086218912792, 'speed.ise': 0.7704182991858401, 'speed.itae': 0.001837766184444786, 'speed.rise_time': None, 'speed.overshoot': 0.0, 'speed.peak_time': 0.03, 'speed.settling_time': None, 'current.peak': 10.121607130039658, 'current.rms': 7.532784189793273, 'current.mean': 7.328641128208205, 'current.min': 0.0, 'current.max': 10.121607130039658, 'energy.integral': 3.587500251290162, 'bridge_loss.peak': 2.0194103875818294, 'bridge_loss.rms': 1.2618535684598746, 'bridge_loss.mean': 1.1817015103286228, 'bridge_loss.min': 0.0, 'bridge_loss.max': 2.0194103875818294, 'loss_energy.integral': 0.03545104530985868}, 'seconds': 16.084, 'counted': True}, 'best': {'evaluation': 10, 'parameters': {'bridge.dead_time': 8.891397050194602e-08}, 'objective': 32.04689134159681, 'violations': [], 'metrics': {'speed.final_value': 4.707384805462611, 'speed.steady_state_error': 3.2926151945373894, 'speed.iae': 0.14511485025650833, 'speed.ise': 0.763592054942343, 'speed.itae': 0.0018228763126170932, 'speed.rise_time': None, 'speed.overshoot': 0.0, 'speed.peak_time': 0.03, 'speed.settling_time': None, 'current.peak': 10.207650817206437, 'current.rms': 7.592897474782701, 'current.mean': 7.386365735078698, 'current.min': 0.0, 'current.max': 10.207650817206437, 'energy.integral': 3.641572015425581, 'bridge_loss.peak': 1.8754149634818351, 'bridge_loss.rms': 1.1475329744937974, 'bridge_loss.mean': 1.0682297113865604, 'bridge_loss.min': 0.0, 'bridge_loss.max': 1.8754149634818351, 'loss_energy.integral': 0.032046891341596814}, 'seconds': 9.844, 'counted': True}, 'feasible': True}
```

</details>

**Tool: Bash** — Add tuning result to docs and rebuild

````
{
 "command": "cd /media/riko/model_store/GIT_/codelayer_forge/engines/embeddyn && python3 - <<'EOF'\nfrom pathlib import Path\np = Path(\"docs/reference/ngspice.md\"); s = p.read_text()\nold = \"sags by about t<sub>d</sub>·f<sub>PWM</sub>·V<sub>bus</sub> (0.98 V at 2 µs). `tuning/dead-time.yaml` lets `tune` choose it.\"\nassert s.count(old) == 1\ns = s.replace(old, '''sags by about t<sub>d</sub>·f<sub>PWM</sub>·V<sub>bus</sub> (0.98 V at 2 µs).\n\n`tuning/dead-time.yaml` lets `tune` choose the dead time against the loss\nenergy over 30 ms:\n\n```bash\nembeddyn mechatronics tune examples/mechatronics/tuning/dead-time.yaml --workers 4\n```\n\n12 evaluations take about 70 s on 4 workers (each worker runs its own\nlibngspice). From 500 ns (35.5 mJ) Nelder–Mead settles at 89 ns (32.0 mJ);\nat 45 ns shoot-through starts (45.0 mJ). The optimum sits just above\nthat cliff, and the model has no driver propagation-delay spread, so a real\ndesign adds margin: put the spread into `tolerance` or constrain the dead\ntime's lower bound.''')\np.write_text(s)\nEOF\n/media/riko/model_store/embeddyn-venv/bin/python -m mkdocs build --strict -d /media/riko/model_store/tmp/embeddyn-site 2>&1 | grep -iE \"warn|error|built\" | grep -v \"Zensical\\|NO_MKDOCS\" | tail -3",
 "description": "Add tuning result to docs and rebuild"
}
````

<details><summary>Result</summary>

```
INFO    -  Documentation built in 2.98 seconds
Shell cwd was reset to /media/riko/model_store/GIT_/embeddyn
```

</details>

### Assistant — 2026-11-24 20:42:05

Docs build strictly. Running the full mechatronics suite (with Renode, ngspice, MuJoCo) in the background.

**Tool: mcp__ccd_turn__update_plan**

```
{
 "advance": true
}
```

<details><summary>Result</summary>

```
Plan updated: step 6 of 6 is in progress.
```

</details>

**Tool: Bash** — Run full mechatronics test suite

```
{
 "command": "cd /media/riko/model_store/GIT_/codelayer_forge/engines/embeddyn && . /media/riko/model_store/renode-merge/env.sh; export PYTHONPATH=$PWD TMPDIR=/media/riko/model_store/tmp NGSPICE_LIBRARY=/media/riko/model_store/ngspice-lib/root/usr/lib/x86_64-linux-gnu/libngspice.so.0; timeout 1500 /media/riko/model_store/embeddyn-venv/bin/python -m pytest -q -p no:cacheprovider tests/mechatronics --basetemp=/media/riko/model_store/tmp/embeddyn-pytest-all2 2>&1 | tail -8",
 "description": "Run full mechatronics test suite",
 "run_in_background": true
}
```

<details><summary>Result</summary>

```
Command running in background with ID: b7ev2ddvb. Output is being written to: /tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/tasks/b7ev2ddvb.output. You will be notified when it completes — do not poll that file or sleep while waiting. Read the file only if the user asks for progress, or if you need a line it prints before you can go on, such as a server's ready line or port.
Session cwd remains /media/riko/model_store/GIT_/embeddyn; directory changes made by the backgrounded command do not apply to subsequent commands.
```

</details>

### Assistant — 2026-11-24 20:42:09

Suite is running; I'll report once it finishes.

## User — 2026-11-24 20:47:22

<task-notification>
<task-id>b7ev2ddvb</task-id>
<tool-use-id>toolu_01EZs32uGRB4GXe54iqhzGUV</tool-use-id>
<output-file>/tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/tasks/b7ev2ddvb.output</output-file>
<status>completed</status>
<summary>Background command "Run full mechatronics test suite" completed (exit code 0)</summary>
</task-notification>

**Tool: Bash** — Read suite result and changed files

```
{
 "command": "tail -4 /tmp/claude-1000/-media-riko-model-store-GIT--embeddyn/0444761b-2a19-464e-af51-e9dde79af500/tasks/b7ev2ddvb.output; cd /media/riko/model_store/GIT_/codelayer_forge && git -c safe.directory='*' status --short",
 "description": "Read suite result and changed files"
}
```

<details><summary>Result</summary>

```
..............................                                           [100%]
232 passed, 14 skipped in 313.47s (0:05:13)

[exited with code 0]
 M engines/embeddyn/docs/project/roadmap.md
 M engines/embeddyn/docs/reference/examples.md
 M engines/embeddyn/docs/reference/ngspice.md
 M engines/embeddyn/docs/theory/electronics.md
 M engines/embeddyn/embeddyn/mechatronics/adapters/mujoco_adapter.py
 M engines/embeddyn/embeddyn/mechatronics/adapters/ngspice_shared.py
 M engines/embeddyn/embeddyn/mechatronics/adapters/renode.py
 M engines/embeddyn/embeddyn/mechatronics/factory.py
 M engines/embeddyn/embeddyn/mechatronics/quantities.py
 M engines/embeddyn/tests/mechatronics/test_ngspice_shared.py
?? .vscode/
?? engines/embeddyn/embeddyn/mechatronics/optics.py
?? engines/embeddyn/examples/mechatronics/bindings/ngspice-hbridge-mosfet.yaml
?? engines/embeddyn/examples/mechatronics/bindings/renode-body-contour.yaml
?? engines/embeddyn/examples/mechatronics/circuits/motor-hbridge-mosfet.cir
?? engines/embeddyn/examples/mechatronics/dc-motor-mosfet.yaml
?? engines/embeddyn/examples/mechatronics/firmware/body-contour-l476/
?? engines/embeddyn/examples/mechatronics/mujoco/spect-body-contour.xml
?? engines/embeddyn/examples/mechatronics/renode/stm32l476-contour.repl
?? engines/embeddyn/examples/mechatronics/renode/stm32l476-contour.resc
?? engines/embeddyn/examples/mechatronics/spect-body-contour.yaml
?? engines/embeddyn/examples/mechatronics/tuning/dead-time.yaml
Shell cwd was reset to /media/riko/model_store/GIT_/embeddyn
```

</details>

**Tool: Bash** — Check factory and roadmap diffs are mine only

```
{
 "command": "cd /media/riko/model_store/GIT_/codelayer_forge && git -c safe.directory='*' diff engines/embeddyn/embeddyn/mechatronics/factory.py engines/embeddyn/docs/project/roadmap.md | head -40",
 "description": "Check factory and roadmap diffs are mine only"
}
```

<details><summary>Result</summary>

```
diff --git a/engines/embeddyn/docs/project/roadmap.md b/engines/embeddyn/docs/project/roadmap.md
index 445928ea..9d7f7afc 100644
--- a/engines/embeddyn/docs/project/roadmap.md
+++ b/engines/embeddyn/docs/project/roadmap.md
@@ -178,7 +178,7 @@ checked against the code.* Order is the build order: each group unblocks the nex
 | 8 | ✅ Monte Carlo tolerance runs with yield; Sobol sensitivity | R4; PHASE6 | done: `mechatronics tolerance` ([TOLERANCE.md](../reference/tolerance.md)), local process pool; experiment-queue execution still open |
 | 9 | ✅ Renode sensor/bus signals: quadrature encoder, UART/SPI/I²C/CAN frames, fault injection | R1; INTEGRATION_PLAN 4 | done: UART consoles, CAN frames, quadrature encoders (TIM CNT injection), SPI/I²C sensors (Renode ≥ 1.17); signal faults (stuck, offset, gain, noise, drift, dropout, intermittent) in projects, scenarios and `run --fault`; bus-frame faults (drop/corrupt UART bytes and CAN frames, both directions, by CAN id) ([fault injection](../reference/faults.md)) |
 | 10 | Renode fork fixes upstreamed (pins, patches 0001–0003, platform files), checkpoints via Save/Load | R1 | patches local only |
-| 11 | ngspice: gate drive/dead time/MOSFET models, `.save` filtering, KiCad round trip | R2; INTEGRATION_PLAN 6 | missing |
+| 11 | ngspice: gate drive/dead time/MOSFET models, `.save` filtering, KiCad round trip | R2; INTEGRATION_PLAN 6 | partly: MOSFET H-bridge with gate drivers, dead time and loss output (`dc-motor-mosfet.yaml`, `tuning/dead-time.yaml`); `save: bound` vector filtering ([ngspice](../reference/ngspice.md)); KiCad round trip still missing |
 | 12 | Webots bridge using real physics (Supervisor motors/sensors, timestep alignment) | R3 | MuJoCo backend now provides multibody physics ([MUJOCO.md](../reference/mujoco.md)); the Webots bridge still computes the plant itself |
 | 13 | ✅ Shared physical constants (`Kt`/`Ke` once); integer-tick time base | R3 | done: parameter references in couplings, unit conversion ([system model](../theory/system-model.md#units)) |
 | 14 | Studio UI for co-simulation runs, plots, binding editor | R5; INTEGRATION_PLAN 2 | Lab GUI done (GUI.md); run comparison and binding-file editor missing |
diff --git a/engines/embeddyn/embeddyn/mechatronics/factory.py b/engines/embeddyn/embeddyn/mechatronics/factory.py
index ce4adf10..e082f1d2 100644
--- a/engines/embeddyn/embeddyn/mechatronics/factory.py
+++ b/engines/embeddyn/embeddyn/mechatronics/factory.py
@@ -60,6 +60,7 @@ def ngspice_shared_kwargs(project: MechatronicProject, cfg) -> dict:
         "max_step": float(max_step) if max_step is not None else project.step_seconds / 50,
         "duration": project.duration_seconds,
         "options": dict(options.get("spice_options", {})),
+        "save": str(options.get("save", "bound")),
     }
 
 
@@ -102,7 +103,8 @@ def _build_instance(project: MechatronicProject, cfg: SimulatorConfig, mock: boo
         return MujocoAdapter(str(project.resolve(cfg.model)), inputs=dict(options.get("inputs", {})),
                              outputs=dict(options.get("outputs", {})),
                              timestep=float(timestep) if timestep is not None else None,
-                             initial=dict(options.get("initial") or {}))
+                             initial=dict(options.get("initial") or {}),
+                             light_curtain=options.get("light_curtain"), clearance=options.get("clearance"))
     if not core:
         raise ValueError(f"{cfg.name}: {domain.value} instances support backends python, fmu and replay (got {backend})")
     if domain is Domain.FIRMWARE:
Shell cwd was reset to /media/riko/model_store/GIT_/embeddyn
```

</details>

**Tool: mcp__ccd_turn__wrap_up**

```
{
 "recap": "Roadmap item 11's power-electronics part is done: MOSFET H-bridge with dead time and loss output, `save: bound` vector filtering, a stop-time fix, a dead-time tuning spec and docs. The mechatronics suite passes (232) and docs build strictly. Nothing is committed; `factory.py` also contains another session's light-curtain change.",
 "ask": "Commit this on a new embeddyn-power-electronics branch and sync the standalone repo?",
 "actions": [
  {
   "label": "Commit and sync"
  },
  {
   "label": "Not yet"
  }
 ]
}
```

<details><summary>Result</summary>

```
The user is working over Remote Control and may not see the next-step box. Do not call wrap_up again this turn; the advice that follows is for your next call. recap is two or three sentences (about 300 characters); this one ran 330. Keep it to what is now true and where it stands. Write your closing message now, as the last thing you say this turn, as if there were no box: what you did and anything that failed or is unverified, no next steps, and only what you need from them to go on, as a question with its choices.
```

</details>

### Assistant — 2026-11-24 20:47:48

The power-electronics part of roadmap item 11 is done in the monorepo copy: a MOSFET H-bridge with real dead time and loss output, and a fix that cuts ngspice's memory use about eightfold. Everything under `tests/mechatronics` passes (232 passed, 14 skipped) and `mkdocs build --strict` passes. Nothing is committed yet.

**Bug fixed along the way:** each co-simulation step used to stop a hair before its end time. When a PWM carrier corner fell exactly on that time, the next step failed with "timestep too small". Steps now stop exactly on the time; a regression test covers it, and the existing H-bridge test still passes.

**Memory filtering (`save: bound`, the new default):** ngspice now keeps only `time` and the vectors the output bindings read.
- On the MOSFET bridge that is 6 vectors instead of 50, about 5 MB instead of 44 MB per 20 ms simulated, with identical results.
- `save: all` keeps the old behaviour.
- Getting this to work needed two workarounds. The shared library ignores `.save` lines in the netlist, and the adapter's per-step `delete all` was also clearing the save list. The adapter now re-sends the `save` command before every step.

**MOSFET H-bridge (`dc-motor-mosfet.yaml`):**
- Four MOSFETs with body diodes, gate drivers with dead time, and an output for the power lost in the transistors.
- Dead time and gate resistance are project parameters, so `-p`, `tune` and `tolerance` reach the circuit.
- It needs the `gear` integration method; with the default method one closed-loop run failed partway.
- I gave this example a rotor 10× lighter than `dc-motor-ngspice.yaml`, so the bridge is actually switching (about 55 % duty) instead of sitting at full duty for most of the run.
- 0.1 s of simulation takes about 40 s.

Losses at 0.1 s, from real runs:

| Dead time | 0 | 100 ns | 200 ns | 500 ns | 1 µs |
|---|---|---|---|---|---|
| Transistor losses | 11.2 W | 0.80 W | 0.82 W | 0.85 W | 0.93 W |

At 0 ns both transistors of a leg conduct at every switching edge. With longer dead times, losses creep up and the output voltage drops by about 1 V at 2 µs, which matches the textbook estimate.

**Tuning (`tuning/dead-time.yaml`):** 12 runs took 72 s on 4 workers. It picked 89 ns (32.0 mJ over the run) over the 500 ns starting point (35.5 mJ); at 45 ns it was already 45.0 mJ. That optimum sits just above the point where both transistors start conducting together, and the model has no driver timing spread, so the docs say a real design should add margin.

**Docs:** updated the ngspice reference, the electronics theory page (a dead-time trade-off section), the examples catalogue and the roadmap. The roadmap marks item 11 as partly done; the KiCad round trip is still missing.

**Before committing:** another session is editing the same checkout for the optical body-contour gate. `factory.py` has both my one-line `save` change and its MuJoCo `light_curtain` change. I'd stage only my part of that file and leave its other files alone.

Should I commit this on a new `embeddyn-power-electronics` branch and sync it to the standalone repo, or hold off for now?

