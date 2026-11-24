# Model-exchange and system-modelling tools for embeddyn (FMI, SSP, DCP, Modelica, acausal)

Research date: 2026-11-23. Repo activity and license data come from the GitHub REST API, queried 2026-11-23 (`pushed_at`, `license.spdx_id`, `releases/latest`). The API rate limit cut off the queries partway through, so some repos (OMSimulator, OMPython, Ecos, FMIL, FMI4j, ModelingToolkit, FMI.jl, CasADi) have no API metadata and are covered by docs or search only.

Context. embeddyn already has its own ctypes FMI 2.0/3.0 CS importer (no fmpy) and RK4 Python plants. Its gaps are: FMU state save/restore in checkpoints, exporting embeddyn models/systems as FMUs, SSP import/export, acausal physical modelling, algebraic loops, and distributed co-simulation.

## Q1: OpenModelica (OMC, OMPython, OMSimulator + Python/SSP, FMU export), MSL and other open Modelica libraries

### Takeaway
OpenModelica is the only mature fully open-source Modelica toolchain. Its FMU export (ME+CS, FMI 2.0 solid, FMI 3.0 still "experimental") is the practical route for getting MSL-based motor, gearbox and thermal plants into embeddyn as FMUs. OMSimulator 3.x was rewritten around a pip-installable Python package with SSP import/export and mixed FMI2/FMI3 support. That makes it the strongest open SSP candidate, but its OSMC-PL license and newness need care.

### Cited Findings
- OpenModelica releases: 1.26.0 published 2025-12-18; 1.26.2 on 2026-02-11; 1.26.8 on 2026-06-05; a 1.26.9 page also exists — [OM 1.26.0](https://openmodelica.org/news/openmodelica-1.26.0/), [1.26.2](https://openmodelica.org/news/openmodelica-1.26.2/), [1.26.8](https://openmodelica.org/news/openmodelica-1.26.8/), [1.26.9](https://openmodelica.org/news/openmodelica-1.26.9/). Wikipedia lists 1.27.1 (2026-09-08) as the current stable release (secondary source, not verified on openmodelica.org) — [Wikipedia](https://en.wikipedia.org/wiki/OpenModelica).
- OpenModelica FMU export: FMI 2.0 ME and CS, with "experimental support for FMI 3.0". By default it generates one FMU that serves both ME and CS. CS FMUs use SUNDIALS CVODE by default, with forward Euler as the alternative. Export runs through `buildModelFMU()` or OMEdit. FMI 1.0 export/import is deprecated and will be removed after OM 1.28.0 — [OM User's Guide, FMI chapter](https://openmodelica.org/doc/OpenModelicaUsersGuide/latest/fmitlm.html).
- OMSimulator license: OSMC Public License (README badge "OSMC-PL") — [OMSimulator README](https://github.com/OpenModelica/OMSimulator).
- OMSimulator also ships OMSimulatorGui, a standalone PySide6 GUI for editing and simulating SSP models. It is built directly on the Python package with no `omc` dependency — [OMSimulator README](https://github.com/OpenModelica/OMSimulator).
- OMSimulator FMI-export coverage tests run against public libraries, including an MSL 4.0.0 report. The README notes that failures "may be attributed to OMSimulator itself or the FMI export in OpenModelica" — [OMSimulator README](https://github.com/OpenModelica/OMSimulator), [MSL4 coverage report](https://libraries.openmodelica.org/branches/master-fmi/Modelica_4.0.0/Modelica_4.0.0.html).
- OMSimulator 3.x architecture: the C API was split into a Python layer (SSP handling) and a C layer (simulation engine). The stated goals were modularity, better Python integration, separating the simulation core from the SSP standards, and importing SSP from other vendors. Status (Feb 2026): "All the API are implemented in new Python package", tests ported, "Available via pip", "Support for SSP from other tools", "FMI 3.0 CS&ME support is implemented", mixed FMI2/FMI3 connections work. TODO: missing SSP concepts and OMEdit integration — [Ochel & Palanisamy, OpenModelica Workshop 2026 slides](https://openmodelica.org/images/M_images/OpenModelicaWorkshop_2026/Lennart_Arun_OMSimulator_talk_OM_2026.pdf).
- Python API (new): `from OMSimulator import FMU, SSP, CRef, Settings, SSV`. `FMU('x.fmu')` exposes modelName, guid, fmiVersion and states, plus instantiate/setValue/initialize/simulate. With `SSP()` you call addResource, addComponent and addConnection, then `export('x.ssp')`. `SSP('x.ssp').instantiate()` followed by initialize/simulate runs the system. SSV parameter files are supported — [Workshop 2026 slides](https://openmodelica.org/images/M_images/OpenModelicaWorkshop_2026/Lennart_Arun_OMSimulator_talk_OM_2026.pdf); [OMSimulatorPython3 docs](https://openmodelica.org/doc/OMSimulator/master/OMSimulator/UsersGuide/html/OMSimulatorPython3.html).
- The older 2.x Python API (`OMSimulator()` object style, `pip3 install OMSimulator`) still exists. The 2.1 maintenance line reached v2.1.3. The 2.1.2 release (Feb 2025) focused on co-simulation, Python API fixes and SSV/SSM support. The master user guide is labelled v3.0.0.post127 (March 2026) — [OMSimulatorPython (2.x)](https://openmodelica.org/doc/OMSimulator/master/OMSimulator/UsersGuide/html/OMSimulatorPython.html), [2025 workshop slides](https://openmodelica.org/images/M_images/OpenModelicaWorkshop_2025/2025-02-03_OMSimulator.pdf), [master PDF](https://openmodelica.org/doc/OMSimulator/master/OMSimulator/UsersGuide/pdf/OMSimulator.pdf), [gitee mirror](https://gitee.com/openGPL/OMSimulator).
- OMPython license: the project README says OSMC Public Runtime License (OSMC-PL-RT). Snyk lists BSD-2-Clause for v3.6.0, which is likely stale. The sources conflict — [OMPython README mirror](https://github.com/syntron/OMPython), [Snyk](https://snyk.io/advisor/python/ompython).
- Licensing of OM-generated executables and code has been discussed on the OpenModelica forum (generated runtime code is covered by the runtime license) — [OM forum thread 3442](https://openmodelica.org/forum/default-topic/3442-license-for-a-tool-that-includes-an-exe-file-generated-by-om.html).
- The Modelica Standard Library repo is BSD-3-Clause and active (pushed 2026-11-20, 634 stars) — [GitHub API: modelica/ModelicaStandardLibrary](https://github.com/modelica/ModelicaStandardLibrary).
- Modelica PowerSystems library: transient and steady-state power systems at several levels of detail, under Modelica License 2.0. Wolfram lists v2.0.0, depending on MSL 4.0.0 — [PowerSystems docs](https://build.openmodelica.org/Documentation/PowerSystems.html), [Wolfram listing](https://www.wolfram.com/system-modeler/libraries/power-systems).
- Other open power libraries: OpenIPSL (MPL-2.0; a fork mentions BSD-3, so the licenses conflict between forks), OpenIMDML for induction motors (AGPL), ModPowerSystems (phasor/EMT, Modelica License 2), PowerSysPro (EDF, Modelica License 2) — [OpenIPSL](https://github.com/openipsl/openipsl), [OpenIMDML paper](https://sites.ecse.rpi.edu/~vanfrl/documents/publications/journal/J092_FF_OpenIMDML.pdf), [ModPowerSystems](https://build.openmodelica.org/Documentation/ModPowerSystems.html), [PowerSysPro](https://build.openmodelica.org/Documentation/PowerSysPro.html).

### Inferences
- For embeddyn's acausal plant gap (motors, gearboxes, thermal, hydraulics), the cheapest path is to author plants in Modelica on top of MSL, then export CS FMUs with OpenModelica (FMI 2.0 is the safest) and load them through embeddyn's existing ctypes importer. That needs no new runtime dependency in embeddyn, only an offline OMC toolchain. The relevant MSL packages are Electrical.Machines (DC/induction/synchronous machines), Electrical.PowerConverters, Mechanics.Rotational (gears and friction), Mechanics.MultiBody, Thermal.HeatTransfer and Thermal.FluidHeatFlow, and Fluid. These names come from general MSL knowledge and were not verified in this session.
- Hydraulics: MSL Modelica.Fluid exists, but proper oil-hydraulics libraries such as HyLib are commercial. Open hydraulics coverage is thin (not verified this session).
- Treat FMI 3.0 FMUs exported by OM as experimental. Validate every exported FMU with FMPy and fmusim before relying on it (see Q2).
- OMSimulator's new Python package is the best fit for SSP import/export. Under OSMC-PL, embeddyn should depend on it as an optional extra and not vendor its code. Since 3.x is very new (2026), pin versions.
- OMPython is useful only for scripting OMC (load a .mo file, then buildModelFMU) in a build step. embeddyn does not need it at runtime.

### Gaps
- I could not read OpenModelica 1.26 or 1.27 release notes for FMU-export changes (FMI 3.0 maturity, clocks, GetFMUState in exported FMUs). My unverified prior knowledge is that OM-exported FMI 2.0 FMUs advertise canGetAndSetFMUstate only partially. Verify by reading modelDescription.xml of a test export.
- I did not verify OMSimulator's algebraic-loop handling in 3.x. Older 2.x documentation describes algebraic-loop solving in its "SC"/"WC" system types, but I did not confirm this in this session.
- The exact OSMC-PL terms (GPL3 vs. OSMC-PL modes for runtime and library use) were not fetched.
- No dedicated open-source "ElectroMechanical" or power-electronics/motor-drive Modelica library (beyond MSL PowerConverters) turned up. Modelica Buildings (BSD-3, building HVAC) was not researched because it is of low relevance.
- I did not find a 2024–2026 Modelica conference paper specifically benchmarking OM FMU export quality. The 2025 proceedings are at [ecp.ep.liu.se issue 105](https://ecp.ep.liu.se/index.php/modelica/issue/view/105) (mentioned in the 2026 workshop slides), but I did not mine them.

## Q2: FMPy, validation and compliance, Reference-FMUs, FMI Cross-Check, PythonFMU / pythonfmu3, UniFMU, fmi4j, FMIL, FMU export from Python

### Takeaway
FMPy (BSD-2, very active, v0.3.32 2026-09) is the de facto open FMI 1/2/3 reference importer. It is ideal as a dev/test-time validator and cross-check for embeddyn's own ctypes adapter, even if embeddyn keeps its own runtime. For exporting embeddyn models as FMUs, PythonFMU3 (MIT, v1.3.0 released 2026-11-18) now claims FMI 3.0 CS **and** ME with FMU state. PythonFMU (MIT, v0.7.0 2025-12) is FMI 2.0 CS-only. UniFMU (v1.1.0 2026-11) adds distributed/remote FMUs but is GPL-3-based (INTO-CPS Association license).

### Cited Findings
- FMPy license: 2-Clause BSD ("Copyright (c) 2017-2025 Dassault Systemes") — [FMPy LICENSE.txt](https://github.com/CATIA-Systems/FMPy/blob/main/LICENSE.txt). GitHub API: last push 2026-11-02, 601 stars, latest release v0.3.32 (2026-09-09) — [FMPy repo](https://github.com/CATIA-Systems/FMPy). Earlier releases were 0.3.31 (2026-08-13) and 0.3.30 (2026-07-03) — [piwheels](https://www.piwheels.org/project/fmpy/).
- FMPy supports FMI 1.0, 2.0 and 3.0, CS and ME. It has a GUI (`python -m fmpy.gui`) and a Dash web app (`python -m fmpy.webapp`). It can compile C-code FMUs and generate CMake projects for debugging — [FMPy README](https://github.com/CATIA-Systems/FMPy), [PyPI](https://pypi.python.org/pypi/fmpy/).
- The FMPy source tree contains `src/fmpy/validation.py`, `src/fmpy/ssp/__init__.py` and `src/fmpy/ssp/simulation.py`, so it has a validation module and SSP reading/simulation (I checked the raw file URLs, HTTP 200). `src/fmpy/fmi3.py` wraps `fmi3GetFMUState`, `fmi3UpdateDiscreteStates`, `fmi3EnterStepMode`, `fmi3GetClock`/`fmi3SetClock` and `fmi3GetIntervalDecimal` — [FMPy fmi3.py](https://github.com/CATIA-Systems/FMPy/blob/main/src/fmpy/fmi3.py).
- FMI 3.0 Scheduled Execution has "practically no tool support", including in FMPy — [FMI 3.0 & FMPy cheatsheet](https://dr-clementcoic.github.io/fmi-cheat-sheet/).
- Reference-FMUs: 2-Clause BSD (Modelica Association Project FMI). Latest release v0.0.42 (2026-10-23), active (pushed 2026-11-11). FMUs: BouncingBall (state events), Dahlquist, Feedthrough (all variable types), Resource, Roberts (DAE), Stair (time events), StateSpace (arrays and structural parameters), VanDerPol. Each comes in FMI 2 and FMI 3 variants — [Reference-FMUs](https://github.com/modelica/Reference-FMUs).
- PythonFMU (NTNU-IHB): MIT, v0.7.0 (2025-12-12). It packages Python 3 code or CSV files as co-simulation FMUs "following FMI version 2.0". The `--handle-state` and `--serialize-state` flags set canGetAndSetFMUstate and canSerializeFMUstate. It does not bundle Python, so the target needs a compatible interpreter and the dependencies (`pythonfmu deploy` installs from requirements.txt or environment.yml). On Linux, a non-Python host must `LD_PRELOAD` libpython — [PythonFMU](https://github.com/NTNU-IHB/PythonFMU).
- PythonFMU3: MIT, v1.3.0 (2026-11-18), very active, 14 stars. README: "Turn a Python class into an FMI 3.0 FMU"; "Co-Simulation and Model Exchange, with events, arrays, structural parameters, units, enumerations and FMU state"; listed on the fmi-standard.org tools page; offers a migration guide from PythonFMU; supports `--handle-state`/`--serialize-state` — [PythonFMU3](https://github.com/StephenSmith25/PythonFMU3). Older docs said binary variables were not supported and clocks/scheduled execution were not implemented. The current README did not restate this, so clock support remains unconfirmed — [pythonfmu3 docs](https://pythonfmu3.readthedocs.io), [PyPI](https://pypi.org/project/pythonfmu3/).
- UniFMU: v1.1.0 (2026-11-06), active. The license is the "INTO-CPS Association Public License", which provides the program "under the terms of GPL version 3" (GitHub reports NOASSERTION) — [unifmu LICENSE.txt](https://github.com/INTO-CPS-Association/unifmu/blob/master/LICENSE.txt). It supports FMI2 and FMI3 Co-Simulation with Python, C# and Java backends, but not FMI1, ME or Scheduled Execution. The fmi2Get/SetFMUstate and serialize functions are supported, but CancelStep, GetDirectionalDerivative and GetStatus are not. `unifmu generate-distributed` creates a proxy FMU plus a remote backend linked over ZeroMQ — [UniFMU fmi-support.md](https://github.com/INTO-CPS-Association/unifmu/blob/master/docs/fmi-support.md), [UniFMU README](https://github.com/INTO-CPS-Association/unifmu).
- Rust alternative: the `fmi-export` crate implements FMI3 export — [docs.rs fmi-export](https://docs.rs/fmi-export/0.2.0/src/fmi_export/fmi3/mod.rs.html).
- fmi4c (Ecos-platform) is a C FMI import library — [fmi4c](https://github.com/Ecos-platform/fmi4c).
- FMI 3.0 news: FMI-LS-BUS v1.0 was released 2025-07-21. The fmi-standard.org tools page listed 250 tools as of 2025-06. Layered standards exist for XCP, BUS, STRUCT and REF — [Workshop 2026 slides](https://openmodelica.org/images/M_images/OpenModelicaWorkshop_2026/Lennart_Arun_OMSimulator_talk_OM_2026.pdf), [FMI news](https://fmi-standard.org/news/). A pre-release of FMI-LS-DAE (exchange of algebraic variables and residuals via FMI 3.0 getters/setters) has been published — [FMI news](https://fmi-standard.org/news/). FMI 3.0.1 is a clarification release, and a 3.0.2 RC appeared in October 2024 — [FMI 3.0.1 news](https://fmi-standard.org/news/2023-10-07-fmi-3.0.1-released/).

### Inferences
- **Validation:** add FMPy as a test-only dependency. Use it to (a) validate the modelDescription.xml of imported and exported FMUs (`fmpy.validation`), (b) produce reference trajectories that cross-check embeddyn's ctypes adapter, and (c) run the Reference-FMUs (BSD-2, so they can be redistributed in tests) as a conformance suite covering events, clocks, state and arrays. This is low-risk and high-value.
- **FMU state in checkpoints:** embeddyn's ctypes adapter can call fmi2GetFMUstate/SerializeFMUstate and fmi3GetFMUState/SerializeFMUState directly, gated on the canGetAndSetFMUstate/canSerializeFMUstate capability flags. FMPy's fmi3.py signatures are a good reference. The Reference-FMUs and PythonFMU3 `--serialize-state` builds make good test targets. libcosim's CBOR-based state serialization (Q3) is a design precedent.
- **Export:** PythonFMU3 is the best match for exporting embeddyn Python plants and controllers as FMI 3.0 FMUs (MIT, ME+CS, state). The catch is that the FMU needs a Python interpreter plus embeddyn installed on the target, which makes it a "tool-coupling" FMU and not a standalone one. Offer PythonFMU (FMI 2.0) as a fallback for older importers. UniFMU's GPL-3-based license makes it unattractive as a bundled dependency, but its distributed mode is notable.
- For a standalone FMU export (no Python on the target), the realistic routes are generating C code for the plant ODEs (embeddyn → C → compile against the Reference-FMUs/fmi3 headers, BSD-2) or exporting through Modelica/OMC. Both are significantly more work.

### Gaps
- FMI Cross-Check repo status: I did not retrieve it because of the API rate limit. My unverified prior knowledge is that the modelica/fmi-cross-check repo has been frozen or archived in favour of the Reference-FMUs plus the FMI tools page.
- FMIL (modelica-tools/fmi-library) and FMI4j: no metadata retrieved (rate limit). From prior knowledge (unverified this session): FMIL is a BSD-licensed C import library that gained FMI 3.0 support in 3.x releases, and FMI4j (NTNU-IHB, MIT, Kotlin/JVM) is FMI 2-focused with limited recent activity. Neither is needed by embeddyn, which already has its own importer.
- The `fmusim` features (in Reference-FMUs) for ME/CS, clocks and state were not documented on the page I fetched.
- No source confirmed whether FMPy implements an SSP *writer* or only reads and simulates SSP.

## Q3: SSP tools, DCP implementations, libcosim/cosim-cli, Maestro (INTO-CPS), Ecos, Ptolemy

### Takeaway
SSP 2.0 (Dec 2024) is current. Open SSP tooling with a Python API is limited to OMSimulator 3.x (OSMC-PL, full import/export), FMPy (read/simulate) and Ecos (MIT, `ecospy`, SSP 1.0). libcosim (MPL-2.0, v0.11.2 May 2026) is the most solid permissive C++ co-sim master. It supports FMI2 state save and serialization, an energy-conserving ECCO algorithm, OSP/SSP configs via cosim-cli, and a "PoC for DCP", but its documented support is FMI 1.0/2.0 only. DCP's reference implementation DCPLib is BSD-3 but has had no release since 2021. Maestro is active (Release/4.1.0, Apr 2026) but Java-based.

### Cited Findings
- SSP 2.0 was released in December 2024 and adds support for interconnecting FMI 3.0-based vECUs — [SSP 2.0 release](https://ssp-standard.org/news/2024-12-20-ssp-2-0-release/), [modelica.org news](https://modelica.org/news/2025-01-08-ssp-20-released/).
- libcosim: MPL-2.0, last push 2026-11-21, v0.11.2 (2026-05-18). The v0.11.2 changelog lists "PoC for DCP (#192)". v0.11.0 (2025-03-31) added a reference implementation of the ECCO (Energy Conservation-based Co-simulation) algorithm, configurable through OspSystemStructure, and FMI2 state saving and serialization (CBOR to file) gated on canGetAndSetFMUstate/canSerializeFMUstate. v0.10.0 added asynchronous `simulate_until` — [libcosim CHANGELOG](https://github.com/open-simulation-platform/libcosim/blob/master/CHANGELOG.md).
- libcosim offers a C++ API and a C wrapper (libcosimc). Distributed simulation runs through proxyfmu, which also allows multiple instances of single-instance FMUs. FMI 1.0 and 2.0 CS are supported — [libcosim distributed](https://open-simulation-platform.github.io/libcosim/distributed), [libcosim docs](https://open-simulation-platform.github.io/libcosim). The OSP page says "FMI 3.0 will be supported when this standard becomes operational". That page is undated and probably stale — [OSP on GitHub](https://open-simulation-platform.github.io).
- cosim-cli: MPL-2.0, last push 2026-05-22, no GitHub releases. It runs systems described in OSP or SSP format, or a single FMU — [cosim-cli](https://www.github.com/open-simulation-platform/cosim-cli).
- Ecos: MIT, C++20, Python bindings via `pip install ecospy`, plus a C API. Supports FMI 1.0/2.0 CS, "Partial support" for FMI 3.0, and SSP 1.0. Optional proxyfmu for per-process or remote execution (Unix domain sockets locally, TCP/IP remotely) — [Ecos](https://github.com/Ecos-platform/ecos).
- DCP: a communication protocol (unlike FMI, which is an API), standardised as a Modelica Association Project. DCPLib is the C++ reference implementation — [FMI tutorial: DCP](https://fmi-tutorial.vti.se/FMI%20Tutorial/Introduction/Distributed%20Co-Simulation%20Protocol), [DCPLib](https://github.com/modelica/DCPLib). GitHub API: DCPLib is BSD-3-Clause, last push 2026-11-12, latest release v0.2 (2021-03-30) — [DCPLib](https://github.com/modelica/DCPLib).
- Maestro (INTO-CPS): mainly Java (Maven, Java 11) with some Scala, and a `maestro-webapi` module. Latest release Release/4.1.0 (2026-04-17), last push 2026-11-19, 23 stars. The GitHub API reports no license, and no LICENSE file was found at the repo root — [Maestro](https://github.com/INTO-CPS-Association/maestro).
- OMSimulator 3.x handles SSP import/export including SSV, and imports SSP from other tools (see Q1) — [Workshop 2026 slides](https://openmodelica.org/images/M_images/OpenModelicaWorkshop_2026/Lennart_Arun_OMSimulator_talk_OM_2026.pdf).

### Inferences
- **SSP in embeddyn:** SSP is a zip containing SystemStructure.ssd (XML), resources/*.fmu and optional .ssv/.ssm files. embeddyn could implement a minimal native SSD reader/writer in Python (lxml or ElementTree) that maps components, connectors, connections and parameter bindings to embeddyn's system graph. That avoids an OSMC-PL dependency. Use OMSimulator 3.x and FMPy's SSP reader as cross-validation oracles, and exchange test files with them.
- **Algebraic loops:** none of the masters found here document a general algebraic-loop solver, except older OMSimulator strongly-coupled mode (unverified). For embeddyn, a native solution looks most practical: detect direct-feedthrough cycles from FMI `<ModelStructure>` dependencies, then iterate with fixed-point/Newton using GetFMUState rollback. libcosim's ECCO and FMI-LS-DAE are adjacent references.
- **Distributed co-sim:** options are (a) proxyfmu (from libcosim/Ecos, Thrift-based) to run FMUs out of process or remotely, (b) UniFMU's distributed mode (ZeroMQ, GPL-3-based), or (c) DCP via DCPLib (BSD-3, but stale releases). For a Python tool, (a) or a home-grown ZeroMQ/gRPC wrapper is likely the pragmatic choice. DCP looks low-priority given the tooling momentum.
- Ecos (`ecospy`, MIT) is the most permissive pip-installable alternative master for comparison, but it has a small community.

### Gaps
- libcosim's DCP PoC scope and libcosim FMI 3.0 status were not confirmed beyond the changelog line.
- Maestro's FMI version support, license terms and rollback/step-size features were not retrievable from the README.
- I found no "ssp4j" project in the searches done. Ptolemy II was not researched because of the time budget. Vico (Kotlin co-sim framework, Ecos lineage) was not checked.
- I found no recent (2024–2026) comparison paper of open SSP tools.

## Q4: Julia ModelingToolkit / Dyad, CasADi, Python acausal libraries, Scilab/Xcos, and whether any give a practical Python path

### Takeaway
No pure-Python acausal library came close to Modelica/MTK maturity in this research. ModelingToolkit (MIT, from prior knowledge) is acausal and powerful but only imports FMUs. Open FMU export from Julia goes through FMIExport.jl, and Dyad's FMUGeneration is tied to a JuliaHub product and licence. The practical Python path for acausal physics remains "model in Modelica (MSL) → OpenModelica FMU export → embeddyn imports the FMU".

### Cited Findings
- ModelingToolkit documents FMI integration for **import** of FMI 2 and 3 FMUs via FMI.jl. I found no MTK-native export documentation — [MTK FMI tutorial](https://docs.sciml.ai/ModelingToolkit/dev/tutorials/fmi/).
- FMIExport.jl lets you export FMUs from any Julia code and is integrated into FMI.jl — [FMIExport.jl](https://github.com/ThummeTo/FMIExport.jl), [docs](https://thummeto.github.io/FMIExport.jl/dev/).
- Dyad FMU export uses FMUGeneration.jl, available to academic users of Dyad Studio via the Dyad Registry. Licensing for redistributing generated FMUs was an open question on the forum (Oct 2025) — [Julia Discourse: Dyad Builder, FMUs](https://discourse.julialang.org/t/dyad-builder-fmus/132792), [Dyad FMU generation docs](https://help.juliahub.com/dyad/v0.5/analyses/fmugeneration.html), [JuliaHub webinar](https://juliahub.com/videos/fast-export-of-functional-mockup-units-in-dyad).
- A 2025 Modelica/FMI conference talk covered connecting Dyad and Modelica via FMI — [pretalx talk](https://pretalx.com/16thmodelicafmiconference/speaker/MBQSEV/).
- FMIExchange.jl: BSD-3, FMI 2 only — [FMIExchange.jl](https://electa-git.github.io/FMIExchange.jl/dev).

### Inferences
- A Julia-based plant path (MTK → FMIExport.jl → FMU) is technically possible, but it adds a Julia toolchain and the exported FMU carries a Julia runtime. That is heavier than OM C-code FMUs, so it is not recommended as the first choice.
- CasADi (LGPL-3, from prior knowledge, unverified this session) is valuable for optimal control and MPC design around embeddyn plants, and recent versions can import FMUs into symbolic graphs. It is not an acausal modelling language.
- Scilab/Xcos (GPL-2-compatible CeCILL, prior knowledge) has Modelica-based acausal blocks, but its Python integration is poor. It is not recommended.

### Gaps
- The searches did not cover pydae or other Python acausal libraries, CasADi's FMU import status, or Scilab/Xcos FMI support. I have no verified 2026 data for them.
- I did not retrieve the Dyad LICENSE.md text, so the open-source status of Dyad itself is unverified. Dyad is likely source-available with commercial terms, so treat it as not open-source until checked.

## Q5: Recommendation (what to integrate first and how)

### Takeaway
The proposed order of work (an inference from all of the findings above):
1. FMPy + Reference-FMUs as test-time oracles and validators (BSD-2).
2. Native FMU state save/restore in the ctypes adapter, gated on capability flags, with serialized blobs stored in embeddyn checkpoints.
3. PythonFMU3 (MIT) for exporting embeddyn models as FMI 3.0 CS/ME FMUs.
4. A native minimal SSP (SSD/SSV) reader and writer, cross-validated against OMSimulator 3.x (an OSMC-PL optional extra).
5. An MSL plant library delivered as OpenModelica-exported FMI 2.0 CS FMUs, generated offline by a build script.
6. Algebraic loops via ModelStructure-based loop detection plus iterative solving with state rollback.
7. Distributed co-sim through out-of-process FMU proxies (proxyfmu-style or ZeroMQ), with DCP deferred.

### Cited Findings
- FMPy BSD-2 and FMI 1/2/3 with fmi3 state, clock and event functions — [FMPy](https://github.com/CATIA-Systems/FMPy), [fmi3.py](https://github.com/CATIA-Systems/FMPy/blob/main/src/fmpy/fmi3.py).
- Reference-FMUs BSD-2, covering events, DAE, arrays and structural parameters — [Reference-FMUs](https://github.com/modelica/Reference-FMUs).
- PythonFMU3: MIT, FMI 3.0 CS+ME, FMU state — [PythonFMU3](https://github.com/StephenSmith25/PythonFMU3).
- OMSimulator 3.x pip package with SSP import/export and mixed FMI2/3; licensed OSMC-PL — [Workshop 2026](https://openmodelica.org/images/M_images/OpenModelicaWorkshop_2026/Lennart_Arun_OMSimulator_talk_OM_2026.pdf), [OMSimulator](https://github.com/OpenModelica/OMSimulator).
- OM FMU export: FMI 2.0 ME/CS stable, FMI 3.0 experimental, CVODE inside CS FMUs — [OM FMI docs](https://openmodelica.org/doc/OpenModelicaUsersGuide/latest/fmitlm.html).
- libcosim FMI2 state serialization (CBOR) and ECCO as design references — [libcosim CHANGELOG](https://github.com/open-simulation-platform/libcosim/blob/master/CHANGELOG.md).

### Inferences
- Comparison table (license | activity, late 2026 | Python API | FMI | features relevant to embeddyn | fit):

| Tool | License | Activity / latest | Python API | FMI | Notable features | embeddyn fit |
|---|---|---|---|---|---|---|
| FMPy | BSD-2 | v0.3.32, 2026-09; push 2026-11 | native | 1.0/2.0/3.0 CS+ME | validation, GUI/webapp, SSP read/sim, fmi3 GetFMUState/clocks/UpdateDiscreteStates/EnterStepMode, no SE | High: test oracle, validator |
| Reference-FMUs (+fmusim) | BSD-2 | v0.0.42, 2026-10 | n/a (C) | 2.0/3.0 | events, DAE, arrays, structural params | High: conformance tests |
| PythonFMU3 | MIT | v1.3.0, 2026-11-18 | native (export) | 3.0 CS+ME | events, arrays, units, FMU state; clocks unconfirmed; needs Python on target | High: FMU export |
| PythonFMU | MIT | v0.7.0, 2025-12 | native (export) | 2.0 CS | state get/set/serialize; needs Python on target | Medium: FMI2 fallback export |
| UniFMU | INTO-CPS PL (GPL-3 based) | v1.1.0, 2026-11 | Python backend | 2.0/3.0 CS only | state get/set/serialize, distributed via ZeroMQ | Low-medium (license); idea source |
| OMSimulator 3.x | OSMC-PL | v3.0.x 2026; active | pip `OMSimulator` | 2.0/3.0 CS+ME, mixed | SSP/SSV import/export, PySide6 GUI | High as optional SSP oracle/extra |
| OpenModelica OMC (+OMPython) | OSMC-PL (OMPython: OSMC-PL-RT; conflicting BSD-2 claim) | 1.26.x 2025-12..2026-06; 1.27.1 2026-09 (secondary source) | OMPython scripting | export 2.0 ME/CS; 3.0 experimental | MSL, CVODE inside CS FMU | High as offline plant-FMU generator |
| MSL | BSD-3 | active (push 2026-11) | via OMC | n/a | machines, converters, rotational, thermal, fluid | High (via OM FMUs) |
| libcosim / cosim-cli | MPL-2.0 | v0.11.2, 2026-05 | none found (C/C++) | 1.0/2.0 CS | FMI2 state CBOR, ECCO, proxyfmu distributed, SSP/OSP configs, DCP PoC | Medium: design reference, external runner |
| Ecos | MIT | active (no releases listed) | `ecospy` | 1.0/2.0 CS, partial 3.0 | SSP 1.0, proxyfmu remote | Medium: permissive alt master |
| Maestro | no license detected | Release/4.1.0, 2026-04 | none (Java) | not confirmed | web API | Low |
| DCPLib | BSD-3 | last release v0.2, 2021 | none (C++) | n/a (DCP) | DCP reference impl | Low (stale) |
| FMIExport.jl / Dyad | FMIExport.jl free (license not confirmed); Dyad not confirmed open | active 2025 | via Julia | export 2.0 (FMIExport.jl, unverified version) | acausal via MTK | Low (Julia toolchain) |

- Rationale: steps 1–3 are permissive, Python-native and directly close the state/checkpoint and export gaps. SSP is simple enough XML to implement natively, which keeps OSMC-PL code out of embeddyn's core. Acausal physics is best "outsourced" to Modelica/MSL through OM FMU export, because embeddyn already imports FMUs and the CS FMUs carry their own CVODE solver, so stiff electrical/thermal plants do not need RK4. Validate OM-generated FMUs with FMPy in CI.
- Risks: OM FMI 3.0 export is experimental, so stay on FMI 2.0 for plant FMUs. PythonFMU3 is a single-maintainer project (14 stars) despite rapid releases. OSMC-PL needs legal review before any bundling.

### Gaps
- No hands-on test was run in this session: OM export of an MSL DC-motor model, its GetFMUState flags, or PythonFMU3 ME round-trips through FMPy and embeddyn. These are the recommended first spikes.
- License terms of OSMC-PL modes and Maestro remain unconfirmed.
