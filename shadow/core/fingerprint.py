"""Element fingerprints: several independent ways to find the same element again.

A fingerprint is recorded when a step is learned and resolved at replay with no
LLM. A match is accepted only when a second property agrees with the first.
"""
from __future__ import annotations

import re
from typing import Optional

from . import textmatch
from .elements import Element, Snapshot
from .serialize import visible

_VOLATILE_ID = re.compile(r"^\d+$|[0-9a-f]{8}-[0-9a-f]{4}-|^\{.*\}$", re.I)
_DIGITS = re.compile(r"\d+")


def stable_id(automation_id: str) -> str:
    """Drop automation IDs that are generated per run (pure numbers, GUIDs)."""
    return "" if not automation_id or _VOLATILE_ID.search(automation_id) else automation_id


def ancestor_path(e: Element) -> list[str]:
    path = []
    node = e.parent
    while node is not None:
        if node.title or node.role in ("window", "dialog", "menu", "toolbar", "tab", "table", "list"):
            path.append(f"{node.role}:{node.title}")
        node = node.parent
    return list(reversed(path))


def neighbours(e: Element, snapshot: Snapshot, limit: int = 3) -> list[dict]:
    """Nearest named elements and the direction to them, for unlabelled and CV elements."""
    cx, cy = e.center
    found = []
    for other in snapshot.elements:
        if other is e or not other.title or other.role in ("window", "dialog"):
            continue
        ox, oy = other.center
        dx, dy = cx - ox, cy - oy
        dist = abs(dx) + abs(dy)
        if dist > 400:
            continue
        if abs(dx) >= abs(dy):
            direction = "right-of" if dx > 0 else "left-of"
        else:
            direction = "below" if dy > 0 else "above"
        found.append((dist, {"role": other.role, "title": other.title, "direction": direction}))
    found.sort(key=lambda item: item[0])
    return [item[1] for item in found[:limit]]


def make(e: Element, snapshot: Snapshot) -> dict:
    wl, wt, wr, wb = snapshot.window.box
    ww, wh = max(1, wr - wl), max(1, wb - wt)
    l, t, r, b = e.box
    return {
        "source": e.source,
        "role": e.role,
        "name": e.name,
        "label": e.label,
        "automation_id": stable_id(e.automation_id),
        "class_name": e.class_name,
        "path": ancestor_path(e),
        "neighbours": neighbours(e, snapshot),
        "rel_box": [round((l - wl) / ww, 4), round((t - wt) / wh, 4),
                    round((r - wl) / ww, 4), round((b - wt) / wh, 4)],
        "window": {"title": _DIGITS.sub("#", snapshot.window.title), "process": snapshot.window.process},
    }


def _same_text(a: str, b: str, fuzzy: bool = False) -> bool:
    """Same text ignoring case and spacing; OCR-read text also ignores look-alike characters."""
    return textmatch.same(a, b, ocr=fuzzy)


def _confirm(e: Element, fp: dict, skip: str) -> int:
    """How many properties other than `skip` agree with the fingerprint."""
    score = 0
    fuzzy = fp.get("source", "tree") != "tree"
    if skip != "role" and e.role == fp["role"]:
        score += 1
    if skip != "name" and fp.get("name") and _same_text(e.name, fp["name"], fuzzy):
        score += 1
    if skip != "label" and fp.get("label") and _same_text(e.label, fp["label"], fuzzy):
        score += 1
    if skip != "class" and fp.get("class_name") and e.class_name == fp["class_name"]:
        score += 1
    if skip != "path" and fp.get("path") and ancestor_path(e)[-1:] == fp["path"][-1:]:
        score += 1
    return score


def _pick(candidates: list[Element], fp: dict, skip: str) -> Optional[Element]:
    """Exactly one candidate, confirmed by a second property; otherwise none."""
    confirmed = [e for e in candidates if _confirm(e, fp, skip) >= 1]
    if len(confirmed) == 1:
        return confirmed[0]
    if len(confirmed) > 1 and fp.get("path"):
        same_path = [e for e in confirmed if ancestor_path(e) == fp["path"]]
        if len(same_path) == 1:
            return same_path[0]
    return None


def resolve(fp: dict, snapshot: Snapshot) -> tuple[Optional[Element], str]:
    """Find the fingerprinted element. Returns (element, locator used) or (None, reason)."""
    pool = [e for e in snapshot.elements if visible(e)]
    fuzzy = fp.get("source", "tree") != "tree"

    if fp.get("automation_id"):
        hit = _pick([e for e in pool if e.automation_id == fp["automation_id"]], fp, "automation_id")
        if hit:
            return hit, "automation_id"

    if fp.get("name"):
        hit = _pick([e for e in pool if e.role == fp["role"] and _same_text(e.name, fp["name"], fuzzy)],
                    fp, "role")
        if hit:
            return hit, "role+name"

    if fp.get("label"):
        hit = _pick([e for e in pool if e.role == fp["role"] and _same_text(e.label, fp["label"], fuzzy)],
                    fp, "role")
        if hit:
            return hit, "role+label"

    # Unnamed element: same role, same container, close to where it was.
    if not fp.get("name") and not fp.get("label") and fp.get("path"):
        wl, wt, wr, wb = snapshot.window.box
        ww, wh = max(1, wr - wl), max(1, wb - wt)
        fx = (fp["rel_box"][0] + fp["rel_box"][2]) / 2
        fy = (fp["rel_box"][1] + fp["rel_box"][3]) / 2
        near = []
        for e in pool:
            if e.role != fp["role"] or e.name or ancestor_path(e) != fp["path"]:
                continue
            cx, cy = e.center
            if abs((cx - wl) / ww - fx) < 0.05 and abs((cy - wt) / wh - fy) < 0.05:
                near.append(e)
        if len(near) == 1:
            return near[0], "path+position"

    return None, "not found"
