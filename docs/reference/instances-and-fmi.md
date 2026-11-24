# Simulator instances and FMI

A project can contain any number of simulators, each a **named instance**
with a domain, a backend and optionally its own step. This is how a system
with two motor axes, several controllers or a mix of FMUs and Python models is
described.

## Declaring instances

```yaml
step_seconds: 0.001          # macro step

instances:
  ctrl_left:
    domain: firmware
    model: "embeddyn.mechatronics.plants:PIController"
    options: {backend: python, inputs: {...}, outputs: {...}}
  wheel_left:
    domain: mechanical
    step: 0.0005             # own step; must divide the macro step
    model: fmus/rotational_load.fmu
    options: {backend: fmu, inputs: {tau: left.torque}, outputs: {w: left.wheel_speed}}
```

* Names: letters, digits, `_`, `-`, starting with a letter; unique across the
  project.
* `domain` is one of `firmware`, `electrical`, `mechanical`, `thermal`,
  `environment`. It decides the execution order within a step (in that domain
  order, then declaration order) and which couplings are applied before the
  instance runs (couplings whose target signal belongs to that domain).
* `step` makes the scheduler multi-rate (it is chosen automatically when no
  `scheduler.algorithm` is given). Activations are ordered by time, then by
  the order above.
* Backends usable in every domain: `python`, `fmu`, `replay` (recorded data, see [ESTIMATION.md](estimation.md)). Firmware also has `renode`
  and `mock`; electrical has `ngspice-shared`, `ngspice`, `ltspice` and `mock`;
  mechanical has `webots` and `mock`.

### Compatibility

The `simulators:` section keeps working: each entry becomes an instance named
after its domain, and a project without `instances:` still gets default
instances for the three core domains. Code can keep addressing adapters and
activation counts by `Domain` (`adapters[Domain.FIRMWARE]` is the same entry
as `adapters["firmware"]`). Run manifests, checkpoints and activation counts
are keyed by instance name.

The built-in mock adapters read and write fixed signal names
(`firmware.pwm_duty`, …), so two mock instances would collide. For several
controllers or motors use mappable models: `PIController` and `DCMotorRL`
(and the drivetrain/thermal models in [TUNING.md](tuning.md)) or FMUs.

Each Renode instance needs its own port. ngspice-shared allows one circuit
per process, so run several circuits with `--isolate-adapters`.

## FMUs (`backend: fmu`)

FMI **2.0 and 3.0 co-simulation** FMUs load directly through ctypes, with no
third-party dependency:

* The FMU is unpacked and its binary for this platform is loaded
  (`binaries/linux64` for FMI 2, `binaries/x86_64-linux` for FMI 3, and the
  macOS/Windows equivalents). Paths in the archive that escape the unpack
  directory are rejected.
* `parameters` (also `"@project.parameter"`) are set before initialisation,
  `inputs` before every `doStep`, `outputs` read after the step. Only
  Real/Float64 variables are exchanged; names are checked against
  `modelDescription.xml` when the project loads.
* `max_step` (or the FMU's `DefaultExperiment stepSize`) splits each
  co-simulation step into several `doStep` calls.
* **Checkpoints:** when the FMU declares `canGetAndSetFMUstate` and
  `canSerializeFMUstate` (FMI 3: `...FMUState`), every checkpoint stores the
  serialized FMU state (base64). A resumed run continues bit for bit. An FMU
  without these flags records only its time, and resuming it fails with a
  clear error instead of restarting at t = 0.
* `integration-validate` unpacks each FMU and checks the variable mapping.

### Example FMU

`examples/mechatronics/fmus/rotational_load/` holds a rotational load
(inertia, viscous and Coulomb friction, load torque) written in C, and
`build_fmu.py`, which compiles it into `rotational_load.fmu` (FMI 2.0) and
`rotational_load_fmi3.fmu` (FMI 3.0):

```bash
python examples/mechatronics/fmus/rotational_load/build_fmu.py
```

Both versions match the closed-form step response to 10 significant digits
(`tests/mechatronics/test_instances_fmu.py`, which builds them in a temporary
directory). The test also checks that a wrong instantiation token, an
unknown variable or an invalid parameter are reported. The example implements
FMU state get/set/serialize and stubs the rest of the FMI API, so it passes
FMPy's validation for both versions.

## Exporting plants as FMUs, and checking FMUs

```bash
pip install 'embeddyn[fmi]'            # PythonFMU3 + FMPy
embeddyn mechatronics fmu-export embeddyn.mechatronics.plants:RCServo -o dist/ \
    -p load_inertia=2e-4 --input-start load_torque=0
embeddyn mechatronics fmu-check dist/RCServo.fmu
```

`fmu-export` wraps any python plant class (`module:Class` or `file.py:Class`)
in an **FMI 3.0 co-simulation FMU** built with PythonFMU3:

* Plant inputs and outputs become FMU inputs and outputs. Numeric constructor
  parameters become tunable FMU parameters; strings and sequences are fixed at
  export. A parameter whose name clashes with a port gets a `p_` prefix
  (RCServo's `supply` parameter becomes `p_supply`).
* An input starts at `--input-start`, else at the same-named parameter, else 0.
  This matters because an FMU input always has a value, while an unmapped
  embeddyn input falls back to the plant's own default.
* The ODE states are local variables, so the FMU declares state get/set and
  serialization, and embeddyn and other importers can checkpoint it.
* Each `doStep` integrates with the same RK4 sub-stepping (`--max-step`) as
  `backend: python`. Swapping the RCServo plant in `rc-servo-lab.yaml` for its
  exported FMU gives **bit-identical** rows. It runs about 2× slower through
  the FMU layer.
* The plant's module file is copied into the FMU, so it must import only the
  standard library (all built-in plants do). The importing machine needs
  Python 3 (a PythonFMU3 property).

`fmu-check` runs FMPy's model-description validation and a short
co-simulation. It reports FMI version, variables, state capabilities and every
problem found. It found two real errors in the hand-written example (no FMI 3
`time` variable, missing `InitialUnknown`s), both now fixed. `fmu-export` runs
it on every export unless `--no-check` is given.

Tests (`tests/mechatronics/test_fmi_export.py`):

* FMU state round trips, in the same instance and in a fresh one, for both FMI versions;
* refusal to resume an FMU that has no state functions;
* the exported RCServo reproduces the python plant exactly and resumes from a
  project checkpoint;
* FMPy validates both the exported and the C FMUs.

### Reference-FMUs

The importer is checked against the Modelica Association
[Reference-FMUs](https://github.com/modelica/Reference-FMUs) v0.0.42, with FMPy
as the oracle (`tests/mechatronics/test_reference_fmus.py`, opt-in):

```bash
python tools/fmi/fetch_reference_fmus.py .cache/reference-fmus     # pinned, SHA-256 checked
EMBEDDYN_REFERENCE_FMUS=.cache/reference-fmus pytest tests/mechatronics/test_reference_fmus.py
```

Dahlquist, VanDerPol and BouncingBall, in FMI 2.0 and 3.0, give **exactly**
FMPy's values at every one of 100 steps. Their FMU state round-trips bit for
bit, and Feedthrough passes inputs through unchanged. CI fetches the archive and
runs these tests.
