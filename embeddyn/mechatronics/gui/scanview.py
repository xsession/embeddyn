"""The Lab's Scan view: read a printed schematic (PDF, PNG, JPEG ...) into a netlist, show what was read over the
page, and import the result as an emulated board project.

``run_scan`` writes into a scan folder (default ``<project root>/.embeddyn/scans/<name>``): the inputs that were
uploaded from the browser, ``schematic.net``, ``scan.json`` and ``overlay-pN.png``. ``import_board`` runs the
board importer on that folder's netlist (into ``<scan folder>/board``) and returns the project file to open.
"""
from __future__ import annotations

import base64
import re
import shutil
from pathlib import Path
from typing import Any

from ..importer.schematic_scan import IMAGE_SUFFIXES, scan_to_files

SCAN_SUFFIXES = IMAGE_SUFFIXES | {".pdf"}
MAX_UPLOAD = 24 * 1024 * 1024        # base64 in a JSON body of at most 32 MB


def _safe_name(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name).strip("._") or "page"
    return stem[:80]


def _pages(text: Any) -> list[int] | None:
    text = str(text or "").strip()
    if not text:
        return None
    pages: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            pages.extend(range(int(a), int(b) + 1))
        elif part:
            pages.append(int(part))
    return pages or None


def _resolve(value: str, root: Path) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else root / path).resolve()


def run_scan(body: dict[str, Any]) -> dict[str, Any]:
    root = Path(str(body.get("root") or ".")).expanduser().resolve()
    inputs = [_resolve(str(p), root) for p in body.get("paths") or [] if str(p).strip()]
    uploads = list(body.get("uploads") or [])
    if not inputs and not uploads:
        raise ValueError("give a PDF or an image of the schematic (a path, or drop the file)")
    for path in inputs:
        if not path.is_file():
            raise FileNotFoundError(str(path))
        if path.suffix.lower() not in SCAN_SUFFIXES:
            raise ValueError(f"{path.name}: not a PDF or an image ({', '.join(sorted(SCAN_SUFFIXES))})")
    first = inputs[0].stem if inputs else Path(str(uploads[0].get("name") or "upload")).stem
    out = _resolve(str(body["out"]), root) if body.get("out") else root / ".embeddyn" / "scans" / _safe_name(first)
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("overlay-p*.png"):
        stale.unlink()
    if uploads:
        folder = out / "input"
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir()
        for upload in uploads:
            name = _safe_name(str(upload.get("name") or "page"))
            if Path(name).suffix.lower() not in SCAN_SUFFIXES:
                raise ValueError(f"{name}: not a PDF or an image")
            data = base64.b64decode(str(upload.get("data") or ""), validate=True)
            if len(data) > MAX_UPLOAD:
                raise ValueError(f"{name}: larger than {MAX_UPLOAD // (1024 * 1024)} MB")
            target = folder / name
            target.write_bytes(data)
            inputs.append(target)
    dpi = int(body["dpi"]) if body.get("dpi") else None
    result, files = scan_to_files(inputs if len(inputs) > 1 else inputs[0], out, _pages(body.get("pages")), dpi,
                                  overlay=True)
    netlist, report = result.netlist, result.report
    guessed = set(report.guessed_pins)

    # where each pin and part is on its page (page points), so the view can point at them over the overlay
    positions: dict[str, list[float]] = {}
    boxes: dict[str, list[float]] = {}
    pages = []
    for page_result in result.pages:
        page = page_result.page
        for symbol in page_result.symbols.values():
            boxes.setdefault(symbol.ref, [page.index, *[round(v, 1) for v in symbol.box]])
            for contact in symbol.contacts:
                if contact.pin:
                    positions.setdefault(f"{symbol.ref}.{contact.pin}", [page.index, round(contact.x, 1),
                                                                        round(contact.y, 1)])
        overlay = files.get(f"overlay{page.index}")
        pages.append({"index": page.index, "width": page.width, "height": page.height,
                      "source": str(getattr(page, "source", "") or ""), "raster": bool(page.raster),
                      "overlay": str(overlay) if overlay else None,
                      "parts": len(page_result.symbols), "unnamed": len(page_result.unnamed_clusters)})

    nets = [{"name": net.name,
             "nodes": [{"ref": n.ref, "pin": n.pin, "name": n.pin_name or "",
                        "guessed": f"{n.ref}.{n.pin}" in guessed} for n in net.nodes]}
            for net in netlist.nets]
    pins_of: dict[str, int] = {}
    for net in netlist.nets:
        for n in net.nodes:
            pins_of[n.ref] = pins_of.get(n.ref, 0) + 1
    parts = [{"ref": ref, "value": c.value, "page": boxes.get(ref, [None])[0], "pins": pins_of.get(ref, 0)}
             for ref, c in sorted(netlist.components.items(), key=lambda kv: _natural(kv[0]))]
    payload: dict[str, Any] = {
        "out": str(out), "netlist": str(files["netlist"]), "report": str(files["report"]),
        "inputs": [str(p) for p in inputs], "pages": pages, "parts": parts, "nets": nets,
        "guessed": sorted(guessed), "notes": list(report.notes), "positions": positions, "boxes": boxes,
    }
    if body.get("reference"):
        from ..importer.kicad_netlist import read_netlist
        from ..importer.schematic_scan.compare import compare
        score = compare(netlist, read_netlist(_resolve(str(body["reference"]), root)), guessed)
        payload["score"] = {"summary": score.summary(), "precision": score.precision, "recall": score.recall,
                            "missing": score.components_missing, "extra": score.components_extra}
    return payload


def _natural(ref: str) -> tuple:
    m = re.match(r"([A-Za-z#_]*)(\d*)(.*)", ref)
    return (m.group(1), int(m.group(2) or 0), m.group(3)) if m else (ref, 0, "")


def import_board(body: dict[str, Any]) -> dict[str, Any]:
    """The board importer on a scan folder's netlist: Renode board, bindings, a project to open."""
    from ..importer import import_project
    out = Path(str(body.get("out") or "")).expanduser().resolve()
    netlist = out / "schematic.net"
    if not netlist.is_file():
        raise FileNotFoundError(f"{netlist}: scan the schematic first")
    source = out / "board-source"
    source.mkdir(exist_ok=True)
    shutil.copyfile(netlist, source / "schematic.net")
    firmware = str(body.get("firmware") or "").strip()
    substitutions = {}
    for item in re.split(r"[,\n]", str(body.get("substitute") or "")):
        if "=" in item:
            ref, part = item.split("=", 1)
            substitutions[ref.strip()] = part.strip()
    target = out / "board"
    result = import_project(source, target, title=str(body.get("title") or out.name),
                            build_firmware=False, substitutions=substitutions)
    project = target / "project.yaml"
    if firmware:
        # a firmware the user already has (ELF/HEX/BIN): the project runs it on the imported board
        import yaml
        path = Path(firmware).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(str(path))
        raw = yaml.safe_load(project.read_text(encoding="utf-8"))
        for instance in (raw.get("instances") or {}).values():
            options = instance.get("options") or {}
            if "firmware" in options or options.get("backend") == "renode":
                options["firmware"] = str(path)
        project.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    plan = result.plan
    return {
        "project": str(project), "folder": str(target),
        "mcu": f"{plan.mcu_ref} {plan.mcu_part}".strip() if plan.mcu_ref else None,
        "platform": plan.platform or None,
        "devices": [f"{d.ref} {d.name} → {d.model} on {d.bus}" for d in plan.devices],
        "findings": [f"{f.level}: {f.message}" for f in plan.findings],
        "report": (target / "report.md").read_text(encoding="utf-8") if (target / "report.md").exists() else "",
    }
