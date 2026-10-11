"""Diagram drawing on a whiteboard, and human actions (drag and drop, hover, modifier clicks)."""
import pytest

from shadow import draw
from shadow.core.elements import Element, WindowInfo
from shadow.core.skill import compile_trace, render_code
from shadow.engine import Engine
from shadow.runner import run_skill

WIN = WindowInfo(hwnd=1, title="Whiteboard", process="chrome.exe", pid=1, box=(0, 0, 1000, 800))


def node(role, name="", children=(), **kw):
    e = Element(role=role, name=name, box=kw.pop("box", (0, 0, 100, 20)), root_hwnd=1, **kw)
    for c in children:
        c.parent = e
        e.children.append(c)
    return e


class Board:
    """A whiteboard: toolbar buttons and a canvas. Typed labels appear on the canvas as
    pixel-read text, like OCR would report them."""

    def __init__(self):
        self.log: list[tuple] = []
        self.labels: list[str] = []
        self.pending = False

    def list_windows(self):
        return [WIN]

    def foreground(self):
        return 1

    def exists(self, hwnd):
        return True

    def window_info(self, hwnd):
        return WIN

    def tree(self, hwnd):
        canvas = node("image", box=(0, 80, 560, 800), actions=["click"])  # side panel to its right
        for n, label in enumerate(self.labels):
            canvas.children.append(node("text", label, source="cv", confidence=0.95, box=(100 + n, 300, 200 + n, 320)))
        tools = [node("button", name, actions=["click"], box=(300 + 60 * i, 10, 350 + 60 * i, 40))
                 for i, name in enumerate(["Selection", "Rectangle", "Arrow"])]
        cards = [node("button", "Card A", actions=["click"], handle="a", box=(20, 100, 120, 140)),
                 node("button", "Done column", actions=["click"], handle="done", box=(600, 100, 760, 400))]
        return [node("window", "Whiteboard", [node("document", "Board", [*tools, canvas, *cards],
                                                     box=(0, 0, 1000, 800))], box=(0, 0, 1000, 800))]

    def perform(self, element, action, value=None):
        self.log.append((action, element.name or element.role, value))
        if action == "drag" and element.role == "image":
            self.pending = True
        return "fake"

    def drag_between(self, source, target):
        self.log.append(("drag_to", source.name, target.name))
        return "fake"

    def press(self, combo, hwnd=0):
        self.log.append(("key", None, combo))

    def type_text(self, text, hwnd=0):
        self.log.append(("text", None, text))
        if self.pending:
            self.labels.append(text)
            self.pending = False


SPEC = {"title": "Shop", "nodes": [
    {"id": "web", "label": "Web App", "col": 0, "row": 0},
    {"id": "api", "label": "API", "col": 1, "row": 0},
    {"id": "db", "label": "Orders DB", "col": 1, "row": 1}],
    "edges": [{"from": "web", "to": "api"}, {"from": "api", "to": "db"}, {"from": "api", "to": "nowhere"}]}


# ---- planning and layout ------------------------------------------------------------------
def test_layout_keeps_boxes_apart_and_inside_the_open_canvas():
    boxes = draw.layout(draw.validate(dict(SPEC)))
    left, top, right, bottom = draw.DRAW_AREA
    for l, t, r, b in boxes.values():
        assert left <= l < r <= right and top <= t < b <= bottom
    ordered = sorted(boxes.values())
    for a, b in zip(ordered, ordered[1:]):
        assert a[2] <= b[0] or a[3] <= b[1] or b[3] <= a[1]  # no overlap


def test_arrows_leave_and_enter_the_facing_sides():
    left_box, right_box, low_box = (100, 100, 200, 160), (400, 100, 500, 160), (100, 400, 200, 460)
    (x1, y1), (x2, y2) = draw.arrow_points(left_box, right_box)
    assert x1 > 200 and x2 < 400 and y1 == y2 == 130
    (x1, y1), (x2, y2) = draw.arrow_points(left_box, low_box)
    assert y1 > 160 and y2 < 400 and x1 == x2 == 150


def test_bad_plans_are_rejected_or_cleaned():
    with pytest.raises(ValueError, match="share an id"):
        draw.validate({"nodes": [{"id": "a", "label": "A", "col": 0, "row": 0},
                                 {"id": "a", "label": "B", "col": 1, "row": 0}], "edges": []})
    with pytest.raises(ValueError, match="on top of another box"):
        draw.validate({"nodes": [{"id": "a", "label": "A", "col": 0, "row": 0},
                                 {"id": "b", "label": "B", "col": 0, "row": 0}], "edges": []})
    cleaned = draw.validate(dict(SPEC))
    assert [e["to"] for e in cleaned["edges"]] == ["api", "db"]  # the arrow to an unknown box is dropped


def test_gemini_plans_the_diagram_from_a_description():
    class Planner:
        def json(self, system, user, schema, files=None):
            assert "boxes with short labels" in system and "ride sharing" in user
            return SPEC

    assert len(draw.plan_diagram(Planner(), "ride sharing app")["nodes"]) == 3


# ---- drawing --------------------------------------------------------------------------------
def test_a_diagram_is_drawn_with_the_boards_own_tools_and_checked():
    board = Board()
    engine = Engine(board, cv=False)
    result = draw.draw(engine, SPEC)
    assert result["ok"] and result["boxes"] == 3 and result["arrows"] == 2
    assert result["labels_found"] == ["Web App", "API", "Orders DB"]
    boxes = draw.layout(draw.validate(dict(SPEC)))
    l, t, r, b = boxes["web"]
    assert board.log[:6] == [("click", "Rectangle", None), ("drag", "image", f"{l},{t} {r},{b}"),
                             ("key", None, "enter"), ("text", None, "Web App"),
                             ("key", None, "escape"), ("key", None, "escape")]
    arrows = [entry for entry in board.log if entry[0] == "drag" and board.log[board.log.index(entry) - 1][1] == "Arrow"]
    assert len(arrows) == 2


def test_a_board_without_the_named_tool_is_reported():
    with pytest.raises(LookupError, match='no "Box" tool'):
        draw.draw(Engine(Board(), cv=False), SPEC, tools={"box": "Box"})


# ---- human actions --------------------------------------------------------------------------
def test_drag_and_drop_is_recorded_by_fingerprints_and_replays():
    board = Board()
    engine = Engine(board, cv=False)
    snap = engine.see()
    card = next(e.index for e in snap.elements if e.name == "Card A")
    column = next(e.index for e in snap.elements if e.name == "Done column")
    engine.record_start()
    engine.act("drag_to", card, str(column), snapshot_id=snap.id, intent="Move the card to Done")
    trace = engine.record_stop()
    assert trace[0]["value"] is None and trace[0]["target"]["name"] == "Done column"
    skill = compile_trace(trace, "move card")
    code = render_code(skill)
    assert 'h.drag_to(el("btn_card_a"), el("btn_done_column"))' in code
    board.log.clear()
    result = run_skill(skill, code, lambda fn: fn(engine))
    assert result["ok"] and board.log == [("drag_to", "Card A", "Done column")]


def test_drag_to_needs_a_target_index():
    engine = Engine(Board(), cv=False)
    snap = engine.see()
    with pytest.raises(ValueError, match="index of the element to drop onto"):
        engine.act("drag_to", snap.elements[0].index, "the done column", snapshot_id=snap.id)


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows driver")
def test_modifier_names_are_recognised():
    from shadow.drivers.windows import _held
    assert _held("ctrl") == "ctrl" and _held("Ctrl + Shift") == "ctrl+shift"
    assert _held("hello") == "" and _held(None) == ""
