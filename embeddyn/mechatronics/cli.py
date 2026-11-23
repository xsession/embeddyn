from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Optional

import typer

from .compiler import ProjectCompiler
from .factory import build_adapters
from .kicad import KiCadBridge
from .catalog import ComponentCatalog
from .integration import validate_integrations
from .metrics import compute_metrics, parse_metric_specs, select_metrics
from .model import MechatronicProject
from .orchestrator import CoSimulationOrchestrator
from .runtime import CheckpointManager, verify_replay
from .storage import ContentAddressedStore, MetadataDatabase, StructuredRecordWriter, TelemetryWriter, export_telemetry_csv
from .distributed import DistributedCoordinator, JobSpec, WorkerAgent, WorkerCapabilities, WorkerRecord, load_experiment
from .hil import HilSafetyPolicy
import platform
import uuid
import yaml

mechatronics_app = typer.Typer(name="mechatronics", help="Coupled firmware, electrical and mechanical simulation.", no_args_is_help=True)


@mechatronics_app.command("compile")
def compile_project(
    project_file: Path = typer.Argument(..., exists=True, dir_okay=False),
    output_root: Path = typer.Option(Path(".embeddyn/runs"), "--output-root"),
) -> None:
    project = MechatronicProject.load(project_file)
    compiled = ProjectCompiler().compile(project, output_root)
    typer.echo(json.dumps({"run_id": compiled.run_id, "directory": str(compiled.directory),
                           "manifest": str(compiled.manifest_path), "lock": str(compiled.lock_path)}, indent=2))


@mechatronics_app.command("run")
def run_project(
    project_file: Path = typer.Argument(..., exists=True, dir_okay=False),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Optional CSV export path."),
    output_root: Path = typer.Option(Path(".embeddyn/runs"), "--output-root"),
    mock: bool = typer.Option(False, help="Use deterministic built-in simulators for all domains."),
    isolate_adapters: bool = typer.Option(False, "--isolate-adapters", help="Run supported adapters in supervised child processes."),
    checkpoint_interval: Optional[float] = typer.Option(None, "--checkpoint-interval", help="Checkpoint interval in seconds."),
    resume_from: Optional[Path] = typer.Option(None, "--resume-from", exists=True, dir_okay=False),
    parameter_overrides: Optional[str] = typer.Option(None, "--parameter-overrides", help="JSON object of resolved parameter overrides."),
) -> None:
    project = MechatronicProject.load(project_file)
    if parameter_overrides:
        try:
            overrides = json.loads(parameter_overrides)
        except json.JSONDecodeError as error:
            raise typer.BadParameter(f"invalid --parameter-overrides JSON: {error}") from error
        if not isinstance(overrides, dict):
            raise typer.BadParameter("--parameter-overrides must decode to a JSON object")
        project.apply_parameter_overrides(overrides, source="cli-override")
    compiler = ProjectCompiler()
    compiled = compiler.compile(project, output_root)
    writer = TelemetryWriter(compiled.directory / "signals" / "telemetry")
    event_writer = StructuredRecordWriter(compiled.directory / "signals" / "events")
    bus_writer = StructuredRecordWriter(compiled.directory / "signals" / "buses")
    try:
        adapters = build_adapters(project, mock=mock, isolate=isolate_adapters)
        runtime = CoSimulationOrchestrator(project, adapters)
        checkpoint_manager = CheckpointManager(compiled.directory / "checkpoints") if checkpoint_interval else None
        rows = runtime.run(
            telemetry_writer=writer,
            checkpoint_manager=checkpoint_manager,
            checkpoint_interval_seconds=checkpoint_interval,
            resume_from=resume_from,
            event_writer=event_writer,
            bus_writer=bus_writer,
        )
        database = MetadataDatabase(compiled.database_path)
        database.register_telemetry(compiled.run_id, "combined", writer.format, writer.path, writer.row_count, writer.schema)
        database.register_telemetry(compiled.run_id, "events", event_writer.format, event_writer.path, event_writer.row_count, event_writer.schema)
        database.register_telemetry(compiled.run_id, "buses", bus_writer.format, bus_writer.path, bus_writer.row_count, bus_writer.schema)
        artifact_store = ContentAddressedStore(compiled.artifact_root)
        telemetry_artifact = artifact_store.add_file(writer.path, created_by="telemetry-writer")
        database.register_artifact(telemetry_artifact)
        database.link_artifact(compiled.run_id, telemetry_artifact.digest, "telemetry", "combined")
        extra_telemetry = {}
        for logical_name, structured_writer in (("events", event_writer), ("buses", bus_writer)):
            record = artifact_store.add_file(structured_writer.path, created_by=f"{logical_name}-writer", media_type="application/x-ndjson")
            database.register_artifact(record)
            database.link_artifact(compiled.run_id, record.digest, "telemetry", logical_name)
            extra_telemetry[logical_name] = {"path": str(structured_writer.path), "format": structured_writer.format,
                                            "rows": structured_writer.row_count, "artifact": record.to_dict()}
        if checkpoint_manager is not None:
            for checkpoint_path in sorted((compiled.directory / "checkpoints").glob("*.json")):
                record = artifact_store.add_file(checkpoint_path, created_by="checkpoint-manager", media_type="application/json")
                database.register_artifact(record)
                database.link_artifact(compiled.run_id, record.digest, "checkpoint", checkpoint_path.name)
                try:
                    checkpoint_time = float(json.loads(checkpoint_path.read_text(encoding="utf-8"))["time_seconds"])
                except Exception:
                    checkpoint_time = 0.0
                database.register_checkpoint(compiled.run_id, checkpoint_time, record.digest, checkpoint_path)
        csv_export = None
        if output is not None:
            csv_export = export_telemetry_csv(writer.path, output.resolve())
        metrics = compute_metrics(rows, parse_metric_specs(project.metrics)) if project.metrics else {}
        if metrics:
            (compiled.directory / "metrics.json").write_text(json.dumps(metrics, indent=2, sort_keys=True), encoding="utf-8")
        compiler.update_status(
            compiled,
            "completed",
            telemetry={
                "combined": {
                    "path": str(writer.path), "format": writer.format, "rows": writer.row_count,
                    "artifact": telemetry_artifact.to_dict(),
                },
                **extra_telemetry,
            },
            csv_export=str(csv_export) if csv_export else None,
            steps=len(rows),
            metrics=metrics,
            runtime={
                "activation_count": dict(runtime.activation_count),
                "isolated_adapters": isolate_adapters,
                "resumed_from": str(resume_from) if resume_from else None,
            },
        )
    except Exception as exc:
        writer.close()
        compiler.update_status(compiled, "failed", error=str(exc))
        raise
    typer.echo(json.dumps({
        "project": project.name,
        "run_id": compiled.run_id,
        "steps": len(rows),
        "telemetry": str(writer.path),
        "telemetry_format": writer.format,
        "csv_export": str(csv_export) if csv_export else None,
        "database": str(compiled.database_path),
        "manifest": str(compiled.manifest_path),
        "final": rows[-1] if rows else {},
        "metrics": metrics,
        "activation_count": dict(runtime.activation_count),
        "isolated_adapters": isolate_adapters,
    }, indent=2))


@mechatronics_app.command("metrics")
def metrics_command(
    project_file: Path = typer.Argument(..., exists=True, dir_okay=False),
    run_csv: Path = typer.Argument(..., exists=True, dir_okay=False, help="CSV from `run --output`."),
) -> None:
    """Compute the project's declared metrics from an exported run CSV."""
    import csv as _csv
    project = MechatronicProject.load(project_file)
    with run_csv.open(newline="", encoding="utf-8") as handle:
        rows = [{key: float(value) for key, value in row.items() if value not in ("", None)}
                for row in _csv.DictReader(handle)]
    typer.echo(json.dumps(compute_metrics(rows, parse_metric_specs(project.metrics)), indent=2, sort_keys=True))


@mechatronics_app.command("tune")
def tune_command(
    spec_file: Path = typer.Argument(..., exists=True, dir_okay=False, help="Tuning spec (YAML)."),
    output: Path = typer.Option(Path(".embeddyn/tuning/result.json"), "--output", "-o"),
    max_evaluations: Optional[int] = typer.Option(None, "--max-evaluations"),
) -> None:
    """Tune project parameters by repeated co-simulation (bounded Nelder-Mead)."""
    from .tuning import load_tuning_spec, tune, write_result
    spec = load_tuning_spec(spec_file)
    if max_evaluations is not None:
        spec.max_evaluations = max_evaluations

    def progress(record):
        values = ", ".join(f"{name}={value:.5g}" for name, value in record["parameters"].items())
        note = record.get("error") or "; ".join(record["violations"]) or "ok"
        typer.echo(f"[{record['evaluation']:3d}] objective={record['objective']:.6g}  {values}  ({note}, {record['seconds']} s)", err=True)

    result = tune(spec, progress)
    write_result(result, output)
    typer.echo(json.dumps({"output": str(output), "evaluations": result["evaluations"], "feasible": result["feasible"],
                           "initial": {"parameters": result["initial"]["parameters"], "objective": result["initial"]["objective"]},
                           "best": {key: result["best"][key] for key in ("parameters", "objective", "violations")}},
                          indent=2, default=lambda value: None))


@mechatronics_app.command("verify")
def verify_command(
    project_file: Path = typer.Argument(..., exists=True, dir_okay=False),
    check: Optional[list[str]] = typer.Option(None, "--check", help="Run only these checks: energy, convergence, golden, scenarios."),
    mock: bool = typer.Option(False, help="Use the built-in simulators for the core domains."),
    update_golden: bool = typer.Option(False, "--update-golden", help="Rewrite the golden waveform file."),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Write the JSON report here."),
) -> None:
    """Verify a model: energy balance, step-size convergence, golden waveforms, scenario matrix.
    Exits with status 1 when a check fails."""
    from .verification import verify

    def progress(name, result):
        if name == "scenario":
            note = result.get("error") or ", ".join(
                f"{c['metric']}={c['value'] if c['value'] is None else round(c['value'], 4)}" for c in result["checks"])
            typer.echo(f"  scenario {result['name']}: {'PASS' if result['passed'] else 'FAIL'} ({note})", err=True)
        else:
            typer.echo(f"{name}: {'PASS' if result['passed'] else 'FAIL'}", err=True)

    report = verify(project_file, mock=mock, only=check, update_golden=update_golden, progress=progress)
    text = json.dumps(report, indent=2, default=lambda value: None)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    typer.echo(text)
    if not report["passed"]:
        raise typer.Exit(1)


@mechatronics_app.command("estimate")
def estimate_command(
    spec_file: Path = typer.Argument(..., exists=True, dir_okay=False, help="Estimation spec (YAML)."),
    output: Path = typer.Option(Path(".embeddyn/estimation/result.json"), "--output", "-o"),
) -> None:
    """Estimate physical parameters from bench measurements (Levenberg-Marquardt, 95 % confidence intervals)."""
    from .estimation import estimate, load_estimation_spec, write_result
    spec = load_estimation_spec(spec_file)

    def progress(record):
        values = ", ".join(f"{name}={value:.5g}" for name, value in record["parameters"].items())
        typer.echo(f"[{record['iteration']:2d}] cost={record['cost']:.6g}  {values}  ({record['evaluations']} runs)", err=True)

    result = estimate(spec, progress)
    write_result(result, output)
    summary = {name: {"estimate": item["estimate"], "ci95": item["ci95"], "at_bound": item["at_bound"]}
               for name, item in result["parameters"].items()}
    typer.echo(json.dumps({"output": str(output), "parameters": summary, "fit": result["fit"],
                           "cost": result["cost"], "evaluations": result["evaluations"]},
                          indent=2, default=lambda value: None))


@mechatronics_app.command("tolerance")
def tolerance_command(
    spec_file: Path = typer.Argument(..., exists=True, dir_okay=False, help="Tolerance spec (YAML)."),
    output: Path = typer.Option(Path(".embeddyn/tolerance/result.json"), "--output", "-o"),
    workers: Optional[int] = typer.Option(None, "--workers", help="Parallel processes (overrides the spec)."),
    samples: Optional[int] = typer.Option(None, "--samples", help="Monte Carlo samples (overrides the spec)."),
) -> None:
    """Monte Carlo yield and Sobol sensitivity over component tolerances."""
    from .tolerance import analyse, load_tolerance_spec, write_result
    spec = load_tolerance_spec(spec_file)
    if workers is not None:
        spec.workers = max(1, workers)
    if samples is not None and spec.monte_carlo is not None:
        spec.monte_carlo["samples"] = samples
    step = {"monte_carlo": 0, "sensitivity": 0}

    def progress(phase, index, total):
        if index == total or index - step[phase] >= max(1, total // 10):
            step[phase] = index
            typer.echo(f"{phase}: {index}/{total} runs", err=True)

    result = analyse(spec, progress)
    write_result(result, output)
    summary = {"output": str(output), "seconds": result["seconds"]}
    if "monte_carlo" in result:
        mc = result["monte_carlo"]
        summary["monte_carlo"] = {key: mc.get(key) for key in ("samples", "errors", "yield", "yield_ci95", "limits")}
        summary["monte_carlo"]["outputs"] = {name: {k: v for k, v in stats.items() if k in ("mean", "std", "p05", "p95", "missing")}
                                             for name, stats in mc["outputs"].items()}
    if "sensitivity" in result:
        summary["sensitivity"] = {name: ({"ranking": item["ranking"],
                                          "total": {p: round(i["total"], 3) for p, i in item["indices"].items()},
                                          "first_order": {p: round(i["first_order"], 3) for p, i in item["indices"].items()}}
                                         if "indices" in item else item)
                                  for name, item in result["sensitivity"]["outputs"].items()}
    typer.echo(json.dumps(summary, indent=2, default=lambda value: None))


@mechatronics_app.command("validate")
def validate_project(project_file: Path = typer.Argument(..., exists=True, dir_okay=False)) -> None:
    project = MechatronicProject.load(project_file)
    typer.echo(json.dumps({"valid": True, "name": project.name, "schema_version": project.schema_version,
                           "signals": len(project.signals), "couplings": len(project.couplings),
                           "scheduler": project.scheduler.algorithm if project.scheduler else None}, indent=2))


@mechatronics_app.command("runs-list")
def runs_list(
    database: Path = typer.Option(Path(".embeddyn/database/metadata.sqlite3"), "--database"),
    limit: int = typer.Option(50, min=1, max=1000),
) -> None:
    typer.echo(json.dumps(MetadataDatabase(database).list_runs(limit), indent=2))


@mechatronics_app.command("runs-inspect")
def runs_inspect(
    run_id: str = typer.Argument(...),
    database: Path = typer.Option(Path(".embeddyn/database/metadata.sqlite3"), "--database"),
) -> None:
    record = MetadataDatabase(database).get_run(run_id)
    if record is None:
        raise typer.BadParameter(f"unknown run id: {run_id}")
    typer.echo(json.dumps(record, indent=2))


@mechatronics_app.command("runs-export")
def runs_export(
    run_id: str = typer.Argument(...),
    output: Path = typer.Option(..., "--output", "-o"),
    database: Path = typer.Option(Path(".embeddyn/database/metadata.sqlite3"), "--database"),
) -> None:
    record = MetadataDatabase(database).get_run(run_id)
    if record is None or not record.get("telemetry"):
        raise typer.BadParameter(f"run has no telemetry: {run_id}")
    source = Path(record["telemetry"][0]["path"])
    typer.echo(str(export_telemetry_csv(source, output.resolve())))


@mechatronics_app.command("artifacts-verify")
def artifacts_verify(
    root: Path = typer.Option(Path(".embeddyn/artifacts"), "--root"),
) -> None:
    results = ContentAddressedStore(root).verify_all()
    valid = all(item["valid"] for item in results)
    typer.echo(json.dumps({"valid": valid, "count": len(results), "artifacts": results}, indent=2))
    if not valid:
        raise typer.Exit(2)


@mechatronics_app.command("replay")
def replay(
    run_directory: Path = typer.Argument(..., exists=True, file_okay=False),
) -> None:
    result = verify_replay(run_directory)
    typer.echo(json.dumps(result, indent=2))
    if not result["monotonic_time"]:
        raise typer.Exit(2)


@mechatronics_app.command("gui")
def gui_command(
    project_file: Optional[Path] = typer.Argument(None, dir_okay=False, help="Project to open at start."),
    host: str = typer.Option("127.0.0.1", "--host", help="Interface to bind (keep it local: the API runs simulators)."),
    port: int = typer.Option(8710, "--port"),
    open_browser: bool = typer.Option(False, "--open", help="Open the Lab in the default browser."),
) -> None:
    """Embeddyn Lab: schematic editor, virtual instruments and 3-D view in the browser."""
    from .gui.server import serve
    server = serve(project_file.resolve() if project_file else None, host, port)
    url = f"http://{host}:{server.server_address[1]}/"
    if project_file:
        url += f"#open={project_file.resolve()}"
    typer.echo(f"embeddyn Lab on {url}  (Ctrl+C to stop)")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.lab_state.stop_all()
        server.server_close()


@mechatronics_app.command("bindings-from-pinplan")
def bindings_from_pinplan(
    plan_file: Path = typer.Argument(..., exists=True, dir_okay=False, help="Forge pin plan (.pinplan.json)."),
    device_file: Path = typer.Option(..., "--device", exists=True, dir_okay=False, help="Device with renode pin data."),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Bindings YAML to write (default: stdout)."),
    merge: Optional[Path] = typer.Option(None, "--merge", exists=True, dir_okay=False,
                                         help="Existing bindings whose non-pin entries (sysbus...) are kept."),
    machine: Optional[str] = typer.Option(None, "--machine"),
) -> None:
    """Generate Renode GPIO/PWM/ADC bindings from a CodeLayer Forge pin plan."""
    from .pinplan import load_json_or_yaml, merge_bindings, renode_bindings, to_yaml
    generated = renode_bindings(load_json_or_yaml(plan_file), load_json_or_yaml(device_file), machine)
    for item in generated["diagnostics"]:
        typer.echo(f"{item['severity']}: {item['message']}", err=True)
    manifest = merge_bindings(generated, load_json_or_yaml(merge) if merge else None) if merge else generated
    text = to_yaml(manifest, plan_file.name)
    if output:
        output.write_text(text, encoding="utf-8")
        typer.echo(f"wrote {output} ({len(generated['bindings'])} pin bindings)")
    else:
        typer.echo(text)
    if any(item["severity"] == "error" for item in generated["diagnostics"]):
        raise typer.Exit(1)


@mechatronics_app.command("doctor")
def doctor() -> None:
    tools = {name: shutil.which(name) for name in ("webots", "ngspice", "ltspice", "kicad-cli", "renode")}
    typer.echo(json.dumps({"tools": tools, "notes": {
        "ltspice": "Windows/macOS executable may need --executable in project YAML",
        "kicad": "KiCad IPC plugin integration is generated separately",
    }}, indent=2))


@mechatronics_app.command("kicad-export")
def kicad_export(
    schematic: Path = typer.Argument(..., exists=True, dir_okay=False),
    output: Path = typer.Option(Path("artifacts/kicad/circuit.cir"), "--output", "-o"),
    executable: Optional[str] = typer.Option(None),
) -> None:
    result = KiCadBridge(executable).export_spice(schematic, output)
    typer.echo(str(result))


@mechatronics_app.command("kicad-plugin")
def kicad_plugin(destination: Path = typer.Argument(Path("plugins/kicad-embeddyn-bridge"))) -> None:
    typer.echo(str(KiCadBridge().write_plugin_skeleton(destination)))


@mechatronics_app.command("integration-validate")
def integration_validate(project_file: Path = typer.Argument(..., exists=True, dir_okay=False)) -> None:
    report = validate_integrations(MechatronicProject.load(project_file))
    typer.echo(json.dumps(report, indent=2))
    if not report["valid"]:
        raise typer.Exit(2)


@mechatronics_app.command("catalog-list")
def catalog_list(root: Path = typer.Option(Path("components/mechatronics"), "--root")) -> None:
    records = ComponentCatalog([root]).scan()
    typer.echo(json.dumps([{
        "id": item.component_id, "version": item.version, "category": item.category,
        "fidelity_levels": item.fidelity_levels, "source": str(item.source),
    } for item in records], indent=2))


@mechatronics_app.command("kicad-inspect")
def kicad_inspect(netlist: Path = typer.Argument(..., exists=True, dir_okay=False)) -> None:
    report = KiCadBridge().validate_spice_netlist(netlist)
    typer.echo(json.dumps(report, indent=2))
    if not report["valid"]:
        raise typer.Exit(2)


@mechatronics_app.command("worker-register")
def worker_register(
    name: str = typer.Option(..., "--name"),
    endpoint: str = typer.Option("local://worker", "--endpoint"),
    worker_id: Optional[str] = typer.Option(None, "--worker-id"),
    simulators: str = typer.Option("", "--simulators", help="Comma-separated simulator names."),
    hardware: str = typer.Option("", "--hardware", help="Comma-separated hardware capabilities."),
    labels: str = typer.Option("", "--labels", help="Comma-separated scheduling labels."),
    max_parallel_jobs: int = typer.Option(1, min=1),
    real_time: bool = typer.Option(False, "--real-time"),
    database: Path = typer.Option(Path(".embeddyn/database/metadata.sqlite3"), "--database"),
) -> None:
    caps = WorkerCapabilities(
        simulators=tuple(x.strip() for x in simulators.split(",") if x.strip()),
        hardware=tuple(x.strip() for x in hardware.split(",") if x.strip()),
        labels=tuple(x.strip() for x in labels.split(",") if x.strip()),
        operating_system=platform.system().lower(),
        architecture=platform.machine().lower(),
        max_parallel_jobs=max_parallel_jobs,
        real_time=real_time,
    )
    worker = WorkerRecord(worker_id or uuid.uuid4().hex, name, endpoint, caps)
    DistributedCoordinator(database).register_worker(worker)
    typer.echo(json.dumps(worker.to_dict(), indent=2))


@mechatronics_app.command("workers-list")
def workers_list(
    database: Path = typer.Option(Path(".embeddyn/database/metadata.sqlite3"), "--database"),
    stale_after: Optional[float] = typer.Option(None, "--stale-after"),
) -> None:
    coordinator = DistributedCoordinator(database)
    if stale_after is not None:
        coordinator.mark_stale_workers(stale_after)
    typer.echo(json.dumps(coordinator.list_workers(), indent=2))


@mechatronics_app.command("job-submit")
def job_submit(
    project_file: Path = typer.Argument(..., exists=True, dir_okay=False),
    requirements_file: Optional[Path] = typer.Option(None, "--requirements", exists=True, dir_okay=False),
    priority: int = typer.Option(0, "--priority"),
    database: Path = typer.Option(Path(".embeddyn/database/metadata.sqlite3"), "--database"),
) -> None:
    requirements = yaml.safe_load(requirements_file.read_text(encoding="utf-8")) if requirements_file else {}
    spec = JobSpec(str(project_file.resolve()), requirements=dict(requirements or {}), priority=priority)
    DistributedCoordinator(database).submit_job(spec)
    typer.echo(json.dumps(spec.to_dict(), indent=2))


@mechatronics_app.command("jobs-list")
def jobs_list(
    status: Optional[str] = typer.Option(None, "--status"),
    limit: int = typer.Option(100, min=1, max=10000),
    database: Path = typer.Option(Path(".embeddyn/database/metadata.sqlite3"), "--database"),
) -> None:
    typer.echo(json.dumps(DistributedCoordinator(database).list_jobs(status=status, limit=limit), indent=2))


@mechatronics_app.command("job-cancel")
def job_cancel(
    job_id: str = typer.Argument(...),
    database: Path = typer.Option(Path(".embeddyn/database/metadata.sqlite3"), "--database"),
) -> None:
    coordinator = DistributedCoordinator(database)
    coordinator.request_cancel(job_id)
    typer.echo(json.dumps(coordinator.get_job(job_id), indent=2))


@mechatronics_app.command("experiment-expand")
def experiment_expand(
    experiment_file: Path = typer.Argument(..., exists=True, dir_okay=False),
) -> None:
    expansion = load_experiment(experiment_file)
    typer.echo(json.dumps({
        "experiment_id": expansion.experiment_id,
        "name": expansion.name,
        "job_count": len(expansion.jobs),
        "jobs": [job.to_dict() for job in expansion.jobs],
    }, indent=2))


@mechatronics_app.command("experiment-submit")
def experiment_submit(
    experiment_file: Path = typer.Argument(..., exists=True, dir_okay=False),
    database: Path = typer.Option(Path(".embeddyn/database/metadata.sqlite3"), "--database"),
) -> None:
    expansion = load_experiment(experiment_file)
    coordinator = DistributedCoordinator(database)
    coordinator.register_experiment(expansion.experiment_id, expansion.name, expansion.source)
    for job in expansion.jobs:
        coordinator.submit_job(job)
    typer.echo(json.dumps({
        "experiment_id": expansion.experiment_id,
        "name": expansion.name,
        "job_count": len(expansion.jobs),
        "status": coordinator.experiment_status(expansion.experiment_id),
    }, indent=2))


@mechatronics_app.command("experiment-status")
def experiment_status(
    experiment_id: str = typer.Argument(...),
    database: Path = typer.Option(Path(".embeddyn/database/metadata.sqlite3"), "--database"),
) -> None:
    status = DistributedCoordinator(database).experiment_status(experiment_id)
    if status is None:
        raise typer.BadParameter(f"unknown experiment: {experiment_id}")
    typer.echo(json.dumps(status, indent=2))


@mechatronics_app.command("hil-validate")
def hil_validate(
    policy_file: Path = typer.Argument(..., exists=True, dir_okay=False),
) -> None:
    raw = yaml.safe_load(policy_file.read_text(encoding="utf-8")) or {}
    policy = HilSafetyPolicy.from_dict(raw)
    typer.echo(json.dumps({
        "valid": True,
        "watchdog_timeout_seconds": policy.watchdog_timeout_seconds,
        "emergency_stop_input": policy.emergency_stop_input,
        "outputs": sorted(policy.outputs),
        "trip_on_deadline_miss": policy.trip_on_deadline_miss,
    }, indent=2))


@mechatronics_app.command("worker-run")
def worker_run(
    worker_id: str = typer.Argument(...),
    once: bool = typer.Option(False, "--once"),
    poll_interval: float = typer.Option(1.0, "--poll-interval", min=0.05),
    output_root: Path = typer.Option(Path(".embeddyn/worker-runs"), "--output-root"),
    database: Path = typer.Option(Path(".embeddyn/database/metadata.sqlite3"), "--database"),
) -> None:
    agent = WorkerAgent(DistributedCoordinator(database), worker_id, output_root, poll_interval)
    agent.serve(once=once)

@mechatronics_app.command("twin-state")
def twin_state(
    measurements: Path = typer.Argument(..., exists=True, dir_okay=False),
    output: Path = typer.Option(Path(".embeddyn/digital-twin/state.json"), "--output", "-o"),
) -> None:
    from .digital_twin import create_state_snapshot
    snapshot = create_state_snapshot(measurements)
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
    typer.echo(json.dumps({"state": str(output), "signals": len(snapshot["state"]), "source_time": snapshot["source_time"]}, indent=2))


@mechatronics_app.command("twin-compare")
def twin_compare(
    simulation: Path = typer.Argument(..., exists=True, dir_okay=False),
    measurements: Path = typer.Argument(..., exists=True, dir_okay=False),
    mapping_file: Optional[Path] = typer.Option(None, "--mapping", exists=True, dir_okay=False),
    output: Path = typer.Option(Path(".embeddyn/digital-twin/comparison.json"), "--output", "-o"),
    aligned_output: Optional[Path] = typer.Option(None, "--aligned-output"),
) -> None:
    from .digital_twin import compare_files, write_rows_csv
    mapping = None
    if mapping_file:
        raw_mapping = yaml.safe_load(mapping_file.read_text(encoding="utf-8")) or {}
        mapping = raw_mapping.get("signals", raw_mapping)
    aligned, comparison = compare_files(simulation, measurements, mapping)
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(comparison, indent=2, sort_keys=True), encoding="utf-8")
    aligned_path = write_rows_csv(aligned, aligned_output.resolve()) if aligned_output else None
    typer.echo(json.dumps({"comparison": str(output), "aligned": str(aligned_path) if aligned_path else None, **comparison}, indent=2))


@mechatronics_app.command("twin-calibrate")
def twin_calibrate(
    simulation: Path = typer.Argument(..., exists=True, dir_okay=False),
    measurements: Path = typer.Argument(..., exists=True, dir_okay=False),
    mapping_file: Optional[Path] = typer.Option(None, "--mapping", exists=True, dir_okay=False),
    output: Path = typer.Option(Path(".embeddyn/digital-twin/calibration.yaml"), "--output", "-o"),
    database: Path = typer.Option(Path(".embeddyn/database/metadata.sqlite3"), "--database"),
) -> None:
    from .digital_twin import compare_files, fit_all
    mapping = None
    if mapping_file:
        raw_mapping = yaml.safe_load(mapping_file.read_text(encoding="utf-8")) or {}
        mapping = raw_mapping.get("signals", raw_mapping)
    aligned, comparison = compare_files(simulation, measurements, mapping)
    calibrations = fit_all(aligned)
    calibration_id = uuid.uuid4().hex
    document = {
        "schema_version": 1,
        "calibration_id": calibration_id,
        "simulation": str(simulation.resolve()),
        "measurements": str(measurements.resolve()),
        "models": {name: value.to_dict() for name, value in calibrations.items()},
        "comparison": comparison,
    }
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    MetadataDatabase(database).register_calibration(calibration_id, document)
    typer.echo(json.dumps({"calibration_id": calibration_id, "output": str(output), "signals": list(calibrations)}, indent=2))


@mechatronics_app.command("uncertainty-run")
def uncertainty_run(
    specification: Path = typer.Argument(..., exists=True, dir_okay=False),
    samples: int = typer.Option(1000, "--samples", min=2, max=1000000),
    seed: int = typer.Option(1, "--seed"),
    output: Path = typer.Option(Path(".embeddyn/digital-twin/uncertainty.json"), "--output", "-o"),
) -> None:
    from .digital_twin import run_uncertainty
    result = run_uncertainty(specification, samples, seed)
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    typer.echo(json.dumps({"output": str(output), "samples": samples, "seed": seed, "parameters": result["parameters"]}, indent=2))


@mechatronics_app.command("requirements-verify")
def requirements_verify(
    requirements: Path = typer.Argument(..., exists=True, dir_okay=False),
    metrics: Path = typer.Argument(..., exists=True, dir_okay=False),
    output: Path = typer.Option(Path(".embeddyn/evidence/traceability.json"), "--output", "-o"),
) -> None:
    from .digital_twin import verify_requirements
    result = verify_requirements(requirements, metrics)
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    typer.echo(json.dumps({"output": str(output), **result["summary"]}, indent=2))
    if result["summary"]["failed"] or result["summary"]["unresolved"]:
        raise typer.Exit(2)


@mechatronics_app.command("evidence-report")
def evidence_report(
    traceability: Path = typer.Argument(..., exists=True, dir_okay=False),
    comparison: Optional[Path] = typer.Option(None, "--comparison", exists=True, dir_okay=False),
    output: Path = typer.Option(Path(".embeddyn/evidence/report.md"), "--output", "-o"),
    run_id: Optional[str] = typer.Option(None, "--run-id"),
    database: Path = typer.Option(Path(".embeddyn/database/metadata.sqlite3"), "--database"),
    artifact_root: Path = typer.Option(Path(".embeddyn/artifacts"), "--artifact-root"),
) -> None:
    from .digital_twin import render_evidence_markdown
    traceability_data = json.loads(traceability.read_text(encoding="utf-8"))
    comparison_data = json.loads(comparison.read_text(encoding="utf-8")) if comparison else None
    rendered = render_evidence_markdown(traceability_data, comparison_data)
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    report_id = uuid.uuid4().hex
    artifact_store = ContentAddressedStore(artifact_root)
    artifact = artifact_store.add_file(output, created_by="phase6-evidence-report", media_type="text/markdown")
    database_service = MetadataDatabase(database)
    database_service.register_artifact(artifact)
    status = "passed" if traceability_data.get("summary", {}).get("failed", 0) == 0 and traceability_data.get("summary", {}).get("unresolved", 0) == 0 else "failed"
    database_service.register_evidence_report(report_id, output, status, traceability_data.get("summary", {}), run_id, artifact.digest)
    if run_id:
        database_service.link_artifact(run_id, artifact.digest, "evidence", output.name)
    typer.echo(json.dumps({"report_id": report_id, "status": status, "output": str(output), "artifact": artifact.to_dict()}, indent=2))
