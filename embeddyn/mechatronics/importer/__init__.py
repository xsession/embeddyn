"""Import a hardware project (KiCad schematic + STM32CubeMX/CubeIDE firmware) as an emulated mechatronic system.

``import_project(source, out)`` takes a project folder or a .zip of one and writes, into ``out``: the Renode board
(``board.repl``) and script (``board.resc``), the External Control bindings, a mechatronic project per firmware
(the board's own firmware in Renode, a motion plant for its sensors, instruments for its LEDs and serial ports),
``report.md`` with what was found (buses, chip selects, interrupts, supply rails, firmware/board mismatches) and
``import.json``. See docs/mechatronics/import.md.
"""
from __future__ import annotations

import os
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .analysis import Plan, Resolver, check_firmware_against_board, load_library, scan_firmware_drivers
from .emit import write_all
from .firmware import FirmwareBuildError, FirmwareProject, build, find_firmware_projects
from .ioc import IocProject, find_ioc, read_ioc
from .kicad_netlist import find_netlist, read_netlist


@dataclass
class ImportResult:
    out: Path
    plan: Plan
    files: dict[str, Path] = field(default_factory=dict)
    firmware: list[Path] = field(default_factory=list)
    build_errors: dict[str, str] = field(default_factory=dict)


def _unpack(source: Path, work: Path) -> Path:
    """A folder as is; a .zip unpacked into work/source (refusing paths that leave it)."""
    if source.is_dir():
        return source
    target = work / "source"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    with zipfile.ZipFile(source) as archive:
        for member in archive.infolist():
            destination = (target / member.filename).resolve()
            if target.resolve() not in destination.parents and destination != target.resolve():
                raise ValueError(f"unsafe path in archive: {member.filename}")
        archive.extractall(target)
    entries = [p for p in target.iterdir() if not p.name.startswith(".")]
    return entries[0] if len(entries) == 1 and entries[0].is_dir() else target


def import_project(source: Path, out: Path, *, title: str | None = None, gcc: Path | None = None,
                   build_firmware: bool = True, firmware_filter: list[str] | None = None,
                   substitutions: dict[str, str] | None = None, parts: list[Path] | None = None,
                   renode_root: Path | None = None) -> ImportResult:
    out.mkdir(parents=True, exist_ok=True)
    root = _unpack(source, out)
    title = title or root.name
    library = load_library(parts)
    netlist_path = find_netlist(root, out)
    if netlist_path is None:
        raise FileNotFoundError(f"{root}: no KiCad netlist (.net/.xml) and no KiCad 6+ schematic to export one from")
    netlist = read_netlist(netlist_path)
    projects = find_firmware_projects(root)
    if firmware_filter:
        projects = [p for p in projects if any(f.lower() in (p.name.lower(), p.directory.name.lower()) for f in firmware_filter)]
    iocs = find_ioc(root)
    ioc: IocProject | None = None
    if iocs:
        # the CubeMX project of the first firmware, else the first one found
        first = projects[0].directory if projects else None
        chosen = next((p for p in iocs if first and p.parent == first), iocs[0])
        ioc = read_ioc(chosen)
    plan = Resolver(netlist, ioc, library, substitutions).resolve()
    drivers = scan_firmware_drivers([p.directory for p in projects], library)
    check_firmware_against_board(plan, drivers, library)
    result = ImportResult(out=out, plan=plan)
    notes = [f"netlist `{netlist_path.relative_to(root) if root in netlist_path.parents else netlist_path.name}`"
             + (f", CubeMX `{ioc.path.relative_to(root)}`" if ioc else "")]
    for project in projects:
        line = f"{project.name} ({project.kind}, {project.target_mcu or '?'}, `{project.directory.relative_to(root)}`)"
        if build_firmware:
            try:
                elf = build(project, out / "build" / project.name, gcc)
                result.firmware.append(elf)
                line += ": built from source"
            except FirmwareBuildError as error:
                result.build_errors[project.name] = str(error)
                if project.prebuilt:
                    result.firmware.append(project.prebuilt[0])
                    line += f": build failed, using the shipped `{project.prebuilt[0].name}`"
                else:
                    line += ": build failed"
        elif project.prebuilt:
            result.firmware.append(project.prebuilt[0])
            line += f": shipped `{project.prebuilt[0].name}`"
        notes.append(line)
    root_dir = renode_root or next((Path(os.environ[k]) for k in ("RENODE_ROOT", "RENODE_ROOT_DIR") if os.environ.get(k)), None)
    if root_dir and not (root_dir / plan.platform).exists():
        plan.warn(f"{plan.platform} is not in the Renode tree {root_dir}: this Renode lacks the platform")
    result.files = write_all(plan, out, title, result.firmware, {"firmware": notes},
                             str(root_dir.resolve()) if root_dir else None)
    return result
