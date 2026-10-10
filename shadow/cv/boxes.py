"""Classical detection of control-shaped rectangles in pixels (inputs, buttons, checkboxes).

Used where a region has no UI tree at all: remote desktop viewers, canvases,
custom-drawn applications.
"""
from __future__ import annotations

import cv2
import numpy as np

Box = tuple[int, int, int, int]


def iou(a: Box, b: Box) -> float:
    l, t, r, b_ = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, r - l) * max(0, b_ - t)
    if not inter:
        return 0.0
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union


def contains(outer: Box, inner: Box, slack: int = 2) -> bool:
    return (outer[0] - slack <= inner[0] and outer[1] - slack <= inner[1]
            and outer[2] + slack >= inner[2] and outer[3] + slack >= inner[3])


def find_boxes(image: np.ndarray, min_w: int = 12, max_w: int = 900, min_h: int = 12, max_h: int = 70) -> list[Box]:
    """Rectangles with a visible border or fill edge, sized like a control."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    thin = cv2.Canny(gray, 40, 120)
    edges = cv2.dilate(thin, np.ones((2, 2), np.uint8))
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    def bordered(x: int, y: int, w: int, h: int) -> bool:
        """All four sides are drawn. Text between two separator bars has only two."""
        band = 3
        top = thin[max(0, y - 1):y + band, x:x + w].any(axis=0).mean()
        bottom = thin[max(0, y + h - band):y + h + 1, x:x + w].any(axis=0).mean()
        left = thin[y:y + h, max(0, x - 1):x + band].any(axis=1).mean()
        right = thin[y:y + h, max(0, x + w - band):x + w + 1].any(axis=1).mean()
        return min(top, bottom, left, right) >= 0.6

    found: list[Box] = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if not (min_w <= w <= max_w and min_h <= h <= max_h):
            continue
        if cv2.contourArea(contour) < 0.75 * w * h:  # not a filled-in rectangle outline
            continue
        if not bordered(x, y, w, h):
            continue
        box = (x, y, x + w, y + h)
        if any(iou(box, other) > 0.7 for other in found):
            continue
        found.append(box)
    # An outline yields an outer and an inner contour; keep the outer one.
    found.sort(key=lambda b: -(b[2] - b[0]) * (b[3] - b[1]))
    kept: list[Box] = []
    for box in found:
        if not any(contains(big, box) and iou(big, box) > 0.5 for big in kept):
            kept.append(box)
    return kept


def interior_brightness(image: np.ndarray, box: Box, inset: int = 4) -> tuple[float, float]:
    """(share of near-white pixels, mean brightness) inside a box."""
    l, t, r, b = box
    crop = image[t + inset:b - inset, l + inset:r - inset]
    if crop.size == 0:
        return 0.0, 0.0
    gray = crop.mean(axis=2)
    return float((gray > 235).mean()), float(gray.mean())


def dark_fraction(image: np.ndarray, box: Box, inset: int = 3) -> float:
    """Share of dark pixels inside a box; a ticked checkbox has more than an empty one."""
    l, t, r, b = box
    crop = image[t + inset:b - inset, l + inset:r - inset]
    if crop.size == 0:
        return 0.0
    return float((crop.mean(axis=2) < 110).mean())
