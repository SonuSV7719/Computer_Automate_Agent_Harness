"""Merge computer vision into the UI tree, producing one tree.

The tree wins wherever it has an answer. Vision fills what it lacks:
  * a control the tree knows but cannot name gets its name from the text drawn on it
  * an unnamed surface shaped like an input becomes an input, with its value read by OCR
  * a region with no tree at all (remote desktop, canvas) gets its elements from pixels
  * text on screen that no tree node explains becomes a text element
Text the tree already explains is dropped, and the tree node is marked cv_confirmed.
"""
from __future__ import annotations

import numpy as np

from ..core.elements import Box, Element
from ..core.serialize import visible
from .boxes import contains, dark_fraction, find_boxes, interior_brightness, iou
from .ocr import Line

NAMELESS_CONTROLS = {"button", "checkbox", "radio", "link", "menuitem", "tabitem", "listitem", "treeitem",
                     "splitbutton"}
SURFACES = {"pane", "custom", "image", "text", "document", "group"}
CHROME = {"titlebar", "menubar", "scrollbar"}
HOLE_MIN_W, HOLE_MIN_H = 200, 90
OVERLAP = 0.6


def _center_in(inner: Box, outer: Box) -> bool:
    cx, cy = (inner[0] + inner[2]) / 2, (inner[1] + inner[3]) / 2
    return outer[0] <= cx <= outer[2] and outer[1] <= cy <= outer[3]


def _squash(text: str) -> str:
    return "".join(text.casefold().split())


def _join(lines: list[Line]) -> str:
    ordered = sorted(lines, key=lambda ln: (ln.box[1] // 12, ln.box[0]))
    return " ".join(ln.text for ln in ordered)


def _content(root: Element):
    """Visible nodes outside the window chrome."""
    stack = [c for c in root.children if c.role not in CHROME]
    while stack:
        node = stack.pop()
        if not visible(node):
            continue
        yield node
        stack.extend(c for c in node.children if c.role not in CHROME)


def _is_leaf(e: Element) -> bool:
    return not any(visible(c) for c in e.children)


def _is_hole(e: Element) -> bool:
    """A large surface with nothing inside it in the tree: a canvas, a remote desktop view."""
    l, t, r, b = e.box
    return e.role in SURFACES and _is_leaf(e) and r - l >= HOLE_MIN_W and b - t >= HOLE_MIN_H


def _is_backdrop(e: Element, nodes: list[Element]) -> bool:
    """True when other tree elements sit on top of this surface. Browsers put an empty
    full-page pane behind the document; it is not a hole, the page is described elsewhere."""
    ancestors = set()
    node = e.parent
    while node is not None:
        ancestors.add(id(node))
        node = node.parent
    on_top = 0
    for other in nodes:
        if other is e or id(other) in ancestors or not (other.name or other.actions):
            continue
        if _center_in(other.box, e.box):
            on_top += 1
            if on_top >= 2:
                return True
    return False


def needs_cv(roots: list[Element]) -> bool:
    """True when the tree alone cannot describe this window."""
    if not roots:
        return False
    root = roots[0]
    nodes = list(_content(root))
    if not nodes:
        return True  # nothing but a title bar: the whole window is pixels
    for e in nodes:
        if e.name:
            continue
        if e.role in NAMELESS_CONTROLS:
            return True
        if e.role in SURFACES and _is_leaf(e) and e.area >= 12 * 12 and not _is_backdrop(e, nodes):
            return True
    return False


def cv_areas(roots: list[Element], window_box: Box):
    """Image-space boxes where vision is needed, or None for the whole window.

    Vision reads only what the tree cannot describe: holes and unnamed controls. In a
    browser that is the canvas, not the page around it."""
    if not roots:
        return None
    root = roots[0]
    nodes = list(_content(root))
    if not nodes:
        return None
    ox, oy = window_box[0], window_box[1]
    w, h = window_box[2] - ox, window_box[3] - oy
    areas = []
    for e in nodes:
        if e.name:
            continue
        wanted = e.role in NAMELESS_CONTROLS or (
            e.role in SURFACES and _is_leaf(e) and e.area >= 12 * 12 and not _is_backdrop(e, nodes))
        if wanted:
            l, t, r, b = e.box
            areas.append((max(0, l - ox - 4), max(0, t - oy - 4), min(w, r - ox + 4), min(h, b - oy + 4)))
    return areas


def known_boxes(roots: list[Element], window_box: Box) -> list[Box]:
    """Image-space boxes of tree nodes that hold one control, for single-line OCR."""
    if not roots:
        return []
    ox, oy = window_box[0], window_box[1]
    out = []
    for e in _content(roots[0]):
        if e.name or not _is_leaf(e) or _is_hole(e):
            continue
        if e.role in NAMELESS_CONTROLS or (e.role in SURFACES and not e.actions):
            l, t, r, b = e.box
            if 12 <= b - t <= 64 and r - l >= 12:
                out.append((l - ox, t - oy, r - ox, b - oy))
    return out


def _input_like(image: np.ndarray, local: Box, needs_contrast: bool = False) -> bool:
    """A light, wide, single-line surface. With needs_contrast it must also stand out from
    what surrounds it: a bordered box on a white canvas is a shape or a button, not a field."""
    w, h = local[2] - local[0], local[3] - local[1]
    if not (14 <= h <= 64 and w >= 50 and w / h >= 1.8):
        return False
    white, inside = interior_brightness(image, local)
    if white < 0.55:
        return False
    if needs_contrast:
        l, t, r, b = local
        ring = 6
        outside = [image[max(0, t - ring):max(0, t - 2), l:r], image[b + 2:b + ring, l:r]]
        values = [strip.mean() for strip in outside if strip.size]
        if values and abs(inside - sum(values) / len(values)) < 12:
            return False
    return True


def merge(roots: list[Element], window_box: Box, image: np.ndarray, lines: list[Line]) -> dict:
    """Mutate the tree in place. `image` is the BGR capture of window_box; `lines` are in image pixels."""
    stats = {"named": 0, "inputs": 0, "cv_elements": 0, "confirmed": 0, "holes": 0}
    if not roots:
        return stats
    root = roots[0]
    ox, oy = window_box[0], window_box[1]
    h_img, w_img = image.shape[:2]
    pending = [ln.shifted(ox, oy) for ln in lines]

    def local(box: Box) -> Box:
        return (max(0, box[0] - ox), max(0, box[1] - oy), min(w_img, box[2] - ox), min(h_img, box[3] - oy))

    def take(box: Box) -> list[Line]:
        inside = [ln for ln in pending if _center_in(ln.box, box)]
        for ln in inside:
            pending.remove(ln)
        return inside

    nodes = sorted(_content(root), key=lambda e: e.area)
    touched: set[int] = set()
    parents: dict[int, Element] = {}

    def mark(parent: Element) -> None:
        touched.add(id(parent))
        parents[id(parent)] = parent

    # 1. Controls the tree knows but cannot name.
    for e in nodes:
        if e.role in NAMELESS_CONTROLS and not e.name:
            found = take(e.box)
            if found:
                e.name = _join(found)
                e.source = "merged"
                e.confidence = min(ln.conf for ln in found)
                stats["named"] += 1

    # 2. Unnamed surfaces: an input, a piece of text, or a hole to read from pixels.
    holes = []
    for e in nodes:
        if e.name or e.role not in SURFACES or not _is_leaf(e):
            continue
        if _is_backdrop(e, nodes):
            continue
        if _is_hole(e):
            holes.append(e)
        elif e.actions:
            continue
        elif _input_like(image, local(e.box)):
            found = take(e.box)
            e.role, e.source = "input", "merged"
            e.value = _join(found)
            e.actions = ["click", "type"]
            e.confidence = min((ln.conf for ln in found), default=0.8)
            stats["inputs"] += 1
        else:
            found = take(e.box)
            if found:
                e.role, e.source = "text", "merged"
                e.name = _join(found)
                e.confidence = min(ln.conf for ln in found)
                stats["named"] += 1
    if not list(_content(root)):
        holes.append(root)  # no content nodes at all: read the whole client area

    # 3. Holes: detect controls from pixels.
    for hole in holes:
        stats["holes"] += 1
        hl, ht, hr, hb = local(hole.box)
        if hole is root:
            ht = min(hb, ht + 34)  # skip the title bar
        crop = np.ascontiguousarray(image[ht:hb, hl:hr])
        boxes = [(l + hl + ox, t + ht + oy, r + hl + ox, b + ht + oy) for l, t, r, b in find_boxes(crop)]
        for box in sorted(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1])):
            w, h = box[2] - box[0], box[3] - box[1]
            element = None
            if any(o is not hole and (o.name or o.actions) and iou(o.box, box) > OVERLAP for o in nodes):
                continue  # the tree already has this element; the tree wins
            if 10 <= w <= 28 and abs(w - h) <= 5:
                label = next((ln for ln in pending
                              if 0 <= ln.box[0] - box[2] <= 16 and ln.box[1] < box[3] and ln.box[3] > box[1]), None)
                if label is not None:
                    pending.remove(label)
                    ticked = dark_fraction(image, local(box)) > 0.12
                    element = Element(role="checkbox", name=label.text, box=box, actions=["click"],
                                      checked="checked" if ticked else "unchecked", confidence=label.conf)
            elif _input_like(image, local(box), needs_contrast=True):
                found = take(box)
                element = Element(role="input", box=box, value=_join(found), actions=["click", "type"],
                                  confidence=min((ln.conf for ln in found), default=0.7))
                stats["inputs"] += 1
            else:
                inside = [ln for ln in pending if contains(box, ln.box, slack=3)]
                # A button has room around its caption; a tight box is just framed text.
                roomy = inside and all(h - (ln.box[3] - ln.box[1]) >= 8 for ln in inside)                     and w - max(ln.box[2] for ln in inside) + min(ln.box[0] for ln in inside) >= 10
                if roomy and h <= 64:
                    for ln in inside:
                        pending.remove(ln)
                    element = Element(role="button", name=_join(inside), box=box, actions=["click"],
                                      confidence=min(ln.conf for ln in inside))
            if element is not None:
                element.source, element.parent, element.root_hwnd = "cv", hole, hole.root_hwnd
                hole.children.append(element)
                stats["cv_elements"] += 1
        if hole is not root and hole.role in SURFACES:
            hole.role = "region"
            hole.source = "merged"
            hole.actions = ["click_at", "drag"]  # positions inside it are given as x,y from 0 to 1000
        mark(hole)

    # 4. Remaining text: explained by the tree, or a new text element.
    everything = [root] + nodes + [c for chrome in root.children if chrome.role in CHROME or chrome.name
                                   for c in chrome.walk()]
    for ln in pending:
        squashed = _squash(ln.text)
        explained = None
        for e in everything:
            if not _center_in(ln.box, e.box):
                continue
            if e.name and e.role in NAMELESS_CONTROLS and e.source == "tree":
                explained = e  # a glyph or caption drawn on a control the tree already names
                break
            known = _squash(e.name) + "\n" + _squash(e.value or "")
            if squashed and squashed in known:
                explained = e
                break
        if explained is not None:
            explained.cv_confirmed = True
            stats["confirmed"] += 1
            continue
        home = root
        for e in nodes:  # smallest first
            if e.role not in NAMELESS_CONTROLS and e.role != "input" and _center_in(ln.box, e.box):
                home = e
                break
        if any(c.source == "cv" and iou(c.box, ln.box) > OVERLAP for c in home.children):
            continue
        text = Element(role="text", name=ln.text, box=ln.box, source="cv", confidence=ln.conf,
                       parent=home, root_hwnd=home.root_hwnd or root.root_hwnd)
        home.children.append(text)
        mark(home)
        stats["cv_elements"] += 1

    # Reading order, so the screen text runs top to bottom with window chrome last.
    for parent in [root] + nodes:
        if parent.children:
            parent.children.sort(key=lambda c: (c.role in CHROME, c.box[1] // 14, c.box[0]))
    return stats
