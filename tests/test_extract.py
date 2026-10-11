"""Tables and repeated items: every shape the engine meets, plus paging and scrolling."""
from typing import Optional

from shadow.core.elements import Element, WindowInfo
from shadow.engine import Engine
from shadow import extract

WIN = WindowInfo(hwnd=1, title="Data", process="app.exe", pid=1, box=(0, 0, 1000, 800))


def node(role, name="", children=(), **kw):
    e = Element(role=role, name=name, box=kw.pop("box", (0, 0, 100, 20)), root_hwnd=1, **kw)
    for c in children:
        c.parent = e
        e.children.append(c)
    return e


# ---- tables -------------------------------------------------------------------------------
def test_html_style_table_with_header_row():
    table = node("table", "Orders", [
        node("row", children=[node("headeritem", "Order"), node("headeritem", "Amount")]),
        node("row", children=[node("cell", "ORD-1"), node("cell", "10.00")]),
        node("row", children=[node("cell", "ORD-2"), node("cell", "20.00")]),
    ])
    assert extract.read_table(table) == {"name": "Orders", "columns": ["Order", "Amount"],
                                         "rows": [["ORD-1", "10.00"], ["ORD-2", "20.00"]]}


def test_legacy_grid_is_normalised_and_cells_use_their_values():
    """WinForms DataGridView: rows named "Row 0", cells named "Amount Row 0" holding the data."""
    table = node("table", "Orders", [
        node("custom", "Top Row", [node("header", "Order"), node("header", "Amount")]),
        node("custom", "Row 0", [node("row", "Order Row 0", value="ORD-1"), node("row", "Amount Row 0", value="10.00")]),
        node("custom", "Row 1", [node("row", "Order Row 1", value="ORD-2"), node("row", "Amount Row 1", value="(null)")]),
    ])
    extract.normalize_table(table)
    data = extract.read_table(table)
    assert data["columns"] == ["Order", "Amount"]
    assert data["rows"] == [["ORD-1", "10.00"], ["ORD-2", ""]]  # (null) is blank, not the cell's label


def test_details_view_list_is_a_table():
    files = node("list", "Files", [node("listitem", f"report_{n}.pdf", [node("text", f"{n * 12} KB"),
                                                                       node("text", "PDF document")])
                                   for n in range(1, 4)])
    assert extract.is_table(files)
    rows = extract.read_table(files)["rows"]
    assert rows[0] == ["12 KB", "PDF document"] or rows[0][-1] == "PDF document"
    assert len(rows) == 3


def test_a_list_of_plain_links_is_not_a_table_or_items():
    nav = node("list", "Menu", [node("listitem", children=[node("link", name)]) for name in ("Home", "Shop", "Help")])
    assert not extract.is_table(nav)
    assert extract.find_item_groups([node("window", "W", [nav])]) == []


def test_rows_out_of_view_are_still_read():
    table = node("table", "T", [node("row", children=[node("cell", f"r{n}"), node("cell", "x")], offscreen=n > 1)
                                for n in range(5)])
    assert len(extract.read_table(table)["rows"]) == 5


# ---- items --------------------------------------------------------------------------------
def card(name, price, rating, url):
    return node("group", name, [
        node("image", f"{name} photo"), node("link", name, value=url),
        node("custom", f"{rating} out of 5 stars"), node("text", "(120)"),
        node("text", price), node("text", "M.R.P. ₹9,999"), node("button", "Add to cart"),
    ])


def test_product_cards_become_records():
    grid = node("group", "Results", [card(f"Item {n}", f"₹{n},499", f"4.{n}", f"https://shop/p/{n}") for n in range(4)])
    window = node("window", "Shop", [node("heading", "Results"), grid])
    groups = extract.find_item_groups([window])
    assert groups and groups[0][0] is grid and len(groups[0][1]) == 4
    first = extract.read_item(groups[0][1][1])
    assert first["title"] == "Item 1" and first["url"] == "https://shop/p/1"
    assert first["price"] == "₹1,499" and first["other_prices"] == ["₹9,999"]
    assert first["rating"] == "4.1" and first["reviews"] == "120"
    assert first["image"] == "Item 1 photo" and first["actions"] == ["Add to cart"]


def test_a_card_without_a_link_takes_its_own_name_not_its_rating():
    panel = node("pane", "Smart Watch", [node("text", "Rs. 5,049"), node("text", "4.5 out of 5 stars"),
                                          node("button", "Add to cart")])
    record = extract.read_item(panel)
    assert record["title"] == "Smart Watch" and record["price"] == "Rs. 5,049" and record["rating"] == "4.5"


def test_a_wrapper_around_two_cards_is_not_mistaken_for_one_card():
    cards = [card("A", "₹1", "4.0", "u1"), card("B", "₹2", "4.1", "u2")]
    inner = node("pane", "Products", cards)
    outer = node("pane", "Products", [inner, node("button", "Next page")])
    reference = extract.shape_of([card("C", "₹3", "4.2", "u3")])
    assert extract.items_in(inner, reference) == cards
    assert not extract._similar(extract._shape(inner), reference)
    assert extract.items_in(outer, reference) == []


def test_noise_from_icons_is_left_out():
    item = node("group", "Lamp", [node("link", "Lamp", value="u"), node("text", "：("),
                                   node("text", "★★★★"), node("text", "garbled", source="cv")])
    assert extract.read_item(item)["texts"] == ["Lamp", "★★★★"]


# ---- paging and scrolling -------------------------------------------------------------------
class PagedDriver:
    """A window with a 23-row table (10 a page, Next button) and a feed that grows on scroll."""

    def __init__(self):
        self.page, self.feed = 0, 12
        self.clicks = 0

    def list_windows(self):
        return [self.window_info(1)]

    def foreground(self):
        return 1

    def exists(self, hwnd):
        return True

    def window_info(self, hwnd):
        return WIN

    def tree(self, hwnd):
        rows = [node("row", children=[node("headeritem", "Order"), node("headeritem", "Amount")])]
        rows += [node("row", children=[node("cell", f"ORD-{n}"), node("cell", f"{n}.00")])
                 for n in range(self.page * 10, min(23, self.page * 10 + 10))]
        last = self.page == 2
        feed = node("list", "Feed", [node("listitem", f"Event {n}") for n in range(self.feed)],
                    actions=["scroll"], patterns={"scroll"}, box=(0, 400, 500, 600))
        root = node("window", "Data", [node("table", "Orders", rows, box=(0, 0, 500, 300)),
                                       node("button", "Next", actions=[] if last else ["click"], enabled=not last,
                                            handle="next", box=(0, 320, 80, 340)),
                                       feed], box=(0, 0, 1000, 800))
        return [root]

    def perform(self, element, action, value=None):
        if action == "click" and element.handle == "next":
            self.page += 1
            self.clicks += 1
        elif action == "scroll":
            self.feed = min(40, self.feed + 12)
        return "fake"

    def scroll_percent(self, element):
        return 100.0 if self.feed >= 40 else 50.0


def test_paging_reads_every_row_once_and_stops_at_the_last_page():
    driver = PagedDriver()
    engine = Engine(driver, cv=False)
    data = engine.extract_table(next="Next")
    assert data["columns"] == ["Order", "Amount"]
    assert [r[0] for r in data["rows"]] == [f"ORD-{n}" for n in range(23)]
    assert data["pages"] == 3 and driver.clicks == 2
    assert data["stopped"] == 'no enabled "Next" control'


def test_scrolling_reads_a_growing_list_until_the_end():
    driver = PagedDriver()
    engine = Engine(driver, cv=False)
    snap = engine.see()
    feed = next(e.index for e in snap.elements if e.name == "Feed")
    data = engine.extract_items(feed, scroll=True)
    assert data["count"] == 40 and data["items"][-1]["title"] == "Event 39"
    assert data["scrolls"] >= 3


def test_extraction_is_recorded_and_replays_as_code(tmp_path):
    from shadow.core.skill import compile_trace, render_code
    from shadow.runner import run_skill
    driver = PagedDriver()
    engine = Engine(driver, cv=False)
    engine.record_start()
    engine.extract_table(next="Next")
    skill = compile_trace(engine.record_stop(), "read orders")
    code = render_code(skill)
    assert 'h.extract_table(el("el_orders"), options=\'{"next": "Next", "scroll": false}\')' in code \
        or "h.extract_table(" in code
    driver.page = 0
    result = run_skill(skill, code, lambda fn: fn(engine))
    assert result["ok"] and result["llm_calls"] == 0
    assert len(result["data"]) == 1 and len(result["data"][0]["rows"]) == 23
