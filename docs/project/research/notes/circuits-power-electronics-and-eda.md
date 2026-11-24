# Circuit simulation, power-electronics simulation and PCB/EDA tools for embeddyn

Scope: open-source (and clearly marked free-but-closed) tools that could fill embeddyn's gaps: gate drive/dead time, MOSFET/diode models, fast averaged/switched power electronics, motor drives, mixed-signal, a KiCad -> SPICE -> simulation round trip, vendor model libraries, and power-stage thermal. Research date: 2026-11-23. Items with no 2025-2026 source are flagged "older info". Statements not backed by a fetched source are in Inferences or Gaps.

Baseline: embeddyn already steps libngspice through ctypes (one transient per 1 ms step with stop/resume, external sources), runs a switching H-bridge, exports through kicad-cli, has an IPC plugin skeleton, and uses openEMS and scikit-rf.

## Q1. SPICE engines and Python wrappers (ngspice, Xyce, Qucs-S, QSPICE, LTspice, PySpice, spicelib/PyLTSpice, InSpice)

### Takeaway
ngspice is still the best primary engine. It is actively released (45 in Sep 2025, 46 in Mar 2026, 47 in Aug 2026), KLU, OSDI, XSPICE and OpenMP are standard build options since 43, and 46/47 added a diode soft-recovery model and a VDMOS soft-recovery model, both directly useful for H-bridge and dead-time work. Xyce (7.10, Aug 2025) is mature and parallel and has C/Python coupling interfaces, but those coupling interfaces are thinly documented and partly dated. Treat it as a secondary or cross-check engine. The Python wrappers are batch or file oriented, except PySpice/InSpice, which use the shared library. None of them beats embeddyn's own ctypes stepping loop for co-simulation.

### Cited Findings
**ngspice (BSD-style/open source; actively maintained)**
- ngspice-47, Aug 11 2026: pyplot command (matplotlib), soft-recovery model for VDMOS power transistors, small-signal noise for code models, WAV file read/write, PSS update — [ngspice news](https://ngspice.sourceforge.io/news.html)
- ngspice-46, Mar 29 2026: vec()/var() functions, code model "astate", voltage-based truncation error correction, new diode soft-recovery model, "CIDER is enabled for KLU matrix solver", better FFT in 'linearize' — [ngspice news](https://ngspice.sourceforge.io/news.html)
- ngspice-45, Sep 1 2025: "Code model d_cosim supports VHDL with GHDL for mixed signal simulation"; BSIM 4.8.3; VBIC revised; 'save nointernals'; XSPICE event-node support for iplot. 45.2 (Sep 6 2025) is a bug-fix release — [ngspice news](https://ngspice.sourceforge.io/news.html); NEWS file dates 45 to Aug 17 2025 — [Debian NEWS](https://sources.debian.org/src/ngspice/45.2%2Bds-1/NEWS)
- ngspice-44, Dec 29 2024: d_cosim supports Icarus Verilog; new shared-ngspice API functions for reset and for skipping .spiceinit; EKV3 as a shared library — [ngspice news](https://ngspice.sourceforge.io/news.html)
- ngspice-43, Jul 14 2024: KLU, OSDI, readline, OpenMP and XSPICE become standard configure options — [ngspice news](https://ngspice.sourceforge.io/news.html)
- KLU arrived in ngspice 42 ('option klu'). OSDI has been integrated since version 39. d_cosim first appeared in 42 as a generic adaptor for digital co-simulation (Verilator was the first target) — [Debian NEWS](https://sources.debian.org/src/ngspice/45.2%2Bds-1/NEWS); [ngspice news](https://ngspice.sourceforge.io/news.html) (the Verilator entry belongs to release 42, 2023, so this is older info)

**Xyce (Sandia; open source, older info on the license, see Gaps)**
- Xyce 7.10 was announced in Aug 2025 (the release-notes PDF is dated May 14 2025). Autotools is deprecated in 7.10, and CMake becomes the default in 7.11. Certified on RHEL 8, Windows 11 (serial) and macOS x86-64/arm64 — [OSTI 7.10 release notes](https://www.osti.gov/biblio/2585573); [Xyce news](https://xyce.sandia.gov/news/)
- Release cadence: 7.8 Dec 2023, 7.9 Jan 2025, 7.10 Aug 2025. No 7.11 or later was found by Nov 2026 — [Xyce news](https://xyce.sandia.gov/news/)
- XDM (since 7.0) is a stand-alone translator from HSPICE and PSpice syntax to Xyce syntax. Release 6.4 added PSpice digital behavioral compatibility (DIGINITSTATE). Verilog-A models come through the ADMS back-end — [Xyce news](https://xyce.sandia.gov/news/)
- Coupling interfaces: (a) the "General External" (GenExt) C++ coupling API, SAND2018-12275, with a General External Device class, time- and frequency-domain coupling functions and an External Output class, aimed at developers coupling C++ codes — [Sandia pub](https://sandia.gov/research/publications/details/application-note-coupled-simulation-with-the-xyce-general-external-interfac-2018-10-01); [OSTI PDF](https://www.osti.gov/servlets/purl/1481564). (b) a MixedSignal interface that uses Xyce as a shared library from C/C++ or through Python ctypes (XyceCInterface), plus a VPI route demonstrated with Icarus Verilog — [Mixed-signal AppNote](https://xyce.sandia.gov/files/xyce/AppNote-MixedSignal.pdf); [OSTI 1762646](https://www.osti.gov/servlets/purl/1762646)
- The 7.1-era note documented these interfaces on RHEL7 with Python 2.6/2.7 and called them "still under development" (older info) — [OSTI 1762646](https://www.osti.gov/servlets/purl/1762646)
- A xyce-users post says a later GenExt update added "voltage limiting" for external devices, but the write-up was never published. The only examples are in Xyce-PyMi (a Python model interface built on GenExt) — [xyce-users group](https://groups.google.com/g/xyce-users/c/BY3-kZRwPh4)
- A GHDL issue thread (around 2019-2020) says mixed-signal interface improvements were "not currently a high priority" for the Xyce team (older info) — [ghdl#1052](https://github.com/ghdl/ghdl/issues/1052)

**Qucs-S (GPL; active)**
- Latest stable is Qucs-S 26.1.0. It is a Qt6 GUI over the ngspice, Qucsator(RF) and Xyce kernels, and ngspice is the recommended engine — [Qucs-S homepage](https://ra3xdh.github.io/); [Qucs-S docs](https://qucs-s-help.readthedocs.io/en/latest/welcome/what-is-qucs-s.html)
- 25.2.0 simulates microstrip lines in ngspice through new XSPICE microstrip models (requires ngspice >= 45). QucsatorRF updated to 1.0.7 — [Habr article](https://habr.com/en/articles/944766) (secondary source)

**QSPICE (Qorvo; FREE BUT CLOSED, Windows)**
- Free, including for commercial use, with "no feature limitations". Email registration is required. Includes C++ and Verilog compilers, so C++ and Verilog models are compiled to native code before simulation — [Qorvo QSPICE page](https://www.qorvo.com/design-hub/calculators-simulation/qspice); [Mouser](https://www.mouser.pe/qorvo-qspice-simulator); [Hackster](https://www.hackster.io/news/qorvo-s-qspice-is-a-free-next-generation-mixed-mode-spice-simulation-tool-bd3222ea0e77)
- A Qorvo forum reply says it is "absolutely free, no restrictions". This is informal, not formal written terms — [Qorvo forum](https://forum.qorvo.com/t/license-of-qspice-simulator-for-commercial-use/25612)

**LTspice (Analog Devices; FREE BUT CLOSED)**
- Its built-in VDMOS has been updated with six temperature coefficients (user report) — [diyAudio](https://www.diyaudio.com/community/goto/post?id=5779497)

**Python wrappers**
- spicelib (GPL-3.0, Nuno Brum) drives LTspice, NGSpice, QSPICE and Xyce, edits netlists and parses .raw/.log files. Version 1.4.4 was released 2025-05-03 — [spicelib README](https://raw.githubusercontent.com/nunobrum/spicelib/main/README.md); [PyPI spicelib](https://pypi.org/project/spicelib)
- PyLTSpice is based on spicelib with LTspice as the default. Releases 5.4.2 through 5.4.5 came out in Feb-Oct 2025 — [PyPI PyLTSpice](https://pypi.org/project/PyLTSpice)
- InSpice (GPL-3.0) is Innovoltive's fork of PySpice: a Python interface to Ngspice (shared library) and Xyce, requiring Python >= 3.12, latest 1.7.0.7 (modified April 2025) — [PyPI InSpice](https://pypi.org/project/InSpice/)
- Upstream PySpice: the latest PyPI release found is 1.5, and the author says it is developed in free time. No 2025-2026 upstream release was found — [PySpice README](https://raw.githubusercontent.com/PySpice-org/PySpice/master/README.rst); [PyPI PySpice](https://pypi.org/project/PySpice)

### Inferences
- embeddyn's existing ctypes stepping of libngspice is already the most direct co-simulation path. PySpice/InSpice add netlist-building convenience but are unlikely to improve the stepping loop, and spicelib's batch/raw-file model fits parameter sweeps and regression runs (LTspice/QSPICE cross-checks), not lock-step co-simulation.
- Upgrading the bundled or targeted libngspice to 46/47 is a cheap, high-value step. The diode and VDMOS soft-recovery models, plus CIDER with KLU, directly address the "MOSFET and diode models" and dead-time/reverse-recovery gap. The 44 API additions for reset and for skipping .spiceinit help embeddyn re-run cleanly within one process.
- Xyce's main value is as a second engine to validate ngspice results and for large parallel or power-grid netlists. Its Python coupling is older, Linux-centric and thinly documented, so it is a poor first co-simulation target.
- QSPICE and LTspice are useful as "golden reference" engines through spicelib on Windows (or Wine), but cannot be embedded or redistributed. Mark them as optional external tools.

### Gaps
- Xyce license: GPLv3 is from memory only, not confirmed on a fetched page. Whether the Xyce Python interface now supports Python 3 was not confirmed by a 2025-2026 source.
- No benchmark numbers were found comparing ngspice (KLU), Xyce and QSPICE on switching converters.
- ngspice PSpice/LTspice compatibility modes ('set ngbehavior=ps/lt/...') are known from memory and were not re-verified against the current manual. Check the ngspice manual's compatibility chapter.
- The ngspice digital/mixed-signal page URL (ngspice.sourceforge.io/digital.html) returned 404, so the d_cosim timing and sync details are not documented here.

## Q2. Power-electronics-specific simulation (GeckoCIRCUITS, PLECS alternatives, Modelica, Julia, averaged models, motor drives)

### Takeaway
There is no mature open PLECS equivalent with a good Python co-simulation API. The realistic options are: (1) GeckoCIRCUITS (Java, GPL; ideal-switch, fast; old but revived), (2) Modelica (MSL Electrical.PowerConverters/Machines, exported as an FMU for stepping from Python), (3) motulator (MIT, pure Python motor-drive plant models, averaged or PWM-level) for motor drives, and (4) in-house averaged switch models in ngspice (behavioral sources) next to the switched H-bridge. Julia MTK StandardLibrary has only basic electrical components (simple NMOS/PMOS/diode) and is not a power-electronics toolbox.

### Cited Findings
- GeckoCIRCUITS: a Java power-electronics circuit simulator "optimized for applications in power electronics", originally from ETH Zurich, released under GPL, and moved from SourceForge to GitHub in July 2021. It can be integrated with MATLAB/Simulink and other environments — [GeckoCIRCUITS GitHub org](https://github.com/geckocircuits); [SourceForge](https://sourceforge.net/projects/geckocircuits/); [linuxlinks](https://www.linuxlinks.com/geckocircuits-circuit-simulator-optimized-power-electronics/)
- A third-party documentation site (tinix84) claims dual GPLv3/commercial licensing and describes a 2025 GUI-free core extraction ("gecko-simulation-core"). This is unverified and not clearly the official project — [tinix84 docs](https://tinix84.github.io/GeckoCIRCUITS/)
- pygeckocircuits is a Python wrapper for GeckoCIRCUITS — [MauererM/pygeckocircuits](https://github.com/MauererM/pygeckocircuits)
- An open-source power-electronics education package based on GeckoCIRCUITS is described in a 2022 paper (older info) — [arXiv 2204.12924](https://ar5iv.labs.arxiv.org/html/2204.12924)
- The Modelica EDrives library offers three inverter abstraction levels: quasi-static, averaging (no switching effects) and switching (2014, older info) — [Modelica 2014 abstract](https://2014.international.conference.modelica.org/proceedings/html/abstracts/abstract_17.pdf)
- A Modelica three-phase VSC averaged model with a DC/AC power-balance link is "very fast to simulate, as no switching events are needed" (SAE 2016, older info) — [SAE 2016-01-2029](https://sae.org/publications/technical-papers/content/2016-01-2029/?PC=DL2BUY)
- The Modelica Standard Library provides physical-to-signal adapters needed before wrapping a model as an FMU for co-simulation — [Altair Modelica co-sim tutorial](https://help.altair.com/twinactivate/help/en_us/topics/math_solutions/tut_modelica_cosim_c.htm)
- OpenIMDML is an open-source Modelica library for induction motors and variable-speed drives — [RPI paper](https://sites.ecse.rpi.edu/~vanfrl/documents/publications/journal/J092_FF_OpenIMDML.pdf)
- motulator (Aalto Electric Drives, MIT license): a Python motor-drive simulator with induction, synchronous reluctance and PMSM models, and a plant built from motor, mechanics and converter submodels. On PyPI, releases up to 0.7.3 — [awesome.ecosyste.ms](https://awesome.ecosyste.ms/projects/github.com%2FAalto-Electric-Drives%2Fmotulator); [PyPI motulator](https://pypi.org/project/motulator/); [IEMDC 2023 paper](https://acris.aalto.fi/ws/portalfiles/portal/120767052/IEMDC_motulator_fullpaper_final.pdf)
- A 2026 arXiv letter couples Python/C FOC controllers with averaged and PWM machine models through HELICS (software co-simulation only) — [arXiv 2607.21486](https://arxiv.org/pdf/2607.21486)
- ModelingToolkitStandardLibrary (Julia) Electrical provides Resistor, Diode, NMOS (V_tn, R_DS, lambda) and PMOS. Latest release found is v2.25.0 (2025-07-15) — [MTKStdLib electrical API](https://docs.sciml.ai/ModelingToolkitStandardLibrary/stable/API/electrical); [SourceForge mirror](https://www.sourceforge.net/projects/modelingtoolkitstandard.mirror/files/v2.25.0)
- Users report instability with an ideal diode plus ideal switch in MTK — [Julia Discourse](https://discourse.julialang.org/t/ideal-diode-and-ideal-switch-instability-problem/107769)

### Inferences
- For embeddyn's 1 ms co-simulation step with tens-of-kHz PWM, a two-tier approach fits: an averaged H-bridge/inverter model (duty-cycle input, power balance; pure Python/NumPy, or an ngspice B-source) for long system runs, and the existing switched ngspice model for detailed windows (dead time, ringing, losses). The Modelica and SAE sources support averaged models as the fast tier.
- motulator is the lowest-friction motor-drive plant: MIT license, pure Python, and the same language as embeddyn. Its machine, mechanics and converter submodels could be stepped directly by embeddyn, or used as a reference for embeddyn's own models.
- FMU export from OpenModelica (stepped with FMPy) is the general route to Modelica power-electronics and machine libraries. It is heavier, but gives standards-based co-simulation that embeddyn can reuse for thermal and mechanical domains.
- GeckoCIRCUITS needs a JVM bridge (JPype, py4j or pygeckocircuits). Its value is speed with ideal switches, but the project activity is uncertain, so it is a medium/low priority.
- Julia (MTK) adds a second language runtime for little power-electronics content. Not recommended.

### Gaps
- No open-source "PLECS-like" piecewise-linear ideal-switch simulator with a Python stepping API and recent activity was found (the search did not surface any beyond GeckoCIRCUITS).
- The official GeckoCIRCUITS GitHub license and recent commit activity were not verified directly.
- OpenModelica MSL Electrical.PowerConverters documentation itself was not fetched, and there is no source here for FMU stepping speed on switched converters.
- motulator's exact latest version date and API stability were not verified (PyPI lists 0.7.3, date not captured).

## Q3. Mixed-signal (ngspice + Verilator/Icarus/GHDL via d_cosim, Xyce + digital)

### Takeaway
ngspice's XSPICE d_cosim is the most current open mixed-signal path: Verilator (42, 2023), Icarus Verilog (44, Dec 2024) and GHDL/VHDL (45, Sep 2025), all in-process inside libngspice. Xyce's digital coupling (Python ctypes or VPI with Icarus) dates from 2018-2020 and is described as low priority.

### Cited Findings
- d_cosim: a generic adaptor for digital co-simulation in 42. Icarus Verilog support in 44 (Dec 29 2024). "supports VHDL with GHDL for mixed signal simulation" in 45 (Sep 1 2025) — [ngspice news](https://ngspice.sourceforge.io/news.html); [Debian NEWS](https://sources.debian.org/src/ngspice/45.2%2Bds-1/NEWS)
- ngspice 45 added XSPICE event-node support for iplot — [ngspice news](https://ngspice.sourceforge.io/news.html)
- Xyce mixed-signal: ADC/DAC devices; coupling through XyceCInterface (Python ctypes) or VPI with Icarus Verilog; the interfaces were "still under development" — [Xyce Mixed-signal AppNote](https://xyce.sandia.gov/files/xyce/AppNote-MixedSignal.pdf); [OSTI 1762646](https://www.osti.gov/servlets/purl/1762646)
- QSPICE (closed) compiles Verilog and C++ blocks to native code — [Hackster](https://www.hackster.io/news/qorvo-s-qspice-is-a-free-next-generation-mixed-mode-spice-simulation-tool-bd3222ea0e77)

### Inferences
- For embeddyn, d_cosim lets a gate-driver logic block (dead-time generator, PWM, protection latch in Verilog/VHDL) run inside the same libngspice transient that embeddyn already steps. No second synchronization loop is needed. The MCU firmware itself stays in Renode, and embeddyn keeps exchanging PWM and ADC values at the 1 ms boundary.
- XSPICE digital primitives (d_dff, d_and, delay models) are a lighter-weight alternative to HDL for dead-time logic.

### Gaps
- The details of d_cosim timing synchronization and performance were not retrieved (the ngspice digital page returned 404). See the ngspice manual's XSPICE d_cosim chapter.
- No source covered Xyce + Verilator.

## Q4. KiCad integration (IPC API / kicad-python, kicad-cli SPICE netlist, built-in simulator, SKiDL, kicad-skip, KiBot, Fritzing, wokwi-elements)

### Takeaway
The robust round trip today is file based: annotate the .kicad_sch symbols with Sim.* fields, run `kicad-cli sch export netlist --format spice`, then load the netlist into libngspice. Reference designators give the stable IDs. The KiCad IPC API (kicad-python) in KiCad 9/10 needs a running GUI, covers PCB rather than schematic commands, and cannot export files (plotting and export were pushed to KiCad 11). So it cannot drive the schematic-to-SPICE path yet.

### Cited Findings
- The IPC API in KiCad 9 and 10 only talks to a running KiCad GUI and has no plotting or export support (deferred to KiCad 11). Schematic-editor support is "planned" — [KiCad dev-docs, for add-on developers](https://dev-docs.kicad.org/en/apis-and-binding/ipc-api/for-addon-developers/); [APIs and bindings](https://dev-docs.kicad.org/en/apis-and-binding)
- kicad-python is the official Python binding over the Protobuf IPC API — [kicad-python docs](https://docs.kicad.org/kicad-python-main/kicad.html)
- Third-party claim: KiCad 10's IPC API ships schematic types but no schematic commands. Tools such as kicad-buddy edit .kicad_sch directly — [PyPI kicad-buddy](https://pypi.org/project/kicad-buddy/) (unofficial)
- Known issue: customized symbol fields are missing with kicad-python 0.7.1 on KiCad 10 — [KiCad forum](https://forum.kicad.info/t/kicad-10-customized-symbol-fields-missing-in-kicad-python-0-7-1/70731)
- FOSDEM 2025 talk on the new KiCad API — [FOSDEM 2025](https://archive.fosdem.org/2025/schedule/event/fosdem-2025-6802-bringing-a-new-api-to-kicad/)
- kicad-cli netlist export formats: kicadsexpr (default), kicadxml, cadstar, orcadpcb2, spice, spicemodel, pads, allegro — [KiCad source, command_sch_export_netlist.cpp](https://docs.kicad.org/doxygen/command__sch__export__netlist_8cpp_source.html)
- KiCad simulator fields: Sim.Device, Sim.Type, Sim.Pins, Sim.Params (also Sim.Decomposition). Legacy Spice_* fields are still recognized — [KiCad doxygen sim_model.h](https://docs.kicad.org/doxygen/sim__model_8h_source.html)
- KiCad integrates ngspice into the schematic editor and accepts spice, ltspice, pspice and hspice models — [kicad.org/discover/spice](https://kicad.org/discover/spice)
- FOSDEM 2025: "ngspice XSPICE elemental devices made available in KiCad" — [FOSDEM 2025 slides](https://fosdem.org/2025/events/attachments/fosdem-2025-5619-ngspice-xspice-elemental-devices-made-available-in-kicad/slides/237470/Fosdem202_8rKMx6m.pdf)
- Pitfall: a netlist that simulates in KiCad 9 (ngspice 44) failed in standalone ngspice (May 2025 bug) — [ngspice bug 791](https://sourceforge.net/p/ngspice/bugs/791); dev-list thread "exported SPICE netlist cannot be successfully simulated" — [KiCad devlist](https://groups.google.com/a/kicad.org/g/devlist/c/_gbMLGfr_wU/m/qIFaPVqZAgAJ)
- SKiDL can generate SPICE netlists directly, and pin order for parts and subcircuits was fixed. skidl.pyspice is a KiCad-free route — [PyPI skidl](https://pypi.org/pypi/skidl); [skidl docs](https://github.com/devbisme/skidl/blob/1.0.0/docs/index.md)
- wokwi-elements: MIT-licensed web components for electronic parts, presentation only, with no simulation behavior — [wokwi/wokwi-elements](https://www.github.com/wokwi/wokwi-elements)
- Fritzing: software is GPL-3.0+, part images CC BY-SA 3.0. The openSUSE fritzing-parts package ships LICENSE.CC-BY-SA — [openSUSE fritzing-parts](https://build.opensuse.org/package/show/openSUSE/fritzing-parts?rev=1)

### Inferences
- Stable-ID round trip: use the reference designator plus the KiCad symbol UUID (stored in .kicad_sch) as the key. Write it into netlist comments or into embeddyn-side mapping files, and read parameters back from Sim.Params. Parsing .kicad_sch directly (S-expressions; kicad-skip or a small parser) avoids the GUI requirement of the IPC API.
- Keep the IPC plugin skeleton for PCB-side features such as live highlighting or back-annotating values. Do not build the simulation path on it until schematic commands land (KiCad 11 or later, unconfirmed).
- wokwi-elements (MIT) are suitable for an embeddyn web UI of visual parts. Fritzing part graphics are CC BY-SA, so share-alike obligations apply if redistributed.
- SKiDL is useful if embeddyn wants code-defined circuits that also produce a KiCad netlist, but it adds a parallel source of truth to .kicad_sch.

### Gaps
- kicad-skip (Python .kicad_sch manipulation library) was not found in search results, so its status and license are unverified.
- KiBot (CI automation for KiCad outputs) was not researched in this pass.
- Whether kicad-cli's spice export carries symbol UUIDs was not verified.
- The KiCad 10 release date and the simulator changes in KiCad 10 were not confirmed from an official release note.

## Q5. Component model libraries (vendor SPICE models) and Verilog-A via OpenVAF; power-stage thermal

### Takeaway
Vendor power-MOSFET models are mostly PSpice/LTspice-syntax subcircuits, and several vendors ship electro-thermal variants with a thermal pin (Infineon Level 3, Rohm L2/TN, Nexperia Tmb). Those thermal pins are the natural hook for embeddyn's power-stage thermal model (a Foster/Cauer RC network in SPICE or in Python). ngspice runs Verilog-A compact models through OpenVAF-reloaded (GPLv3, Rust) and OSDI, which is the open route to modern compact models. ADMS is no longer part of ngspice development.

### Cited Findings
- Infineon power-MOSFET models are evaluated with SIMetrix/PSpice and work in LTspice. There are up to four model types, including a Level 3 electro-thermal model with physical temperature dependence of the die and package — [Infineon AN PowerMOSFET simulation models](https://www.infineon.com/dgdl/Infineon-ApplicationNote_PowerMOSFET_SimulationModels-AN-v01_00-EN.pdf); [Infineon simulation models page](https://www.infineon.com/cms/en/product/promopages/simulationmodels/)
- Rohm thermal models: an L2 level adds an RC thermal network for self-heating, plus a thermal-only TN level — [Rohm design models](https://www.rohm.com/support/design-model)
- Nexperia electrothermal models add a Tmb pin for connecting to a PCB or heatsink thermal model — [Nexperia IAN50012](https://nexperia.com/applications/interactive-app-notes/IAN50012_Nexperia_Precision_Electrothermal_models_for_Power_MOSFETs)
- Renesas AN9210: PSpice power-MOSFET subcircuit with global temperature options (older info) — [Renesas AN9210](https://renesas.com/en/document/apn/an9210-new-pspice-subcircuit-power-mosfet-featuring-global-temperature-options)
- PSpice-specific inductor-line workaround (insert an extra node) used on Wolfspeed models — [Wolfspeed forum](https://forum.wolfspeed.com/discussion/comment/3240)
- ngspice VDMOS: forum reports of a 'vdmos-thermal' branch extending temperature coefficients (branch-level, unconfirmed) — [diyAudio](https://www.diyaudio.com/community/goto/post?id=5784075). Release 47 adds VDMOS soft recovery — [ngspice news](https://ngspice.sourceforge.io/news.html)
- OpenVAF-reloaded is a revived (2024) fork of OpenVAF: a GPLv3 Verilog-A compiler written in Rust, which produces .osdi shared libraries loaded in ngspice with 'pre_osdi'. ngspice supports OSDI API 0.3 and 0.4, and 0.4 is OpenVAF-reloaded only — [ngspice OSDI page](https://ngspice.sourceforge.io/osdi.html); [ESSERC 2025 slides](https://fides.fe.uni-lj.si/~arpadb/ESSERC25/ESSERC2025.pdf); [OpenVAF fork](https://github.com/dwarning/OpenVAF)
- ADMS is no longer part of ngspice development — [ngspice ADMS page](https://ngspice.sourceforge.io/adms.html)
- VACASK (Á. Bűrmen) is another open simulator that uses OpenVAF-reloaded/OSDI 0.4 models — [VACASK on Codeberg](https://codeberg.org/nikosavola/VACASK)

### Inferences
- Model pipeline for embeddyn: a curated "models/" library (vendor .lib files kept with their licenses, never redistributed blindly), a smoke-test harness that loads each model in ngspice with PSpice compatibility mode and checks the I-V and switching curves, and a mapping to KiCad Sim.* fields.
- Thermal: drive the vendor electro-thermal Tj/Tmb pins from an embeddyn-side Cauer/Foster network, updated every 1 ms step from the switching and conduction losses computed in ngspice. Thermal time constants (ms to s) are much slower than the electrical ones, so this co-simulation boundary is numerically benign.
- OpenVAF/OSDI is the path for custom Verilog-A models (for example SiC MOSFET compact models, or a behavioral gate driver written in Verilog-A). It needs the OpenVAF-reloaded binary in the toolchain.

### Gaps
- Vendor model licenses (redistribution terms) were not checked per vendor.
- No authoritative source confirms that Infineon Level 3 models run unmodified in ngspice.
- Open model collections (for example ngspice's own model examples, KiCad-Spice-Library on GitHub) were not researched in this pass.

## Q6. Comparison and recommendation: what to integrate first

### Takeaway
Integrate in this order: (1) upgrade to ngspice 46/47 and add gate-drive, dead-time and soft-recovery MOSFET/diode models to the existing stepped H-bridge; (2) a KiCad .kicad_sch -> kicad-cli spice netlist -> libngspice round trip keyed by reference designator and UUID; (3) an averaged-converter fast path plus motulator-based motor plant; (4) vendor electro-thermal models with an embeddyn RC thermal network; (5) d_cosim (Verilog/VHDL) for gate-driver logic; and later, optionally, Xyce or spicelib-driven LTspice/QSPICE as cross-check engines and Modelica FMUs.

### Cited Findings (summary table; each cell is backed by the citations in Q1-Q5)

| Tool | License | Activity (latest found) | External stepping / co-sim API | Switching speed notes | Model compat | Fit for embeddyn |
|---|---|---|---|---|---|---|
| ngspice | Open source (BSD-style, from memory) | 47, Aug 2026 ([news](https://ngspice.sourceforge.io/news.html)) | libngspice shared library (already used); new reset API in 44 | KLU option; CIDER+KLU in 46 | PSpice/LTspice compat modes (unverified), OSDI Verilog-A | High: already integrated; upgrade |
| Xyce | Open source (GPLv3, from memory) | 7.10, Aug 2025 ([news](https://xyce.sandia.gov/news/)) | GenExt C++ API; XyceCInterface ctypes; VPI | Parallel (MPI); no benchmarks found | XDM translates PSpice/HSPICE | Medium: cross-check or large netlists |
| Qucs-S | GPL | 26.1.0 ([home](https://ra3xdh.github.io/)) | GUI only (uses ngspice/Xyce) | n/a | via backends | Low: GUI reference only |
| QSPICE | FREE BUT CLOSED | active ([Qorvo](https://www.qorvo.com/design-hub/calculators-simulation/qspice)) | none embeddable; batch via spicelib | C++/Verilog native blocks | SPICE | Optional reference engine |
| LTspice | FREE BUT CLOSED | — | batch via spicelib/PyLTSpice | — | LTspice models, VDMOS | Optional reference engine |
| spicelib / PyLTSpice | GPL-3.0 | 1.4.x 2025; PyLTSpice 5.4.5 Oct 2025 ([PyPI](https://pypi.org/project/PyLTSpice)) | batch, raw-file parsing | n/a | multi-engine | Medium: sweeps and regression |
| PySpice / InSpice | GPL-3.0 | PySpice 1.5 (stale); InSpice 1.7.0.7, 2025 ([PyPI](https://pypi.org/project/InSpice/)) | libngspice wrapper | n/a | ngspice/Xyce | Low: embeddyn's ctypes loop is already better suited |
| GeckoCIRCUITS | GPL | GitHub since 2021; activity unclear ([org](https://github.com/geckocircuits)) | Java; pygeckocircuits | ideal switches, fast | own models | Low-medium |
| Modelica (OpenModelica, MSL, EDrives, OpenIMDML) | Open | — | FMU co-simulation | averaged or switched variants | Modelica | Medium (later) |
| motulator | MIT | 0.7.3 ([PyPI](https://pypi.org/project/motulator/)) | pure Python | averaged/PWM models | own | High for motor drives |
| MTK StdLib (Julia) | MIT (from memory) | v2.25.0, Jul 2025 | Julia | basic devices only | own | Low |
| OpenVAF-reloaded | GPLv3 | revived 2024; OSDI 0.4 ([ESSERC25](https://fides.fe.uni-lj.si/~arpadb/ESSERC25/ESSERC2025.pdf)) | compiler -> .osdi for ngspice | — | Verilog-A | Medium: custom compact models |
| KiCad kicad-cli + Sim.* fields | GPL | KiCad 9/10 | file based, headless | — | spice/ltspice/pspice/hspice models | High: round trip |
| KiCad IPC (kicad-python) | Open | KiCad 9/10; no schematic commands, no export ([dev-docs](https://dev-docs.kicad.org/en/apis-and-binding/ipc-api/for-addon-developers/)) | socket/Protobuf, GUI required | — | — | Medium: PCB-side only for now |
| SKiDL | MIT (from memory) | — | Python netlist generator | — | SPICE output | Low-medium |
| wokwi-elements | MIT | — | web components (visual only) | — | — | Medium for UI |
| Fritzing parts | CC BY-SA 3.0 images; GPL-3.0+ software | — | — | — | — | Low (share-alike) |

### Inferences
- First increment (lowest risk, highest gap coverage): pin ngspice >= 46 (ideally 47), add a gate-driver subcircuit (driver resistance, Miller plateau through a vendor MOSFET model, programmable dead time as an embeddyn parameter or XSPICE digital delay), and enable diode/VDMOS soft recovery. This covers the gate-drive, dead-time, MOSFET/diode model gaps without new dependencies.
- Second increment: the KiCad round trip, done headless with kicad-cli and Sim.* fields. Use reference designators and symbol UUIDs as stable IDs, and test against the known KiCad-vs-standalone ngspice discrepancies (ngspice bug 791).
- Third increment: an averaged H-bridge/inverter model and a motulator-style PMSM/BLDC plant in Python, switchable with the detailed ngspice model at run time or per scenario.
- Thermal and vendor libraries follow naturally once the MOSFET models are in place.
- d_cosim and Xyce are later and optional. Modelica FMUs only if embeddyn needs multi-domain (mechanical or thermal) libraries beyond what Python models give.

### Gaps
- License claims marked "from memory" (ngspice BSD-style, Xyce GPLv3, MTK MIT, SKiDL MIT) need confirmation from the repository LICENSE files.
- No quantitative speed comparison for switching circuits across engines was found. A local benchmark on embeddyn's H-bridge netlist (ngspice default vs KLU, vs Xyce) is recommended.
