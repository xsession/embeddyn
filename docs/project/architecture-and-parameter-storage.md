# Scalable Architecture and Parameter Storage Specification for the Mechatronic Emulator/Simulator

## 1. Purpose

This document defines a long-term, scalable architecture and parameter-storage strategy for a complex mechatronic emulator and simulator platform integrating:

- firmware emulation and hardware-in-the-loop
- electrical simulation
- mechanical simulation
- thermal and environmental models
- KiCad-based electrical design data
- Webots-based mechanical interaction
- LTspice/ngspice-based circuit simulation
- telemetry, replay, observability, and experiment management

The central design principle is:

> **Models define behavior, projects compose models, scenarios override parameters, and runs freeze everything into an immutable snapshot.**

This separation keeps the system maintainable as it grows in complexity.

---

## 2. Recommended Target Architecture

Treat the system as a versioned engineering platform rather than a single simulator process.

```text
                         Embeddyn
                                  |
                     Project API / Command Service
                                  |
              +-------------------+-------------------+
              |                                       |
       Model Registry                         Experiment Manager
              |                                       |
              +-------------------+-------------------+
                                  |
                        Co-simulation Runtime
                                  |
          +-----------+-----------+-----------+-----------+
          |           |           |           |           |
       Firmware    Electrical   Mechanical  Thermal    Environment
       Renode      SPICE        Webots      models     custom/FMUs
          |           |           |           |           |
          +-----------+-----------+-----------+-----------+
                                  |
                    Typed signal/event transport
                                  |
                  Telemetry, artifacts and replay
```

---

## 3. Architectural Layers

### 3.1 Authoring Layer

Responsibilities:

- visual graph editor
- component browser
- parameter editor
- signal and bus editor
- schematic and mechanical model association
- scenario editor
- experiment comparison
- plot and logic-analyzer configuration

The user interface must not write directly into simulator-specific files.

Instead, it edits a normalized project model through an API.

```text
UI
 |
 v
Project service
 |
 v
Normalized domain model
```

Example authored value:

```yaml
motor:
  rated_voltage: 24 V
  resistance: 2 ohm
  torque_constant: 0.08 N*m/A
```

The electrical adapter generates SPICE parameters, while the Webots adapter generates or modifies mechanical fields.

---

### 3.2 Engineering Model Layer

This is the permanent source of truth.

It contains:

- system topology
- component instances
- reusable component definitions
- ports and signals
- physical parameters
- simulator model references
- firmware images
- transformations and couplings
- units and dimensions
- validation rules

This layer must remain simulator-independent.

Do not store the complete system primarily as:

- a Webots world
- an LTspice schematic
- a KiCad schematic
- a Renode `.repl`
- a large flat YAML file

These should instead be domain artifacts referenced by the normalized system model.

---

### 3.3 Compilation Layer

Add an explicit compilation phase between project configuration and execution.

```text
Editable project
      |
      v
Validation
      |
      v
Model compilation
      |
      +--> Renode configuration
      +--> SPICE netlist
      +--> Webots runtime mapping
      +--> signal routing table
      +--> scheduler plan
      +--> telemetry schema
      |
      v
Immutable run manifest
```

Benefits:

- errors are detected before simulators start
- run behavior is reproducible
- generated simulator input can be inspected
- simulator-specific mappings remain isolated
- compiled projects can be cached

The compiled result should be immutable.

---

### 3.4 Runtime and Scheduling Layer

The orchestrator should evolve into a pluggable co-simulation master.

#### Deterministic fixed-step mode

Best for:

- firmware regression
- ordinary motor systems
- automated tests
- reproducible debugging

#### Multi-rate fixed-step mode

Each domain runs at its own step:

```yaml
scheduler:
  macro_step: 1 ms

  domains:
    firmware:
      step: 100 us
    electrical:
      step: 10 us
    mechanical:
      step: 1 ms
    thermal:
      step: 10 ms
```

Example execution cadence:

```text
10 us: electrical
20 us: electrical
...
100 us: firmware + electrical
...
1 ms: firmware + electrical + mechanical
```

This is more scalable than forcing all simulators to share one common time step.

#### Iterative coupling mode

For tightly coupled systems:

- motor current and torque
- hydraulic pressure and displacement
- contact forces
- switching power electronics
- thermal/electrical feedback

```text
electrical -> mechanical -> electrical
                  |
          convergence check
```

#### Real-time/HIL mode

Later support:

- monotonic wall-clock scheduling
- missed-deadline reporting
- hardware I/O watchdogs
- emergency-safe output states
- bounded queue lengths
- real-time telemetry decimation

Do not mix real-time assumptions into the ordinary offline scheduler.

---

### 3.5 Adapter Layer

Every simulator should implement a common lifecycle interface.

```python
class SimulatorAdapter:
    def prepare(self, context): ...
    def start(self): ...
    def initialize(self, initial_state): ...
    def set_inputs(self, values, time): ...
    def step(self, target_time): ...
    def get_outputs(self): ...
    def checkpoint(self): ...
    def restore(self, checkpoint): ...
    def stop(self): ...
```

Recommended adapter structure:

```text
adapters/
  firmware/
    renode/
    qemu/
    native_process/
    hardware_target/

  electrical/
    ngspice/
    ltspice/
    xyce/
    reduced_order/

  mechanical/
    webots/
    internal_rigid_body/
    external_fmi/

  electronics_design/
    kicad/

  environment/
    python_model/
    lookup_table/
    fmu/
```

The KiCad integration is mainly a model-authoring and extraction adapter, not a time-step simulator.

It should:

- export SPICE-compatible circuits
- associate symbols with component definitions
- map net names to simulator signals
- collect metadata and model references
- validate missing SPICE models
- optionally synchronize selected parameters

---

## 4. Use Three Separate Graphs

A complex mechatronic system needs three distinct graph models.

### 4.1 Structural Graph

Describes physical and logical composition:

```text
controller
motor_driver
motor
gearbox
shaft
load
encoder
power_supply
```

Edges represent:

- electrical nets
- mechanical joints
- buses
- thermal paths
- logical dependencies

### 4.2 Signal Graph

Describes runtime values:

```text
PWM duty
phase current
motor torque
shaft position
encoder ticks
temperature
CAN frames
```

### 4.3 Execution Graph

Describes simulator dependencies and scheduling:

```text
Renode -> SPICE -> Webots -> Renode
```

Keeping these separate prevents confusion between physical connectivity and execution order.

---

## 5. Parameter Storage Architecture

Use a layered parameter system.

### 5.1 Parameter Precedence

Recommended precedence order:

```text
1. built-in schema default
2. component model default
3. manufacturer/model variant
4. project instance value
5. assembly override
6. scenario override
7. experiment sweep value
8. runtime calibration
```

Higher layers override lower layers.

Example:

```text
Generic DC motor default:
  resistance = 2.5 ohm

Selected motor model:
  resistance = 2.1 ohm

Measured unit calibration:
  resistance = 2.07 ohm

Hot-temperature scenario:
  resistance = 2.35 ohm
```

The resolved run stores `2.35 ohm`, but provenance records the full override chain.

---

### 5.2 Unit-Aware Values

Never store numeric physical parameters without units.

Avoid:

```yaml
inertia: 0.02
```

Use:

```yaml
inertia:
  value: 0.02
  unit: kg*m^2
```

Or:

```yaml
inertia: 0.02 kg*m^2
```

Internally normalize to:

```json
{
  "value_si": 0.02,
  "display_value": 0.02,
  "display_unit": "kg*m^2",
  "dimension": "mass*length^2",
  "source": "project-instance"
}
```

This supports:

- automatic conversion
- dimensional validation
- consistent plotting
- parameter comparison
- safer simulator mappings

Use SI internally while preserving user-selected display units.

---

## 6. Supported Parameter Types

The parameter system should support:

- scalar
- integer
- boolean
- string
- enum
- quantity with unit
- vector
- matrix
- curve/table
- distribution
- expression
- reference
- artifact reference
- time series
- structured object
- secret

Example:

```yaml
parameters:
  motor.resistance:
    type: quantity
    value: 2.0
    unit: ohm

  motor.commutation:
    type: enum
    value: brushed
    allowed: [brushed, bldc, pmsm]

  gearbox.efficiency_curve:
    type: table
    columns: [speed, torque, efficiency]
    artifact: sha256:...

  sensor.noise:
    type: distribution
    distribution: normal
    mean: 0 mV
    sigma: 2 mV

  control.sample_time:
    type: expression
    expression: "${scheduler.firmware_step} * 10"
```

---

## 7. Storage Tiers

### 7.1 Git-Managed Human-Readable Files

Use for:

- project topology
- component definitions
- schemas
- scenarios
- mappings
- experiment definitions
- test expectations
- documentation

Recommended formats:

- YAML for authored configuration
- JSON Schema for validation
- JSON for generated normalized manifests
- TOML for application-level configuration

Suggested structure:

```text
project/
  project.yaml
  assemblies/
  components/
  scenarios/
  experiments/
  mappings/
  schemas/
```

---

### 7.2 Relational Metadata Database

For local installation, use SQLite.

For collaborative/server deployment, support PostgreSQL through the same repository abstraction.

Store:

- project metadata
- component indexes
- parameter definitions
- parameter revisions
- scenario metadata
- run records
- model relationships
- artifact references
- tags
- validation results
- user-created presets

Do not store large telemetry or binaries directly in relational tables.

Suggested tables:

```text
projects
project_revisions
component_definitions
component_versions
component_instances
parameter_definitions
parameter_values
parameter_overrides
scenarios
experiments
runs
run_parameters
artifacts
artifact_links
signal_definitions
model_bindings
```

---

### 7.3 Content-Addressed Artifact Store

Store large and binary objects by SHA-256 identity.

```text
.artifacts/
  sha256/
    ab/
      abcdef...
```

Suitable artifacts:

- firmware ELF/bin/hex
- Webots worlds and PROTO files
- KiCad exports
- SPICE models
- generated netlists
- CAD meshes
- material maps
- lookup tables
- test captures
- screenshots
- simulator logs
- checkpoints

Metadata example:

```json
{
  "digest": "sha256:abcdef...",
  "media_type": "application/x-spice-netlist",
  "size": 18422,
  "original_name": "motor_driver.cir",
  "created_by": "kicad-export",
  "source_revision": "git:4f893a..."
}
```

Benefits:

- deduplication
- immutable references
- reproducibility
- cache reuse
- integrity verification

---

### 7.4 Telemetry Storage

CSV is useful for export but should not be the main storage format.

Use Parquet or Arrow IPC for ordinary simulation output.

Suggested run layout:

```text
runs/<run-id>/
  manifest.json
  parameters.parquet
  signals/
    firmware.parquet
    electrical.parquet
    mechanical.parquet
    events.parquet
  logs/
  artifacts/
  summary.json
```

Advantages:

- efficient partial-column reads
- compression
- typed fields
- faster plotting
- large-run scalability
- easy CSV conversion

Do not write every high-frequency sample into SQLite.

---

## 8. Component Model Structure

Each reusable component needs a stable identity and version.

```yaml
schema_version: 1
kind: component-definition

identity:
  namespace: codelayer.motion
  name: dc-motor
  version: 1.2.0

ports:
  electrical:
    positive:
      type: electrical_terminal
    negative:
      type: electrical_terminal

  mechanical:
    shaft:
      type: rotational_flange

  signals:
    temperature:
      direction: output
      quantity: temperature

parameters:
  resistance:
    type: quantity
    unit: ohm
    default: 2.0
    constraints:
      exclusive_minimum: 0

  torque_constant:
    type: quantity
    unit: N*m/A
    default: 0.08

models:
  electrical:
    adapter: ngspice
    artifact: sha256:...
    mapping: mappings/motor-ngspice.yaml

  mechanical:
    adapter: webots
    artifact: sha256:...
    mapping: mappings/motor-webots.yaml

  reduced_order:
    adapter: python
    entrypoint: models.motor:MotorModel
```

A component may have several fidelity levels:

```text
motor:
  level 0: algebraic mock
  level 1: differential Python model
  level 2: ngspice + simplified Webots
  level 3: vendor SPICE + detailed geometry
```

Scenarios should select fidelity without changing system topology.

---

## 9. Separate Definition, Instance, and Resolved Value

### Definition

```yaml
id: codelayer.motion.dc-motor/resistance
type: quantity
unit: ohm
default: 2.0
```

### Instance Override

```yaml
component: drive_motor
parameters:
  resistance: 2.1 ohm
```

### Resolved Run Value

```json
{
  "parameter_id": "drive_motor.resistance",
  "value_si": 2.35,
  "unit_si": "ohm",
  "source_chain": [
    {"source": "component-default", "value": "2.0 ohm"},
    {"source": "project-instance", "value": "2.1 ohm"},
    {"source": "thermal-scenario", "value": "2.35 ohm"}
  ]
}
```

This provenance is essential for debugging and certification-oriented work.

---

## 10. Recommended Project Structure

```text
mechatronic-project/
  project.yaml
  project.lock

  components/
    instances.yaml

  assemblies/
    drive-system.yaml
    controller-cabinet.yaml

  topology/
    electrical.yaml
    mechanical.yaml
    communication.yaml
    thermal.yaml

  signals/
    signals.yaml
    buses.yaml
    events.yaml

  models/
    webots/
      worlds/
      protos/
      mappings/

    spice/
      circuits/
      models/
      mappings/

    renode/
      platforms/
      scripts/
      peripherals/

    kicad/
      source/
      exports/
      mappings/

  scenarios/
    nominal.yaml
    high-load.yaml
    stalled-motor.yaml
    sensor-failure.yaml

  experiments/
    pid-sweep.yaml
    thermal-cycle.yaml
    regression.yaml

  calibration/
    motor-unit-001.yaml
    encoder-unit-003.yaml

  dashboards/
    motor-control.yaml
    power-analysis.yaml

  tests/
    system/
    integration/
    golden/

  schemas/
  docs/
```

Generated output should be separated:

```text
.embeddyn/
  cache/
  artifacts/
  compiled/
  runs/
  logs/
  database/
```

---

## 11. Project Lock File

Add a lock file similar to a package manager.

```yaml
lock_version: 1

project_revision: sha256:...

components:
  codelayer.motion.dc-motor:
    version: 1.2.0
    digest: sha256:...

simulators:
  renode:
    adapter_version: 0.4.0
    runtime_digest: sha256:...

  webots:
    adapter_version: 0.3.0
    model_digest: sha256:...

  ngspice:
    adapter_version: 0.2.0

artifacts:
  firmware:
    digest: sha256:...
  electrical_netlist:
    digest: sha256:...
  webots_world:
    digest: sha256:...
```

A run should not start in reproducible mode when the lock file and project differ.

---

## 12. Run Manifest

Every run should produce an immutable manifest.

```json
{
  "run_id": "01J...",
  "project_revision": "sha256:...",
  "created_at": "...",
  "scheduler": {
    "algorithm": "multirate-fixed-step",
    "macro_step_s": 0.001
  },
  "simulators": {},
  "resolved_parameters_digest": "sha256:...",
  "signal_schema_digest": "sha256:...",
  "input_artifacts": {},
  "random_seed": 184722,
  "host": {},
  "result_status": "completed"
}
```

The manifest must contain enough information to reproduce the run independently from the editable project.

---

## 13. Signal System

Use a typed signal registry.

```yaml
id: drivetrain.motor.current
data_type: float64
quantity: electric_current
unit: A
domain: electrical
direction: output
sample_policy: every_step
retention: full
limits:
  warning: 8 A
  critical: 12 A
```

Supported semantics:

### Continuous Signal

Examples:

- voltage
- current
- torque
- temperature
- position

### Discrete State

Examples:

- limit switch
- relay state
- fault flag

### Event

Examples:

- CAN frame
- interrupt edge
- collision
- watchdog reset

### Sampled Bus

Examples:

- SPI transfer
- CAN frame
- UART packet
- EtherCAT process data

A CAN frame should not be represented as a floating-point scalar.

---

## 14. Coupling Model

Use declarative transformations instead of unrestricted expression evaluation.

```yaml
couplings:
  - source: electrical.motor_current
    target: mechanical.motor_torque

    transform:
      type: affine
      gain: 0.08 N*m/A
      offset: 0 N*m

    timing:
      delay: 0 s
      interpolation: zero_order_hold
      extrapolation: hold
```

Supported transformations:

- identity
- affine
- clamp
- dead band
- lookup table
- polynomial
- unit conversion
- quantization
- hysteresis
- filter
- sampled latch
- custom sandboxed model

Custom code should run in an isolated plugin process.

---

## 15. Configuration Ownership

Each parameter needs one declared owner.

Examples:

```text
motor winding resistance       component model
supply voltage                 scenario
PWM frequency                  firmware configuration
gear ratio                     mechanical assembly
ambient temperature            environment scenario
encoder resolution             hardware component definition
controller gain                calibration/control preset
simulation step                scheduler configuration
```

Without ownership rules, duplicate values will appear in YAML, SPICE, Webots, and firmware configuration.

The compiler should detect conflicts such as:

```text
Project says supply = 24 V
SPICE netlist says supply = 12 V
Firmware device tree says supply = 24 V
```

---

## 16. KiCad Synchronization Policy

Avoid uncontrolled bidirectional synchronization.

Recommended ownership:

```text
KiCad owns:
- schematic topology
- net names
- symbol references
- electrical component values
- model assignments

Embeddyn owns:
- cross-domain component identity
- simulation scenarios
- runtime signal routing
- mechanical associations
- firmware associations
- experiment overrides
```

Synchronization flow:

```text
KiCad source
   |
   v
KiCad extractor
   |
   v
normalized electrical model
   |
   v
validation report
   |
   +--> accepted into project revision
```

Write-back should always be explicit and previewable.

---

## 17. Webots Integration Policy

Keep Webots focused on mechanical and spatial behavior.

Example binding:

```yaml
bindings:
  - parameter: drive_motor.rotor_inertia
    target:
      node: DRIVE_MOTOR_ROTOR
      field: physics.inertiaMatrix

  - signal: drivetrain.motor.torque
    target:
      device: drive_motor
      command: torque

  - signal: drivetrain.shaft.position
    source:
      device: shaft_sensor
      property: value
```

Do not place production control algorithms in Webots controllers when validating firmware.

The Webots controller should remain a thin bridge.

---

## 18. Database Design

Use repository abstractions so SQLite and PostgreSQL are interchangeable.

```python
class ProjectRepository:
    def get_revision(...)
    def save_revision(...)
    def list_components(...)

class RunRepository:
    def create_run(...)
    def update_status(...)
    def attach_artifact(...)

class ParameterRepository:
    def resolve(...)
    def save_override(...)
    def get_provenance(...)
```

Recommended local stack:

```text
SQLite:
  metadata, indexing, revisions, run registry

filesystem/CAS:
  models, firmware, generated artifacts

Parquet:
  telemetry and parameter snapshots

YAML/Git:
  authored engineering configuration
```

Recommended server stack:

```text
PostgreSQL:
  shared metadata and coordination

S3-compatible object storage:
  artifacts and run outputs

Parquet:
  telemetry

Git:
  source-controlled engineering models
```

The application should expose the same service API in both modes.

---

## 19. Versioning Rules

Version independently:

- project schema
- component definition
- component model
- simulator adapter
- artifact format
- signal schema
- telemetry format
- database schema

Example:

```yaml
schema_versions:
  project: 2
  component: 3
  coupling: 2
  telemetry: 1
```

Provide explicit migrations:

```text
migrations/
  project/
    v1_to_v2.py
  database/
    0001_initial.sql
    0002_parameter_provenance.sql
```

Never silently reinterpret an old parameter.

---

## 20. Parameter Validation

Validation should occur in stages.

### Schema Validation

- required fields
- allowed types
- field shapes

### Dimensional Validation

- volts cannot connect to torque
- seconds cannot override radians
- gain dimensions must be valid

### Range Validation

- resistance must be positive
- PWM duty must be in `[-1, 1]`
- inertia must be greater than zero

### Cross-Parameter Validation

```text
minimum voltage < nominal voltage < maximum voltage
encoder CPR is divisible by quadrature factor
scheduler step <= controller sample period
```

### Model Validation

- Webots node exists
- SPICE parameter exists
- Renode peripheral property exists
- KiCad model assignment exists

### Runtime Compatibility Validation

- adapter supports the requested capability
- checkpointing is available when requested
- real-time mode is supported
- selected signal rate is acceptable

---

## 21. Experiment and Sweep Storage

Experiments should reference a base scenario and contain only changes.

```yaml
kind: experiment
name: pid-gain-sweep

base_scenario: scenarios/nominal.yaml

variables:
  controller.kp:
    values: [0.2, 0.4, 0.6, 0.8]

  controller.ki:
    range:
      start: 0.01
      stop: 0.1
      count: 10
      scale: logarithmic

metrics:
  - settling_time
  - overshoot
  - peak_current

constraints:
  - peak_current < 12 A
  - overshoot < 5 percent
```

Each expanded experiment should generate independent immutable run manifests.

---

## 22. Calibration and Uncertainty

Store nominal parameters separately from measured calibration.

```yaml
device_identity:
  serial_number: MOTOR-001

calibration:
  resistance:
    measured: 2.07 ohm
    uncertainty: 0.03 ohm
    temperature: 22 degC
    measured_at: 2026-07-10
    method: four-wire
```

The parameter model should support:

- uncertainty
- confidence intervals
- measurement conditions
- calibration expiry
- serial-number-specific overrides
- Monte Carlo distributions

This is important for comparing simulation with real equipment.

---

## 23. Security and Plugin Isolation

External adapters and custom models should run out of process.

```text
Core orchestrator
      |
      | authenticated local RPC
      v
Adapter worker process
      |
      v
External simulator
```

Benefits:

- simulator crashes do not terminate the whole application
- plugins cannot corrupt scheduler state
- resource limits can be applied
- adapters can use different runtimes
- remote execution becomes possible

Capability manifest example:

```yaml
capabilities:
  deterministic_step: true
  variable_step: false
  checkpoint: true
  real_time: false
  events: true
```

---

## 24. Service Boundaries

Start as a modular monolith while preserving future service boundaries.

```text
embeddyn/
  domain/
    projects/
    components/
    parameters/
    signals/
    experiments/
    runs/

  application/
    commands/
    queries/
    validation/
    compilation/

  infrastructure/
    database/
    artifact_store/
    telemetry/
    process_runtime/

  simulation/
    scheduler/
    adapters/
    coupling/
    checkpoints/

  api/
    rest/
    websocket/
    cli/

  ui/
```

Recommended initial process split:

```text
Studio/API process
Simulation worker process
Simulator adapter processes
```

Avoid starting with many network microservices.

---

## 25. Recommended Changes for the Current Implementation

The current flat YAML model is suitable for a prototype, but the next revision should add:

1. `schema_version` in every authored document.
2. Separate component definitions from component instances.
3. Unit-aware quantities instead of plain floats.
4. Typed signals and events.
5. Multi-rate scheduler configuration.
6. Compiled immutable run plans.
7. Parameter override layers with provenance.
8. Content-addressed model and firmware artifacts.
9. Parquet telemetry instead of in-memory-only runs and CSV-only export.
10. Process-isolated adapters.
11. Safe declarative coupling transformations.
12. Project and dependency lock files.
13. Simulator capability declarations.
14. Database-backed run and artifact registry.
15. Explicit schema and database migrations.

Target orchestrator structure:

```text
RunCoordinator
  |
  +-- ProjectCompiler
  +-- SchedulerStrategy
  +-- SignalBroker
  +-- AdapterManager
  +-- CheckpointManager
  +-- TelemetryWriter
  +-- RuntimeValidator
```

---

## 26. Practical Implementation Sequence

### Phase 1: Stabilize the Model

- introduce schema versions
- define parameter and unit schemas
- separate project, scenario, and run manifest
- add lock file
- implement model validation
- replace raw expressions with typed transforms

### Phase 2: Scalable Storage

- add SQLite metadata schema
- implement content-addressed artifact storage
- write telemetry to Parquet
- add parameter provenance
- add run manifests and run registry

### Phase 3: Runtime Redesign

- split compiler from orchestrator
- add adapter capability model
- introduce multi-rate scheduling
- isolate adapter processes
- add event and bus messages
- add checkpoints

### Phase 4: Engineering Integrations

- stronger KiCad model extraction and validation
- complete Webots field/device mappings
- persistent ngspice worker
- Renode bus and peripheral bridge
- reusable component model catalog

### Phase 5: Distributed and HIL Use

- PostgreSQL backend
- object-storage backend
- remote worker agents
- experiment queue
- real-time scheduler
- hardware I/O safety service

---

## 27. Final Recommended Storage Model

```text
Git/YAML/JSON Schema
    authored engineering definitions

SQLite or PostgreSQL
    metadata, revisions, relationships, run registry

Content-addressed filesystem or object storage
    firmware, CAD, Webots, SPICE and generated artifacts

Parquet/Arrow
    high-volume telemetry and resolved parameter snapshots

Immutable JSON run manifest
    exact reproducibility record
```

This architecture supports a simple single-machine workflow today while allowing the platform to grow into a distributed simulation, experiment, firmware-in-the-loop, and digital-twin environment without redesigning its fundamental data model.
