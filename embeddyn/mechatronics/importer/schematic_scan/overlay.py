"""A picture to check a scan by eye: the page, each net in its own colour, symbols boxed with their reference,
pins as dots with their numbers (red when guessed), ports and labels."""
from __future__ import annotations

import colorsys
import shutil
import subprocess
import tempfile
from pathlib import Path

from .connectivity import PageResult


def _page_image(result: PageResult, scale: float):
    from PIL import Image
    page = result.page
    if page.image is not None:
        image = page.image.convert("RGB")
        factor = scale / page.scale
        return image.resize((max(1, int(image.width * factor)), max(1, int(image.height * factor))))
    if page.source is not None and Path(str(page.source)).suffix.lower() == ".pdf" and shutil.which("mutool"):
        with tempfile.TemporaryDirectory(prefix="schscan") as work:
            png = Path(work) / "page.png"
            subprocess.run(["mutool", "draw", "-q", "-r", str(int(72 * scale)), "-o", str(png), str(page.source),
                            str(page.source_page)], capture_output=True)
            if png.exists():
                return Image.open(png).convert("RGB")
    return Image.new("RGB", (int(page.width * scale), int(page.height * scale)), "white")


def _colour(i: int) -> tuple[int, int, int]:
    r, g, b = colorsys.hsv_to_rgb((i * 0.61803) % 1.0, 0.85, 0.85)
    return int(r * 255), int(g * 255), int(b * 255)


def draw_overlay(result: PageResult, path: Path, scale: float = 2.0) -> Path:
    from PIL import Image, ImageDraw
    base = _page_image(result, scale)
    # fade the page so the overlay stands out
    base = Image.blend(base, Image.new("RGB", base.size, "white"), 0.55)
    draw = ImageDraw.Draw(base)
    page = result.page
    k = base.width / page.width
    groups = sorted({g for g in result.wire_group if g >= 0})
    colour = {g: _colour(n) for n, g in enumerate(groups)}
    for i, g in enumerate(result.wire_group):
        if g < 0:
            continue
        s = page.segments[i]
        draw.line([(s.x1 * k, s.y1 * k), (s.x2 * k, s.y2 * k)], fill=colour[g], width=max(2, int(k)))
    for symbol in result.symbols.values():
        x0, y0, x1, y1 = symbol.box
        draw.rectangle([x0 * k - 2, y0 * k - 2, x1 * k + 2, y1 * k + 2], outline=(40, 40, 220), width=2)
        draw.text((x0 * k, y1 * k + 2), symbol.ref, fill=(40, 40, 220))
        for c in symbol.contacts:
            fill = (220, 0, 0) if c.guessed else (0, 150, 0)
            draw.ellipse([c.x * k - 4, c.y * k - 4, c.x * k + 4, c.y * k + 4], fill=fill)
            draw.text((c.x * k + 5, c.y * k - 12), c.pin, fill=fill)
    for port in result.ports:
        for c in port.contacts:
            draw.rectangle([c.x * k - 4, c.y * k - 4, c.x * k + 4, c.y * k + 4], outline=(200, 120, 0), width=2)
            draw.text((c.x * k + 6, c.y * k + 2), port.name, fill=(200, 120, 0))
    for _, box, _ in result.unnamed_clusters:
        x0, y0, x1, y1 = box
        draw.rectangle([x0 * k, y0 * k, x1 * k, y1 * k], outline=(255, 0, 255), width=2)
    base.save(path)
    return path
