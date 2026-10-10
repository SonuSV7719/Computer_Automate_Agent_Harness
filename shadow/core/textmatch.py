"""Text comparison that tolerates how OCR misreads, without tolerating real differences.

OCR confuses specific look-alike characters (9/g, 0/O, 1/l/I, 5/S, 8/B, 2/Z) and
drops or adds spaces. Mapping each look-alike group to one character lets
"Saved INV-g" equal "Saved INV-9" while "INV-8" still differs from "INV-9".
"""
from __future__ import annotations

_GROUPS = ["0oOQD", "1lI|i!", "5sS", "8B", "9gq", "2zZ", "6G", "uv", "cC(", "wW", "xX", "kK", "pP"]
_CANON = {ch: group[0] for group in _GROUPS for ch in group}


def squash(text: str) -> str:
    """Case and spacing removed."""
    return "".join(text.casefold().split())


def ocr_canon(text: str) -> str:
    """Spacing removed and look-alike characters folded together."""
    folded = "".join(_CANON.get(ch, ch) for ch in "".join(text.split()))
    return "".join(_CANON.get(ch, ch) for ch in folded.casefold())


def same(a: str, b: str, ocr: bool = False) -> bool:
    if squash(a) == squash(b):
        return True
    return ocr and ocr_canon(a) == ocr_canon(b)


def contains(haystack: str, needle: str, ocr: bool = False) -> bool:
    if not needle:
        return False
    if needle.casefold() in haystack.casefold() or squash(needle) in squash(haystack):
        return True
    return ocr and ocr_canon(needle) in ocr_canon(haystack)
