"""Text recognition with boxes. RapidOCR by default; anything with read() can replace it."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Protocol

Box = tuple[int, int, int, int]


@dataclass
class Line:
    text: str
    box: Box  # left, top, right, bottom in image pixels
    conf: float

    def shifted(self, dx: int, dy: int) -> "Line":
        l, t, r, b = self.box
        return Line(self.text, (l + dx, t + dy, r + dx, b + dy), self.conf)


class Ocr(Protocol):
    def read(self, image) -> list[Line]:
        """Find and read all text. image: HxWx3 BGR uint8 array."""

    def read_line(self, image) -> Optional[Line]:
        """Read an image known to hold one line of text (no detection pass)."""


def _is_number(value) -> bool:
    if isinstance(value, (int, float)):
        return True
    try:
        float(value)
        return isinstance(value, str) and value.replace(".", "", 1).isdigit()
    except (TypeError, ValueError):
        return False


class RapidOcr:
    def __init__(self, min_conf: float = 0.5) -> None:
        from rapidocr_onnxruntime import RapidOCR

        # Only shrink large images. The default upscales every crop to 736 px on its
        # short side, which makes a small changed region slower to read than a whole window.
        # A fixed, small thread count is several times faster here than the library default.
        threads = int(os.environ.get("SHADOW_OCR_THREADS", "4"))
        self._engine = RapidOCR(det_limit_type="max", det_limit_side_len=960,
                                intra_op_num_threads=threads, inter_op_num_threads=1)
        self.min_conf = min_conf

    def read_line(self, image) -> Optional[Line]:
        if image.shape[0] < 8 or image.shape[1] < 8:
            return None
        result, _ = self._engine(image, use_det=False, use_cls=False)
        for item in result or []:
            text = next((part for part in item if isinstance(part, str) and not _is_number(part)), "")
            conf = next((float(part) for part in reversed(item) if _is_number(part)), 0.0)
            text = text.strip()
            if text and conf >= self.min_conf:
                return Line(text, (0, 0, image.shape[1], image.shape[0]), round(conf, 2))
        return None

    def read(self, image) -> list[Line]:
        if image.shape[0] < 8 or image.shape[1] < 8:
            return []
        result, _ = self._engine(image, use_cls=False)
        lines = []
        for points, text, conf in result or []:
            conf = float(conf)
            text = text.strip()
            if not text or conf < self.min_conf:
                continue
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            lines.append(Line(text, (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))), round(conf, 2)))
        return lines
