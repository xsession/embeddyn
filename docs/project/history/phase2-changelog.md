# Phase 2 Storage Implementation Changelog

## Scope

This release begins Phase 2 of the mechatronic architecture roadmap by adding persistent metadata, immutable artifact storage, and streaming telemetry storage.

## Implemented

### SQLite metadata registry

- explicit schema migration table
- WAL journaling and foreign-key enforcement
- project revision registry
- run lifecycle registry
- resolved parameter snapshots with provenance JSON
- artifact metadata and run-to-artifact links
- telemetry dataset registration
- run listing and inspection APIs

The default local database is:

```text
.embeddyn/database/metadata.sqlite3
```

### Content-addressed artifact store

- SHA-256 object identity
- immutable object paths
- atomic writes
- automatic deduplication
- metadata sidecars
- per-object and whole-store integrity verification

Default layout:

```text
.embeddyn/artifacts/
  sha256/<prefix>/<digest>
  metadata/<digest>.json
```

### Streaming telemetry storage

- incremental buffered writes
- stable schema checks during a run
- Parquet output with Zstandard compression when `pyarrow` is installed
- newline-delimited JSON fallback when `pyarrow` is unavailable
- CSV export kept as an interchange feature
- no requirement to retain all telemetry only in SQLite

### Compiler and run integration

Project compilation now:

- initializes the metadata database
- registers immutable project revisions
- stores normalized project snapshots in the artifact store
- links input artifacts to the run
- records resolved parameters
- includes artifact and telemetry sections in the run manifest

Simulation runs now:

- stream telemetry to the selected storage backend
- register telemetry metadata in SQLite
- content-address the completed telemetry dataset
- link telemetry artifacts to the run
- optionally export CSV with `--output`

### New CLI commands

```bash
embeddyn mechatronics runs-list
embeddyn mechatronics runs-inspect <run-id>
embeddyn mechatronics runs-export <run-id> --output run.csv
embeddyn mechatronics artifacts-verify
```

## Test coverage

Phase 2 tests cover:

- artifact deduplication
- SHA-256 verification
- database run registration
- parameter persistence
- artifact linking
- streaming telemetry round trips
- CSV export
- telemetry metadata registration

## Remaining Phase 2 work

- richer parameter override layers and provenance sources
- component-definition and component-instance persistence
- PostgreSQL repository backend
- telemetry partitioning by domain
- telemetry range-query API
- artifact garbage collection and retention policies
- project bundle import/export
- interrupted-run recovery
- optional remote object-store backend
- database backup and repair commands
