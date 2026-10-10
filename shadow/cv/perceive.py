"""Cached screen reading: one full OCR pass, then only the pixels that changed.

A full pass over a window costs about a second on CPU; a changed field costs
tens of milliseconds. Unchanged pixels cost nothing.
"""
from __future__ import annotations

from typing import Optional

import cv2
import numpy as np

from .ocr import Line, Ocr

MARGIN = 10
FULL_PASS_FRACTION = 0.4  # more changed area than this: read everything again
SMALL_REGION = 90  # regions no taller than this are enlarged before reading
UPSCALE = 1.5


def _intersects(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _has_ink(crop: np.ndarray) -> bool:
    """True when the crop holds more than background and a thin caret."""
    gray = crop.mean(axis=2)
    different = np.abs(gray - np.median(gray)) > 60
    columns = different.any(axis=0).sum()
    return columns >= 4


def erase_carets(image: np.ndarray, boxes: list) -> np.ndarray:
    """Paint out the text caret in single-line controls.

    A caret sitting against the last character is read as part of it ("100" becomes "10d").
    The caret is the one thin vertical stroke that is taller than any letter."""
    out = None
    for l, t, r, b in boxes:
        if not 14 <= b - t <= 64:
            continue
        inset = 3
        crop = image[t + inset:b - inset, l + inset:r - inset]
        if crop.shape[0] < 8 or crop.shape[1] < 8:
            continue
        gray = crop.mean(axis=2)
        ink = np.abs(gray - np.median(gray)) > 70
        tall = np.where(ink.sum(axis=0) >= 0.62 * crop.shape[0])[0]
        if tall.size == 0 or tall.size > 3 or tall[-1] - tall[0] != tall.size - 1:
            continue  # nothing caret-like, or more than one such stroke
        first, last = int(tall[0]), int(tall[-1])
        if out is None:
            out = image.copy()
        background = np.median(crop.reshape(-1, 3), axis=0).astype(np.uint8)
        out[t + inset:b - inset, l + inset + first:l + inset + last + 1] = background
    return image if out is None else out


def changed_regions(before: np.ndarray, after: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Bounding boxes of the pixel regions that differ between two frames."""
    mask = np.any(before != after, axis=2).astype(np.uint8)
    if not mask.any():
        return []
    mask = cv2.dilate(mask, np.ones((9, 25), np.uint8))  # join the characters of one line
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    boxes = []
    for i in range(1, count):
        x, y, w, h, _area = stats[i]
        boxes.append((int(x), int(y), int(x + w), int(y + h)))
    return boxes


class Perceiver:
    def __init__(self, ocr: Optional[Ocr] = None) -> None:
        self._ocr = ocr
        self._frames: dict = {}  # key -> (image, lines)
        self._known: list = []
        self.last = {"mode": "none", "ms": 0.0, "regions": 0}

    @property
    def ocr(self) -> Ocr:
        if self._ocr is None:
            from .ocr import RapidOcr
            self._ocr = RapidOcr()
        return self._ocr

    def lines(self, key, image: np.ndarray, known: Optional[list] = None) -> list[Line]:
        """OCR lines for this frame, in image pixels, reusing whatever did not change.

        `known` are boxes the UI tree says hold a single control (an input, a button):
        a change inside one re-reads that whole control."""
        self._known = known or []
        image = erase_carets(image, self._known)
        cached = self._frames.get(key)
        if cached is not None and cached[0].shape == image.shape:
            old_image, old_lines = cached
            regions = changed_regions(old_image, image)
            if not regions:
                self.last = {"mode": "cached", "regions": 0}
                return old_lines
            h, w = image.shape[:2]
            area = sum((r - l) * (b - t) for l, t, r, b in regions)
            if area < FULL_PASS_FRACTION * w * h:
                lines = self._partial(image, old_lines, regions)
                self._frames[key] = (image, lines)
                self.last = {"mode": "partial", "regions": len(regions)}
                return lines
        lines = self._refine_inputs(image, self.ocr.read(image))
        self._frames[key] = (image, lines)
        self.last = {"mode": "full", "regions": 1}
        return lines

    def _partial(self, image: np.ndarray, old_lines: list[Line], regions) -> list[Line]:
        h, w = image.shape[:2]
        grown = []
        for region in regions:
            l, t, r, b = region
            # A change inside a control the tree knows (a caret, a typed character) means
            # re-reading that whole control, so its text is never read in halves.
            box = next((k for k in self._known
                        if k[0] - 3 <= l and k[1] - 3 <= t and k[2] + 3 >= r and k[3] + 3 >= b), None)
            if box is not None:
                l, t, r, b = box
            l, t, r, b = max(0, l - MARGIN), max(0, t - MARGIN), min(w, r + MARGIN), min(h, b + MARGIN)
            # The box must hold every text line it touches in full: a line cut in half is not
            # detected, and would silently vanish. Repeat until nothing new is touched.
            changed = True
            while changed:
                changed = False
                for line in old_lines:
                    if _intersects(line.box, (l, t, r, b)):
                        grown_box = (min(l, line.box[0] - 2), min(t, line.box[1] - 2),
                                     max(r, line.box[2] + 2), max(b, line.box[3] + 2))
                        if grown_box != (l, t, r, b):
                            l, t, r, b = grown_box
                            changed = True
            grown.append((max(0, l), max(0, t), min(w, r), min(h, b)))
        merged: list[tuple[int, int, int, int]] = []
        for box in sorted(grown):
            while True:  # joining two boxes can make the result touch a third
                hit = next((other for other in merged if _intersects(box, other)), None)
                if hit is None:
                    break
                merged.remove(hit)
                box = (min(box[0], hit[0]), min(box[1], hit[1]), max(box[2], hit[2]), max(box[3], hit[3]))
            merged.append(box)
        kept = [line for line in old_lines if not any(_intersects(line.box, box) for box in merged)]
        for box in merged:
            kept += self._read_box(image, box)
        return kept

    def _read_box(self, image: np.ndarray, box) -> list[Line]:
        """Read one region. Small regions are enlarged first: UI text is tiny and
        reads noticeably better at 1.5x, at almost no cost for a crop this size."""
        l, t, r, b = box
        crop = np.ascontiguousarray(image[t:b, l:r])
        if crop.size == 0 or not _has_ink(crop):
            return []
        if b - t > SMALL_REGION:
            return [line.shifted(l, t) for line in self.ocr.read(crop)]
        big = cv2.resize(crop, None, fx=UPSCALE, fy=UPSCALE, interpolation=cv2.INTER_CUBIC)
        out = []
        for line in self.ocr.read(big):
            bl, bt, br, bb = (int(v / UPSCALE) for v in line.box)
            out.append(Line(line.text, (bl + l, bt + t, br + l, bb + t), line.conf))
        return out

    def _refine_inputs(self, image: np.ndarray, lines: list[Line]) -> list[Line]:
        """After a full pass, re-read each filled input on its own for an accurate value."""
        for box in self._known:
            l, t, r, b = box
            if not 14 <= b - t <= 64:
                continue
            crop = image[t + 4:b - 4, l + 4:r - 4]
            if crop.size == 0 or (crop.mean(axis=2) > 235).mean() < 0.55:
                continue  # not a light input surface
            fresh = self._read_box(image, box)
            if fresh:
                lines = [ln for ln in lines if not _intersects(ln.box, box)] + fresh
        return lines

    def forget(self, key=None) -> None:
        if key is None:
            self._frames.clear()
        else:
            self._frames.pop(key, None)
