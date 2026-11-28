"""A small character reader for SMD markings: split a cleaned, binarised marking into characters (connected ink
blobs) and match each against characters rendered in a few sans fonts.

General OCR (tesseract) is trained on words in documents; a three-character code such as ``472`` between the dark
end caps of a resistor, with an underline under the middle digit, often defeats it, while each character on its
own is easy to match. The bars at the crop's ends, underlines and specks are left out by their shape.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage

CHARS = "0123456789ABCDEFGHJKLMNPRSTUVWXYZt"
VALUE_CHARS = "0123456789R"                        # what resistors and capacitors carry (plus EIA-96 letters)
FONTS = ["DejaVuSans-Bold.ttf", "DejaVuSansCondensed-Bold.ttf", "LiberationSans-Bold.ttf",
         "LiberationSansNarrow-Bold.ttf", "FreeSansBold.ttf", "NimbusSansNarrow-Bold.otf", "NimbusSans-Bold.otf",
         "DejaVuSansMono-Bold.ttf", "LiberationMono-Bold.ttf", "UbuntuMono-B.ttf"]
SIZE = (20, 30)                                    # normalised glyph (width, height)
LEARNED = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "embeddyn" / "glyphs" / "learned.npz"
LEARNED_BONUS = 0.06                               # a character seen on real parts beats a font's version of it


@dataclass
class Glyph:
    char: str
    score: float
    x0: int
    y0: int
    x1: int
    y1: int
    mask: np.ndarray | None = None
    patch: np.ndarray | None = None


def fit(gray: np.ndarray, text: str) -> tuple[float, list[tuple[str, np.ndarray]]]:
    """How well ``text`` (lines separated by newlines) fits the characters found in a marking image, and the
    (char, mask) pairs to learn from. Extra blobs at a line's ends (solder, a body edge) are dropped."""
    wanted = [line.replace(" ", "") for line in text.upper().splitlines() if line.strip()]
    best: tuple[float, list] = (0.0, [])
    otsu = _otsu(gray)
    for level in (otsu - 25, otsu, otsu + 25):
        lines = read(gray < level)
        lines = [l for l in lines if len(l) >= 1]
        if len(lines) < len(wanted):
            continue
        # the longest lines, in reading order, against the wanted lines
        chosen = sorted(sorted(lines, key=len, reverse=True)[:len(wanted)], key=lambda l: l[0].y0)
        samples, scores = [], []
        for line, chars in zip(chosen, wanted):
            if len(line) < len(chars):
                break
            # drop extra blobs from whichever end matches worse
            while len(line) > len(chars):
                left = classify(line[0].mask, chars[0])[1] if line[0].mask is not None else 0
                right = classify(line[-1].mask, chars[-1])[1] if line[-1].mask is not None else 0
                line = line[1:] if left < right else line[:-1]
            for glyph, char in zip(line, chars):
                scores.append(classify(glyph.mask, char)[1])
                samples.append((char, glyph.mask))
        else:
            if scores and float(np.mean(scores)) > best[0]:
                best = (float(np.mean(scores)), samples)
    return best


def _normalise(mask: np.ndarray) -> np.ndarray:
    image = Image.fromarray((mask * 255).astype(np.uint8)).resize(SIZE, Image.BILINEAR)
    # a little blur makes the match tolerant to broken and thickened strokes
    return ndimage.gaussian_filter(np.asarray(image, dtype=np.float32) / 255.0, 1.0)


_learned: list[tuple[str, np.ndarray, float]] | None = None


def learned() -> list[tuple[str, np.ndarray, float]]:
    """Characters learned from markings people confirmed (kept in ~/.config/embeddyn/glyphs/learned.npz)."""
    global _learned
    if _learned is None:
        _learned = []
        path = Path(os.environ.get("EMBEDDYN_GLYPHS") or LEARNED)
        if path.is_file():
            try:
                data = np.load(path)
                _learned = [(str(c), b.astype(np.float32), float(a))
                            for c, b, a in zip(data["chars"], data["bitmaps"], data["aspects"])]
            except (OSError, ValueError, KeyError):
                _learned = []
    return _learned


_learned_masks: list[tuple[str, np.ndarray]] | None = None


def learned_masks() -> list[tuple[str, np.ndarray]]:
    """The learned characters as glyph masks (for the nearest-neighbour classifier)."""
    global _learned_masks
    if _learned_masks is None:
        _learned_masks = [(c, b > 0.5) for c, b, _ in learned()]
    return _learned_masks


def remember(samples: list[tuple[str, np.ndarray]], save: bool = True) -> int:
    """Add characters (char, binary mask) to the learned set; keep at most 12 per char (the newest)."""
    global _learned, _learned_masks, _learned_feats
    _learned_masks = None
    _learned_feats = None
    current = list(learned())
    for char, mask in samples:
        ys, xs = np.nonzero(mask)
        if not len(xs) or not char.strip():
            continue
        mask = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        current.append((char, _normalise(mask), mask.shape[1] / mask.shape[0]))
    by_char: dict[str, list] = {}
    for item in current:
        by_char.setdefault(item[0], []).append(item)
    _learned = [item for items in by_char.values() for item in items[-12:]]
    if save and _learned:
        path = Path(os.environ.get("EMBEDDYN_GLYPHS") or LEARNED)
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(path, chars=np.array([c for c, _, _ in _learned]),
                            bitmaps=np.stack([b for _, b, _ in _learned]),
                            aspects=np.array([a for _, _, a in _learned]))
    return len(samples)


def all_templates() -> list[tuple[str, np.ndarray, float, float]]:
    """(char, bitmap, aspect, bonus): learned characters first, then the fonts'."""
    return [(c, b, a, LEARNED_BONUS) for c, b, a in learned()] + [(c, b, a, 0.0) for c, b, a in templates()]


@lru_cache(maxsize=1)
def templates() -> list[tuple[str, np.ndarray, float]]:
    """(char, normalised bitmap, aspect) for every char in every font found; empty without fonts."""
    out = []
    for name in FONTS:
        try:
            font = ImageFont.truetype(name, 96)
        except OSError:
            continue
        for char in CHARS:
            image = Image.new("L", (140, 140), 0)
            ImageDraw.Draw(image).text((20, 10), char, fill=255, font=font)
            arr = np.asarray(image) > 127
            ys, xs = np.nonzero(arr)
            if not len(xs):
                continue
            glyph = arr[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
            out.append((char, _normalise(glyph), glyph.shape[1] / glyph.shape[0]))
            # markings use condensed fonts: the same glyph squeezed
            narrow = np.asarray(Image.fromarray(glyph.astype(np.uint8) * 255).resize(
                (max(2, int(glyph.shape[1] * 0.7)), glyph.shape[0]), Image.BILINEAR)) > 127
            out.append((char, _normalise(narrow), narrow.shape[1] / narrow.shape[0]))
    return out


# --- a nearest-neighbour classifier on gradient features, trained on rendered fonts ---------------------------

FEATURE_SIZE = (24, 32)
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "embeddyn" / "glyph-knn-v2.npz"


def _drawn() -> list[tuple[str, np.ndarray]]:
    """Digit shapes common on SMD markings that most fonts draw differently: the open 4 (a straight left stroke
    down to the bar, no diagonal), a 1 with a flag, a plain 7."""
    shapes = {
        "4": [[(0.15, 0.0), (0.15, 0.62), (0.95, 0.62)], [(0.72, 0.0), (0.72, 1.0)]],
        "1": [[(0.25, 0.22), (0.6, 0.0), (0.6, 1.0)]],
        "7": [[(0.05, 0.0), (0.95, 0.0), (0.35, 1.0)]],
    }
    out = []
    for char, strokes in shapes.items():
        for width in (0.13, 0.18, 0.24):
            for aspect in (0.45, 0.6):
                h, w = 90, int(90 * aspect)
                image = Image.new("L", (w + 20, h + 20), 0)
                d = ImageDraw.Draw(image)
                for stroke in strokes:
                    d.line([(10 + x * w, 10 + y * h) for x, y in stroke], fill=255, width=int(width * h),
                           joint="curve")
                out.append((char, np.asarray(image, dtype=np.float32) / 255.0))
    return out


def features(mask: np.ndarray) -> np.ndarray:
    """Gradient-orientation histograms (8 directions, 4 x 4 cells) of a glyph scaled to 24 x 32, plus its aspect."""
    image = Image.fromarray((np.clip(mask.astype(np.float32), 0, 1) * 255).astype(np.uint8)).resize(
        FEATURE_SIZE, Image.BILINEAR)
    a = ndimage.gaussian_filter(np.asarray(image, dtype=np.float32) / 255.0, 0.8)
    gx, gy = ndimage.sobel(a, 1), ndimage.sobel(a, 0)
    mag = np.hypot(gx, gy)
    ang = (np.arctan2(gy, gx) % (2 * np.pi)) / (2 * np.pi) * 8
    bins = np.floor(ang).astype(int) % 8
    feats = []
    h, w = a.shape
    for cy in range(4):
        for cx in range(4):
            sl = (slice(cy * h // 4, (cy + 1) * h // 4), slice(cx * w // 4, (cx + 1) * w // 4))
            hist = np.bincount(bins[sl].ravel(), weights=mag[sl].ravel(), minlength=8)
            feats.append(hist)
    f = np.concatenate(feats)
    f = f / (np.linalg.norm(f) + 1e-6)
    aspect = mask.shape[1] / max(1, mask.shape[0])
    return np.concatenate([f, [0.6 * np.log(max(aspect, 0.05))]]).astype(np.float32)


def _fonts() -> list:
    import glob
    found = []
    roots = ["/usr/share/fonts", "/usr/local/share/fonts", str(Path.home() / ".local/share/fonts"),
             "C:/Windows/Fonts", "/Library/Fonts", "/System/Library/Fonts"]
    skip = ("serif", "italic", "oblique", "cjk", "symbol", "emoji", "math", "dingbat", "music")
    for root in roots:
        for path in glob.glob(root + "/**/*.[ot]tf", recursive=True):
            low = Path(path).name.lower()
            if any(k in low for k in skip) and "sans" not in low:
                continue
            if any(k in low for k in ("italic", "oblique")):
                continue
            if not any(k in low for k in ("sans", "mono", "gothic", "grotesk", "helvet", "arial", "ubuntu", "lato",
                                          "roboto", "source", "fira", "cantarell", "liberation", "dejavu",
                                          "free", "open", "nimbus", "verdana", "tahoma", "consol", "inter")):
                continue
            found.append(path)
    # prefer bold and condensed faces, which markings use; keep the list short
    found.sort(key=lambda p: (not any(k in p.lower() for k in ("bold", "black", "heavy")),
                              not any(k in p.lower() for k in ("cond", "narrow")), p))
    return found[:60]


@lru_cache(maxsize=1)
def knn_set() -> tuple[np.ndarray, np.ndarray]:
    """(features, labels) of rendered characters with photo-like variations; cached on disk."""
    if CACHE.is_file():
        try:
            data = np.load(CACHE)
            return data["x"], data["y"]
        except (OSError, ValueError, KeyError):
            pass
    rng = np.random.default_rng(7)
    xs, ys = [], []
    bases: list[tuple[str, np.ndarray]] = list(_drawn())
    for path in _fonts():
        try:
            font = ImageFont.truetype(path, 64)
        except OSError:
            continue
        for char in CHARS:
            image = Image.new("L", (110, 110), 0)
            ImageDraw.Draw(image).text((20, 8), char, fill=255, font=font)
            base = np.asarray(image, dtype=np.float32) / 255.0
            if base.max() >= 0.5:
                bases.append((char, base))
    for char, base in bases:
        if True:
            for k in range(6):
                a = base
                if k:
                    a = np.asarray(Image.fromarray((a * 255).astype(np.uint8)).rotate(
                        float(rng.uniform(-4, 4)), resample=Image.BILINEAR), dtype=np.float32) / 255.0
                    a = ndimage.gaussian_filter(a, float(rng.uniform(0.5, 2.0)))
                    a = a + rng.normal(0, 0.08, a.shape)
                level = [0.5, 0.35, 0.65, 0.5, 0.3, 0.7][k]
                m = a > level
                ys_, xs_ = np.nonzero(m)
                if not len(xs_):
                    continue
                m = m[ys_.min():ys_.max() + 1, xs_.min():xs_.max() + 1]
                for squeeze in ((1.0,) if k % 2 else (1.0, 0.7)):
                    if squeeze != 1.0:
                        m2 = np.asarray(Image.fromarray(m.astype(np.uint8) * 255).resize(
                            (max(2, int(m.shape[1] * squeeze)), m.shape[0]), Image.BILINEAR)) > 127
                    else:
                        m2 = m
                    xs.append(features(m2))
                    ys.append(char)
                    # the same, as the reader sees it: a blurred grey patch
                    patch = ndimage.gaussian_filter(m2.astype(np.float32), 1.0)
                    xs.append(features(patch))
                    ys.append(char)
    x = np.array(xs, dtype=np.float32) if xs else np.zeros((0, 129), np.float32)
    y = np.array(ys)
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(CACHE, x=x, y=y)
    except OSError:
        pass
    return x, y


_learned_feats: tuple[np.ndarray, np.ndarray] | None = None


def _learned_features() -> tuple[np.ndarray, np.ndarray]:
    global _learned_feats
    if _learned_feats is None:
        items = learned_masks()
        _learned_feats = (np.array([features(m) for _, m in items], dtype=np.float32).reshape(-1, 129),
                          np.array([c for c, _ in items]))
    return _learned_feats


def knn(mask: np.ndarray, charset: str | None = None, k: int = 7) -> tuple[str, float]:
    """The char a blob most likely is (votes of its nearest rendered and learned neighbours) and the share of
    the votes it got, weighted by similarity (0..1)."""
    x, y = knn_set()
    lx, ly = _learned_features()
    if len(lx):
        x, y = np.concatenate([x, lx]), np.concatenate([y, ly])
        weight = np.concatenate([np.ones(len(x) - len(lx)), np.full(len(lx), 2.0)])
    else:
        weight = np.ones(len(x))
    if not len(x):
        return "", 0.0
    if charset is not None:
        keep = np.isin(y, list(charset))
        x, y, weight = x[keep], y[keep], weight[keep]
    f = features(mask)
    sim = x[:, :-1] @ f[:-1] - 0.5 * np.abs(x[:, -1] - f[-1])     # cosine of the gradients, minus aspect gap
    top = np.argsort(-sim)[:k]
    votes: dict[str, float] = {}
    for i in top:
        votes[y[i]] = votes.get(y[i], 0.0) + max(sim[i], 0) * weight[i]
    char = max(votes, key=votes.get)
    total = sum(votes.values()) or 1.0
    return char, float(votes[char] / total * max(0.0, min(1.0, sim[top[0]])))


_training_cache: dict = {}


def _training(charset: str | None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Rendered plus learned samples, restricted to ``charset``; learned ones count double."""
    lx, ly = _learned_features()
    key = (charset, len(lx))
    if key not in _training_cache:
        x, y = knn_set()
        weight = np.ones(len(x), np.float32)
        if len(lx):
            x, y = np.concatenate([x, lx]), np.concatenate([y, ly])
            weight = np.concatenate([weight, np.full(len(lx), 2.0, np.float32)])
        if charset is not None and len(x):
            keep = np.isin(y, list(charset))
            x, y, weight = x[keep], y[keep], weight[keep]
        _training_cache.clear() if len(_training_cache) > 8 else None
        _training_cache[key] = (np.ascontiguousarray(x), y, weight)
    return _training_cache[key]


def classify_many(patches: list[np.ndarray], charset: str | None = None, k: int = 7) -> list[tuple[str, float]]:
    """``knn`` for several blobs at once (one matrix product)."""
    if not patches:
        return []
    x, y, weight = _training(charset)
    if not len(x):
        return [classify_templates(p > 0.5, charset) for p in patches]
    f = np.stack([features(p) for p in patches])
    sim = f[:, :-1] @ x[:, :-1].T - 0.5 * np.abs(f[:, -1:] - x[:, -1][None, :])
    # characters learned on this board decide when one matches about as well as the best rendered one
    lx, ly = _learned_features()
    if len(lx) and charset is not None:
        keep = np.isin(ly, list(charset))
        lx, ly = lx[keep], ly[keep]
    learned_sim = (f[:, :-1] @ lx[:, :-1].T - 0.5 * np.abs(f[:, -1:] - lx[:, -1][None, :])) if len(lx) else None
    out = []
    for r, row in enumerate(sim):
        if learned_sim is not None:
            j = int(np.argmax(learned_sim[r]))
            if learned_sim[r, j] >= row.max() - 0.03 and learned_sim[r, j] > 0.6:
                out.append((str(ly[j]), float(min(1.0, learned_sim[r, j] + 0.1))))
                continue
        top = np.argpartition(-row, k)[:k] if len(row) > k else np.arange(len(row))
        top = top[np.argsort(-row[top])]
        votes: dict[str, float] = {}
        for i in top:
            votes[y[i]] = votes.get(y[i], 0.0) + max(row[i], 0) * weight[i]
        char = max(votes, key=votes.get)
        total = sum(votes.values()) or 1.0
        out.append((char, float(votes[char] / total * max(0.0, min(1.0, row[top[0]])))))
    return out


def classify(mask: np.ndarray, charset: str | None = None) -> tuple[str, float]:
    """The best matching char for one blob: the nearest-neighbour classifier when it has data, else templates."""
    if len(knn_set()[0]):
        return knn(mask, charset)
    return classify_templates(mask, charset)


def classify_templates(mask: np.ndarray, charset: str | None = None) -> tuple[str, float]:
    """The best matching char (from ``charset``, default all) for one blob and how well it matches (0..1)."""
    table = [t for t in all_templates() if charset is None or t[0] in charset]
    if not table or mask.size == 0:
        return "", 0.0
    norm = _normalise(mask)
    aspect = mask.shape[1] / max(1, mask.shape[0])
    best: dict[str, float] = {}
    for char, bitmap, t_aspect, bonus in table:
        score = 1.0 - float(np.abs(norm - bitmap).mean()) * 2.2
        score *= float(np.exp(-abs(np.log(max(aspect, 0.05) / t_aspect)) * 0.35))
        score += bonus
        if score > best.get(char, -1.0):
            best[char] = score
    char = max(best, key=best.get)
    return char, max(0.0, best[char])


def read_gray(gray: np.ndarray, charset: str | None = None) -> list[list[Glyph]]:
    """Lines of characters in a grey marking image (dark text on white): binarised at a few levels, the reading
    whose characters match best."""
    best: tuple[float, list[list[Glyph]]] = (-1.0, [])
    otsu = _otsu(gray)
    for level in (otsu - 25, otsu, otsu + 25):
        lines = read(gray < level, charset, gray)
        glyphs = [g for line in lines for g in line]
        if not glyphs:
            continue
        score = float(np.mean([g.score for g in glyphs])) * min(len(glyphs), 4) / 4
        if score > best[0]:
            best = (score, lines)
    return best[1]


def _otsu(arr: np.ndarray) -> float:
    hist, edges = np.histogram(arr, bins=64, range=(0, 256))
    w = np.cumsum(hist)
    m = np.cumsum(hist * (edges[:-1] + 2))
    total, mt = w[-1], m[-1]
    between = (mt * w - m * total) ** 2 / np.maximum(w * (total - w), 1)
    return float(edges[int(np.argmax(between))] + 2)


def read(binary: np.ndarray, charset: str | None = None, grey: np.ndarray | None = None) -> list[list[Glyph]]:
    """Lines of characters in a binarised marking (ink = True), top to bottom, each left to right."""
    if not all_templates():
        return []
    h, w = binary.shape
    # the body's outline and the ends of a resistor body: straight runs longer than any character
    b8 = binary.astype(np.uint8)
    lh, lv = max(3, int(0.8 * h)), max(3, int(0.85 * h))
    long_h = ndimage.maximum_filter1d(ndimage.minimum_filter1d(b8, lh, axis=1), lh, axis=1) > 0
    long_v = ndimage.maximum_filter1d(ndimage.minimum_filter1d(b8, lv, axis=0), lv, axis=0) > 0
    lines = ndimage.maximum_filter(long_h | long_v, size=3)
    binary = binary & ~lines
    labels, n = ndimage.label(binary)
    if n == 0:
        return []
    boxes = ndimage.find_objects(labels)
    blobs = []
    for i, sl in enumerate(boxes, 1):
        if sl is None:
            continue
        bh, bw = sl[0].stop - sl[0].start, sl[1].stop - sl[1].start
        area = int((labels[sl] == i).sum())
        if area < 0.002 * h * w or bh < 0.12 * h:
            continue
        touches_side = sl[1].start <= 1 or sl[1].stop >= w - 1
        touches_end = sl[0].start <= 1 or sl[0].stop >= h - 1
        centre = (sl[1].start + sl[1].stop) / 2
        if (touches_side or centre < 0.12 * w or centre > 0.88 * w) and bw < 0.3 * bh:
            continue                                    # the dark end of a resistor body
        if bw > 2.2 * bh:                               # an underline, or a body edge
            continue
        if touches_side and touches_end:
            continue
        blobs.append((sl, i, bh, bw))
    if not blobs:
        return []
    tall = [b[2] for b in blobs if b[2] >= 0.35 * h]
    height = float(np.median(tall or [b[2] for b in blobs]))
    # characters are rarely the tallest blobs: the end bars are taller; use the most common height
    heights = sorted(b[2] for b in blobs)
    if len(heights) >= 3:
        groups = [[heights[0]]]
        for value in heights[1:]:
            if value <= groups[-1][-1] * 1.18:
                groups[-1].append(value)
            else:
                groups.append([value])
        height = float(np.median(max(groups, key=len)))
    glyphs = []
    for sl, i, bh, bw in blobs:
        if bh < 0.55 * height or bh > 1.25 * height:      # specks; full-height bars at the body's ends
            continue
        mask = labels[sl] == i
        # characters that touch: split wide blobs evenly
        parts = max(1, int(round(bw / (0.72 * height)))) if bw > 1.05 * height else 1
        step = bw / parts
        for k in range(parts):
            x0, x1 = int(round(k * step)), int(round((k + 1) * step))
            piece = mask[:, x0:x1]
            ys, xs = np.nonzero(piece)
            if not len(xs):
                continue
            piece = piece[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
            patch = piece
            if grey is not None:
                y0g, x0g = sl[0].start + ys.min(), sl[1].start + x0 + xs.min()
                patch = 1.0 - grey[y0g:y0g + piece.shape[0], x0g:x0g + piece.shape[1]] / 255.0
                lo_, hi_ = float(patch.min()), float(patch.max())
                patch = (patch - lo_) / max(hi_ - lo_, 1e-3)
            glyph = Glyph("", 0.0, sl[1].start + x0 + xs.min(), sl[0].start + ys.min(),
                          sl[1].start + x0 + xs.max() + 1, sl[0].start + ys.max() + 1)
            glyph.mask = piece
            glyph.patch = patch
            glyphs.append(glyph)
    for glyph, (char, score) in zip(glyphs, classify_many([g.patch for g in glyphs], charset)):
        glyph.char, glyph.score = char, min(1.0, score)
    # lines: by vertical centre
    lines: list[list[Glyph]] = []
    for g in sorted(glyphs, key=lambda g: (g.y0 + g.y1) / 2):
        cy = (g.y0 + g.y1) / 2
        for line in lines:
            lcy = np.mean([(x.y0 + x.y1) / 2 for x in line])
            if abs(cy - lcy) < 0.5 * height:
                line.append(g)
                break
        else:
            lines.append([g])
    return [sorted(line, key=lambda g: g.x0) for line in lines]
