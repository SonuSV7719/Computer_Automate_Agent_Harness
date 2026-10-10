"""Vision: OCR, incremental reading, and merging pixels into the tree.

The fixtures are real captures of the Tk invoice app, whose widgets have no names
in the UI tree. Tests that need the OCR models are skipped if they are not installed.
"""
from pathlib import Path

import numpy as np
import pytest

from shadow.core.elements import Element, WindowInfo
from shadow.core.serialize import serialize
from shadow.core import textmatch
from shadow.cv.boxes import find_boxes
from shadow.cv.merge import merge, needs_cv
from shadow.cv.ocr import Line
from shadow.cv.perceive import Perceiver, changed_regions, erase_carets

FIXTURES = Path(__file__).parent / "fixtures"
# Image-space boxes of the three entry fields and the status line (from the UI tree).
INVOICE, VENDOR, AMOUNT, STATUS = (248, 69, 548, 104), (248, 126, 548, 161), (248, 184, 548, 219), (40, 339, 680, 372)
KNOWN = [INVOICE, VENDOR, AMOUNT, STATUS]


def load(name: str) -> np.ndarray:
    from PIL import Image
    return np.ascontiguousarray(np.array(Image.open(FIXTURES / f"{name}.png").convert("RGB"))[:, :, ::-1])


@pytest.fixture(scope="session")
def ocr():
    pytest.importorskip("rapidocr_onnxruntime")
    from shadow.cv.ocr import RapidOcr
    return RapidOcr()


def texts(lines) -> set[str]:
    return {textmatch.squash(ln.text).strip("|") for ln in lines}


class CountingOcr:
    """Wraps an OCR engine and counts the pixels it was asked to read."""

    def __init__(self, inner) -> None:
        self.inner, self.calls, self.pixels = inner, 0, 0

    def read(self, image):
        self.calls += 1
        self.pixels += image.shape[0] * image.shape[1]
        return self.inner.read(image)


# ---- no models needed -------------------------------------------------------
def test_look_alike_characters_match_but_real_differences_do_not():
    assert textmatch.same("Saved INV-g", "Saved INV-9", ocr=True)
    assert textmatch.same("Vendorname", "Vendor name")
    assert textmatch.contains("status: SavedINV-1O42 ok", "Saved INV-1042", ocr=True)
    assert not textmatch.same("INV-8", "INV-9", ocr=True)
    assert not textmatch.same("Saved INV-g", "Saved INV-9")  # tree text is exact


def test_changed_regions_finds_only_what_changed():
    a = np.full((200, 400, 3), 255, np.uint8)
    b = a.copy()
    b[50:60, 100:140] = 0
    b[150:160, 300:320] = 0
    assert changed_regions(a, a) == []
    regions = changed_regions(a, b)
    assert len(regions) == 2
    assert all(l <= x and r >= x2 for (l, t, r, bb), (x, x2) in zip(sorted(regions), [(100, 140), (300, 320)]))


def test_unchanged_frame_is_never_read_again():
    class Fake:
        calls = 0

        def read(self, image):
            self.calls += 1
            return [Line("hello", (10, 10, 60, 30), 0.9)]

    fake = Fake()
    p = Perceiver(fake)
    frame = np.full((100, 200, 3), 255, np.uint8)
    frame[12:28, 12:58] = 0
    assert [ln.text for ln in p.lines("w", frame)] == ["hello"]
    assert [ln.text for ln in p.lines("w", frame.copy())] == ["hello"]
    assert fake.calls == 1 and p.last["mode"] == "cached"


def test_caret_is_erased_only_inside_known_single_line_boxes():
    frame = load("tk_caret")
    cleaned = erase_carets(frame, KNOWN)
    changed = np.argwhere((frame != cleaned).any(axis=2))
    assert len(changed) > 0
    l, t, r, b = AMOUNT
    assert changed[:, 0].min() >= t and changed[:, 0].max() <= b  # only the focused amount field
    assert changed[:, 1].max() - changed[:, 1].min() <= 3  # a stroke at most 3 px wide
    assert erase_carets(frame, []) is frame


def test_tree_without_names_needs_vision_and_a_named_tree_does_not(engine):
    assert not needs_cv(engine.driver.tree(1001))
    root = Element(role="window", name="Legacy", box=(0, 0, 400, 300))
    root.children.append(Element(role="button", box=(10, 40, 90, 70), actions=["click"], parent=root))
    assert needs_cv([root])
    assert needs_cv([Element(role="window", name="Viewer", box=(0, 0, 400, 300))])  # only a title bar


def test_pixel_boxes_need_all_four_sides():
    import cv2
    canvas = np.full((120, 300, 3), 217, np.uint8)
    cv2.rectangle(canvas, (20, 20), (140, 50), (255, 255, 255), -1)
    cv2.rectangle(canvas, (20, 20), (140, 50), (0, 0, 0), 1)  # a bordered input
    cv2.line(canvas, (180, 70), (180, 95), (0, 0, 0), 1)  # two separator bars, no top or bottom
    cv2.line(canvas, (230, 70), (230, 95), (0, 0, 0), 1)
    boxes = find_boxes(canvas)
    assert len(boxes) == 1
    l, t, r, b = boxes[0]
    assert abs(l - 20) <= 3 and abs(r - 140) <= 3


# ---- with the OCR models ------------------------------------------------------
def test_full_read_finds_labels_buttons_and_rows(ocr):
    found = texts(ocr.read(load("tk_three_rows")))
    for expected in ("invoiceno", "vendorname", "amount", "save", "clear", "savedinv-203",
                     "inv-201", "inv-202", "inv-203", "contososupplies"):
        assert expected in found, f"{expected} missing from {sorted(found)}"


def test_typed_value_next_to_the_caret_is_read_correctly(ocr):
    """Without caret erasing and enlargement this field reads as '10d'."""
    p = Perceiver(ocr)
    lines = p.lines("w", load("tk_caret"), KNOWN)
    l, t, r, b = AMOUNT
    value = [ln.text for ln in lines if l <= (ln.box[0] + ln.box[2]) / 2 <= r and t <= (ln.box[1] + ln.box[3]) / 2 <= b]
    assert value == ["100"]


def test_incremental_read_matches_a_full_read_and_reads_far_fewer_pixels(ocr):
    counting = CountingOcr(ocr)
    p = Perceiver(counting)
    empty, rows = load("tk_empty"), load("tk_three_rows")
    p.lines("w", empty, KNOWN)
    full_pixels = empty.shape[0] * empty.shape[1]
    counting.pixels = 0
    lines = p.lines("w", rows, KNOWN)
    assert p.last["mode"] == "partial"
    assert counting.pixels < 0.5 * full_pixels
    # OCR may split one row into several pieces or keep it whole, so compare content:
    # every piece of text a full read finds must appear somewhere in the incremental read.
    everything = textmatch.squash(" ".join(ln.text for ln in lines))
    for piece in texts(ocr.read(erase_carets(rows, KNOWN))) - {"口", "×", ""}:
        assert piece in everything, f"{piece!r} was lost by the incremental read"
    for piece in ("inv-201", "inv-202", "inv-203", "savedinv-203", "300"):
        assert piece in everything


def test_pixels_alone_become_labelled_inputs_and_buttons(ocr):
    """A remote-desktop style window: one pane, no tree inside it."""
    image = load("tk_three_rows")
    h, w = image.shape[:2]
    window = WindowInfo(hwnd=1, title="Remote Desktop", process="mstsc.exe", pid=1, box=(0, 0, w, h))
    root = Element(role="window", name="Remote Desktop", box=(0, 0, w, h), root_hwnd=1)
    view = Element(role="pane", box=(8, 34, w - 8, h - 8), parent=root, root_hwnd=1)
    root.children.append(view)

    stats = merge([root], window.box, image, ocr.read(image))
    _, body, elements = serialize([root], window, 1)
    assert stats["holes"] == 1 and stats["inputs"] == 3
    assert "region (no tree, read by CV, " in body
    inputs = [e for e in elements if e.role == "input"]
    assert [textmatch.squash(e.title) for e in inputs] == ["invoiceno", "vendorname", "amount"]
    assert all(e.source == "cv" and e.actions == ["click", "type"] for e in inputs)
    buttons = {e.name for e in elements if e.role == "button"}
    assert buttons == {"Save", "Clear"}, body  # and no false button around table text
    assert any(e.role == "text" and textmatch.same(e.name, "Saved INV-203", ocr=True) for e in elements)
    # every vision element can be clicked at a point inside the pane
    assert all(view.box[0] <= e.center[0] <= view.box[2] for e in elements if e.source == "cv")


def test_tree_boxes_are_named_by_ocr_and_tree_text_is_not_duplicated(ocr):
    """The tree knows where the controls are but not what they are called."""
    image = load("tk_empty")
    h, w = image.shape[:2]
    window = WindowInfo(hwnd=1, title="Legacy Invoice Entry", process="pythonw.exe", pid=1, box=(0, 0, w, h))
    root = Element(role="window", name="Legacy Invoice Entry", box=(0, 0, w, h), root_hwnd=1)

    def child(role, box, **kw):
        e = Element(role=role, box=box, parent=root, root_hwnd=1, **kw)
        root.children.append(e)
        return e

    save = child("button", (248, 284, 386, 324), actions=["click"])
    entry = child("pane", INVOICE)
    label = child("image", (40, 71, 136, 104))
    named = child("button", (398, 284, 536, 324), name="Clear", actions=["click"])

    merge([root], window.box, image, ocr.read(image))
    assert save.name == "Save" and save.source == "merged"
    assert entry.role == "input" and entry.source == "merged" and entry.actions == ["click", "type"]
    assert label.role == "text" and textmatch.same(label.name, "Invoice No")
    assert named.source == "tree" and named.cv_confirmed  # OCR agreed with the tree; no duplicate added
    _, body, _ = serialize([root], window, 1)
    assert body.count('"Clear"') == 1
    assert 'input "Invoice No"' in body.replace("InvoiceNo", "Invoice No")


# ---- browsers and canvases ----------------------------------------------------
def _page_with(*extra):
    """A browser-like tree: a full-page backdrop pane behind a document with named controls."""
    root = Element(role="window", name="Page - Browser", box=(0, 0, 900, 700), root_hwnd=1)
    backdrop = Element(role="pane", box=(0, 80, 900, 700), parent=root, root_hwnd=1)
    doc = Element(role="document", name="Page", box=(0, 80, 900, 700), parent=root, root_hwnd=1)
    doc.children = [
        Element(role="input", name="Invoice No", box=(150, 100, 400, 130), actions=["type"], value="", parent=doc),
        Element(role="button", name="Save", box=(150, 150, 230, 185), actions=["click"], parent=doc),
        *extra,
    ]
    for child in extra:
        child.parent = doc
    root.children = [backdrop, doc]
    return root, backdrop, doc


def test_a_fully_described_web_page_needs_no_vision():
    from shadow.cv.merge import cv_areas
    root, _backdrop, _doc = _page_with()
    assert not needs_cv([root])  # the empty pane behind the page is a backdrop, not a hole
    assert cv_areas([root], root.box) == []


def test_a_canvas_becomes_a_region_and_only_it_is_read():
    import cv2

    from shadow.cv.merge import cv_areas
    canvas = Element(role="image", box=(100, 300, 740, 560), actions=["click"], root_hwnd=1)
    root, _backdrop, doc = _page_with(canvas)
    assert needs_cv([root])
    assert cv_areas([root], root.box) == [(96, 296, 744, 564)]

    image = np.full((700, 900, 3), 244, np.uint8)
    image[300:560, 100:740] = 255  # the white canvas
    cv2.rectangle(image, (116, 314), (226, 348), (0, 0, 0), 1)  # a "Rectangle" tool drawn on it
    lines = [Line("Rectangle", (132, 322, 210, 340), 0.99), Line("Draw here", (360, 322, 440, 340), 0.98)]
    stats = merge([root], root.box, image, lines)
    window = WindowInfo(hwnd=1, title="Page - Browser", process="chrome.exe", pid=1, box=root.box)
    _, body, elements = serialize([root], window, 1)

    assert stats["holes"] == 1
    assert canvas.role == "region" and canvas.actions == ["click_at", "drag"] and canvas.index >= 0
    tool = next(e for e in elements if e.name == "Rectangle")
    assert tool.role == "button" and tool.source == "cv"  # a box on a white canvas is not an input
    assert "region (no tree, read by CV, 640x260 px)  actions: click_at, drag" in body
    import re
    at = re.search(r'button "Rectangle" \(at (\d+),(\d+), cv 0.99\)  actions: click', body)
    assert at and abs(int(at.group(1)) - 111) <= 5 and abs(int(at.group(2)) - 119) <= 8, body
    assert body.count('"Save"') == 1  # the page's own button is untouched


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows driver")
def test_positions_inside_an_element_use_a_0_to_1000_grid():
    from shadow.drivers.windows import WindowsDriver
    region = Element(role="region", box=(100, 200, 500, 400))
    assert WindowsDriver._points(region, "0,0 1000,1000 500,500") == [(100, 200), (500, 400), (300, 300)]
    with pytest.raises(ValueError):
        WindowsDriver._points(region, "1200,10")
    with pytest.raises(ValueError):
        WindowsDriver._points(region, "50")


def test_stacked_canvases_with_a_floating_toolbar_are_one_region():
    """Excalidraw's layout: two full-size canvases on top of each other, toolbar buttons over them."""
    root = Element(role="window", name="Whiteboard - Browser", box=(0, 0, 1000, 800), root_hwnd=1)
    doc = Element(role="document", name="Whiteboard", box=(0, 80, 1000, 800), parent=root, root_hwnd=1)
    static = Element(role="image", box=(0, 80, 1000, 800), actions=["click"], parent=doc, root_hwnd=1)
    interactive = Element(role="image", box=(0, 80, 1000, 800), actions=["click"], parent=doc, root_hwnd=1)
    tools = [Element(role="button", name=name, box=(400 + 50 * i, 100, 440 + 50 * i, 140), actions=["click"],
                     parent=doc, root_hwnd=1) for i, name in enumerate(["Selection", "Rectangle", "Text"])]
    doc.children = [static, interactive, *tools]
    root.children = [doc]
    assert needs_cv([root])  # the toolbar floating over the canvas does not make it a backdrop

    image = np.full((800, 1000, 3), 255, np.uint8)
    lines = [Line("Invoice Box", (450, 420, 560, 445), 0.99), Line("Rectangle", (402, 110, 438, 130), 0.9)]
    merge([root], root.box, image, lines)
    window = WindowInfo(hwnd=1, title="Whiteboard - Browser", process="chrome.exe", pid=1, box=root.box)
    _, body, elements = serialize([root], window, 1)

    assert body.count("region (no tree") == 1
    label = next(e for e in elements if e.name == "Invoice Box")
    assert label.source == "cv" and label.parent is interactive and label.center == (505, 432)
    assert 'text "Invoice Box" (at 505,489, cv 0.99)' in body  # with its position inside the region
    assert body.count('"Rectangle"') == 1  # the toolbar caption read by OCR is the tree's own button
