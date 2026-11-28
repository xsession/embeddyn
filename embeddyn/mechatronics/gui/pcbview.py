"""The Lab's Scan view, board-photo mode: reverse engineer a board from photos, correct markings by hand, and
decode SMD markings on their own.

``read`` runs ``pcb_photo.read_board`` into a scan folder (default ``<project>/.embeddyn/scans/<name>``) and keeps
the result in memory so that ``marking`` can update a part (decode, renumber pins, rewrite the netlist) without
reading the photos again.
"""
from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any

from .scanview import MAX_UPLOAD, _resolve, _safe_name

PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp"}
OVERLAY_SCALE = 0.5


def _photo(value: Any, root: Path, folder: Path, uploads: dict[str, dict]) -> Path | None:
    """A photo given as a path, or as the name of an uploaded file."""
    if not value:
        return None
    text = str(value).strip()
    if text in uploads:
        upload = uploads[text]
        name = _safe_name(text)
        if Path(name).suffix.lower() not in PHOTO_SUFFIXES:
            raise ValueError(f"{name}: not a photo ({', '.join(sorted(PHOTO_SUFFIXES))})")
        data = base64.b64decode(str(upload.get("data") or ""), validate=True)
        if len(data) > MAX_UPLOAD:
            raise ValueError(f"{name}: larger than {MAX_UPLOAD // (1024 * 1024)} MB")
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / name
        target.write_bytes(data)
        return target
    path = _resolve(text, root)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    if path.suffix.lower() not in PHOTO_SUFFIXES:
        raise ValueError(f"{path.name}: not a photo")
    return path


def read(body: dict[str, Any], store: dict[str, Any]) -> dict[str, Any]:
    from ..importer.pcb_photo import read_board, write_all
    root = Path(str(body.get("root") or ".")).expanduser().resolve()
    uploads = {str(u.get("name")): u for u in body.get("uploads") or []}
    first = str(body.get("top") or "board")
    out = _resolve(str(body["out"]), root) if body.get("out") else \
        root / ".embeddyn" / "scans" / ("board-" + _safe_name(Path(first).stem))
    folder = out / "input"
    top = _photo(body.get("top"), root, folder, uploads)
    if top is None:
        raise ValueError("a photo of the top side is needed")
    bottom = _photo(body.get("bottom"), root, folder, uploads)
    closeups = [p for p in (_photo(c, root, folder, uploads) for c in body.get("closeups") or []) if p]
    board_mm = None
    if body.get("board_mm"):
        numbers = [float(v) for v in re.split(r"[x×*, ]+", str(body["board_mm"]).strip()) if v]
        if len(numbers) != 2:
            raise ValueError("board size as WIDTHxHEIGHT in mm, e.g. 107x126")
        board_mm = (numbers[0], numbers[1])
    result = read_board(top, bottom, closeups, board_mm)
    write_all(result, out, top)
    store[str(out)] = result
    while len(store) > 3:                     # results hold images: keep the last few
        store.pop(next(iter(store)))
    return payload(result, out)


def marking(body: dict[str, Any], store: dict[str, Any]) -> dict[str, Any]:
    from ..importer.pcb_photo import set_marking, write_all
    out = str(Path(str(body.get("out") or "")).expanduser().resolve())
    result = store.get(out)
    if result is None:
        raise LookupError("read the board again: its result is no longer in memory")
    set_marking(result, str(body["ref"]), str(body.get("text") or ""), body.get("package") or None,
                body.get("pin1") or None)
    write_all(result, Path(out), result.top.image.source)
    return payload(result, Path(out))


def decode_marking(body: dict[str, Any]) -> dict[str, Any]:
    from ..smd import classify, decode
    answer: dict[str, Any] = {}
    if str(body.get("text") or "").strip():
        answer["decoded"] = decode(str(body["text"]), body.get("package") or None, body.get("kind") or None).as_dict()
    if body.get("size"):
        numbers = [float(v) for v in re.split(r"[x×*, ]+", str(body["size"]).strip()) if v]
        if len(numbers) >= 2:
            matches = classify(numbers[0], numbers[1], int(numbers[2]) if len(numbers) > 2 else None)
            answer["packages"] = [m.as_dict() for m in matches]
    return answer


def payload(result, out: Path) -> dict[str, Any]:
    """The board read in the Scan view's shape: pages (top, bottom, schematic), parts, nets, positions."""
    k = OVERLAY_SCALE
    pages = []
    height, width = result.top.balanced.shape[:2]
    pages.append({"index": 1, "name": "top", "width": width * k, "height": height * k,
                  "overlay": str(out / "overlay-top.jpg"), "source": str(result.top.image.source), "raster": True,
                  "parts": len(result.top.parts), "unnamed": 0})
    if result.bottom is not None:
        pages.append({"index": 2, "name": "bottom", "width": width * k, "height": height * k,
                      "overlay": str(out / "overlay-bottom.jpg"), "source": str(result.bottom.image.source),
                      "raster": True, "parts": len(result.bottom.parts), "unnamed": 0})
    svg = out / "schematic.svg"
    if svg.exists():
        text = svg.read_text(encoding="utf-8")[:400]
        m = re.search(r'width="(\d+)" height="(\d+)"', text)
        sw, sh = (int(m.group(1)), int(m.group(2))) if m else (1600, 1200)
        pages.append({"index": 3, "name": "schematic", "width": sw, "height": sh, "overlay": str(svg),
                      "source": "draft schematic", "raster": False, "parts": len(result.parts), "unnamed": 0})
    positions, boxes, parts, guessed = {}, {}, [], []
    px = result.px_per_mm
    for part in result.parts:
        side = result.top if part.side == "top" else result.bottom
        page = 1 if part.side == "top" else 2
        xs = [x for x, _ in part.corners()]
        ys = [y for _, y in part.corners()]
        boxes[part.ref] = [page, min(xs) * k, min(ys) * k, max(xs) * k, max(ys) * k]
        for pid in part.pads:
            pad = side.pads[pid]
            key = f"{part.ref}.{pad.pin}"
            positions.setdefault(key, [page, round(pad.x * k, 1), round(pad.y * k, 1)])
            if pad.guessed:
                guessed.append(key)
        parts.append({"ref": part.ref, "value": part.value or part.part_number or "", "page": page,
                      "pins": len(part.pads), "package": part.package, "candidates": part.candidates,
                      "kind": part.kind, "marking": part.marking, "readings": part.decoded,
                      "size_mm": [round(part.length / px, 2), round(part.width / px, 2)], "notes": part.notes})
    guessed_set = set(guessed)
    nets = [{"name": name, "nodes": [{"ref": r, "pin": p, "name": "", "guessed": f"{r}.{p}" in guessed_set}
                                     for r, p in nodes]} for name, nodes in result.nets.items()]
    from ..importer.pcb_photo.output import _num
    parts.sort(key=lambda p: ("".join(c for c in p["ref"] if c.isalpha()), _num(p["ref"])))
    return {"mode": "board", "out": str(out), "netlist": str(out / "board.net"), "report": str(out / "board.json"),
            "pages": pages, "parts": parts, "nets": nets, "positions": positions, "boxes": boxes,
            "guessed": sorted(guessed_set), "notes": result.notes + result.connectivity.notes,
            "closeups": result.closeups, "px_per_mm": round(px, 2),
            "board_mm": [round(v, 1) for v in result.top.image.size_mm]}
