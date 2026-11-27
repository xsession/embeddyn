"""The mcu-emu tools. Each returns a dict with "ok" and short, structured fields.

Every tool is deterministic and does one step of the full-MCU-emulation workflow, so an agent
never has to parse vendor files, Renode logs or traces by hand.
"""
from __future__ import annotations

import collections
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

REGISTRY: dict[str, dict] = {}
PROFILE_DIR = Path(__file__).parent / "profiles"


class ToolError(Exception):
    """A user-facing error: the message says what to change."""


def tool(description: str, properties: dict, required: list[str] | None = None) -> Callable:
    def register(func: Callable) -> Callable:
        REGISTRY[func.__name__] = {"func": func, "description": description,
                                   "schema": {"type": "object", "properties": properties,
                                              "required": required or [], "additionalProperties": False}}
        return func
    return register


S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}
N = {"type": "number"}


def _int(value: Any) -> int:
    if isinstance(value, int):
        return value
    return int(str(value).strip(), 0)


def _path(value: str | None, base: Path | None = None) -> Path | None:
    if not value:
        return None
    p = Path(os.path.expandvars(os.path.expanduser(value)))
    if not p.is_absolute() and base is not None:
        p = base / p
    return p


def _need_file(p: Path | None, what: str) -> Path:
    if p is None or not p.exists():
        raise ToolError(f"{what} not found: {p}")
    return p


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 600, env: dict | None = None,
         open_stdin: bool = False) -> subprocess.CompletedProcess:
    # open_stdin: give the program a stdin that stays open for the whole run (Renode's console reader spins on
    # end-of-file, slowing the emulation and growing its memory)
    feeder = subprocess.Popen(["sleep", str(timeout + 60)], stdout=subprocess.PIPE) if open_stdin else None
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, **(env or {})}, stdin=feeder.stdout if feeder else None)
    except FileNotFoundError as exc:
        raise ToolError(f"program not found: {cmd[0]} ({exc})") from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"timed out after {timeout} s: {' '.join(cmd[:3])}...") from exc
    finally:
        if feeder:
            feeder.kill()
            feeder.wait()


def _lines(text: str, pattern: str, limit: int = 20) -> list[str]:
    rx = re.compile(pattern, re.I)
    out = [l.strip() for l in text.splitlines() if rx.search(l)]
    return list(dict.fromkeys(out))[:limit]


# ----------------------------------------------------------------------------- profiles

def _profile_files() -> dict[str, Path]:
    files = {p.stem: p for p in PROFILE_DIR.glob("*.json")}
    for extra in os.environ.get("MCU_EMU_PROFILES", "").split(os.pathsep):
        if extra and Path(extra).is_dir():
            files.update({p.stem: p for p in Path(extra).glob("*.json")})
    return files


def _expand(value: Any, env: dict) -> Any:
    if isinstance(value, str):
        return re.sub(r"\$\{(\w+)\}", lambda m: env.get(m.group(1), m.group(0)), value)
    if isinstance(value, list):
        return [_expand(v, env) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v, env) for k, v in value.items()}
    return value


def load_profile(name: str) -> dict:
    files = _profile_files()
    if name not in files:
        raise ToolError(f"unknown profile {name!r}; known: {sorted(files)}")
    raw = json.loads(files[name].read_text())
    env = {**raw.get("env_defaults", {}), **{k: v for k, v in os.environ.items()}}
    for _ in range(3):                           # defaults may refer to each other
        env = {k: _expand(v, env) for k, v in env.items()}
    profile = _expand(raw, env)
    profile["_file"] = str(files[name])
    return profile


@tool("List the MCU profiles (toolchain, files, memory layout, Renode platform per MCU).", {})
def mcu_profile_list() -> dict:
    out = []
    for name, path in sorted(_profile_files().items()):
        try:
            out.append({"name": name, "description": json.loads(path.read_text()).get("description", "")})
        except json.JSONDecodeError as exc:
            out.append({"name": name, "error": f"bad JSON: {exc}"})
    return {"ok": True, "profiles": out,
            "hint": "add your own JSON profiles in a directory named by the MCU_EMU_PROFILES environment variable"}


@tool("Show one MCU profile with ${VAR}s expanded and which of its files and programs exist on this machine.",
      {"name": S}, ["name"])
def mcu_profile_show(name: str) -> dict:
    p = load_profile(name)
    checks = {}
    for section in ("toolchain", "files"):
        for key, value in (p.get(section) or {}).items():
            if isinstance(value, str) and ("/" in value or value.endswith((".h", ".gld", ".ld", ".svd"))):
                checks[f"{section}.{key}"] = Path(value).exists() or shutil.which(value) is not None
    return {"ok": True, "profile": p, "exists": checks,
            "missing": [k for k, v in checks.items() if not v]}


# ----------------------------------------------------------------------------- extraction

_EQU = re.compile(r"^\s*(?:PROVIDE\s*\(\s*)?_?([A-Za-z][A-Za-z0-9_]*)\s*=\s*(0x[0-9A-Fa-f]+)\s*\)?\s*;", re.M)
_BITS = re.compile(r"typedef\s+struct\s+tag(\w+?)BITS\s*\{(.*?)\}\s*\1BITS\s*;", re.S)
_FIELD = re.compile(r"(?:unsigned(?:\s+int)?|uint(?:8|16|32)_t|int)\s*(\w*)\s*:\s*(\d+)\s*;")
_DEFINE_ADDR = re.compile(r"#define\s+(\w+)\s+\(\s*\*\s*\(\s*volatile\s+[\w\s]+\*\s*\)\s*\(?\s*(0x[0-9A-Fa-f]+)\s*\)?\s*\)")
_ATTR_ADDR = re.compile(r"extern\s+volatile\s+[\w\s]+?\s(\w+)\s+__attribute__\s*\(\s*\(\s*(?:__)?address(?:__)?\s*\(\s*(0x[0-9A-Fa-f]+)")


def _fields_from_struct(body: str) -> list[dict]:
    first = re.search(r"struct\s*\{(.*?)\}\s*;", body, re.S)   # unions: the first layout
    if first:
        body = first.group(1)
    bit, fields = 0, []
    for name, width in _FIELD.findall(body):
        width = int(width)
        if name:
            fields.append({"name": name, "lsb": bit, "width": width})
        bit += width
    return fields


def _parse_svd(path: Path) -> tuple[list[dict], list[dict]]:
    root = ET.parse(path).getroot()

    def text(node, tag, default=None):
        el = node.find(tag)
        return el.text.strip() if el is not None and el.text else default

    peripherals = {p.findtext("name"): p for p in root.iter("peripheral")}
    regs, irqs = [], []
    for name, p in peripherals.items():
        source = peripherals.get(p.get("derivedFrom"), p) if p.get("derivedFrom") else p
        base = _int(text(p, "baseAddress", "0"))
        for irq in p.findall("interrupt"):
            irqs.append({"index": _int(text(irq, "value", "0")), "name": text(irq, "name")})
        for r in source.iter("register"):
            fields = [{"name": text(f, "name"), "lsb": _int(text(f, "bitOffset", "0")), "width": _int(text(f, "bitWidth", "1"))}
                      for f in r.iter("field") if text(f, "bitOffset") is not None]
            regs.append({"name": f"{name}_{text(r, 'name')}", "peripheral": name,
                         "address": base + _int(text(r, "addressOffset", "0")),
                         "width": _int(text(r, "size", "32")), "reset": text(r, "resetValue"), "fields": fields})
    return regs, sorted({i["index"]: i for i in irqs}.values(), key=lambda i: i["index"])


@tool("Extract the special-function-register map (addresses and bit fields) from vendor files: a C device header "
      "(XC16-style bitfield structs, #define *(volatile*)ADDR, or address attributes), a GNU/XC16 linker script "
      "(NAME = 0xADDR;), and/or a CMSIS-SVD file. Writes the full map as JSON to `out` and returns a summary. "
      "Use `profile` to take the files from an MCU profile.",
      {"profile": S, "header": S, "linker_script": S, "svd": S, "out": S, "max_address": S})
def extract_sfr_map(profile: str | None = None, header: str | None = None, linker_script: str | None = None,
                    svd: str | None = None, out: str | None = None, max_address: str | None = None) -> dict:
    if profile:
        files = load_profile(profile).get("files", {})
        header = header or files.get("header")
        linker_script = linker_script or files.get("linker_script")
        svd = svd or files.get("svd")
    if not (header or linker_script or svd):
        raise ToolError("give header, linker_script or svd (or a profile that has them)")
    regs: dict[str, dict] = {}
    irqs: list[dict] = []
    if linker_script:
        text = _need_file(_path(linker_script), "linker script").read_text(errors="replace")
        for name, addr in _EQU.findall(text):
            if not name.startswith("_"):
                regs.setdefault(name, {"name": name, "address": int(addr, 16), "fields": []})
    if header:
        text = _need_file(_path(header), "header").read_text(errors="replace")
        for name, addr in _DEFINE_ADDR.findall(text) + _ATTR_ADDR.findall(text):
            regs.setdefault(name, {"name": name, "address": int(addr, 16), "fields": []})
        for name, body in _BITS.findall(text):
            if name in regs:
                regs[name]["fields"] = _fields_from_struct(body)
            else:
                regs[name] = {"name": name, "address": None, "fields": _fields_from_struct(body)}
    if svd:
        svd_regs, irqs = _parse_svd(_need_file(_path(svd), "SVD file"))
        for r in svd_regs:
            regs.setdefault(r["name"], r)
    limit = _int(max_address) if max_address else None
    located = sorted((r for r in regs.values() if r["address"] is not None and (limit is None or r["address"] < limit)),
                     key=lambda r: r["address"])
    unlocated = sorted(r["name"] for r in regs.values() if r["address"] is None)
    out_path = _path(out) or Path(tempfile.gettempdir()) / "sfr_map.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"registers": located, "interrupts": irqs, "unlocated": unlocated}, indent=1))
    # clusters: runs of registers less than 0x20 apart, a first idea of the peripheral blocks
    clusters, start, prev = [], None, None
    for r in located:
        if start is None or r["address"] - prev > 0x20:
            if start is not None:
                clusters.append(f"0x{start['address']:04X}-0x{prev:04X} ({start['name']}..)")
            start = r
        prev = r["address"]
    if start is not None:
        clusters.append(f"0x{start['address']:04X}-0x{prev:04X} ({start['name']}..)")
    return {"ok": True, "out": str(out_path), "registers": len(located),
            "with_fields": sum(1 for r in located if r["fields"]), "interrupts": len(irqs),
            "unlocated_bitfield_structs": len(unlocated), "clusters": clusters[:60]}


@tool("Show registers of an SFR map (from extract_sfr_map) in an address range or by name pattern, with bit fields.",
      {"sfr_map": S, "start": S, "end": S, "name": S, "limit": I}, ["sfr_map"])
def sfr_lookup(sfr_map: str, start: str | None = None, end: str | None = None, name: str | None = None, limit: int = 40) -> dict:
    data = json.loads(_need_file(_path(sfr_map), "SFR map").read_text())
    lo, hi = (_int(start) if start else 0), (_int(end) if end else 1 << 40)
    rx = re.compile(name, re.I) if name else None
    hits = [r for r in data["registers"] if lo <= r["address"] < hi and (rx is None or rx.search(r["name"]))]
    rows = [{"name": r["name"], "address": f"0x{r['address']:04X}",
             "fields": " ".join(f"{f['name']}:{f['lsb']}" + (f"-{f['lsb'] + f['width'] - 1}" if f["width"] > 1 else "")
                                for f in r.get("fields", []))} for r in hits[:limit]]
    return {"ok": True, "count": len(hits), "registers": rows}


@tool("Interrupt vector order: from an XC16/GNU linker script vector table (LONG(DEFINED(__X)...)) or an SVD file. "
      "Index 0 is the first entry after the traps when `skip_traps` is set (dsPIC: 8).",
      {"linker_script": S, "svd": S, "profile": S, "skip_traps": I})
def extract_vectors(linker_script: str | None = None, svd: str | None = None, profile: str | None = None, skip_traps: int = 0) -> dict:
    if profile:
        files = load_profile(profile).get("files", {})
        linker_script = linker_script or files.get("linker_script")
        svd = svd or files.get("svd")
    if svd:
        _, irqs = _parse_svd(_need_file(_path(svd), "SVD file"))
        return {"ok": True, "vectors": [{"index": i["index"], "name": i["name"]} for i in irqs]}
    if not linker_script:
        raise ToolError("give linker_script or svd")
    text = _need_file(_path(linker_script), "linker script").read_text(errors="replace")
    ivt = text[text.find("Interrupt Vector Table"):] if "Interrupt Vector Table" in text else text
    ivt = ivt[:ivt.find("Alternate") if "Alternate" in ivt else len(ivt)]
    names = re.findall(r"LONG\s*\(\s*DEFINED\s*\(\s*(__\w+)\s*\)", ivt)
    vectors = [{"index": i - skip_traps, "name": n.lstrip("_")} for i, n in enumerate(names) if i >= skip_traps]
    return {"ok": True, "count": len(vectors), "vectors": vectors}


_REPL_ENTRY = re.compile(r"^(\w+)\s*:\s*([\w.]+)\s*@\s*sysbus\s*(<\s*)?(0x[0-9A-Fa-f]+)(?:\s*,\s*\+?(0x[0-9A-Fa-f]+)\s*>)?", re.M)


@tool("SFR coverage of a Renode platform: which registers of the SFR map no peripheral (or the CPU) covers. "
      "Sizes come from `size:` lines or <addr, +size> in the .repl, then from `sizes` ({name: size}); others are "
      "assumed to reach the next peripheral (at most 0x100). `cpu_ranges`: [[start, end], ...] served by the CPU core.",
      {"sfr_map": S, "platform": S, "sizes": {"type": "object"}, "cpu_ranges": {"type": "array"}, "max_address": S},
      ["sfr_map", "platform"])
def sfr_coverage(sfr_map: str, platform: str, sizes: dict | None = None, cpu_ranges: list | None = None,
                 max_address: str | None = None) -> dict:
    data = json.loads(_need_file(_path(sfr_map), "SFR map").read_text())
    text = _need_file(_path(platform), "platform").read_text()
    entries = []
    for m in _REPL_ENTRY.finditer(text):
        name, kind, _, addr, size = m.groups()
        block = text[m.end():].split("\n\n", 1)[0]
        sm = re.search(r"^\s+size:\s*(0x[0-9A-Fa-f]+|\d+)", block, re.M)
        entries.append({"name": name, "type": kind, "start": int(addr, 16),
                        "size": int(size, 16) if size else (_int(sm.group(1)) if sm else (sizes or {}).get(name))})
    entries.sort(key=lambda e: e["start"])
    for i, e in enumerate(entries):
        if e["size"] is None:
            nxt = entries[i + 1]["start"] if i + 1 < len(entries) else e["start"] + 0x100
            e["size"], e["guessed"] = min(0x100, nxt - e["start"]), True
        e["size"] = _int(e["size"])
    ranges = [(e["start"], e["start"] + e["size"], e["name"]) for e in entries]
    ranges += [(_int(a), _int(b), "cpu") for a, b in (cpu_ranges or [])]
    limit = _int(max_address) if max_address else None
    uncovered, per = [], collections.Counter()
    for r in data["registers"]:
        if limit is not None and r["address"] >= limit:
            continue
        owner = next((n for lo, hi, n in ranges if lo <= r["address"] < hi), None)
        if owner is None:
            uncovered.append(f"{r['name']}@0x{r['address']:04X}")
        else:
            per[owner] += 1
    return {"ok": True, "uncovered": uncovered[:200], "uncovered_count": len(uncovered),
            "registers_per_peripheral": dict(per.most_common()),
            "guessed_sizes": [e["name"] for e in entries if e.get("guessed")]}


# ----------------------------------------------------------------------------- firmware

def hex_images(hex_file: Path, out_dir: Path, base: int, gap: int = 0x1000, phantom_lane: int | None = None,
               align: int = 4, pad: int = 0xFF) -> list[dict]:
    mem, upper = {}, 0
    for line in hex_file.read_text().splitlines():
        if not line.startswith(":"):
            continue
        n, addr, kind = int(line[1:3], 16), int(line[3:7], 16), int(line[7:9], 16)
        data = bytes.fromhex(line[9:9 + 2 * n])
        if kind == 4:
            upper = int.from_bytes(data, "big") << 16
        elif kind == 2:
            upper = int.from_bytes(data, "big") << 4
        elif kind == 0:
            for i, b in enumerate(data):
                mem[upper + addr + i] = b
    if not mem:
        raise ToolError(f"no data records in {hex_file}")
    addrs = sorted(mem)
    regions, start, prev = [], addrs[0], addrs[0]
    for a in addrs[1:]:
        if a - prev > gap:
            regions.append((start, prev + 1))
            start = a
        prev = a
    regions.append((start, prev + 1))
    out_dir.mkdir(parents=True, exist_ok=True)
    images = []
    for lo, hi in regions:
        lo -= lo % align
        hi += (-hi) % align
        blob = bytes(mem.get(a, 0 if phantom_lane is not None and a % 4 == phantom_lane else pad) for a in range(lo, hi))
        path = out_dir / f"{hex_file.stem}_{lo:07x}.bin"
        path.write_bytes(blob)
        images.append({"file": str(path), "address": f"0x{base + lo:x}", "hex_address": f"0x{lo:x}", "bytes": len(blob)})
    return images


@tool("Convert an Intel HEX file into binary images for `sysbus LoadBinary` (one per contiguous region). "
      "`base` is added to the HEX addresses (dsPIC profile: 0x01000000); `phantom_lane` zero-fills that byte of every "
      "4-byte word (dsPIC: 3). Returns the files and the Renode load commands.",
      {"hex": S, "out_dir": S, "base": S, "phantom_lane": I, "profile": S}, ["hex"])
def hex_to_images(hex: str, out_dir: str | None = None, base: str | None = None, phantom_lane: int | None = None,
                  profile: str | None = None) -> dict:
    image = load_profile(profile).get("image", {}) if profile else {}
    hex_path = _need_file(_path(hex), "HEX file")
    base_v = _int(base if base is not None else image.get("base", 0))
    lane = phantom_lane if phantom_lane is not None else image.get("phantom_lane")
    images = hex_images(hex_path, _path(out_dir) or hex_path.parent / "out", base_v, phantom_lane=lane)
    return {"ok": True, "images": images,
            "load_commands": [f"sysbus LoadBinary @{i['file']} {i['address']}" for i in images]}


@tool("Build a test firmware with the profile's toolchain (C or assembly), then make the HEX and LoadBinary images. "
      "Returns elf, hex, images, load commands, and compiler errors/warnings (deduplicated).",
      {"profile": S, "source": S, "out_dir": S, "extra_flags": {"type": "array", "items": S}}, ["profile", "source"])
def build_firmware(profile: str, source: str, out_dir: str | None = None, extra_flags: list | None = None) -> dict:
    p = load_profile(profile)
    tc = p.get("toolchain", {})
    src = _need_file(_path(source), "source")
    out = _path(out_dir) or src.parent / "build"
    out.mkdir(parents=True, exist_ok=True)
    elf = out / (src.stem + ".elf")
    logs = []
    if tc.get("style") == "ti":
        return _build_ti(p, tc, src, out, extra_flags)
    if src.suffix.lower() in (".s", ".asm"):
        obj = out / (src.stem + ".o")
        r = _run([tc["as"], *tc.get("asflags", []), "-o", str(obj), str(src)])
        logs.append(r.stdout + r.stderr)
        if r.returncode == 0:
            r = _run([tc["ld"], *tc.get("ldflags", []), "-o", str(elf), str(obj), *tc.get("ldflags_post", [])])
            logs.append(r.stdout + r.stderr)
    else:
        r = _run([tc["cc"], *tc.get("cflags", []), *(extra_flags or []), "-o", str(elf), str(src), *tc.get("cflags_post", [])])
        logs.append(r.stdout + r.stderr)
    text = "\n".join(logs)
    if r.returncode != 0 or not elf.exists():
        return {"ok": False, "stage": "compile/link", "errors": _lines(text, r"error|undefined|cannot", 25)}
    hex_path = elf.with_suffix(".hex")
    if tc.get("bin2hex"):
        r = _run([tc["bin2hex"], str(elf)])
    elif tc.get("objcopy"):
        r = _run([tc["objcopy"], "-O", "ihex", str(elf), str(hex_path)])
    if not hex_path.exists():
        return {"ok": False, "stage": "hex", "errors": _lines(r.stdout + r.stderr, r".", 10)}
    image = p.get("image", {})
    images = hex_images(hex_path, out / "out", _int(image.get("base", 0)), phantom_lane=image.get("phantom_lane"))
    # a profile can give its own load commands (e.g. per-CPU loads on multi-core parts), ';'-separated
    loads = ([c.strip().format(elf=elf) for c in image["loader"].split(";")] if image.get("loader")
             else [f"sysbus LoadBinary @{i['file']} {i['address']}" for i in images])
    return {"ok": True, "elf": str(elf), "hex": str(hex_path), "images": images, "load_commands": loads,
            "warnings": _lines(text, r"warning", 15)}


def _build_ti(p: dict, tc: dict, src: Path, out: Path, extra_flags: list | None) -> dict:
    """TI code generation tools (cl2000): compile and link in one call, load the ELF with LoadTIProgram."""
    elf = out / (src.stem + ".out")
    link = [*tc.get("link_files", [])]
    if tc.get("noinit_cmd"):
        # EABI zero-initialises uninitialised sections: the register "variables" of the device headers
        # must be NOINIT, or the start-up code writes 0 to every peripheral register
        text = Path(tc["noinit_cmd"]).read_text()
        noinit = out / (Path(tc["noinit_cmd"]).stem + "_noinit.cmd")
        noinit.write_text(re.sub(r"(> *[A-Za-z0-9_]+, *PAGE *= *1)", r"\1, type=NOINIT", text))
        link.append(str(noinit))
    cmd = [tc["cc"], *tc.get("cflags", []), *(extra_flags or []), f"--obj_directory={out}", str(src),
           *tc.get("extra_sources", []), "-z", f"--output_file={elf}", f"--map_file={elf.with_suffix('.map')}",
           *tc.get("ldflags", []), *link, *tc.get("libs", [])]
    r = _run(cmd)
    text = r.stdout + r.stderr
    if r.returncode != 0 or not elf.exists():
        return {"ok": False, "stage": "compile/link", "errors": _lines(text, r"error|undefined|cannot", 25)}
    loader = p.get("image", {}).get("loader", "cpu LoadTIProgram @{elf}")
    return {"ok": True, "elf": str(elf), "map": str(elf.with_suffix(".map")), "images": [],
            "load_commands": [loader.format(elf=elf)], "warnings": _lines(text, r"warning", 15)}


@tool("Disassemble part of an ELF with the profile's objdump: a symbol, or an address range (program addresses as the "
      "toolchain prints them).", {"profile": S, "elf": S, "symbol": S, "start": S, "end": S, "max_lines": I},
      ["profile", "elf"])
def disassemble(profile: str, elf: str, symbol: str | None = None, start: str | None = None, end: str | None = None,
                max_lines: int = 80) -> dict:
    objdump = load_profile(profile).get("toolchain", {}).get("objdump")
    if not objdump:
        raise ToolError("profile has no toolchain.objdump")
    tc = load_profile(profile).get("toolchain", {})
    r = _run([objdump, *tc.get("objdump_args", ["-d"]), str(_need_file(_path(elf), "ELF"))])
    lines = r.stdout.splitlines()
    if symbol:
        # GNU objdump "<sym>:" or TI dis2000 "00009000        sym:"
        pat = rf"<_?{re.escape(symbol)}>:|^[0-9a-f]+\s+_?{re.escape(symbol)}:\s*$"
        idx = next((i for i, l in enumerate(lines) if re.search(pat, l)), None)
        if idx is None:
            raise ToolError(f"symbol {symbol} not found")
        sel = lines[idx:idx + max_lines]
    else:
        lo, hi = _int(start or 0), _int(end) if end else 1 << 40
        sel = [l for l in lines if (m := re.match(r"\s*([0-9a-f]+):|([0-9a-f]{8})   [0-9a-f]{4}", l))
               and lo <= int(m.group(1) or m.group(2), 16) < hi][:max_lines]
    return {"ok": True, "lines": sel}


# ----------------------------------------------------------------------------- Renode

def _renode(renode_root: str | None) -> tuple[str, Path]:
    binary = os.environ.get("RENODE_BIN") or shutil.which("renode")
    if not binary:
        raise ToolError("set RENODE_BIN to the Renode executable")
    root = _path(renode_root) if renode_root else Path(os.environ.get("RENODE_ROOT", Path(binary).resolve().parent))
    return binary, root


def _run_script(lines: list[str], renode_root: str | None, timeout: int) -> tuple[str, float]:
    binary, root = _renode(renode_root)
    with tempfile.TemporaryDirectory(prefix="mcu_emu_") as tmp:
        script = Path(tmp) / "run.resc"
        script.write_text("\n".join(lines + ["quit"]) + "\n")
        t0 = time.time()
        r = _run([binary, "--disable-xwt", "--console", "-e", f"include @{script}"], cwd=root, timeout=timeout,
                 open_stdin=True)
    clean = re.sub(r"\x1b\[[0-9;]*m", "", r.stdout + r.stderr).replace("\r", "").replace("﻿", "")
    return clean, time.time() - t0


def _problems(text: str) -> dict:
    counts = collections.Counter()
    for l in text.splitlines():
        if "[WARNING]" in l or "[ERROR]" in l or "There was an error" in l or "Exception" in l:
            counts[re.sub(r"^\[[0-9:.]+\]\s*", "", l.strip())[:200]] += 1
    return {"distinct": len(counts), "top": [f"{n}x {m}" for m, n in counts.most_common(12)]}


def _load_lines(loads: list | None) -> list[str]:
    out = []
    for item in loads or []:
        if isinstance(item, str):
            # monitor commands ("sysbus ...", "cpu LoadTIProgram ...", "c28 LoadTIProgram ...", "m3 VectorTableOffset 0")
            # pass through; a bare path is a binary to load
            raw = item.startswith(("sysbus", "cpu ")) or re.match(r"^[\w.]+ [A-Z]\w+", item)
            out.append(item if raw else f"sysbus LoadBinary @{item}")
        else:
            kind = item.get("kind", "binary")
            if kind == "elf":
                out.append(f"sysbus LoadELF @{item['file']}")
            elif kind == "ti":
                out.append(f"cpu LoadTIProgram @{item['file']}")
            elif kind == "hex":
                out.append(f"sysbus LoadHEX @{item['file']}")
            else:
                out.append(f"sysbus LoadBinary @{item['file']} {item['address']}")
    return out


@tool("Load a Renode platform (.repl, plus optional extra .repl files) and report errors only. Fast check after "
      "editing platform files or peripheral models.", {"platform": S, "extra_repl": {"type": "array", "items": S},
                                                       "renode_root": S}, ["platform"])
def renode_check_platform(platform: str, extra_repl: list | None = None, renode_root: str | None = None) -> dict:
    lines = ["mach create", f"machine LoadPlatformDescription @{platform}"]
    lines += [f"machine LoadPlatformDescription @{x}" for x in extra_repl or []]
    lines += ['echo "@@PERIPHERALS"', "peripherals"]
    text, wall = _run_script(lines, renode_root, 300)
    errors = _lines(text, r"error|exception|could not|not found", 15)
    return {"ok": not errors, "errors": errors, "seconds": round(wall, 1)}


@tool("Run firmware in Renode headless and return what you ask for. `loads`: LoadBinary/LoadELF/LoadHEX items "
      "({file, address} or {file, kind: elf|hex|ti}) or the load_commands from build_firmware. `before`/`after`: monitor "
      "commands around the run (e.g. 'adc SetVoltage 1250000 3'); after-commands return their output. `reads`: "
      "addresses to read after the run ('0xA00', '0xA00:8' for a byte, ':32' for a double word). `uart`: peripheral "
      "names whose output is captured. `stimuli`: [{at: seconds, command}] run at those times. Returns values, UART text, "
      "warnings/errors (counted), executed instructions and wall time. `cpu`: the CPU whose PC is reported and in whose "
      "address map `reads` are done (multi-core platforms, e.g. 'm3' or 'c28').",
      {"platform": S, "loads": {"type": "array"}, "run_for": S, "before": {"type": "array", "items": S},
       "after": {"type": "array", "items": S}, "reads": {"type": "array", "items": S},
       "uart": {"type": "array", "items": S}, "stimuli": {"type": "array"}, "extra_repl": {"type": "array", "items": S},
       "cpu": S, "renode_root": S, "timeout": I}, ["platform", "loads"])
def renode_run(platform: str, loads: list, run_for: str = "0.1", before: list | None = None, after: list | None = None,
               reads: list | None = None, uart: list | None = None, stimuli: list | None = None,
               extra_repl: list | None = None, cpu: str = "cpu", renode_root: str | None = None, timeout: int = 900) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="mcu_emu_uart_"))
    lines = ['mach create "m"', f"machine LoadPlatformDescription @{platform}"]
    lines += [f"machine LoadPlatformDescription @{x}" for x in extra_repl or []]
    lines += _load_lines(loads)
    for u in uart or []:
        lines.append(f"{u} CreateFileBackend @{tmp / (u + '.txt')} true")
    lines += list(before or [])
    elapsed = 0.0
    for s in sorted(stimuli or [], key=lambda s: float(s["at"])):
        at = float(s["at"])
        if at > elapsed:
            lines.append(f'emulation RunFor "{at - elapsed:.9f}"')
            elapsed = at
        lines.append(s["command"])
    total = float(run_for)
    if total > elapsed:
        lines.append(f'emulation RunFor "{total - elapsed:.9f}"')
    for i, cmd in enumerate(after or []):
        lines += [f'echo "@@AFTER {i}"', cmd]
    for i, r in enumerate(reads or []):
        addr, _, width = str(r).partition(":")
        op = {"8": "ReadByte", "32": "ReadDoubleWord"}.get(width, "ReadWord")
        # multi-core platforms register memory per CPU: read in the named CPU's address map
        ctx = f" {cpu}" if cpu != "cpu" else ""
        lines += [f'echo "@@READ {i}"', f"sysbus {op} {addr}{ctx}"]
    lines += ['echo "@@CPU"', f"{cpu} PC", f"{cpu} ExecutedInstructions", 'echo "@@END"']
    text, wall = _run_script(lines, renode_root, timeout)

    def section(tag: str) -> str:
        m = re.search(re.escape(tag) + r"\n(.*?)(?=\n@@|\Z)", text, re.S)
        return m.group(1).strip() if m else ""

    values = {str(r): section(f"@@READ {i}").splitlines()[-1:] for i, r in enumerate(reads or [])}
    after_out = {cmd: section(f"@@AFTER {i}")[-1500:] for i, cmd in enumerate(after or [])}
    cpu_out = section("@@CPU").splitlines()
    uart_text = {}
    for u in uart or []:
        f = tmp / (u + ".txt")
        uart_text[u] = f.read_text(errors="replace")[-3000:] if f.exists() else ""
    shutil.rmtree(tmp, ignore_errors=True)
    executed = next((l for l in cpu_out[1:] if l.startswith("0x")), None)
    result = {"ok": "@@END" in text, "reads": {k: (v[0] if v else None) for k, v in values.items()},
              "after": after_out, "uart": uart_text, "pc": cpu_out[0] if cpu_out else None,
              "executed_instructions": int(executed, 16) if executed else None,
              "wall_seconds": round(wall, 1), "problems": _problems(text)}
    if result["executed_instructions"] and wall > 0:
        result["host_mips"] = round(result["executed_instructions"] / max(wall - 3.0, 0.1) / 1e6, 2)
    if not result["ok"]:
        result["tail"] = text.splitlines()[-25:]
    return result


@tool("Sample the program counter while firmware runs, to find hot spots (slow polling loops, bit-banging). "
      "Returns the most frequent PCs. `pc_command` reads the PC (default 'cpu PC'; dsPIC: 'cpu GetRegister 26' for the ISA PC).",
      {"platform": S, "loads": {"type": "array"}, "warmup": S, "samples": I, "interval": S, "pc_command": S,
       "extra_repl": {"type": "array", "items": S}, "renode_root": S}, ["platform", "loads"])
def pc_hotspots(platform: str, loads: list, warmup: str = "0.2", samples: int = 150, interval: str = "0.0013",
                pc_command: str = "cpu PC", extra_repl: list | None = None, renode_root: str | None = None) -> dict:
    lines = ['mach create "m"', f"machine LoadPlatformDescription @{platform}"]
    lines += [f"machine LoadPlatformDescription @{x}" for x in extra_repl or []]
    lines += _load_lines(loads) + [f'emulation RunFor "{warmup}"']
    for _ in range(samples):
        lines += [f'emulation RunFor "{interval}"', 'echo "@@PC"', pc_command]
    text, wall = _run_script(lines, renode_root, 1800)
    pcs = re.findall(r"@@PC\s*\n\s*(0x[0-9A-Fa-f]+)", text)
    hist = collections.Counter(p.lower() for p in pcs)
    return {"ok": bool(pcs), "samples": len(pcs), "top": [{"pc": p, "count": n} for p, n in hist.most_common(15)],
            "wall_seconds": round(wall, 1), "hint": "disassemble the top addresses to see the loop"}


@tool("Build Renode (`target`: 'all' runs build.sh; 'core' rebuilds one tlib core with make and copies the library). "
      "Returns success and the distinct compiler errors.",
      {"renode_root": S, "target": S, "arch": S, "timeout": I}, ["renode_root"])
def renode_build(renode_root: str, target: str = "all", arch: str = "dspic33", timeout: int = 2400) -> dict:
    root = _need_file(_path(renode_root), "Renode source tree")
    if target == "all":
        r = _run(["./build.sh"], cwd=root, timeout=timeout)
    else:
        obj = root / f"src/Infrastructure/src/Emulator/Cores/obj/Release/linux-x64/{arch}/le"
        _need_file(obj, "core build directory (run a full build once)")
        _run(["cmake", "."], cwd=obj, timeout=timeout)
        r = _run(["make", "-j8"], cwd=obj, timeout=timeout)
        lib = obj / f"tlib/translate-{arch}-le.so"
        if r.returncode == 0 and lib.exists():
            for dest in (root / "src/Infrastructure/src/Emulator/Cores/bin/Release/linux-x64",
                         root / "output/bin/Release/platform-lib/linux-x64"):
                if dest.exists():
                    shutil.copy2(lib, dest)
    text = r.stdout + r.stderr
    errors = _lines(text, r"\berror\b", 30)
    return {"ok": r.returncode == 0 and not [e for e in errors if " 0 Error" not in e],
            "errors": [e for e in errors if " 0 Error" not in e], "tail": text.splitlines()[-5:]}


# ----------------------------------------------------------------------------- checks

def _read_trace(path: Path) -> list[dict]:
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(json.loads(line))
    return out


@tool("Convert a column trace (one step per line, whitespace-separated numbers, e.g. a native runner's 'PC OP W0..W15 SR') "
      "into JSON Lines for compare_traces. `columns` names the columns in order ('-' skips one); lines whose first "
      "token is not a number are dropped. `base`: 16 or 10.",
      {"input": S, "out": S, "columns": {"type": "array", "items": S}, "base": I}, ["input", "out", "columns"])
def trace_from_columns(input: str, out: str, columns: list, base: int = 16) -> dict:
    src = _need_file(_path(input), "trace")
    dst = _path(out)
    dst.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with dst.open("w") as f:
        for line in src.read_text(errors="replace").splitlines():
            parts = line.split()
            if not parts:
                continue
            try:
                values = [int(x, base) for x in parts[:len(columns)]]
            except ValueError:
                continue
            f.write(json.dumps({c: v for c, v in zip(columns, values) if c != "-"}) + "\n")
            n += 1
    return {"ok": n > 0, "steps": n, "out": str(dst)}


@tool("Compare two execution traces step by step (JSON Lines, one object per instruction, e.g. "
      '{"pc": 256, "regs": {"W0": 1}, "sr": 3}). Reports the first divergence with the previous step, after applying '
      "`ignore` (field names) and `masks` ({field: bitmask of bits to ignore}); counts how often each mask was needed.",
      {"reference": S, "candidate": S, "ignore": {"type": "array", "items": S}, "masks": {"type": "object"},
       "max_steps": I}, ["reference", "candidate"])
def compare_traces(reference: str, candidate: str, ignore: list | None = None, masks: dict | None = None,
                   max_steps: int = 10_000_000) -> dict:
    ref = _read_trace(_need_file(_path(reference), "reference trace"))
    got = _read_trace(_need_file(_path(candidate), "candidate trace"))
    ignore_set = set(ignore or [])
    masks = {k: _int(v) for k, v in (masks or {}).items()}
    tolerated = collections.Counter()

    def flat(step: dict) -> dict:
        out = {}
        for k, v in step.items():
            if isinstance(v, dict):
                out.update({f"{k}.{kk}": vv for kk, vv in v.items()})
            else:
                out[k] = v
        return out

    n = min(len(ref), len(got), max_steps)
    for i in range(n):
        a, b = flat(ref[i]), flat(got[i])
        diffs = {}
        for key in sorted(set(a) | set(b)):
            if key in ignore_set or key.split(".")[-1] in ignore_set:
                continue
            x, y = a.get(key), b.get(key)
            if x == y:
                continue
            if key in masks and isinstance(x, int) and isinstance(y, int) and (x ^ y) & ~masks[key] == 0:
                tolerated[key] += 1
                continue
            diffs[key] = {"reference": x, "candidate": y}
        if diffs:
            return {"ok": False, "identical_steps": i, "step": i, "differences": diffs,
                    "previous_reference": ref[i - 1] if i else None, "tolerated": dict(tolerated)}
    return {"ok": True, "identical_steps": n, "reference_steps": len(ref), "candidate_steps": len(got),
            "tolerated": dict(tolerated)}


@tool("Scan files or directories for private names before committing (product, board, customer names, private "
      "paths). Patterns come from `patterns` and/or a rules file kept outside the repository (one regex per line, or "
      "'replace<TAB>regex<TAB>...' / 'private<TAB>regex' lines as used by the chat renderer). Matches are shown masked.",
      {"paths": {"type": "array", "items": S}, "patterns": {"type": "array", "items": S}, "rules_file": S},
      ["paths"])
def privacy_scan(paths: list, patterns: list | None = None, rules_file: str | None = None) -> dict:
    regexes = [re.compile(p, re.I) for p in patterns or []]
    if rules_file:
        for line in _need_file(_path(rules_file), "rules file").read_text().splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.split("\t")
            regexes.append(re.compile(parts[1] if parts[0] in ("replace", "private") and len(parts) > 1 else parts[0], re.I))
    if not regexes:
        raise ToolError("give patterns or rules_file")
    hits, files = [], 0
    for p in paths:
        root = _need_file(_path(p), "path")
        for f in ([root] if root.is_file() else [x for x in root.rglob("*") if x.is_file() and ".git" not in x.parts]):
            files += 1
            try:
                text = f.read_text(errors="strict")
            except (UnicodeDecodeError, OSError):
                continue
            for n, line in enumerate(text.splitlines(), 1):
                for rx in regexes:
                    m = rx.search(line)
                    if m:
                        s = m.group(0)
                        hits.append(f"{f}:{n}: {s[:2]}{'*' * max(0, len(s) - 2)}")
                        break
    return {"ok": not hits, "files_scanned": files, "matches": hits[:100], "match_count": len(hits)}


# ----------------------------------------------------------------------------- code generation

_SKELETON = '''//
// {name}: generated by mcu-emu peripheral_skeleton from the SFR map; fill in the TODOs.
// This file is licensed under the MIT License.
//
using System;

using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;

namespace {namespace}
{{
    /// <summary>
    /// {name} at 0x{base:04X} ({count} registers, {width}-bit).
    /// TODO: describe the behaviour, timing, interrupts and limits (see the peripheral-models skill).
    /// </summary>
    [AllowedTranslations({translations})]
    public class {name} : I{wordiface}Peripheral, IKnownSize
    {{
        public {name}(IMachine machine)
        {{
            this.machine = machine;
{irq_init}            Reset();
        }}

        public long Size => 0x{size:X};
{irq_props}
        public void Reset()
        {{
{reset}        }}

        public {ctype} Read{wordname}(long offset)
        {{
            switch(offset)
            {{
{read_cases}            default:
                this.Log(LogLevel.Warning, "Read from unknown offset 0x{{0:X}}", offset);
                return 0;
            }}
        }}

        public void Write{wordname}(long offset, {ctype} value)
        {{
            switch(offset)
            {{
{write_cases}            default:
                this.Log(LogLevel.Warning, "Write 0x{{0:X}} to unknown offset 0x{{1:X}}", value, offset);
                break;
            }}
        }}

{fields}        private readonly IMachine machine;
    }}
}}
'''


@tool("Generate a C# Renode peripheral skeleton for one register block: a field per register, Read/Write switch with "
      "bit-field comments, Reset, IRQ GPIOs, TODO markers. Registers come from an SFR map (start..end) or a list "
      "[{name, offset, reset}]. Writes the .cs file and returns its path.",
      {"name": S, "out": S, "sfr_map": S, "start": S, "end": S, "registers": {"type": "array"}, "width": I,
       "irqs": {"type": "array", "items": S}, "namespace": S}, ["name", "out"])
def peripheral_skeleton(name: str, out: str, sfr_map: str | None = None, start: str | None = None, end: str | None = None,
                        registers: list | None = None, width: int = 16, irqs: list | None = None,
                        namespace: str = "Antmicro.Renode.Peripherals.Miscellaneous") -> dict:
    if not re.fullmatch(r"[A-Za-z_]\w*", name):
        raise ToolError("name must be a C# identifier")
    regs = []
    if sfr_map:
        data = json.loads(_need_file(_path(sfr_map), "SFR map").read_text())
        lo, hi = _int(start), _int(end)
        regs = [{"name": r["name"], "offset": r["address"] - lo, "fields": r.get("fields", []), "reset": r.get("reset")}
                for r in data["registers"] if lo <= r["address"] < hi]
        base = lo
    else:
        regs = [{"name": r["name"], "offset": _int(r["offset"]), "reset": r.get("reset"), "fields": r.get("fields", [])}
                for r in registers or []]
        base = _int(start or 0)
    if not regs:
        raise ToolError("no registers: give sfr_map with start/end or registers")
    ctype, wordname, iface, step = {8: ("byte", "Byte", "Byte", 1), 16: ("ushort", "Word", "Word", 2),
                                    32: ("uint", "DoubleWord", "DoubleWord", 4)}[width]
    translations = {8: "AllowedTranslation.WordToByte", 16: "AllowedTranslation.ByteToWord",
                    32: "AllowedTranslation.ByteToDoubleWord | AllowedTranslation.WordToDoubleWord"}[width]
    seen, rr = set(), []
    for r in sorted(regs, key=lambda r: r["offset"]):
        field = re.sub(r"\W", "_", r["name"]).lower()
        if r["offset"] in seen:
            continue
        seen.add(r["offset"])
        rr.append({**r, "field": field})
    read_cases = write_cases = fields = reset = ""
    for r in rr:
        bits = " ".join(f"{f['name']}:{f['lsb']}" + (f"-{f['lsb'] + f['width'] - 1}" if f["width"] > 1 else "") for f in r["fields"])
        comment = f"            // {r['name']}" + (f": {bits}" if bits else "") + "\n"
        read_cases += f"{comment}            case 0x{r['offset']:X}:\n                return {r['field']};   // TODO: computed bits, side effects\n"
        write_cases += f"{comment}            case 0x{r['offset']:X}:\n                {r['field']} = value;   // TODO: writable mask, actions\n                break;\n"
        fields += f"        private {ctype} {r['field']};\n"
        reset += f"            {r['field']} = {_int(r['reset']) if r.get('reset') else 0};\n"
    irq_init = "".join(f"            {i} = new GPIO();\n" for i in irqs or [])
    irq_props = "".join(f"\n        public GPIO {i} {{ get; }}\n" for i in irqs or [])
    size = rr[-1]["offset"] + step
    code = _SKELETON.format(name=name, namespace=namespace, base=base, count=len(rr), width=width, translations=translations,
                            wordiface=iface, size=size, irq_init=irq_init, irq_props=irq_props, reset=reset, ctype=ctype,
                            wordname=wordname, read_cases=read_cases, write_cases=write_cases, fields=fields + "\n")
    path = _path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(code)
    return {"ok": True, "out": str(path), "registers": len(rr), "size": f"0x{size:X}",
            "next": "fill the TODOs, add it to the platform .repl, run renode_build and renode_check_platform"}
