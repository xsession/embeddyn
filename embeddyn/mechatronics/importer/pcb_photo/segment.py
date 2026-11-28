"""Pixel classes of a straightened board photo: solder mask, copper under the mask (traces, pours), bare metal
(pads, solder, leads, vias), part bodies (dark: ICs, transistors, resistors; light: ceramic capacitors, beige
plastic), silkscreen and holes.

Colours are judged relative to the board's own mask hue and, for brightness, to the local mask brightness, so
uneven light and shadows in phone photos matter less.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import ndimage

from .image import hsv, hue_distance, mask_hue


@dataclass
class Classes:
    mask: np.ndarray        # solder mask (with or without copper under it)
    copper: np.ndarray      # copper under the mask: traces, pours (lighter than bare mask)
    metal: np.ndarray       # exposed metal: pads, solder, leads, via rings
    dark: np.ndarray        # dark bodies (black/grey plastic)
    light: np.ndarray       # light bodies (beige/brown ceramic and plastic)
    silk: np.ndarray        # silkscreen (white, thin)
    hole: np.ndarray        # holes: small dark spots inside metal rings
    hue: float
    v: np.ndarray

    def conductor(self) -> np.ndarray:
        return self.copper | self.metal


def white_balance(rgb: np.ndarray) -> np.ndarray:
    """Scale the channels so that the brightest off-mask pixels (solder highlights, silkscreen) are neutral:
    phone photos under warm or cold light otherwise make silver solder look beige or blue."""
    h, s, v = hsv(rgb)
    hue = mask_hue(h, s, v)
    off = (hue_distance(h, hue) > 0.08) & (v > 0.3)
    if off.sum() < 200:
        return rgb
    vals = v[off]
    ref = rgb[off][vals >= np.percentile(vals, 97)].astype(np.float64)
    ref = ref[ref.max(axis=1) < 250] if (ref.max(axis=1) < 250).sum() > 50 else ref
    mean = ref.mean(axis=0)
    gains = mean.max() / np.maximum(mean, 1.0)
    gains = np.clip(gains, 1.0, 1.6)
    return np.clip(rgb.astype(np.float32) * gains, 0, 255).astype(np.uint8)


def classify(rgb: np.ndarray, px_per_mm: float, hue: float | None = None, balance: bool = True) -> Classes:
    if balance:
        rgb = white_balance(rgb)
    h, s, v = hsv(rgb)
    hue = mask_hue(h, s, v) if hue is None else hue
    dh = hue_distance(h, hue)
    on_board = rgb.max(axis=2) > 8
    mask = (dh < 0.07) & (s > 0.22) & on_board
    mask |= (dh < 0.13) & (s > 0.18) & (v < 0.35) & on_board      # the mask in the shadow of a tall part

    # copper under the mask: brighter than the bare mask around it (local reference over ~3 mm)
    win = max(15, int(3.0 * px_per_mm)) | 1
    vm = np.where(mask, v, np.nan)
    small = max(1, win // 15)
    vm_s = vm[::small, ::small]
    filled = np.where(np.isnan(vm_s), np.nanmedian(vm_s), vm_s)
    base = ndimage.percentile_filter(filled, 35, size=max(3, win // small))
    base = ndimage.zoom(base, (v.shape[0] / base.shape[0], v.shape[1] / base.shape[1]), order=1)[:v.shape[0], :v.shape[1]]
    if base.shape != v.shape:
        base = np.pad(base, ((0, v.shape[0] - base.shape[0]), (0, v.shape[1] - base.shape[1])), mode="edge")
    copper = mask & (v > base * 1.13 + 0.015)
    copper = ndimage.binary_opening(copper, structure=np.ones((2, 2)))

    warm = (h > 0.01) & (h < 0.17)
    metal = (((s < 0.1) & (v > 0.55)) | ((s < 0.25) & (v > 0.86) & ~warm)) & ~mask
    silk = metal & (s < 0.12) & (v > 0.7)
    # silkscreen is thin: lines and letters; pads and solder are blobs (a disc of ~0.25 mm survives an opening)
    r = max(1, int(round(0.12 * px_per_mm)))
    blob = ndimage.binary_opening(metal, structure=_disc(r))
    silk = silk & ~blob
    metal = metal & ~silk

    # smooth bright regions are plastic (white/beige bodies), not metal: solder and plating are shiny, with
    # strong brightness changes over a fraction of a millimetre
    k = max(3, int(round(0.35 * px_per_mm)))
    mean = ndimage.uniform_filter(v, k)
    std = np.sqrt(np.maximum(ndimage.uniform_filter(v * v, k) - mean * mean, 0))
    smooth = std < 0.035
    plastic = ndimage.binary_opening((metal | (~mask & (v > 0.62))) & smooth,
                                     structure=_disc(max(1, int(0.3 * px_per_mm))))
    plastic = ndimage.binary_dilation(plastic, iterations=max(1, int(0.08 * px_per_mm))) & ~mask & (v > 0.55)
    metal &= ~plastic
    body = ~mask & ~metal & ~silk & on_board
    dark = body & (v < 0.62)
    light = body & (v >= 0.62) & ((warm & (s >= 0.1)) | plastic)
    hole = _holes(dark, metal, px_per_mm)
    dark &= ~hole
    return Classes(mask, copper, metal, dark, light, silk, hole, hue, v)


def _disc(r: int) -> np.ndarray:
    y, x = np.ogrid[-r:r + 1, -r:r + 1]
    return x * x + y * y <= r * r


def _holes(dark: np.ndarray, metal: np.ndarray, px_per_mm: float) -> np.ndarray:
    """Small round dark spots (0.15-0.8 mm across) mostly surrounded by metal: drilled holes of vias and pads."""
    labels, n = ndimage.label(dark)
    if n == 0:
        return np.zeros_like(dark)
    objs = ndimage.find_objects(labels)
    ring = ndimage.binary_dilation(metal, iterations=max(1, int(0.1 * px_per_mm)))
    keep = np.zeros(n + 1, bool)
    lo, hi = (0.15 * px_per_mm) ** 2 * 0.6, (0.9 * px_per_mm) ** 2
    for i, sl in enumerate(objs, 1):
        if sl is None:
            continue
        hgt, wid = sl[0].stop - sl[0].start, sl[1].stop - sl[1].start
        region = labels[sl] == i
        area = region.sum()
        if not (lo <= area <= hi) or max(hgt, wid) > 1.6 * min(hgt, wid) + 2:
            continue
        pad = max(2, int(0.15 * px_per_mm))
        y0, y1 = max(0, sl[0].start - pad), sl[0].stop + pad
        x0, x1 = max(0, sl[1].start - pad), sl[1].stop + pad
        around = ndimage.binary_dilation(labels[y0:y1, x0:x1] == i, iterations=pad) & ~(labels[y0:y1, x0:x1] == i)
        if around.sum() and ring[y0:y1, x0:x1][around].mean() > 0.55:
            keep[i] = True
    return keep[labels]
