"""Write docs/reference/cli.md from the `embeddyn mechatronics` Typer commands.

Usage (from the embeddyn root): python tools/docs/make_cli_reference.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import click
import typer

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from embeddyn.mechatronics.cli import mechatronics_app  # noqa: E402


# Commands without a docstring get a summary here (keep in step with cli.py).
FALLBACK = {
    "compile": "Compile a project into a run directory with its manifest and lock file, without running it.",
    "run": "Run a co-simulation; write telemetry, a manifest and optionally a CSV, and print final values and metrics.",
    "validate": "Parse and validate a project file without starting any simulator.",
    "runs-list": "List recorded runs from the metadata database.",
    "runs-inspect": "Show one recorded run: manifest, parameters, artifacts.",
    "runs-export": "Export a recorded run's signals.",
    "artifacts-verify": "Verify the content hashes of every artifact in the content-addressed store.",
    "replay": "Check that a recorded run directory replays consistently.",
    "doctor": "Report which external tools (Renode, ngspice, Webots, KiCad, LTspice) are found.",
    "kicad-export": "Export a KiCad schematic to a SPICE netlist with kicad-cli.",
    "kicad-plugin": "Create a KiCad IPC plugin skeleton that talks to the local Studio API.",
    "kicad-inspect": "Summarise a SPICE netlist (sources, models, subcircuits) without KiCad.",
    "integration-validate": "Check a project's integrations: model files, bindings, FMU variables, tool availability.",
    "catalog-list": "List the component definitions found under a catalog root.",
    "worker-register": "Register a worker with the distributed coordinator.",
    "workers-list": "List registered workers.",
    "job-submit": "Submit a simulation job to the distributed queue.",
    "jobs-list": "List queued and finished jobs.",
    "job-cancel": "Cancel a queued job.",
    "experiment-expand": "Expand an experiment file into its individual runs (sweeps, matrices).",
    "experiment-submit": "Submit every run of an experiment to the queue.",
    "experiment-status": "Show the progress of a submitted experiment.",
    "hil-validate": "Validate a hardware-in-the-loop safety policy (watchdog, emergency stop, outputs, deadline trips).",
    "worker-run": "Run a worker agent that executes queued jobs.",
    "twin-state": "Create a digital-twin state snapshot.",
    "twin-compare": "Compare simulated and measured signal files.",
    "twin-calibrate": "Fit digital-twin parameters to measured data.",
    "uncertainty-run": "Run an uncertainty propagation from a specification file.",
    "requirements-verify": "Check requirements against computed metrics.",
    "evidence-report": "Render a Markdown evidence report from verification results.",
}


def cell(text: str) -> str:
    return " ".join((text or "").split()).replace("|", "/")


def main() -> int:
    group = typer.main.get_command(mechatronics_app)
    context = getattr(group, 'context_class', click.Context)(group)
    commands = []
    for name in group.list_commands(context):
        command = group.get_command(context, name)
        summary = (command.help or "").strip().split("\n\n")[0] or FALLBACK.get(name, "")
        # Typer >= 0.29 vendors its own click, so match parameter kinds by class name.
        arguments = [p for p in command.params if "Argument" in type(p).__name__]
        options = [p for p in command.params if "Option" in type(p).__name__ and p.name != "help"]
        commands.append((name, cell(summary), arguments, options))

    lines = [
        "# Command line", "",
        "All mechatronics commands live under `embeddyn mechatronics` (or `python -m embeddyn mechatronics`). "
        "This page is generated from the command definitions by `tools/docs/make_cli_reference.py`; "
        "`--help` on any command shows the same.", "",
        "| Command | Purpose |", "|---|---|",
    ]
    lines += [f"| [`{name}`](#{name}) | {summary or '—'} |" for name, summary, _, _ in commands]
    lines.append("")
    for name, summary, arguments, options in commands:
        lines += [f"## {name}", ""]
        if summary:
            lines += [summary, ""]
        usage = " ".join(p.name.upper() for p in arguments)
        lines += ["```text", f"embeddyn mechatronics {name} {usage} [OPTIONS]".replace("  ", " "), "```", ""]
        if options:
            lines += ["| Option | Default | Description |", "|---|---|---|"]
            for option in options:
                flags = ", ".join(f"`{flag}`" for flag in option.opts + option.secondary_opts)
                default = option.default
                shown = "" if default is None or callable(default) or default in ((), []) else f"`{default}`"
                lines.append(f"| {flags} | {shown} | {cell(option.help)} |")
            lines.append("")
    (ROOT / "docs" / "reference" / "cli.md").write_text("\n".join(lines) + "\n")
    print(f"{len(commands)} commands")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
