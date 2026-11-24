# Mechatronic feature-pack merge report

## Scope

This merge extends the existing local-first Embeddyn repository rather than replacing its structure. It adds a coherent system-level co-simulation layer for embedded firmware, electrical circuits and mechanical plants.

## Added runtime modules

- `embeddyn/mechatronics/model.py`: project schema, typed signals, couplings and simulator configuration.
- `embeddyn/mechatronics/orchestrator.py`: deterministic fixed-step co-simulation master with delayed feedback and CSV telemetry.
- `embeddyn/mechatronics/adapters/`: Webots, LTspice, ngspice, Renode socket and deterministic mock adapters.
- `embeddyn/mechatronics/kicad.py`: reproducible `kicad-cli` SPICE/BOM export and IPC plugin skeleton generation.
- `embeddyn/mechatronics/cli.py`: validate, run, doctor, KiCad export and plugin commands.

## Integration changes

- Registered `studio` and `mechatronics` command groups in the primary `vsd` CLI.
- Made heavy original Pipeline Manager/Renode commands optional at CLI import time, so lightweight co-simulation and Studio commands work without the full Git-pinned workspace.
- Added a closed-loop DC motor example spanning firmware, electrical and mechanical domains.
- Added a Webots world and Supervisor bridge reference controller.
- Added architecture, quick-start, integration-roadmap and safety documentation.
- Added deterministic unit/integration tests.

## Verification

The feature pack was verified with Python bytecode compilation, project validation, a 501-step deterministic mock co-simulation and the dedicated pytest suite. External Webots, LTspice, ngspice, KiCad and Renode executables were not available in the build container, so their adapters were validated structurally and through mocked boundaries, not by launching those applications.
