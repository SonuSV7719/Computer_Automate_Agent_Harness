"""Draw a whole diagram on a whiteboard (Excalidraw and similar): boxes with labels and the
arrows between them.

A diagram is planned as data (by Gemini from a description, or given directly), laid out on
a grid, then drawn with the board's own tools, the way a person would: pick the rectangle
tool, drag a box, type its label; pick the arrow tool, drag from one box to the next.
Afterwards the canvas is read back and every label is checked.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from .core import textmatch

DIAGRAM_SYSTEM = """\
You plan a diagram that will be drawn on a whiteboard: boxes with short labels, and arrows
between them. Place boxes on a grid with "col" and "row" starting at 0; keep it readable,
at most 4 columns and 4 rows. Every arrow must join boxes in neighbouring cells (left, right,
above, below or diagonal) so it is short and never passes through another box; never place a
box between two boxes that are connected to each other. Labels are 1 to 3 words. Reply with JSON:
{"title": "...", "nodes": [{"id": "api", "label": "API Gateway", "col": 1, "row": 0}, ...],
 "edges": [{"from": "api", "to": "auth"}, ...]}"""

DIAGRAM_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "title": {"type": "STRING"},
        "nodes": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "id": {"type": "STRING"}, "label": {"type": "STRING"},
            "col": {"type": "INTEGER"}, "row": {"type": "INTEGER"}}, "required": ["id", "label", "col", "row"]}},
        "edges": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "from": {"type": "STRING"}, "to": {"type": "STRING"}}, "required": ["from", "to"]}},
    },
    "required": ["nodes", "edges"],
}

# Where on the canvas (0-1000 each way) boxes may go: clear of the toolbar along the top,
# the style panel that opens on the left, and the zoom and help controls at the bottom.
DRAW_AREA = (250, 170, 975, 930)
TOOLS = {"box": "Rectangle", "arrow": "Arrow"}


def plan_diagram(client, description: str) -> dict:
    spec = client.json(DIAGRAM_SYSTEM, f"DIAGRAM TO DRAW\n{description}", DIAGRAM_SCHEMA)
    return validate(spec)


def validate(spec: dict) -> dict:
    nodes = spec.get("nodes") or []
    if not nodes:
        raise ValueError("the diagram has no boxes")
    ids = [n["id"] for n in nodes]
    if len(set(ids)) != len(ids):
        raise ValueError("two boxes share an id")
    taken = set()
    for n in nodes:
        n["col"], n["row"] = max(0, int(n["col"])), max(0, int(n["row"]))
        if (n["col"], n["row"]) in taken:
            raise ValueError(f'box "{n["label"]}" is placed on top of another box')
        taken.add((n["col"], n["row"]))
    known = set(ids)
    edges = [e for e in spec.get("edges") or [] if e["from"] in known and e["to"] in known and e["from"] != e["to"]]
    return {"title": spec.get("title", ""), "nodes": nodes, "edges": edges}


def layout(spec: dict, area: tuple = DRAW_AREA) -> dict[str, tuple[int, int, int, int]]:
    """Each box's rectangle on the 0-1000 canvas grid."""
    left, top, right, bottom = area
    cols = max(n["col"] for n in spec["nodes"]) + 1
    rows = max(n["row"] for n in spec["nodes"]) + 1
    cell_w, cell_h = (right - left) / cols, (bottom - top) / rows
    # Wide enough that a two-word label fits on one or two lines, never broken mid-word.
    box_w, box_h = min(cell_w * 0.8, 260), min(cell_h * 0.55, 120)
    boxes = {}
    for n in spec["nodes"]:
        cx, cy = left + cell_w * (n["col"] + 0.5), top + cell_h * (n["row"] + 0.5)
        boxes[n["id"]] = (round(cx - box_w / 2), round(cy - box_h / 2), round(cx + box_w / 2), round(cy + box_h / 2))
    return boxes


def arrow_points(a: tuple, b: tuple) -> tuple[tuple[int, int], tuple[int, int]]:
    """From the side of box a that faces box b, to the facing side of box b."""
    acx, acy = (a[0] + a[2]) / 2, (a[1] + a[3]) / 2
    bcx, bcy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    gap = 6
    if abs(bcx - acx) >= abs(bcy - acy):
        if bcx > acx:
            return (a[2] + gap, round(acy)), (b[0] - gap, round(bcy))
        return (a[0] - gap, round(acy)), (b[2] + gap, round(bcy))
    if bcy > acy:
        return (round(acx), a[3] + gap), (round(bcx), b[1] - gap)
    return (round(acx), a[1] - gap), (round(bcx), b[3] + gap)


def _canvas(snap):
    """The drawing surface: the largest region or image in the window."""
    surfaces = [e for e in snap.elements if e.role in ("region", "image") and not e.offscreen]
    return max(surfaces, key=lambda e: e.area) if surfaces else None


def _tool(snap, name: str):
    return next((e for e in snap.elements if e.role in ("button", "radio", "listitem")
                 and textmatch.same(e.name, name)), None)


def draw(engine, spec: dict, tools: Optional[dict] = None, on_event: Optional[Callable[[dict], None]] = None) -> dict:
    """Draw the diagram on the target window's canvas and check the labels afterwards."""
    tools = {**TOOLS, **(tools or {})}
    spec = validate(spec)
    boxes = layout(spec)
    started = time.perf_counter()
    actions = 0

    def step(action: str, target: Optional[str] = None, value: Optional[str] = None) -> None:
        nonlocal actions
        snap = engine.see(cv=False)  # indexes change after every action; the tree is enough here
        if target == "canvas":
            element = _canvas(snap)
            if element is None:
                raise LookupError("no drawing surface found in this window")
        elif target is not None:
            element = _tool(snap, target)
            if element is None:
                raise LookupError(f'no "{target}" tool found; name the board\'s tools with --box-tool/--arrow-tool')
        else:
            element = None
        engine.act(action, index=element.index if element is not None else None, value=value,
                   snapshot_id=snap.id, settle=False)
        actions += 1

    for n in spec["nodes"]:
        l, t, r, b = boxes[n["id"]]
        step("click", tools["box"])
        step("drag", "canvas", f"{l},{t} {r},{b}")
        step("key", value="enter")  # edit the new box's text
        time.sleep(0.15)
        step("text", value=n["label"])
        step("key", value="escape")
        step("key", value="escape")  # leave the box unselected
        if on_event:
            on_event({"type": "draw", "what": "box", "label": n["label"]})
    for edge in spec["edges"]:
        (x1, y1), (x2, y2) = arrow_points(boxes[edge["from"]], boxes[edge["to"]])
        step("click", tools["arrow"])
        step("drag", "canvas", f"{x1},{y1} {x2},{y2}")
        step("key", value="escape")
        if on_event:
            on_event({"type": "draw", "what": "arrow", "from": edge["from"], "to": edge["to"]})

    # Read the canvas back: every label should now be visible inside its own box. A label
    # that wrapped onto two lines comes back as two pieces of text, so the pieces found
    # within the box are joined before comparing.
    time.sleep(0.4)
    snap = engine.see()
    canvas = _canvas(snap)
    pieces = [e for root in snap.roots for e in root.walk() if e.source == "cv" and e.name]
    found = []
    for n in spec["nodes"]:
        inside = pieces
        if canvas is not None:
            cl, ct, cr, cb = canvas.box
            l, t, r, b = boxes[n["id"]]
            sl, st = cl + (cr - cl) * l / 1000, ct + (cb - ct) * t / 1000
            sr, sb = cl + (cr - cl) * r / 1000, ct + (cb - ct) * b / 1000
            inside = [e for e in pieces if sl <= e.center[0] <= sr and st <= e.center[1] <= sb]
        joined = " ".join(e.name for e in sorted(inside, key=lambda e: (e.box[1], e.box[0])))
        if textmatch.contains(joined, n["label"], ocr=True) or                 any(textmatch.contains(e.name, n["label"], ocr=True) for e in pieces):
            found.append(n["label"])
    missing = [n["label"] for n in spec["nodes"] if n["label"] not in found]
    return {"title": spec["title"], "boxes": len(spec["nodes"]), "arrows": len(spec["edges"]), "actions": actions,
            "labels_found": found, "labels_missing": missing, "ok": not missing,
            "ms": round((time.perf_counter() - started) * 1000, 1)}
