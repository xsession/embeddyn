from __future__ import annotations

import hashlib
import json
import platform
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .model import MechatronicProject
from .storage import ContentAddressedStore, MetadataDatabase


@dataclass(frozen=True)
class CompiledRun:
    run_id: str
    directory: Path
    manifest_path: Path
    lock_path: Path
    database_path: Path
    artifact_root: Path


class ProjectCompiler:
    """Compile editable project data into immutable, inspectable run inputs."""

    def compile(self, project: MechatronicProject, output_root: Path) -> CompiledRun:
        output_root = output_root.resolve()
        state_root = output_root.parent
        database_path = state_root / "database" / "metadata.sqlite3"
        artifact_root = state_root / "artifacts"
        database = MetadataDatabase(database_path)
        artifact_store = ContentAddressedStore(artifact_root)
        run_id = self._new_run_id()
        run_dir = output_root / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        (run_dir / "signals").mkdir()
        (run_dir / "logs").mkdir()
        (run_dir / "artifacts").mkdir()

        project_snapshot = self._project_snapshot(project)
        snapshot_bytes = _canonical_json(project_snapshot)
        project_digest = _sha256(snapshot_bytes)
        snapshot_path = run_dir / "project.normalized.json"
        snapshot_path.write_bytes(snapshot_bytes + b"\n")
        snapshot_artifact = artifact_store.add_file(snapshot_path, created_by="project-compiler", media_type="application/json")

        lock = {
            "lock_version": 1,
            "project_revision": project_digest,
            "schema_version": project.schema_version,
            "simulators": {
                name: {
                    "model": cfg.model,
                    "executable": cfg.executable,
                    "options": cfg.options,
                    **({"domain": cfg.domain.value} if name != cfg.domain.value else {}),
                    **({"step_seconds": cfg.step_seconds} if cfg.step_seconds is not None else {}),
                }
                for name, cfg in sorted(project.declared_instances().items())
            },
        }
        lock_path = run_dir / "project.lock.json"
        lock_path.write_bytes(_canonical_json(lock) + b"\n")

        manifest = {
            "manifest_version": 1,
            "run_id": run_id,
            "project": project.name,
            "project_revision": project_digest,
            "schema_version": project.schema_version,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "scheduler": {
                "algorithm": project.scheduler.algorithm if project.scheduler else "deterministic-fixed-step",
                "macro_step_seconds": project.step_seconds,
                "domain_steps": {
                    domain.value: step for domain, step in (project.scheduler.domain_steps if project.scheduler else {}).items()
                },
                "convergence": asdict(project.scheduler.convergence) if project.scheduler else {},
                "real_time": asdict(project.scheduler.real_time) if project.scheduler else {},
            },
            "resolved_parameters": {
                key: {**value.to_dict(), "provenance": project.parameter_provenance.get(key, [])}
                for key, value in sorted(project.parameters.items())
            },
            "signal_schema_digest": _sha256(_canonical_json(project_snapshot["signals"])),
            "random_seed": 0,
            "host": {"system": platform.system(), "machine": platform.machine(), "python": platform.python_version()},
            "artifacts": {"project_snapshot": snapshot_artifact.to_dict()},
            "telemetry": {},
            "result_status": "compiled",
        }
        manifest_path = run_dir / "manifest.json"
        manifest_path.write_bytes(_canonical_json(manifest) + b"\n")
        database.register_project(project_digest, project.name, project.schema_version, snapshot_path)
        database.register_components(project_digest, project.component_definitions, project.component_instances)
        database.create_run(run_id, project_digest, run_dir, manifest_path)
        database.save_parameters(run_id, manifest["resolved_parameters"])
        database.register_artifact(snapshot_artifact)
        database.link_artifact(run_id, snapshot_artifact.digest, "input", "project_snapshot")
        return CompiledRun(run_id, run_dir, manifest_path, lock_path, database_path, artifact_root)

    @staticmethod
    def update_status(compiled: CompiledRun, status: str, **extra: Any) -> None:
        manifest = json.loads(compiled.manifest_path.read_text(encoding="utf-8"))
        manifest["result_status"] = status
        manifest.update(extra)
        compiled.manifest_path.write_bytes(_canonical_json(manifest) + b"\n")
        MetadataDatabase(compiled.database_path).update_run(compiled.run_id, status, str(extra.get("error")) if extra.get("error") else None)

    @staticmethod
    def _project_snapshot(project: MechatronicProject) -> dict[str, Any]:
        return {
            "name": project.name,
            "schema_version": project.schema_version,
            "duration_seconds": project.duration_seconds,
            "step_seconds": project.step_seconds,
            "scheduler": {
                "algorithm": project.scheduler.algorithm if project.scheduler else "deterministic-fixed-step",
                "macro_step_seconds": project.step_seconds,
                "domain_steps": {domain.value: step for domain, step in (project.scheduler.domain_steps if project.scheduler else {}).items()},
            },
            "parameters": {key: {**value.to_dict(), "provenance": project.parameter_provenance.get(key, [])}
                           for key, value in sorted(project.parameters.items())},
            "components": {"definitions": project.component_definitions, "instances": project.component_instances},
            "simulators": {
                name: {"enabled": cfg.enabled, "executable": cfg.executable, "model": cfg.model, "options": cfg.options,
                       **({"domain": cfg.domain.value} if name != cfg.domain.value else {}),
                       **({"step_seconds": cfg.step_seconds} if cfg.step_seconds is not None else {})}
                for name, cfg in sorted(project.declared_instances().items())
            },
            "signals": {
                name: {
                    "domain": signal.domain.value,
                    "kind": signal.kind.value,
                    "data_type": signal.data_type,
                    "unit": signal.unit,
                    "quantity": signal.quantity,
                    "initial": signal.initial,
                    "minimum": signal.minimum,
                    "maximum": signal.maximum,
                    "retention": signal.retention,
                }
                for name, signal in sorted(project.signals.items())
            },
            "couplings": [
                {
                    "source": c.source,
                    "target": c.target,
                    "transform": asdict(c.transform),
                    "delay_steps": c.delay_steps,
                    "interpolation": c.interpolation,
                    "extrapolation": c.extrapolation,
                    **({"parameters": dict(c.references)} if c.references else {}),
                }
                for c in project.resolved_couplings()
            ],
        }

    @staticmethod
    def _new_run_id() -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        return f"{timestamp}-{uuid.uuid4().hex[:8]}"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()
