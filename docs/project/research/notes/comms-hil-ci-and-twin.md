# Communication, middleware, HIL, CI and digital-twin integration candidates for embeddyn

Scope note: research was done on 2026-11-23 with about 15 web queries. Facts marked [src] have an inline source. Licence and maturity details marked **[bg]** come from background knowledge and were NOT re-checked this session. The report writer should treat them as probable but unverified, or check them before publishing.

---

## CAN / CAN-FD / LIN / Ethernet between emulated ECUs and real tools

### Takeaway
Renode has a native, Linux-only SocketCAN bridge for Classical CAN and CAN-FD. Combined with python-can and cantools, it gives embeddyn a near-zero-effort route from emulated ECUs to vcan/real adapters and to standard tools (candump, Wireshark, SavvyCAN). The open issue is timing: the bridge is not tied to virtual time, so lock-step determinism is lost once frames cross into real time. LIN exists in Renode (1.15.1+) but its host-bridging story is undocumented. No turnkey Renode to ns-3/OMNeT++ co-simulation was found.

### Cited Findings
- Renode connects simulated CAN networks to host SocketCAN interfaces (native, virtual, or SLCAN) through an internal bridge. It is created with `CreateSocketCANBridge` (default `vcan0`) or declared in `.repl` with `canInterfaceName`, `ensureFdFrames`, `ensureXlFrames`. It is Linux only. — [Renode CAN integration docs](https://renode.readthedocs.io/en/latest/host-integration/can.html)
- Classical CAN and CAN-FD frames are forwarded. CAN XL frames are accepted by the bridge but not forwarded, because Renode does not support XL internally yet. — [Renode CAN docs](https://renode.readthedocs.io/en/latest/host-integration/can.html)
- Caveat: two bridges on the same vcan network can create packet loops, so topologies must avoid cycles. The docs say nothing about timing or real-time behaviour of the bridge. — [Renode CAN docs](https://renode.readthedocs.io/en/latest/host-integration/can.html)
- Antmicro demonstrated CAN in Renode with SocketCAN and Wireshark in Nov 2024, using a Nucleo H743ZI demo where packets from vcan0 are routed to a `canHub`. — [Antmicro blog 2024-11](https://antmicro.com/blog/2024/11/demonstrating-can-support-in-renode); [socketcan_bridge demo README](https://codex.ac6.fr/Renode/raw/Renode-v1.15.3-c57714d/scripts/complex/socketcan_bridge/README.rst)
- In Aug 2026, Antmicro ran Toyota's RAMN (an open automotive CAN testbed board) in Renode together with the CARLA driving simulator. Through the bridge, the virtual RAMN can be used like a physical USB-CAN device with standard Linux CAN tools. This is a direct precedent for embeddyn's "emulated ECU + physics sim + real tools" use case. — [Antmicro blog 2026-08](https://antmicro.com/blog/2026/08/toyota-ramn-and-carla-in-renode)
- python-can: the latest stable release is 4.6.1 (12 Aug 2025). Dev docs are at 4.6.2.dev (June 2026), and the unreleased changelog drops Python 3.9. No stable 2026 release was found. — [pepy python-can](https://pepy.tech/project/python-can); [python-can changelog](https://python-can.readthedocs.io/en/main/changelog.html)
- cannelloni tunnels SocketCAN over UDP, TCP or SCTP (C++11). UDP is meant for loss-free links and SCTP for lossy ones. A python-can backend (`can-cannelloni`, TCP client) exists. An ESP32 port exists but is beta and standard-frames only. — [cannelloni (ecosyste.ms)](https://awesome.ecosyste.ms/projects/github.com%2Fmguentner%2Fcannelloni); [can-cannelloni PyPI](https://pypi.org/project/can-cannelloni/); [ESP32cannelloni](https://github.imc.re/MyHomeMyData/ESP32cannelloni)
- Alternative tunnel: Lely `can2udp` (CiA 315 generic frames over UDP). — [Lely can2udp](https://opensource.lely.com/canopen/docs/can2udp/)
- Users report timing distortion when CAN is tunnelled over UDP/VPN. — [can-utils issue 436](https://github.imc.re/linux-can/can-utils/issues/436)
- LIN: the Renode 1.15.1 changelog lists LIN protocol support. How it bridges to the host was not documented in what was found. — [Renode CHANGELOG](https://opensecura.googlesource.com/3p/renode/renode/+/a9c9cecfade9876d487188704948f956e6df7e5d/CHANGELOG.rst)
- Host-side LIN on Linux: the slLIN TTY line discipline maps UART-based LIN to the SocketCAN API. An upstream kernel LIN patch series (hexdev, 2024) was posted to netdev. — [linux-lin](https://github.com/lin-bus/linux-lin); [LWN 2024](https://lwn.net/Articles/972207/); [netdev patchwork](https://patchwork.kernel.org/project/netdevbpf/cover/20240502075534.882628-1-christoph.fritz@hexdev.de/mbox)
- Ethernet/network co-simulation: no Renode + ns-3/OMNeT++ integration was found. Relevant generic frameworks are SimBricks (synchronised ns-3/OMNeT++ adapters), NSB2 (2026 arXiv, app/network co-sim middleware with ns-3/OMNeT++ helpers), and NIST's ns-3 gateway module for runtime exchange with external processes. — [SimBricks](https://arxiv.org/pdf/2012.14219); [NSB2](https://arxiv.org/pdf/2610.35872); [NIST ns-3 gateway](https://catalog-old.data.gov/dataset/gateway-for-co-simulation-using-ns-3)

### Inferences
- **Recommended embeddyn design**: add a "CAN bus" domain in embeddyn that owns a virtual-time-stamped frame queue. It would connect Renode `canHub`s (via pyrenode3 or the Renode monitor) to each other inside the lock-step, and optionally mirror traffic out to `vcan0` with python-can for tools. Out-bound mirroring is harmless. In-bound frames from real tools must be timestamped at the next sync point, which loses determinism, so log them for replay.
- cantools (DBC/KCD/ARXML/SYM decode and encode), canopen (CANopen master/slave in Python) and udsoncan + can-isotp (UDS diagnostics over ISO-TP) all sit on top of python-can. They fit naturally as a "signal layer" for the GUI: DBC signals become plot channels. Licences **[bg]**: python-can LGPL-3.0, cantools MIT, canopen MIT, udsoncan MIT, can-isotp MIT, cannelloni GPL-2.0.
- For Ethernet/TSN, the realistic short-term path is Renode's own internal switch plus TAP to host. ns-3 co-sim would be a research project; SimBricks' sync protocol is the closest design reference.

### Gaps
- The bridge latency/jitter of Renode's SocketCAN bridge and its interaction with `AdvanceImmediately` are undocumented.
- Renode LIN model details and host bridging were not found. The current cantools, canopen and udsoncan versions were not re-checked.
- There was no source on Renode TSN or ns-3 coupling.

---

## UART / serial consoles

### Takeaway
Renode exposes UARTs to the host as a pty (Linux/macOS) or a TCP server socket (all platforms). That is enough for embeddyn to connect a browser terminal (xterm.js over WebSocket) or pyserial/pexpect test scripts.

### Cited Findings
- `emulation CreateUartPtyTerminal "term" "/tmp/uart"` plus `connector Connect sysbus.uart0 term` gives a host pty. `emulation CreateServerSocketTerminal 3456 "term"` gives a TCP port, and the server sends telnet-style setup bytes to new clients unless an optional argument suppresses them. — [Renode UART integration](https://renode.readthedocs.io/en/latest/host-integration/uart.html)

### Inferences
- Use the socket terminal with the telnet bytes suppressed for the browser GUI. That avoids pty portability issues and lets you proxy the port through the existing embeddyn web server. A pty lets real tools (minicom, picocom, a vendor flasher GUI) connect as if to real hardware.
- The same pty or socket is the hook for HIL swap: a real board's `/dev/ttyUSB0` can be presented to embeddyn with the same interface.

### Gaps
- Baud-rate and timing fidelity of Renode UART models to the host were not researched.

---

## ROS 2 / micro-ROS / Zenoh

### Takeaway
Renode can already run micro-ROS-on-Zephyr firmware talking to a host ROS 2 agent (Antmicro demos, 2022). ROS 2 Kilted (May 2025) made rmw_zenoh Tier-1. That makes Zenoh, with zenoh-pico on MCUs, a strong lightweight transport candidate even outside ROS. Embeddyn could expose its own signals on Zenoh/ROS 2 topics without hard-depending on a ROS install.

### Cited Findings
- Antmicro and Zephyr: test-driven development of multi-node Zephyr + micro-ROS in Renode, with a publisher/subscriber demo that publishes into ROS 2 topics on the host OS. — [Zephyr Project blog](https://zephyrproject.org/test-driven-development-of-multi-node-zephyr-micro-ros-solutions-with-renode/)
- A deterministic test setup ran two emulated STM32F4 nodes with micro-ROS plus a Linux node in one Renode simulation (2022, older info). — [Antmicro blog 2022-07](https://antmicro.com/blog/2022/07/fully-deterministic-linux-zephyr-micro-ros-testing-in-renode)
- The micro-ROS Zephyr module docs say it was tested on Zephyr 2.7.0 and 3.1.0. Those versions are old, so expect porting work on current Zephyr 4.x. — [micro_ros_zephyr_module](https://www.github.com/micro-ROS/micro_ros_zephyr_module)
- A third-party project simulates STM32H755 + micro-ROS firmware in Renode. — [rovertech simulation overview](https://rovertech-team.github.io/Stm-Micro_ros-eth/simulation/overview.html)
- ROS 2 Kilted Kaiju is the first release with Eclipse Zenoh as a Tier-1 middleware. — [Kilted release announcement](https://discourse.ros.org/t/ros-2-kilted-kaiju-release/43902)
- Zenoh 1.9.0 "Longwang" was released in 2026 (regions, QUIC multistream, Go binding) and affects rmw_zenoh. Zenoh 1.8.0 packages appeared on the OSRF prerelease repo in April 2026. — [Zenoh 1.9.0 post](https://discourse.openrobotics.org/t/eclipse-zenoh-1-9-0-longwang-released-regions-quic-multistream-go-binding-rmw-zenoh-impact-inside/54135); [rmw_zenoh_pico update](https://discourse.openrobotics.org/t/update-rmw-zenoh-pico-for-zenoh-1-4-0/50364)
- The eSOL community "rmw_zenoh_pico" targets micro-ROS, latest tag 1.4.0. It is not official upstream. — [Reference implementation thread](https://discourse.openrobotics.org/t/reference-implementation-of-zenoh-pico-to-micro-ros/40750); [Integrating ROS 2 with MCUs via Zenoh](https://discourse.ros.org/t/integrating-ros-2-with-microcontrollers-when-using-zenoh/43463)
- ZettaScale says zenoh-nostd lacks liveliness tokens and advanced pub/sub needed for rmw_zenoh interop. — [Hiroz announcement](https://discourse.openrobotics.org/t/introducing-hiroz-by-zettascale/55081)

### Inferences
- **Integration pattern**: an embeddyn "ROS 2 gateway" process (rclpy, or zenoh-python, which needs no ROS install) publishes sim signals and subscribes to commands at each sync step. Put `/clock` on the sim time so ROS nodes run in lock-step (`use_sim_time`). This is the standard ROS mechanism for slaving ROS to a simulator.
- micro-ROS firmware in Renode needs an XRCE-DDS agent on the host, connected over a Renode UART (pty/socket) or emulated Ethernet. That fits embeddyn's existing Renode domain.
- ros2_control: an embeddyn-backed `SystemInterface` hardware plugin (C++) would let ROS controllers drive the simulated plant, then switch to real hardware by changing the plugin. That is a clean HIL-swap story. This is an inference only: no source on an existing generic co-sim ros2_control plugin was gathered.
- Licences **[bg]**: ROS 2/rclpy/micro-ROS Apache-2.0; Zenoh EPL-2.0 OR Apache-2.0.

### Gaps
- No 2024-2026 confirmation that micro-ROS on current Zephyr runs in current Renode.
- The official zenoh-pico 1.x release status and its interop with rmw_zenoh on Kilted are unconfirmed.
- Gazebo-ROS bridge (ros_gz) and ros2_control specifics were not researched this session.

---

## HIL and test automation (swapping an emulated domain for real hardware)

### Takeaway
labgrid (Pengutronix, pytest-native, active with 25.0 in 2025) is the best fit for orchestrating real boards: power, serial, flashing, and remote place reservation. pyOCD and probe-rs are actively released (2026) and scriptable for flashing and debugging. Renode's own CI uses Robot Framework. libsigrok appears stagnant (no upstream release found since 0.5.2). For a lock-step co-sim, real hardware breaks determinism, so the HIL swap must switch embeddyn into a wall-clock-paced mode.

### Cited Findings
- labgrid is an "embedded board control python library with a focus on testing, development and general automation", intended to be combined with pytest. Its pytest plugin runs tests on real hardware. — [labgrid GitHub](https://github.com/labgrid-project/labgrid)
- labgrid 25.0 was released 7 May 2025, and 25.0.1 shows in Debian in Oct 2025. — [labgrid docs v25.0](https://labgrid.readthedocs.io/en/v25.0/index.html); [Debian NEW 25.0.1](https://ftp-master.debian.org/new/labgrid_25.0.1-1.html)
- Pengutronix ran a labgrid demonstrator at Embedded World 2025 and a hands-on training at Embedded Recipes 2025. — [Pengutronix EW2025](https://pengutronix.de/en/blog/2025-02-28-pengutronix-at-the-embedded-world-2025.html); [ER2025 training](https://pengutronix.de/en/blog/2025-04-10-labgrid-hands-on-training-at-embedded-recipes-2025.html)
- pyOCD releases: 0.42.0 (16 Dec 2025), 0.43.0 (Feb 2026), 0.44.0 (1 Apr 2026, adds Open-CMSIS-Pack default debug sequences). — [pyOCD 0.44.0](https://pyocd.io/posts/2026/04-01-pyocd-0.44.0-released.html); [pyOCD 0.43.0](https://pyocd.io/posts/2026/02-23-pyocd-0.43.0-released.html)
- probe-rs 0.31.0 was released 17 Jan 2026, and docs.rs indexes 0.32.0. — [probe-rs v0.31.0](https://github.com/probe-rs/probe-rs/releases/tag/v0.31.0); [docs.rs probe-rs](https://docs.rs/crate/probe-rs/latest)
- libsigrok: Debian is at 0.5.2 (last upgraded 2024). No 2025-2026 upstream release was found. — [Debian libsigrok](https://tracker.debian.org/pkg/libsigrok)
- Renode test flows use Robot Framework keywords. Antmicro combines Zephyr Twister + Renode + Robot Framework. — [Antmicro blog 2023-05](https://antmicro.com/blog/2023/05/building-advanced-testing-suites-with-zephyr-twister-renode-and-robot-framework)
- Renode real-time behaviour: by default Renode slows down when virtual time runs ahead of real time. `AdvanceImmediately` disables pacing. Pausing any time-domain member blocks virtual time for all. Quanta are fixed between sync phases, and a zero quantum is illegal. — [Renode time framework](https://renode.readthedocs.io/en/latest/advanced/time_framework.html)
- Renode programmatic control: pyrenode3 (Python.NET CLR bindings, a full Python wrapper of Renode). A C external-control client exposes `renode_run_for` and `renode_get_current_time` on virtual time. — [pyrenode3 blog](https://antmicro.com/blog/2023/11/python-driven-automation-and-scripting-in-renode-with-pyrenode3/); [External control API](https://renode.readthedocs.io/en/latest/api-description/external-control.html)

### Inferences
- **Safe HIL swap pattern for embeddyn**: (1) define each domain behind one interface (step(dt), get/set signals). (2) A "hardware proxy" domain implements that interface against real I/O (serial, CAN via python-can, GPIO/ADC via a cheap MCU board, instruments via PyVISA). (3) When any real domain is present, switch the master clock from "as fast as possible" to wall-clock pacing, and record overruns. (4) Add safety interlocks: watchdog on the proxy, limits on actuator commands, an e-stop signal, and a dry-run mode using pyvisa-sim or a Renode stand-in. labgrid handles power-cycling and serial; pyOCD/probe-rs handle flashing the same ELF that runs in Renode.
- Low-cost HIL I/O: an RP2040 or STM32 board running simple firmware that exposes GPIO, PWM and ADC over USB-CDC, driven by a Python driver. This is an inference/design suggestion; no specific off-the-shelf open project was verified this session.
- Licences **[bg]**: labgrid LGPL-2.1+; pytest-embedded (Espressif) MIT; OpenHTF (Google) Apache-2.0; Robot Framework Apache-2.0; probe-rs MIT/Apache-2.0; pyOCD Apache-2.0; OpenOCD GPL-2.0; Black Magic Probe GPL-3.0; libsigrok GPL-3.0 (sigrok-cli/PulseView GPL-3.0); PyVISA and pyvisa-sim MIT.

### Gaps
- pytest-embedded, OpenHTF, PyVISA/pyvisa-sim and Black Magic current versions and activity were not checked.
- Whether sigrok has a 2025/2026 release outside Debian is unconfirmed; check the sigrok git and nightly builds.
- No source on BeagleBone- or RP2040-based open HIL I/O projects.

---

## Digital twin and IoT (Ditto, BaSyx/AAS, OPC UA, MQTT, Node-RED)

### Takeaway
BaSyx Python SDK 2.x (Dec 2025, AAS metamodel v3.1) is a fitting, pure-Python way to describe embeddyn models as Asset Administration Shells. Eclipse Ditto (3.x, JVM, heavyweight) suits device-fleet twin sync rather than a desktop co-sim tool. For live sync with real devices, MQTT (Mosquitto + paho) and OPC UA (asyncua / open62541) are the pragmatic transports.

### Cited Findings
- BaSyx Python SDK: 1.2.0 (2025-04-24), 2.0.0 (2025-12-05), and 2.1.0, which aligns with AAS spec v3.1 and implements Part 1 Metamodel v3.1.2. Version 1.0.0 had Metamodel v3.0 and no Part 2 API. — [BaSyx 2.1.0 Python](https://projects.eclipse.org/node/35987); [BaSyx 1.0.0 review](https://projects.eclipse.org/projects/dt.basyx/releases/1.0.0-python/review)
- Eclipse Ditto 3.6.0 (Oct 2024) adds validation against linked WoT Thing Models. The Ditto feed mentions a 3.9.0 release, details unverified. The Python client (`ditto-client` 0.3.0 on PyPI) has no GitHub releases, so it is low maturity. — [Ditto 3.6.0 notes](https://eclipse.dev/ditto/release_notes_360.html); [Ditto feed](https://eclipse.dev/ditto/feed.xml); [ditto-client PyPI](https://pypi.org/project/ditto-client/); [ditto-clients-python](https://github.com/eclipse/ditto-clients-python)

### Inferences
- **Fit for embeddyn**: (a) An MQTT bridge (paho-mqtt) publishing selected signals at sync points, with Node-RED or Ditto consuming them, is the cheapest "live twin" path and works with real devices that already speak MQTT. (b) An OPC UA server (asyncua) embedded in embeddyn exposes the sim as an industrial asset, so SCADA/PLC tools can read and write it. Mapping sim time to OPC UA SourceTimestamp keeps traces comparable. (c) AAS via BaSyx gives model metadata and interchange (nameplate, technical data, sim-model submodels), not real-time data.
- Ditto's WoT Thing Model support could double as a schema for embeddyn device signals.
- Licences **[bg]**: Ditto EPL-2.0; BaSyx Python SDK MIT; open62541 MPL-2.0; asyncua (opcua-asyncio) LGPL-3.0; Mosquitto EPL-2.0/EDL-1.0; paho-mqtt EPL-2.0/EDL-1.0; Node-RED Apache-2.0.

### Gaps
- The current versions of asyncua, open62541, Mosquitto and Node-RED (2025-2026) were not checked. The latest Ditto version is unconfirmed (3.9.0 seen only in a feed title).

---

## Telemetry storage and dashboards

### Takeaway
For embeddyn, MCAP files plus Rerun or Foxglove/PlotJuggler for viewing are the best fit for run logs. MCAP is self-describing and timestamped, and Rerun reads it natively since 0.30. Parquet/Arrow suits post-run analysis, and InfluxDB 3 Core (MIT/Apache, GA April 2025) + Grafana suits live, long-running twin telemetry.

### Cited Findings
- InfluxDB 3 Core is dual MIT/Apache-2.0 and has been GA since April 2025 (alpha Jan 2025). Wikipedia lists 3.9.2 (30 Apr 2026), unverified. Core has a 5-database limit and no compactor; Enterprise is commercial. — [InfluxData GA blog](https://www.influxdata.com/blog/influxdb-3-oss-ga); [InfluxDB 3 alpha blog](https://influxdata.com/blog/influxdb3-open-source-public-alpha); [Wikipedia InfluxDB](https://en.wikipedia.org/wiki/InfluxDB)
- Foxglove SDK (shared Rust core; C++, Python, Rust; Python 3.10+) first shipped at v0.7.0, and the changelog has reached v0.10.0 with breaking changes. Foxglove schema packages are MIT. The Foxglove 2.0 app went closed-source/commercial, which the community discussed. — [foxglove-sdk PyPI](https://pypi.org/project/foxglove-sdk/); [Foxglove SDK changelog](https://docs.foxglove.dev/changelog/tags/sdk); [Foxglove 2.0 thread](https://discourse.openrobotics.org/t/foxglove-2-0-integrated-ui-new-pricing-and-open-source-changes/36583); [Using the Foxglove SDK to generate MCAP](https://foxglove.dev/blog/using-the-foxglove-sdk-to-generate-mcap)
- Rerun: rerun-sdk is in the 0.3x line in 2026 (0.35.1rc1 on PyPI). 0.30 added Foxglove Protobuf schemas to its MCAP loader, and 0.32 made the MCAP decoder multithreaded and added a PyTorch dataloader. — [rerun-sdk 0.35.1rc1](https://pypi.org/project/rerun-sdk/0.35.1rc1/); [Rerun 0.32.0](https://sourceforge.net/projects/rerun.mirror/files/0.32.0/); [Rerun 0.30.0](https://sourceforge.net/projects/rerun.mirror/files/0.30.0)

### Inferences
- **Recommended**: embeddyn writes one MCAP per run, with channels per domain (Renode GPIO/UART/CAN frames, ngspice node voltages, mechanics state) on sim-time timestamps. Viewers include Rerun (open, embeddable web viewer), PlotJuggler (open, desktop), and Foxglove (free tier, commercial app). Export Parquet for pandas/polars analysis. Use InfluxDB 3 Core/QuestDB/TimescaleDB + Grafana only for the live-twin deployment.
- Licences **[bg]**: MCAP MIT; Rerun MIT/Apache-2.0; PlotJuggler MPL-2.0; Apache Arrow/Parquet Apache-2.0; Grafana AGPL-3.0; QuestDB Apache-2.0; TimescaleDB Apache-2.0 core + Timescale License for some features.

### Gaps
- The Foxglove SDK licence was not confirmed (likely MIT, unverified). Current QuestDB, TimescaleDB, PlotJuggler and Grafana versions were not checked.

---

## CI: reproducible images (Renode, ngspice, Webots, Nix)

### Takeaway
Use `antmicro/renode-test-action@v5` for Robot-based Renode tests, or the `antmicro/renode` Docker image (tags nightly, latest, 1.16.1) as a base, adding ngspice from distro packages. The Webots image is stale-ish (`R2025a-ubuntu22.04`) and GPU-oriented, so headless CI needs `--batch` plus Xvfb.

### Cited Findings
- `antmicro/renode` Docker image: built on Microsoft .NET runtime, installs a Renode release package. Tags include nightly, latest and 1.16.1. — [Docker Hub antmicro/renode](https://hub.docker.com/r/antmicro/renode); [tags](https://hub.docker.com/r/antmicro/renode/tags)
- `antmicro/renode-test-action` (v5) runs Robot Framework tests with Renode keywords and caches Renode builds. The older `antmicro/renode-actions` is obsolete. `renode-linux-runner-action` exists for running commands in emulated Linux. — [Antmicro blog 2024-10](https://antmicro.com/blog/2024/10/renode-github-action-for-automated-testing-in-simulation); [renode-actions (obsolete)](https://www.github.com/antmicro/renode-actions); [renode-linux-runner-action](https://github.com/antmicro/renode-linux-runner-action); [marketplace note 2026-01](https://devops-actions.github.io/github-actions-marketplace-news/blog/2026/01/13/run-tests-in-renode)
- `cyberbotics/webots` is a GPU-accelerated Ubuntu image, latest listed tag `R2025a-ubuntu22.04`, with an old `latest` tag, so pin a version. Headless running uses `webots --batch <world>`. — [Docker Hub cyberbotics/webots](https://hub.docker.com/r/cyberbotics/webots/tags); [Running Webots in Docker](https://github.com/cyberbotics/webots-cloud/wiki/Running-Webots-in-a-Docker-container)

### Inferences
- **Recommended**: one multi-stage `embeddyn-ci` image: FROM `antmicro/renode:<pinned>`, plus `libngspice0`, plus a Python venv with embeddyn, python-can, cantools, mcap. A separate optional Webots image, run under `xvfb-run`. Pin digests for reproducibility. On GitHub runners, `vcan` may not be loadable (kernel modules), so CAN tests should use Renode-internal hubs or python-can's `virtual` interface in CI. That is an inference, not verified.
- Nix: nixpkgs packages for renode and ngspice would give bit-reproducible dev shells. Not verified this session.
- Licences **[bg]**: Renode MIT; ngspice modified BSD (mixed); Webots Apache-2.0.

### Gaps
- No verification of Nix packaging state for Renode, ngspice or Webots, of Webots CI without GPU, or of vcan availability on GitHub-hosted runners.

---

## Recommendation: prioritized shortlist for embeddyn

### Takeaway
Priority order by value versus effort for a lock-step co-sim that must also bridge to real-time tools:
1. **python-can + cantools** with an embeddyn CAN domain and Renode SocketCAN mirroring.
2. **Renode UART socket/pty to browser terminal** (xterm.js).
3. **MCAP run logs + Rerun/PlotJuggler**, with Parquet export.
4. **Docker CI image based on antmicro/renode** plus renode-test-action.
5. **ROS 2 / Zenoh gateway** with sim-time `/clock`.
6. **HIL proxy domain using labgrid + pyOCD/probe-rs + PyVISA**, with wall-clock pacing and safety interlocks.
7. **MQTT / OPC UA (asyncua) live-twin bridge**.
8. Later: BaSyx AAS export, InfluxDB 3/Grafana, Ditto, ns-3/SimBricks network co-sim.

### Cited Findings
- Precedent for emulated ECU + physics sim + real CAN tools: Renode + RAMN + CARLA (2026). — [Antmicro blog 2026-08](https://antmicro.com/blog/2026/08/toyota-ramn-and-carla-in-renode)
- Renode's real-time pacing and pause semantics, which constrain bridging: [Renode time framework](https://renode.readthedocs.io/en/latest/advanced/time_framework.html)

### Inferences
- The key architectural rule: **inside the lock-step, everything is virtual-time-stamped and deterministic. Every bridge to a real-time tool or device is a "boundary adapter" that either only observes (safe, deterministic) or injects at sync points (non-deterministic, must be logged for replay).** Once any real hardware is in the loop, switch the master scheduler to wall-clock pacing (Renode's default slow-down behaviour helps) and monitor `CurrentLoad` for overruns.
- Comparison summary (licence **[bg]** unless cited; RT = real-time behaviour):

| Candidate | Licence | Maturity/activity | Python API | RT behaviour | Lock-step fit |
|---|---|---|---|---|---|
| Renode SocketCAN bridge | MIT [bg] | Active, docs updated 2026 | via pyrenode3/monitor | wall-clock, untimed | Good for observe; inject = nondeterministic |
| python-can / cantools | LGPL-3 / MIT [bg] | 4.6.1 (Aug 2025), dev ongoing [src] | native | host-timed | Excellent as signal layer |
| cannelloni | GPL-2 [bg] | stable, small | python-can backend | UDP jitter | Remote real-tool bridge only |
| rclpy / rmw_zenoh | Apache-2 / EPL-2+Apache-2 [bg] | Zenoh Tier-1 since Kilted [src] | native | soft RT | Good with `/clock` sim time |
| micro-ROS in Renode | Apache-2 [bg] | demos 2022, stale [src] | via agent | virtual time | Good, needs porting check |
| labgrid | LGPL-2.1 [bg] | 25.0 (2025) [src] | native, pytest | real HW | HIL orchestration |
| pyOCD / probe-rs | Apache-2 / MIT-Apache [bg] | 0.44 (Apr 2026) / 0.31 (Jan 2026) [src] | native / CLI+Rust (Python bindings unverified) | real HW | flashing/debug in HIL |
| sigrok | GPL-3 [bg] | upstream release stagnant [src] | limited bindings | real HW | capture only |
| BaSyx Python SDK | MIT [bg] | 2.1 (AAS 3.1) [src] | native | n/a | metadata/interchange |
| Ditto | EPL-2 [bg] | 3.x active [src] | weak client [src] | cloud, soft | fleet twin, heavy |
| asyncua / open62541 | LGPL-3 / MPL-2 [bg] | active [bg] | native / C | soft RT | live twin to industrial tools |
| MQTT (Mosquitto/paho) | EPL-2 [bg] | very mature [bg] | native | soft | simplest live twin |
| MCAP + Rerun | MIT / MIT-Apache [bg] | Rerun 0.3x 2026 [src] | native | offline/live | excellent run logs |
| InfluxDB 3 Core + Grafana | MIT-Apache [src] / AGPL [bg] | GA Apr 2025 [src] | client libs | live | live twin dashboards |
| antmicro/renode image + renode-test-action | MIT [bg] | v5 action active [src] | n/a | n/a | CI base |
| cyberbotics/webots image | Apache-2 [bg] | R2025a tag [src] | n/a | GPU/X needed | optional CI |

### Gaps
- The prioritisation is the researcher's judgement from the evidence above. There are no benchmarks of bridge latency or throughput for any candidate inside a lock-step loop.
