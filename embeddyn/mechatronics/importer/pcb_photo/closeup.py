"""Close-up photos: read markings where the board photo is too coarse, and carry them onto the board's parts.

A close-up is not straightened (it shows part of the board, often at an angle). It is placed onto the straightened
board by normalised cross-correlation of edge images over scale and rotation (its middle half, where perspective
distorts least); its parts are found like on the board photo, their markings read and given to the board parts
they land on. Close-ups that do not correlate clearly (strong perspective, blur) are reported as not placed.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

from .image import load, mask_hue, hsv
from .parts import Part, body_mask, find_pads, find_parts, identify, read_markings
from .segment import classify, white_balance


def _apply(m: np.ndarray, pts: np.ndarray) -> np.ndarray:
    return pts @ m[:, :2].T + m[:, 2]


def _gradient(gray: np.ndarray) -> np.ndarray:
    from scipy import ndimage
    g = ndimage.gaussian_filter(gray.astype(np.float32), 1.0)
    mag = np.hypot(ndimage.sobel(g, 0), ndimage.sobel(g, 1))
    return mag / (np.percentile(mag, 99) + 1e-6)


def _ncc(image: np.ndarray, template: np.ndarray) -> tuple[float, int, int]:
    """Best normalised cross-correlation of ``template`` inside ``image``: (score, x, y) of its top left."""
    from numpy.fft import irfft2, rfft2
    th, tw = template.shape
    ih, iw = image.shape
    if th >= ih or tw >= iw:
        return -1.0, 0, 0
    t = template - template.mean()
    tnorm = np.sqrt((t * t).sum()) + 1e-9
    shape = (ih + th, iw + tw)
    fi = rfft2(image, shape)
    corr = irfft2(fi * np.conj(rfft2(t, shape)), shape)[:ih - th + 1, :iw - tw + 1]
    ones = np.ones_like(template)
    s1 = irfft2(fi * np.conj(rfft2(ones, shape)), shape)[:ih - th + 1, :iw - tw + 1]
    s2 = irfft2(rfft2(image * image, shape) * np.conj(rfft2(ones, shape)), shape)[:ih - th + 1, :iw - tw + 1]
    var = np.maximum(s2 - s1 * s1 / ones.size, 1e-9)
    ncc = corr / (np.sqrt(var) * tnorm)
    y, x = np.unravel_index(int(np.argmax(ncc)), ncc.shape)
    return float(ncc[y, x]), int(x), int(y)


def place_by_image(close_rgb: np.ndarray, board_rgb: np.ndarray, board_px_per_mm: float,
                   work_px_per_mm: float = 8.0) -> tuple[np.ndarray | None, float]:
    """Similarity transform close-up px -> board px found by correlating edge images over scale and rotation."""
    from PIL import Image as _Image
    k = work_px_per_mm / board_px_per_mm
    board_small = np.asarray(_Image.fromarray(board_rgb).convert("L").resize(
        (max(8, int(board_rgb.shape[1] * k)), max(8, int(board_rgb.shape[0] * k))), _Image.BILINEAR))
    board_feat = _gradient(board_small)
    gray = _Image.fromarray(close_rgb).convert("L")
    best = (-1.0, None)

    def trial(scale: float, angle: float):
        w, h = max(8, int(gray.width * scale)), max(8, int(gray.height * scale))
        small = gray.resize((w, h), _Image.BILINEAR).rotate(angle, expand=True, resample=_Image.BILINEAR,
                                                            fillcolor=0)
        feat = _gradient(np.asarray(small))
        # the middle of the close-up (less perspective, no rotation padding)
        fh, fw = feat.shape
        cy0, cx0 = int(fh * 0.25), int(fw * 0.25)
        template = feat[cy0:fh - cy0, cx0:fw - cx0]
        score, x, y = _ncc(board_feat, template)
        return score, x, y, cx0, cy0, small.size, (w, h)
    # close-ups show 15-80 mm across their long side
    long_side = max(gray.size)
    scales = [work_px_per_mm * mm / long_side for mm in np.geomspace(15, 80, 16)]
    for scale in scales:
        for angle in (0, 90, 180, 270):
            res = trial(scale, angle)
            if res[0] > best[0]:
                best = (res[0], (scale, angle, res))
    if best[1] is None:
        return None, 0.0
    scale0, angle0, _ = best[1]
    for scale in np.geomspace(scale0 * 0.92, scale0 * 1.08, 5):
        for angle in np.arange(angle0 - 6, angle0 + 7, 3):
            res = trial(float(scale), float(angle))
            if res[0] > best[0]:
                best = (res[0], (float(scale), float(angle), res))
    score, (scale, angle, (s, x, y, cx0, cy0, rot_size, (w, h))) = best[0], best[1]
    # close-up px -> scaled px -> rotated (about the centre, canvas grown) -> board work px -> board px
    a = math.radians(angle)
    c, sn = math.cos(a), math.sin(a)
    # PIL rotates counter-clockwise for positive angles in an image with y down
    R = np.array([[c, sn], [-sn, c]])
    centre_in = np.array([w / 2, h / 2])
    centre_out = np.array([rot_size[0] / 2, rot_size[1] / 2])
    A = R * scale / k
    t = (-(R @ (centre_in)) * 1 + centre_out - np.array([cx0, cy0]) + np.array([x, y])) / k
    # scaling happens before the rotation: point p -> R (scale p - centre_in) + centre_out
    t = (R @ (-centre_in) + centre_out - np.array([cx0, cy0]) + np.array([x, y])) / k
    m = np.hstack([A, t[:, None]])
    return m, score


def apply_closeups(paths: list[Path], board, board_px_per_mm: float) -> list[dict]:
    """Read every close-up and give its markings to the board parts it shows. Returns one summary per photo."""
    info = []
    for path in paths:
        rgb = load(path, 2400)
        # a close-up shows some 30-60 mm of the board across its long side
        ppm = max(rgb.shape[:2]) / 45.0
        h, s, v = hsv(rgb)
        hue = mask_hue(h, s, v)
        balanced = white_balance(rgb)
        classes = classify(rgb, ppm, hue)
        bodies = body_mask(classes, ppm)
        pads, pad_labels = find_pads(classes, ppm, "top", bodies)
        pad_by_id = {p.id: p for p in pads}
        parts = find_parts(balanced, classes, ppm, pads, pad_labels, "top")
        for part in parts:
            identify(part, pad_by_id, ppm)
        m, match_score = place_by_image(rgb, board.balanced, board_px_per_mm)
        pairs = []
        plausible = m is not None and 0.05 < float(np.sqrt(abs(np.linalg.det(m[:, :2])))) < 1.5
        if plausible and match_score >= 0.45:
            moved = np.array([_apply(m, np.array([[p.cx, p.cy]]))[0] for p in parts]) if parts else np.zeros((0, 2))
            tree = cKDTree(np.array([[p.cx, p.cy] for p in board.parts]))
            taken: dict[int, tuple[float, int]] = {}
            for a, (x, y) in enumerate(moved):
                d, j = tree.query((x, y), distance_upper_bound=1.0 * board_px_per_mm)
                if np.isfinite(d) and (j not in taken or d < taken[j][0]):
                    taken[int(j)] = (float(d), a)
            pairs = [(a, b) for b, (_, a) in taken.items()]
        else:
            m = None
        entry = {"photo": str(path), "parts": len(parts), "placed": m is not None, "matched": len(pairs),
                 "match": round(float(match_score), 3), "markings": 0}
        if m is None:
            entry["note"] = "could not place this close-up on the board (no clear match: too oblique or blurred?)"
            info.append(entry)
            continue
        matched_close = [parts[a] for a, _ in pairs]
        read_markings(balanced, matched_close, pad_by_id, ppm, only_dark=True)
        lin = m[:, :2]
        for a, b in pairs:
            cp, bp = parts[a], board.parts[b]
            if not cp.marking or cp.marking_conf <= bp.marking_conf:
                continue
            up = lin @ np.array(cp.up)
            n = float(np.linalg.norm(up)) or 1.0
            bp.marking, bp.marking_conf = cp.marking, cp.marking_conf
            bp.up = (float(up[0] / n), float(up[1] / n))
            bp.decoded, bp.value, bp.part_number = cp.decoded, cp.value, cp.part_number
            if cp.kind in ("resistor", "capacitor", "inductor") and bp.kind != cp.kind:
                bp.kind = cp.kind
            bp.notes.append(f"marking read on {path.name}")
            entry["markings"] += 1
        sx = float(np.sqrt(abs(np.linalg.det(lin))))
        entry["scale"] = round(sx, 4)
        info.append(entry)
    return info
