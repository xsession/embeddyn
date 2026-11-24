# Mechanics, multibody physics and 3-D visualisation candidates for embeddyn

Research date: 2026-11-23. Scope: open-source / free engines that could become embeddyn's mechanical domain, stepped from the Python orchestrator in lock-step at 1 ms (sometimes 0.1 ms) macro steps, plus browser 3-D viewers and CAD pipelines.
Source-quality legend: items marked **[PK]** are from the researcher's prior knowledge with the canonical page cited but **not re-fetched this session**; treat them as likely-correct but verify before relying on exact names/versions. Everything else was seen in a search result or fetched page during this session.

Repository context observed directly (local code, not web):
- `embeddyn/mechatronics/adapters/base.py` defines `SimulatorAdapter` with `prepare/start/step(time, dt, inputs)->StepResult/stop/checkpoint/restore/health/capabilities` — any new engine adapter must fit this interface.
- An `fmu.py` adapter already exists in `embeddyn/mechatronics/adapters/`, so FMU-exporting tools (Chrono, OpenModelica) can plug in without a bespoke adapter.
- The current Webots bridge (`examples/mechatronics/controllers/embeddyn_bridge/embeddyn_bridge.py`) **does not use Webots physics at all**: it integrates its own explicit-Euler single-inertia ODE (`acceleration = (torque - load - damping*velocity)/inertia`) in the controller and calls `robot.step(max(1, round(dt*1000)))` only to advance the clock. Outputs `mechanical.position/velocity/acceleration` come from that ODE, not from any Webots Motor/PositionSensor. The adapter `capabilities()` claims `checkpoint: False`, `mechanical_contacts: True` (the latter is not actually exercised). `round(dt*1000)` with `max(1, …)` means a 0.1 ms macro step silently becomes a 1 ms Webots step — time drift between embeddyn and Webots.

---

## 1. Engine-by-engine comparison (MuJoCo, Chrono, Drake, PyBullet, Gazebo, Webots, Isaac/Genesis/Newton, OpenModelica MultiBody, DART, Brax)

### Takeaway
MuJoCo is the best first integration for lock-step co-simulation: in-process Python, fixed-step deterministic `mj_step`, first-class full-state save/restore (`mj_getState`/`mj_setState`, plus `mj_extractState` since 3.5.0), headless, and since 3.7.0 a built-in `dcmotor` actuator with gearing/cogging/LuGre/thermal and documented backlash via dual-joint decomposition; it reached 4.0 in November 2026. Project Chrono is the strongest second choice when FMI export/import, gears/belts/motors as explicit mechanism elements, or checkpointing of vehicle-style systems matter (FMI 2.0 since 9.0, checkpointing since 10.0). Drake is excellent for correctness/contact but heavier; Gazebo and Webots are process-separated robot simulators best kept as optional "scene" backends.

### Cited Findings

**MuJoCo (Google DeepMind)**
- Current versions: 4.0.0 released 2026-11-06 (major: speculative contacts, elasto-plasticity, flex overhaul), 4.0.1 on 2026-11-11 (Studio web viewer enabled on Windows); 3.17.0 (2026-10-22) added `mujoco.batch` with step/forward/reset/set_const/rollout; 3.16.0 (2026-10-14) added "saving controls" and Bazel build — [MuJoCo changelog](https://mujoco.readthedocs.io/en/latest/changelog.html)
- 4.0.0 split the Python package into `mujoco-core`, `mujoco-studio`, and `mujoco`, and dropped Python 3.10 support (embeddyn must be on Python >=3.11 to track 4.x) — [MuJoCo changelog](https://mujoco.readthedocs.io/en/latest/changelog.html)
- 4.0.0 added `mjSTATE_PLASTIC` so plastic deformation can be saved/restored with the state API — [MuJoCo changelog](https://mujoco.readthedocs.io/en/latest/changelog.html)
- 4.0.0 behaviour change: actuators saturated by force limits no longer contribute implicit derivatives (may change integration results vs 3.x — matters for regression baselines) — [MuJoCo changelog](https://mujoco.readthedocs.io/en/latest/changelog.html)
- State API: `void mj_getState(const mjModel* m, const mjData* d, mjtNum* state, int sig)`, where `sig` bits select `mjtState` components — [MuJoCo API 3.6.0](https://mujoco.readthedocs.io/en/3.6.0/APIreference/APIfunctions.html)
- 3.5.0 (2026-02-12) added `mj_extractState` to take a subset of a state returned by `mj_getState` without writing it back into `mjData`; it errors if requested bits are not a subset of the source — [changelog 3.5.0](https://mujoco.readthedocs.io/en/3.5.0/_sources/changelog.rst.txt); [API 3.8.0](https://mujoco.readthedocs.io/en/3.8.0/APIreference/APIfunctions.html)
- `dcmotor` actuator added in 3.7.0: optional electrical dynamics (inductance), cogging torque, thermal resistance variation, LuGre friction — [changelog 3.7.0](https://mujoco.readthedocs.io/en/3.7.0/changelog.html)
- dcmotor technical note: net torque `clip(K(v−Kω)/R, ±K·imax) − Bω − τc·sgn(ω)`; with inductance L the current becomes a state (`v = L di/dt + iR + Kω`); gear ratio N reflects inertia `Jeff = Jr·N²`, gearbox efficiency η via `K' = ηK` (70–90 % typical); cogging `A·sin(Np·θ+φ)`; lumped thermal ODE with `R(T)=R0(1+α(T−T0))`; "MuJoCo supports backlash modeling via a dual-joint decomposition"; Coulomb friction already available as joint `frictionloss` — [MuJoCo dcmotor PDF](https://mujoco.readthedocs.io/en/latest/_static/dcmotor.pdf)
- All MuJoCo equality constraints are soft (virtual spring-damper), and backlash can be modelled with a low-impedance deadband region — [arXiv 2507.00273](https://arxiv.org/pdf/2507.00273)
- Batch-rollout / Warp: MuJoCo Warp (GPU) still requires `solver="CG"` for some flex gripper models (only MJX/Warp mention in the fetched changelog excerpt) — [MuJoCo changelog](https://mujoco.readthedocs.io/en/latest/changelog.html)
- License Apache-2.0; `pip install mujoco`; actuators: motor/position/velocity/general/dcmotor with `gear`, transmissions to joint/tendon/site; sensors: jointpos, jointvel, actuatorfrc, touch, force/torque, accelerometer, gyro, framepos etc.; `mj_step` is fixed-step, single-threaded and bit-reproducible on the same build/CPU **[PK]** — [MuJoCo docs](https://mujoco.readthedocs.io/en/stable/overview.html), [GitHub](https://github.com/google-deepmind/mujoco)

**Project Chrono (UW-Madison SBEL / Univ. of Parma)**
- Chrono 9.0.0 (Zenodo record 2024-05-20): FMI 2.0 co-simulation FMU **export and import**, direct ROS2 integration, URDF parsers; PyChrono conda packages via the `projectchrono` Anaconda channel, `release` label — [Chrono 9.0.0 announcement](https://groups.google.com/g/projectchrono/c/Zz7n22fSJh8/m/6mjqxSefBQAJ); [Zenodo](https://zenodo.org/records/11218911)
- Chrono::FMI exports only co-simulation FMUs (FMI limits on DAE models prevent model-exchange export); on Windows FMI build needs static runtime /MT — [module_fmi_installation](https://api.projectchrono.org/module_fmi_installation.html)
- fmu-forge library underpins Chrono::FMI; 2025 Multibody System Dynamics paper on Chrono FMI vehicle co-simulation and coupling external FMUs — [Springer 2025](https://link.springer.com/article/10.1007/s11044-025-10106-9); [preprint](https://sciety.org/articles/activity/10.21203/rs.3.rs-6565111/v1)
- Chrono 10.0.0 announced 2026-03-26: **checkpointing for Chrono and Chrono::Vehicle simulations**, new Jacobian update strategies for implicit integrators, refactored FSI (SPH + TDPF), new Peridynamics module, Sensor camera features, VSG visualisation plugin mechanism, modern-CMake build; PyChrono still via conda (no pip mentioned) — [Chrono 10.0.0 announcement](https://groups.google.com/g/projectchrono/c/OGb3bWbCjbc)
- BSD-3-Clause; rich mechanism library: `ChLinkMotorRotationTorque/Speed/Angle`, `ChLinkLockGear`, `ChShaft*` 1-D powertrain elements (shafts, gearboxes, clutches, `ChShaftsMotor`), belts/pulleys, bushings, springs/dampers, NSC (complementarity) and SMC (penalty) contact **[PK]** — [projectchrono.org](https://www.projectchrono.org/about/), [API docs](https://api.projectchrono.org/)

**Drake (Toyota Research Institute)**
- Release cadence ~monthly; v1.52.0 added beta Ubuntu 26.04 support and pickling for several pydrake classes plus new MultibodyPlant joint bindings; v1.56.0 bound `MultibodyPlant.AddTendonConstraint` and a ModelVisualizer render-engine option; Docker images deprecated/no longer published after 2026-06-01 — [v1.52.0](https://drake.mit.edu/release_notes/v1.52.0.html), [v1.56.0](https://drake.mit.edu/release_notes/v1.56.0.html), [v1.53.0](https://drake.mit.edu/release_notes/v1.53.0.html)
- No FMU import/export found in any Drake release notes searched — [Drake release notes index](https://drake.mit.edu/release_notes/v1.55.0.html) (gap: absence of evidence)
- BSD-3-Clause; `pip install drake`; Systems framework with `Simulator.AdvanceTo(t)` (external stepping), `Context` cloneable/`SetTimeStateAndParametersFrom` (cheap checkpoint), discrete MultibodyPlant (`time_step`) with SAP/TAMSI hydroelastic or point contact, `JointActuator` with gear ratio and rotor inertia (reflected inertia), MeshCat built-in browser visualiser **[PK]** — [drake.mit.edu](https://drake.mit.edu/), [MultibodyPlant docs](https://drake.mit.edu/doxygen_cxx/classdrake_1_1multibody_1_1_multibody_plant.html)

**PyBullet**
- Last official release 3.2.7 on 2025-01-30; Snyk rates it "Inactive"; an RL guide says it has not been well maintained since 2023; community `pybullet-arm64` fork had a 3.2.8 release in March 2026 — [Snyk](https://snyk.io/advisor/python/pybullet), [RL guide news](https://kushal-a.github.io/robot_RL_guide/news.html), [pybullet-arm64](https://pypi.org/project/pybullet-arm64/)
- zlib license; `saveState/restoreState` in-memory, `stepSimulation`, `setTimeStep`, `DIRECT` headless mode **[PK]** — [bullet3](https://github.com/bulletphysics/bullet3)

**Gazebo (gz-sim, Open Robotics)**
- Jetty (gz-sim 10) is the current recommended release on Ubuntu 26.04/macOS; new `gz-sim10-server` package (10.1.1-2) provides a headless server binary at `/usr/libexec/gz/sim10/gz-sim-server` — [Jetty server-only install](https://discourse.openrobotics.org/t/gazebo-jetty-server-only-installation-for-ubuntu/53375)
- Python API: `TestFixture` loads a world, steps simulation, registers pre/post-update callbacks; `server.run(blocking, iterations, paused)` runs N steps; Python systems via `PythonSystemLoader` — [gz-sim 9 Python interfaces](https://gazebosim.org/api/sim/9/python_interfaces.html), [gz-sim 7](https://gazebosim.org/api/sim/7/python_interfaces.html)
- gz-transport Python bindings `python3-gz-transport15` available via apt for Jetty — [Jetty gz-transport guide](https://robobe.github.io/new_blog/Simulation/gazebo_jetty/gz_transport)
- Apache-2.0; physics via gz-physics plugins (DART default, Bullet-featherstone); no built-in full-state snapshot API comparable to MuJoCo **[PK]** — [gazebosim.org/libs/sim](https://gazebosim.org/libs/sim/)

**Webots (Cyberbotics)**
- `Robot.synchronization` defaults TRUE: the simulator waits for the controller at each `wb_robot_step`; with basicTimeStep 16 ms and controller step 64 ms Webots executes exactly 4 physics steps then waits; FALSE = runs as fast as possible using latest commands — [Webots reference robot.md](https://github.com/omichel/webots-doc/blob/master/reference/robot.md)
- `<extern>` controller field lets an external process attach to the robot (used e.g. by Simulink bridges) — [webots-simulink connecting](https://harunkurtdev.github.io/webots-simulink/usage/connecting)
- MEbots (2025) coupled a RISC-V virtual platform to Webots with a connector handling data exchange and time sync; notes robot simulators use fixed-step physics with ms-scale granularity — [arXiv 2505.16682](https://arxiv.org/pdf/2505.16682)
- No evidence found of native Webots FMU import/export — (gap; search returned no Cyberbotics FMU material)
- Apache-2.0 (since R2019a/R2020b), ODE physics fork, Supervisor API (`simulationReset`, `simulationResetPhysics`, field get/set, `getFromDef`), `Motor` with torque/position/velocity control, `PositionSensor`, `basicTimeStep` is a float ms in WorldInfo but `robot.step()` takes an integer ms duration; no full in-memory state snapshot **[PK]** — [cyberbotics.com/doc](https://cyberbotics.com/doc/reference/supervisor)

**Newer GPU engines (Isaac Sim, Newton, Genesis)**
- Newton: announced GTC March 2025 by Disney Research, Google DeepMind, NVIDIA; contributed to Linux Foundation 2025-09-29; Apache-2.0; TRI (Drake) partnering on solvers/contact — [Linux Foundation press](https://www.linuxfoundation.org/press/linux-foundation-announces-contribution-of-newton-by-disney-research-google-deepmind-and-nvidia-to-accelerate-open-robot-learning); [control.com](https://control.com/news/newton-project-advances-open-robotics-simulation/)
- Newton 1.0 GA announced at GTC 2026; a secondary (Russian) source says it is integrated in Isaac Lab 3.0 — [control.com](https://control.com/news/newton-project-advances-open-robotics-simulation/); [habr](https://habr.com/ru/post/1011560) (secondary, unverified)
- Genesis (now "Genesis World"): Apache-2.0, unified rigid/FEM/MPM/PBD/SPH; claims 10–80× faster than Isaac Sim/MuJoCo "with important caveats about scene complexity"; PyPI 1.1.1 is the newest seen — [Genesis README](https://raw.githubusercontent.com/Genesis-Embodied-AI/Genesis/main/README.md); [PyPI 1.1.1](https://pypi.org/project/genesis-world/1.1.1/); [Linux Journal](https://www.linuxjournal.com/content/linux-foundation-welcomes-newton-next-open-physics-engine-robotics)
- Isaac Sim requires NVIDIA RTX GPU; deterministic replay is a dedicated FAQ topic on NVIDIA's site (PhysX GPU determinism is conditional) — [NVIDIA Isaac Sim determinism FAQ](https://perspectives.nvidia.com/isaac-sim/task/faq/deterministic-replay-mechanisms-reproducible-benchmarks-1/)

**Benchmarks**
- Classic Erez/Tassa/Todorov ICRA 2015 comparison of Bullet, Havok, MuJoCo, ODE, PhysX (MuJoCo fastest/most accurate on robotics tasks; dated) — [ErezICRA15](https://homes.cs.washington.edu/%7Etodorov/papers/ErezICRA15.pdf)
- Chung & Pollard 2016 found a trade-off between accuracy and predictability across engines — [CMU 2016](https://www.ri.cmu.edu/app/uploads/2017/12/ChungCAVW2016.pdf)
- arXiv 2103.04616 is a survey of simulation tools for robotics — [arXiv 2103.04616](https://arxiv.org/pdf/2103.04616.pdf)

### Inferences
Dense comparison matrix (ratings are the researcher's synthesis of the findings above; [PK] items need verification):

| Engine | License | Activity (2026) | Python API | External stepping / determinism | Save/restore | Contact/friction | Motors/gears/backlash | Sensors | Headless/CI | Viz | Lock-step fit |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **MuJoCo 4.0** | Apache-2.0 | Very high (4.0.1 Nov 2026) | Excellent, in-process, numpy views, pip wheels | `mj_step` N substeps per macro step; deterministic same build/CPU | **Native** `mj_getState/setState/extractState`, `mj_copyData` | Soft convex contacts, pyramidal/elliptic cones, torsional/rolling friction; 4.0 speculative contacts | `gear`, `dcmotor` (L, cogging, LuGre, thermal), tendons, equality coupling for gear trains, backlash via dual joint | Rich built-in | Yes (EGL/OSMesa for offscreen) | Native viewer, Studio web client (4.0.1), mujoco_wasm, Rerun loader | **Best** |
| **Chrono 10** | BSD-3 | High (10.0 Mar 2026) | PyChrono via conda only (SWIG) | `DoStepDynamics(dt)`; deterministic single-thread | **Checkpointing new in 10.0** | NSC + SMC, good granular/vehicle | Strongest explicit mechanism library (ChShaft gearbox/clutch, gears, belts, rotational motors) [PK] | Chrono::Sensor (camera, lidar, IMU, GPS) | Yes | Irrlicht/VSG, Blender export [PK] | Very good; **FMI 2.0 export/import** |
| **Drake** | BSD-3 | High (monthly) | Excellent pydrake, pip | `Simulator.AdvanceTo`; deterministic discrete plant | Clone Context [PK] | Best-in-class hydroelastic / SAP [PK] | JointActuator gear ratio + rotor inertia [PK]; no built-in backlash | Fewer turnkey sensors | Yes | **MeshCat** (browser) built-in | Good, heavier learning curve |
| **PyBullet** | zlib | Low (inactive) | Simple | `stepSimulation` | `saveState/restoreState` | OK, less accurate | Basic motors | Basic | DIRECT mode | GUI/OpenGL | Works but legacy risk |
| **Gazebo Jetty** | Apache-2.0 | High | gz-sim Python TestFixture + gz-transport | `server.run(True,N,False)` stepping; separate process | No native snapshot [PK] | Via DART/Bullet | Joint controllers via plugins | Very rich (ROS-world) | gz-sim10-server headless | gz GUI, web (gzweb) [PK] | Medium (process + transport overhead) |
| **Webots** | Apache-2.0 | Medium | controller API in-sim; extern controllers | `synchronization TRUE` → lock-step; int-ms `robot.step` | Supervisor reset only, no snapshot [PK] | ODE | Motor, gears via PROTO only | Rich | `--batch --no-rendering --mode=fast` | 3-D GUI, web streaming | Medium–low for 0.1 ms |
| **Newton / Genesis / Isaac** | Apache-2.0 / Apache-2.0 / mixed (Isaac Sim NVIDIA license) | Very high | Python, GPU-first | Batched GPU stepping; determinism conditional | Varies | Good (Newton uses MuJoCo-Warp style solvers) | RL-oriented actuators | Varies | Needs GPU (Isaac: RTX) | Own viewers | Poor fit for single-scene 1 ms lock-step |
| **OpenModelica MultiBody** | OSMC-PL/BSD-ish (MSL BSD-3) [PK] | High | OMPython; export FMU | Via FMU `doStep` | FMU `getFMUstate` if supported | No general contact | Excellent 1-D Rotational library: IdealGear, ElastoBacklash, BearingFriction, motors [PK] | — | Yes | Animation via OMEdit / FMU | Very good for drivetrains via existing fmu adapter |
| **DART** | BSD-2 | Low–medium (mostly as Gazebo backend) [PK] | dartpy | `step()` | `getState/setState`-like via skeleton [PK] | OK | Basic | Few | Yes | OSG | Fair |
| **Brax** | Apache-2.0 | Low (superseded by MJX/MuJoCo Warp) [PK] | JAX | Batched | Pure-functional state (trivially) | Simplified | Minimal | Minimal | Yes | HTML renderer | Poor for embedded co-sim |

- Electrical/mechanical split: embeddyn already models motor electrics in ngspice. For co-simulated runs, use MuJoCo `motor` (pure torque) actuators with `gear` and let ngspice own winding current/back-EMF (feed ω back as a voltage source); use `dcmotor` only when running mechanics stand-alone, otherwise the winding dynamics are double-counted.
- At 0.1 ms macro steps, MuJoCo with `timestep=1e-4` (or 1e-3 with N substeps) is cheap for small robots; Python-call overhead per step (~µs) is negligible versus Renode.
- OpenModelica's Rotational library (gears with backlash, elasto-backlash, friction) exported as an FMU is the most physically faithful drivetrain option and needs essentially no new embeddyn code because `fmu.py` exists.

### Gaps
- Exact MuJoCo 4.0 Python API name for batched state (`mujoco.batch`) semantics and whether `mjSTATE_INTEGRATION` covers warm-start for bit-exact restore — not read in full changelog (only first 100 k chars fetched).
- Chrono 10 FMI version (still FMI 2.0 or FMI 3.0?) and whether PyChrono exposes checkpointing — not stated in the announcement.
- Drake FMU support: none found; not confirmed absent from docs.
- No 2024–2026 head-to-head benchmark on determinism/accuracy across MuJoCo/Chrono/Drake found.
- DART and Brax 2026 activity, OpenModelica license specifics, Webots latest release number — not verified this session.

---

## 2. Which tools provide FMI export/import or are used in FMI co-simulation

### Takeaway
Chrono (FMI 2.0 CS export + import since 9.0) and OpenModelica (Modelica MultiBody/Rotational → FMU export) are the only mechanics candidates with first-class FMI; MuJoCo, Drake, Gazebo and Webots have no native FMI, so embeddyn should keep native Python adapters for them and use its existing FMU adapter for Chrono/OpenModelica models.

### Cited Findings
- Chrono::FMI: export co-simulation FMUs and import FMUs for co-simulation with Chrono models (FMI 2.0) — [Chrono 9.0.0](https://groups.google.com/g/projectchrono/c/Zz7n22fSJh8/m/6mjqxSefBQAJ); [module_fmi_installation](https://api.projectchrono.org/module_fmi_installation.html); [chrono::fmi namespace](https://api.projectchrono.org/namespacechrono_1_1fmi.html)
- Chrono FMI used for vehicle co-simulation in a 2025 peer-reviewed paper (fmu-forge) — [Springer 2025](https://link.springer.com/article/10.1007/s11044-025-10106-9)
- FMI 3 adds clocks and clocked variables for hybrid co-simulation; a 2026 robotics co-simulation paper discusses it — [Gil 2026 PDF](https://clagms.github.io/assets/pdfs/Gil2026.pdf)
- Python FMI masters exist (e.g. cofmupy, syssimx) that could serve as reference orchestration — [cofmupy](https://pypi.org/project/cofmupy/), [syssimx](https://pypi.org/project/syssimx/0.1.7/)
- In FMI co-simulation a master algorithm calls `fmiDoStep` on each slave and handles data exchange — [Berkeley EECS-2013-153](https://www2.eecs.berkeley.edu/Pubs/TechRpts/2013/EECS-2013-153.pdf)
- Webots: no FMU material found; Webots–Simulink coupling is done via extern controllers instead — [webots-simulink](https://harunkurtdev.github.io/webots-simulink/usage/connecting)
- OpenModelica exports FMI 2.0 (and FMI 3.0 CS in recent versions) FMUs including Modelica.Mechanics.MultiBody/Rotational models **[PK]** — [OpenModelica FMI docs](https://openmodelica.org/doc/OpenModelicaUsersGuide/latest/fmitlm.html)

### Inferences
- A MuJoCo model could be wrapped into an FMU with a Python-FMU tool (e.g. pythonfmu) if external FMI tools need it, but for embeddyn itself the native adapter is faster and keeps `mj_getState` checkpointing; FMI `fmi2GetFMUstate` support in Chrono FMUs is unverified.

### Gaps
- Whether Chrono-exported FMUs implement `canGetAndSetFMUstate` (needed for embeddyn checkpoint/restore through the FMU adapter).
- Whether Chrono 10 moved to FMI 3.0.

---

## 3. Browser 3-D viewers and CAD import

### Takeaway
For the Lab GUI, the lowest-effort high-value path is three.js with a URDF loader (NASA JPL `urdf-loaders`) or `mujoco_wasm` for MJCF, fed by poses streamed from the orchestrator over the existing GUI channel; MeshCat (three.js over WebSockets, built into Drake) and Rerun (time-series + 3-D log viewer with built-in URDF and community MJCF loaders) are good as external debugging/replay viewers; Foxglove is ROS-centric. For CAD, onshape-to-robot (URDF + MJCF) and Onshape's own URDF exporter (March 2026) are the mature pipelines.

### Cited Findings
- MeshCat: remotely-controllable 3-D viewer built on three.js; browser listens for geometry commands over WebSockets — [meshcat GitHub](https://github.com/rdeits/meshcat); [meshcat-dev](https://relatedrepos.com/gh/meshcat-dev/meshcat)
- Foxglove 3D panel displays URDF; via foxglove bridge ≥0.7 it fetches `package://` meshes over the Foxglove WebSocket; Foxglove uses three.js — [Foxglove URDF blog](https://foxglove.dev/blog/adding-urdfs-to-foxglove-studios-3d-panel); [three.js forum](https://discourse.threejs.org/t/foxglove-uses-three-js-to-optimize-rendering-performance-for-their-3d-data-visualizer/40560)
- NASA JPL URDF loader for three.js and Unity, with drag-and-drop web viewer — [ROS Discourse announcement](https://discourse.openrobotics.org/t/web-based-urdf-visualization-tool-and-library-opensourced-from-nasa-jpl/14404)
- mechaverse: three.js web viewer for URDF, SDF, MJCF, OpenUSD using mujoco_wasm for in-browser physics (young, small project) — [awesome.ecosyste.ms entry](https://awesome.ecosyste.ms/projects/github.com%2Fjurmy24%2Fmechaverse)
- TorchRL ships a `mujoco_wasm` render backend (evidence that MuJoCo WASM is used for browser rendering) — [TorchRL mujoco_wasm](https://docs.pytorch.org/rl/main/%5Fmodules/torchrl/render/mujoco_wasm.html)
- MuJoCo 4.0.1 ships a Studio web client in a platform-independent wheel — [MuJoCo changelog](https://mujoco.readthedocs.io/en/latest/changelog.html)
- Rerun: built-in URDF data-loader (`rec.log_file_from_path(urdf_path, static=True)`; loader runs in the SDK process), `log_urdf_to_recording` with `frame_prefix`; community `rerun-loader-mjcf` (0.4.0 adds CLI, `--simulate`, `MJCFRecorder`; 0.2.0 `MJCFLogger.log_model()/log_data(data)`); Rerun 0.36.0 dated 2026-09-09 — [Rerun URDF how-to](https://rerun.io/docs/howto/urdf); [rerun-loader-mjcf 0.4.0](https://pypi.org/project/rerun-loader-mjcf/0.4.0/); [rerun-loader-mjcf 0.2.0](https://pypi.org/project/rerun-loader-mjcf/0.2.0/); [radiancefields 0.36.0](https://radiancefields.com/rerun-adds-an-experimental-gaussian-splat-archetype-and-ewa-renderer-in-0.36.0)
- sim-web-visualizer streams simulator scenes into a browser — [sim-web-visualizer](https://github.com/0iui0/sim-web-visualizer)
- onshape-to-robot (Rhoban) exports Onshape assemblies to URDF or MuJoCo (MJCF); topology implied by mate ordering — [docs](https://onshape-to-robot.readthedocs.io/en/latest/); [GitHub](https://www.github.com/Rhoban/onshape-to-robot)
- onshape-mjcf builds the kinematic tree from CAD graph analysis (only a root needs specifying) — [onshape_mjcf](https://awesome.ecosyste.ms/projects/github.com%2Fhansihe%2Fonshape_mjcf)
- onshnap: frozen-snapshot (no kinematic chain) URDF/MJCF exporter — [onshnap PyPI](https://pypi.org/project/onshnap/)
- Onshape March 13 2026 release announced a built-in URDF exporter — [Onshape forum](https://forum.onshape.com/discussion/30410/improvements-to-onshape-march-13-2026)
- Recent forum threads on fully automated CAD→URDF with Gazebo verification — [ROS Discourse](https://discourse.openrobotics.org/t/fully-automated-cad-to-urdf-with-automatic-gazebo-verification/56857.md); [CAD to URDF](https://discourse.openrobotics.org/t/cad-to-urdf-conversion/55892.md)
- Licenses: three.js MIT, Babylon.js Apache-2.0, MeshCat MIT, Rerun MIT/Apache-2.0 dual, Foxglove app is now proprietary (open-source Lichtblick fork exists), FreeCAD LGPL with CROSS/RobotCAD workbenches for URDF export, MuJoCo can import URDF directly and `mj_saveLastXML` emits MJCF **[PK]** — [three.js](https://github.com/mrdoob/three.js), [Babylon.js](https://github.com/BabylonJS/Babylon.js), [Rerun](https://github.com/rerun-io/rerun), [Lichtblick](https://github.com/Lichtblick-Suite/lichtblick)

### Inferences
- Architecture suggestion: the orchestrator already owns time; publish per-macro-step (or decimated, e.g. 60 Hz wall-clock) body poses `{body: [x,y,z,qw,qx,qy,qz]}` to the Lab GUI; GUI loads the URDF/MJCF meshes once (urdf-loaders or MJCF→glTF conversion) and only updates transforms. This decouples rendering from physics and keeps headless CI unaffected.
- Rerun is attractive for recording/replay of whole co-sim runs (signals + 3-D on one timeline), complementing embeddyn's `replay.py` adapter.
- Avoid Foxglove for new work because of its license change [PK]; use Lichtblick if a Foxglove-style UI is wanted.

### Gaps
- FreeCAD CROSS/RobotCAD current status not found in this session's searches.
- Whether MuJoCo Studio web client can attach to an externally stepped `mjData` from Python (would give a free browser viewer) — not verified.

---

## 4. Fixing or replacing the Webots bridge

### Takeaway
The current bridge is a clock-only shim: physics comes from a hand-written Euler ODE inside the controller, so Webots adds latency without mechanics. Fix it by driving real Webots `Motor`/`PositionSensor` devices with `synchronization TRUE` and `basicTimeStep` equal to (a divisor of) the macro step, or—better—demote Webots to an optional visual/scene backend and make MuJoCo the default mechanical adapter.

### Cited Findings
- With `synchronization TRUE` Webots blocks at each controller `wb_robot_step` and executes exactly `controlStep/basicTimeStep` physics steps — [Webots robot.md](https://github.com/omichel/webots-doc/blob/master/reference/robot.md)
- `<extern>` controllers let a process outside Webots act as the controller — [webots-simulink connecting](https://harunkurtdev.github.io/webots-simulink/usage/connecting)
- Lock-step is crucial when coupling a virtual platform to Webots; MEbots did it with a dedicated connector — [arXiv 2505.16682](https://arxiv.org/pdf/2505.16682)
- Local code: bridge computes `acceleration = (torque - load - damping*velocity)/inertia` itself and steps `robot.step(max(1, round(dt*1000)))` (see repository context above).

### Inferences
Concrete bridge fixes (if Webots is kept):
1. Replace the internal ODE with device I/O: `motor = robot.getDevice(name)`, `motor.setPosition(float('inf'))`, `motor.setTorque(τ)` (or `setForce`), read `PositionSensor.getValue()` and compute/obtain velocity via `motor.getVelocity()` [PK]; map port names from a JSON config instead of hard-coded `mechanical.*` keys.
2. Set `WorldInfo.basicTimeStep` to the macro step (1 ms; Webots cannot do 0.1 ms via integer-ms `robot.step`), assert `dt*1000` is an integer multiple of basicTimeStep and raise instead of rounding.
3. Run the bridge as an `<extern>` controller launched by the adapter (removes the in-world TCP server/port race; Webots' `WEBOTS_CONTROLLER_URL` env selects the robot [PK]), keep `--batch --no-rendering --mode=fast` for CI.
4. Use a binary/struct or msgpack framing instead of JSON-lines if per-step overhead matters.
5. Keep `checkpoint: False` (Webots has no in-memory snapshot) and set `mechanical_contacts` honestly; offer `simulationReset()` only for "restart from t=0".
Replacement: implement `MujocoAdapter(SimulatorAdapter)` in-process — `start()` loads MJCF/URDF, sets `opt.timestep`, maps embeddyn signal names to actuator `ctrl` indices and sensor addresses; `step()` writes `data.ctrl`, calls `mj_step` `round(dt/timestep)` times, returns `sensordata` slices; `checkpoint()` returns `mj_getState(..., mjSTATE_INTEGRATION)` array + time; `restore()` calls `mj_setState` + `mj_forward`; `capabilities()` → deterministic_step, checkpoint, mechanical_contacts all True.

### Gaps
- Exact current Webots release number/API changes in 2025–2026 not verified; whether Webots added any state-snapshot feature recently is unknown.

---

## 5. Recommendation: which engine first for motors, gearboxes, mechanisms and small robots

### Takeaway
Integrate **MuJoCo (>=4.0, Python bindings, in-process)** first as the default mechanical domain; add **OpenModelica-exported FMUs** (Rotational/MultiBody: gears with ElastoBacklash, bearing friction) through the existing FMU adapter for high-fidelity drivetrains; consider **PyChrono** later for complex mechanisms/vehicles needing explicit gear/belt/clutch elements or FMI interoperability; keep Webots/Gazebo as optional scene backends; use three.js (+urdf-loaders / mujoco_wasm) in the Lab GUI and Rerun for recorded runs.

### Cited Findings
- MuJoCo provides native full-state save/restore (`mj_getState`/`mj_extractState`) — [API 3.8.0](https://mujoco.readthedocs.io/en/3.8.0/APIreference/APIfunctions.html)
- MuJoCo `dcmotor` covers gearing efficiency, reflected inertia, cogging, LuGre, thermal; backlash via dual-joint decomposition — [dcmotor PDF](https://mujoco.readthedocs.io/en/latest/_static/dcmotor.pdf); [changelog 3.7.0](https://mujoco.readthedocs.io/en/3.7.0/changelog.html)
- MuJoCo 4.0 is the current major release (Nov 2026), very active — [changelog](https://mujoco.readthedocs.io/en/latest/changelog.html)
- Chrono offers FMI 2.0 export/import and (10.0) checkpointing — [9.0.0](https://groups.google.com/g/projectchrono/c/Zz7n22fSJh8/m/6mjqxSefBQAJ); [10.0.0](https://groups.google.com/g/projectchrono/c/OGb3bWbCjbc)
- onshape-to-robot exports MJCF directly, giving a CAD→MuJoCo path — [onshape-to-robot docs](https://onshape-to-robot.readthedocs.io/en/latest/)

### Inferences
Suggested integration plan:
1. **MujocoAdapter** (as sketched in §4) with a signal-map file: `actuators: {mechanical.motor_torque: {actuator: m1}}`, `sensors: {mechanical.position: {sensor: q1}, mechanical.velocity: {sensor: qd1}}`. Use `motor` actuators with `gear` when ngspice models the motor electrics; feed back joint velocity × gear to the spice back-EMF source. Golden test: replicate the current single-inertia example (hinge joint, `armature`=inertia, `damping`) and compare against the existing python_model plant.
2. **Determinism test in CI**: run N steps, checkpoint, run M, restore, rerun M, assert bitwise-equal `qpos/qvel` — the orchestrator's rollback/what-if features depend on it.
3. **Gearbox/backlash library**: provide MJCF templates — gear train via joint equality (`<equality><joint polycoef="0 N …"/>`), backlash via motor-side + output-side joints joined by a stiff deadband (soft joint limit/ tendon) per the dual-joint pattern, compliant joint via `stiffness`/`damping`, servo horn + linkage examples for the RC-servo lab.
4. **Visualisation**: stream body poses (`data.xpos`, `data.xquat`) to the Lab GUI at a decimated rate; load meshes in three.js; optionally a "Open in Rerun" export using rerun-loader-mjcf.
5. **FMU drivetrains**: document an OpenModelica workflow (Rotational.IdealGear + ElastoBacklash + DC motor) exported as CS FMU into the existing fmu adapter, for users wanting Simscape-like 1-D accuracy.
6. **Webots**: apply the bridge fixes in §4 or mark experimental.

### Gaps
- No measured per-step overhead of MuJoCo Python bindings at 0.1 ms with Renode in the loop; should be benchmarked locally.
- Verify that the installed embeddyn test venv Python version is >=3.11 before targeting MuJoCo 4.x (4.0 dropped 3.10).
