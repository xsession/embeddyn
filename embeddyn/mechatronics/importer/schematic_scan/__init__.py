"""Import schematics that exist only as drawings: PDF prints (vector or scanned) and images (PNG, JPEG ...).

``scan(path)`` reads every page, finds the symbols (reference designator, value, pins with numbers and names), the
wires, junction dots, power ports and net labels, and returns a :class:`~..kicad_netlist.Netlist` plus a
:class:`~.assemble.ScanReport` saying what was guessed. ``scan_to_files`` also writes a KiCad netlist (which the
project importer reads like any other), a JSON report and an overlay picture per page to check the result.

PDFs exported by EDA tools are read as vectors (exact geometry and text); scanned PDF pages and image files are
read from pixels (line detection plus OCR with tesseract), which is less certain: check the overlay.
See docs/guides/schematic-scan.md.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..kicad_netlist import Netlist
from .assemble import ScanReport, assemble, write_kicad_netlist
from .connectivity import PageResult, analyse

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp"}


@dataclass
class ScanResult:
    netlist: Netlist
    report: ScanReport
    pages: list[PageResult]


def read_pages(path: Path, pages: list[int] | None = None, dpi: int | None = None):
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from .pdf_vector import read_pdf
        return read_pdf(path, pages, dpi=dpi or 300)
    if suffix in IMAGE_SUFFIXES:
        from .raster import read_image
        return [read_image(path, dpi=dpi)]
    raise ValueError(f"{path}: not a PDF or an image ({', '.join(sorted(IMAGE_SUFFIXES))})")


def scan(paths: Path | list[Path], pages: list[int] | None = None, dpi: int | None = None) -> ScanResult:
    """Scan one file, or several (the pages of one design: their nets join by name)."""
    files = [paths] if isinstance(paths, Path) else list(paths)
    results: list[PageResult] = []
    number = 0
    for file in files:
        for page in read_pages(file, pages, dpi):
            number += 1
            page.index = number
            result = analyse(page)
            if page.raster and result.unnamed_clusters:
                # symbols without a reference: read the text around them again, closer
                from .raster import reread_near
                if reread_near(page, [box for _, box, _ in result.unnamed_clusters]):
                    result = analyse(page)
            results.append(result)
    netlist, report = assemble(results, files[0])
    return ScanResult(netlist=netlist, report=report, pages=results)


def scan_to_files(paths: Path | list[Path], out: Path, pages: list[int] | None = None, dpi: int | None = None,
                  overlay: bool = True) -> tuple[ScanResult, dict[str, Path]]:
    out.mkdir(parents=True, exist_ok=True)
    result = scan(paths, pages, dpi)
    files = {"netlist": write_kicad_netlist(result.netlist, out / "schematic.net")}
    report = out / "scan.json"
    data = result.report.to_dict()
    data["components"] = {ref: {"value": c.value, "page": c.sheet} for ref, c in result.netlist.components.items()}
    data["nets"] = {n.name: [f"{x.ref}.{x.pin}" + (f" ({x.pin_name})" if x.pin_name else "") for x in n.nodes]
                    for n in result.netlist.nets}
    report.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    files["report"] = report
    if overlay:
        from .overlay import draw_overlay
        for page_result in result.pages:
            files[f"overlay{page_result.page.index}"] = draw_overlay(
                page_result, out / f"overlay-p{page_result.page.index}.png")
    return result, files
