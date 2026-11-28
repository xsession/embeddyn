"""Find and build the firmware projects of an imported hardware project.

Supported project kinds:

* STM32CubeIDE / SW4STM32 Eclipse projects (``.project`` + ``.cproject``): the build settings (target MCU, FPU,
  float ABI, defines, include paths, optimisation, linker script, newlib-nano float printf) and the source
  folders come from the ``.cproject``; the generated ``Debug/makefile`` is not used (it holds the author's
  absolute paths).
* Plain GNU Make projects (a ``Makefile`` at the project root, e.g. STM32CubeMX "Makefile" toolchain): run
  ``make`` with the GCC directory on ``PATH``.

``build()`` compiles with an ``arm-none-eabi-gcc`` (``GCC_ARM`` or ``--gcc``) into an output directory outside the
source tree, caching objects per source file and command line, and returns the ELF path. A prebuilt ELF shipped
with the project (``Debug/*.elf``) is reported as ``prebuilt`` and used only when building is not possible.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

# Cortex-M core and FPU flags per STM32 family (the target MCU name starts with the family)
FAMILY_CPU = [
    ("STM32F0", ["-mcpu=cortex-m0", "-mfloat-abi=soft"]),
    ("STM32G0", ["-mcpu=cortex-m0plus", "-mfloat-abi=soft"]),
    ("STM32L0", ["-mcpu=cortex-m0plus", "-mfloat-abi=soft"]),
    ("STM32C0", ["-mcpu=cortex-m0plus", "-mfloat-abi=soft"]),
    ("STM32F1", ["-mcpu=cortex-m3", "-mfloat-abi=soft"]),
    ("STM32F2", ["-mcpu=cortex-m3", "-mfloat-abi=soft"]),
    ("STM32L1", ["-mcpu=cortex-m3", "-mfloat-abi=soft"]),
    ("STM32F3", ["-mcpu=cortex-m4"]),
    ("STM32F4", ["-mcpu=cortex-m4"]),
    ("STM32G4", ["-mcpu=cortex-m4"]),
    ("STM32L4", ["-mcpu=cortex-m4"]),
    ("STM32WB", ["-mcpu=cortex-m4"]),
    ("STM32F7", ["-mcpu=cortex-m7"]),
    ("STM32H7", ["-mcpu=cortex-m7"]),
    ("STM32L5", ["-mcpu=cortex-m33"]),
    ("STM32U5", ["-mcpu=cortex-m33"]),
]

SOURCE_SUFFIXES = {".c", ".s", ".S", ".cpp", ".cc"}
BUILD_DIRS = {"Debug", "Release", "build", ".settings"}


class FirmwareBuildError(RuntimeError):
    pass


@dataclass
class FirmwareProject:
    """A firmware project found in an imported tree."""

    kind: str                      # "cubeide" or "make"
    directory: Path
    name: str
    target_mcu: str = ""
    defines: list[str] = field(default_factory=list)
    includes: list[Path] = field(default_factory=list)
    source_dirs: list[Path] = field(default_factory=list)
    excluded: list[Path] = field(default_factory=list)
    linker_script: Path | None = None
    cpu_flags: list[str] = field(default_factory=list)
    optimisation: str = "-O0"
    nano_printf_float: bool = False
    prebuilt: list[Path] = field(default_factory=list)

    def sources(self) -> list[Path]:
        found: list[Path] = []
        for root in self.source_dirs:
            for path in sorted(root.rglob("*")):
                if path.suffix not in SOURCE_SUFFIXES or not path.is_file():
                    continue
                if any(part in BUILD_DIRS for part in path.relative_to(self.directory).parts[:-1]):
                    continue
                if any(path == ex or ex in path.parents for ex in self.excluded):
                    continue
                found.append(path)
        return found

    def describe(self) -> dict:
        return {
            "kind": self.kind, "name": self.name, "directory": str(self.directory), "target_mcu": self.target_mcu,
            "defines": self.defines, "sources": len(self.sources()) if self.kind == "cubeide" else None,
            "linker_script": str(self.linker_script) if self.linker_script else None,
            "prebuilt": [str(p) for p in self.prebuilt],
        }


def find_firmware_projects(root: Path) -> list[FirmwareProject]:
    """All CubeIDE/SW4STM32 and Makefile firmware projects under ``root``."""
    projects: list[FirmwareProject] = []
    seen: set[Path] = set()
    for cproject in sorted(root.rglob(".cproject")):
        directory = cproject.parent
        if (directory / ".project").exists() and directory not in seen:
            try:
                projects.append(parse_cubeide(directory))
                seen.add(directory)
            except (ET.ParseError, FirmwareBuildError):
                continue
    for makefile in sorted(root.rglob("Makefile")):
        directory = makefile.parent
        if directory in seen or any(d in seen for d in directory.parents) or directory.name in BUILD_DIRS:
            continue
        if any((directory / name).exists() for name in ("Core", "Src", "src")) or list(directory.glob("*.ld")):
            projects.append(FirmwareProject(kind="make", directory=directory, name=directory.name,
                                            prebuilt=sorted(directory.glob("build/*.elf"))))
            seen.add(directory)
    return projects


def _option_values(configuration: ET.Element, suffix: str) -> tuple[str, list[str]]:
    for option in configuration.iter("option"):
        if (option.get("superClass") or "").endswith(suffix):
            return option.get("value") or "", [v.get("value") or "" for v in option.findall("listOptionValue")]
    return "", []


def _resolve_eclipse_path(value: str, project: Path, base: Path) -> Path:
    value = value.strip().strip('"')
    m = re.match(r"\$\{workspace_loc:/\$\{ProjName\}/?(.*)\}$", value)
    if m:
        return project / m.group(1)
    m = re.match(r"\$\{ProjDirPath\}/?(.*)$", value)
    if m:
        return project / m.group(1)
    if re.match(r"^[A-Za-z]:[\\/]", value):
        # an absolute Windows path from the author's machine: keep the file name, look for it in the project
        name = re.split(r"[\\/]", value)[-1]
        hits = sorted(project.rglob(name))
        return hits[0] if hits else project / name
    return (base / value).resolve()


def parse_cubeide(directory: Path, configuration_name: str = "Debug") -> FirmwareProject:
    root = ET.parse(directory / ".cproject").getroot()
    configurations = list(root.iter("cconfiguration"))
    if not configurations:
        raise FirmwareBuildError(f"{directory}: no build configuration in .cproject")
    chosen = configurations[0]
    for configuration in configurations:
        name = configuration.find(".//configuration")
        if name is not None and name.get("name") == configuration_name:
            chosen = configuration
            break
    name_el = ET.parse(directory / ".project").getroot().find("name")
    project = FirmwareProject(kind="cubeide", directory=directory,
                              name=name_el.text.strip() if name_el is not None and name_el.text else directory.name)
    build_dir = directory / configuration_name  # include paths are relative to the build folder
    project.target_mcu = _option_values(chosen, "option.target_mcu")[0]
    if not project.target_mcu:
        _, legacy = _option_values(chosen, "option.target.mcu")
        project.target_mcu = legacy[0] if legacy else ""
    _, defines = _option_values(chosen, "c.compiler.option.definedsymbols")
    project.defines = [d for d in defines if d]
    _, includes = _option_values(chosen, "c.compiler.option.includepaths")
    project.includes = [_resolve_eclipse_path(i, directory, build_dir) for i in includes if i]
    level, _ = _option_values(chosen, "c.compiler.option.optimization.level")
    project.optimisation = {"": "-O0", "o0": "-O0", "o1": "-O1", "o2": "-O2", "o3": "-O3", "os": "-Os", "og": "-Og",
                            "ofast": "-Ofast", "oz": "-Os"}.get(level.split(".")[-1].lower().replace("value.", ""), "-O0")
    script, _ = _option_values(chosen, "c.linker.option.script")
    project.linker_script = _resolve_eclipse_path(script, directory, build_dir) if script else None
    if project.linker_script is None or not project.linker_script.exists():
        flash = sorted(directory.glob("*_FLASH.ld")) or sorted(directory.glob("*.ld"))
        project.linker_script = flash[0] if flash else None
    project.nano_printf_float = _option_values(chosen, "option.nanoprintffloat")[0] == "true"
    fpu, _ = _option_values(chosen, "option.fpu")
    abi, _ = _option_values(chosen, "option.floatabi")
    cpu = next((flags for prefix, flags in FAMILY_CPU if project.target_mcu.upper().startswith(prefix)), None)
    if cpu is None:
        raise FirmwareBuildError(f"{directory}: unknown target MCU {project.target_mcu!r}")
    project.cpu_flags = ["-mthumb"] + list(cpu)
    if "-mfloat-abi=soft" not in cpu:
        if fpu:
            project.cpu_flags.append("-mfpu=" + fpu.split(".")[-1])
        project.cpu_flags.append("-mfloat-abi=" + (abi.split(".")[-1] if abi else "hard"))
    entries = chosen.find(".//sourceEntries")
    if entries is not None:
        for entry in entries:
            folder = directory / (entry.get("name") or "")
            project.source_dirs.append(folder)
            for excluded in (entry.get("excluding") or "").split("|"):
                if excluded:
                    project.excluded.append(folder / excluded)
    if not project.source_dirs:
        project.source_dirs = [directory]
    # the start-up assembler file lives in Core/Startup or Startup: add those folders if not covered
    for startup in list(directory.glob("Core/Startup")) + list(directory.glob("Startup")):
        if not any(startup == d or d in startup.parents for d in project.source_dirs):
            project.source_dirs.append(startup)
    project.prebuilt = sorted(directory.glob("Debug/*.elf")) + sorted(directory.glob("Release/*.elf"))
    return project


def _gcc(gcc_dir: Path | None, tool: str) -> str:
    if gcc_dir:
        candidate = Path(gcc_dir) / "bin" / tool
        if candidate.exists():
            return str(candidate)
    env = os.environ.get("GCC_ARM")
    if env and (Path(env) / "bin" / tool).exists():
        return str(Path(env) / "bin" / tool)
    found = shutil.which(tool)
    if not found:
        raise FirmwareBuildError(f"{tool} not found: set GCC_ARM or pass --gcc")
    return found


def build(project: FirmwareProject, out_dir: Path, gcc_dir: Path | None = None, jobs: int = 8) -> Path:
    """Compile and link the project into ``out_dir``; returns the ELF path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    if project.kind == "make":
        gcc = Path(_gcc(gcc_dir, "arm-none-eabi-gcc")).parent
        env = dict(os.environ, PATH=f"{gcc}{os.pathsep}{os.environ.get('PATH', '')}")
        work = out_dir / "src"
        if work.exists():
            shutil.rmtree(work)
        shutil.copytree(project.directory, work, ignore=shutil.ignore_patterns("build", "*.o", "*.d"))
        result = subprocess.run(["make", "-j", str(jobs)], cwd=work, env=env, capture_output=True, text=True)
        (out_dir / "build.log").write_text(result.stdout + result.stderr)
        elves = sorted(work.rglob("*.elf"))
        if result.returncode != 0 or not elves:
            raise FirmwareBuildError(f"make failed (see {out_dir / 'build.log'})")
        return elves[0]

    cc = _gcc(gcc_dir, "arm-none-eabi-gcc")
    if project.linker_script is None:
        raise FirmwareBuildError(f"{project.directory}: no linker script")
    common = project.cpu_flags + [f"-D{d}" for d in project.defines] + [f"-I{i}" for i in project.includes]
    cflags = common + [project.optimisation, "-g3", "-ffunction-sections", "-fdata-sections", "-Wall",
                       "--specs=nano.specs", "-std=gnu11",
                       # projects generated for older toolchains rely on stdio.h pulling in sys/types.h (caddr_t)
                       "-include", "sys/types.h"]
    obj_dir = out_dir / "obj"
    obj_dir.mkdir(exist_ok=True)
    log: list[str] = []

    def compile_one(source: Path) -> Path:
        flags = cflags if source.suffix in (".c",) else (common + ["-g3", "-x", "assembler-with-cpp"]
                                                         if source.suffix in (".s", ".S") else
                                                         common + [project.optimisation, "-g3", "-ffunction-sections",
                                                                   "-fdata-sections", "--specs=nano.specs"])
        command = [cc, "-c", str(source)] + flags
        key = hashlib.sha1(("\0".join(command)).encode() + source.read_bytes()).hexdigest()[:16]
        obj = obj_dir / f"{source.stem}.{key}.o"
        if not obj.exists():
            result = subprocess.run(command + ["-o", str(obj)], capture_output=True, text=True)
            log.append(f"$ {' '.join(command)}\n{result.stdout}{result.stderr}")
            if result.returncode != 0:
                raise FirmwareBuildError(f"compiling {source} failed:\n{result.stderr[-2000:]}")
        return obj

    sources = project.sources()
    if not sources:
        raise FirmwareBuildError(f"{project.directory}: no sources")
    try:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            objects = list(pool.map(compile_one, sources))
    finally:
        (out_dir / "build.log").write_text("\n".join(log))
    elf = out_dir / f"{project.name}.elf"
    link = [cc, "-o", str(elf)] + [str(o) for o in objects] + project.cpu_flags + [
        f"-T{project.linker_script}", f"-Wl,-Map={out_dir / (project.name + '.map')}", "-Wl,--gc-sections", "-static",
        "--specs=nano.specs", "--specs=nosys.specs"] + (["-u", "_printf_float"] if project.nano_printf_float else []) + [
        "-Wl,--start-group", "-lc", "-lm", "-Wl,--end-group"]
    result = subprocess.run(link, capture_output=True, text=True)
    with open(out_dir / "build.log", "a") as handle:
        handle.write(f"\n$ {' '.join(link)}\n{result.stdout}{result.stderr}")
    if result.returncode != 0:
        raise FirmwareBuildError(f"linking {project.name} failed:\n{result.stderr[-2000:]}")
    return elf
