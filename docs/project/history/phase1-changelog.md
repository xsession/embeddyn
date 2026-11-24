# Mechatronic Architecture Phase 1 Changelog

This implementation starts the migration from the prototype flat co-simulation model toward the scalable architecture specification.

## Added

- `embeddyn/mechatronics/quantities.py`: unit-bearing quantity representation and parser.
- `embeddyn/mechatronics/compiler.py`: immutable run compilation, canonical snapshots, lock files, and manifests.
- Schema v2 fields for scheduler, typed signals, parameters, and declarative transforms.
- CLI command: `embeddyn mechatronics compile`.
- Run status recording (`compiled`, `completed`, or `failed`).
- Architecture and implementation-status documentation.
- Tests for quantities, schema v2 compilation, manifests, and unsafe expression rejection.

## Changed

- Existing schema v1 projects continue to load.
- Couplings now execute through typed transform objects.
- Runtime execution always creates a reproducible run directory before simulation.

## Known limitations

- Multi-rate plans are validated and stored, but the current runtime still uses the deterministic macro-step executor.
- Telemetry remains CSV in this phase.
- Parameter override provenance and component-instance resolution are not yet implemented.
