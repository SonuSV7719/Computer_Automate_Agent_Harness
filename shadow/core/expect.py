"""Post-conditions: what must be true on screen after a step.

Spec forms (join several with ' && '):
  text:Saved successfully     some visible element shows this text
  gone:Saving                 no visible element shows this text
  window:Invoice Entry        the window title contains this
  value:Invoice No=INV-1      the element with this name or label holds this value
"""
from __future__ import annotations

from . import textmatch
from .elements import Snapshot
from .serialize import visible

KINDS = ("text", "gone", "window", "value")


def parse(spec: str) -> list[tuple[str, str]]:
    out = []
    for part in spec.split("&&"):
        part = part.strip()
        if not part:
            continue
        kind, sep, arg = part.partition(":")
        if not sep or kind not in KINDS or not arg:
            raise ValueError(f"bad expectation '{part}'; use one of {', '.join(k + ':…' for k in KINDS)}")
        out.append((kind, arg.strip()))
    return out


def _has_text(snapshot: Snapshot, needle: str) -> bool:
    for root in snapshot.roots:
        for e in root.walk():
            if e is not root and not visible(e):
                continue
            read_by_ocr = e.source != "tree"
            for field in (e.name, e.label, e.value or ""):
                if textmatch.contains(field, needle, ocr=read_by_ocr):
                    return True
    return False


def _has_value(snapshot: Snapshot, arg: str) -> bool:
    title, _, want = arg.partition("=")
    for e in snapshot.elements:
        read_by_ocr = e.source != "tree"
        if textmatch.same(e.title, title, read_by_ocr) and textmatch.same(e.value or "", want, read_by_ocr):
            return True
    return False


def check(spec: str, snapshot: Snapshot) -> bool:
    for kind, arg in parse(spec):
        if kind == "text":
            ok = _has_text(snapshot, arg)
        elif kind == "gone":
            ok = not _has_text(snapshot, arg)
        elif kind == "window":
            ok = arg.casefold() in snapshot.window.title.casefold()
        else:
            ok = _has_value(snapshot, arg)
        if not ok:
            return False
    return True
