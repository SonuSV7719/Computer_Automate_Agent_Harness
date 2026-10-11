"""Structured data from any window: tables and repeated items (cards, grid tiles, list rows).

Works on the merged element tree, so the same code reads an HTML table, a desktop data
grid, a details-view list, or a page of product cards. Content scrolled out of view is
read too when the application keeps it in the tree; for content that only appears on
scrolling or on another page, `collect` drives the paging.
"""
from __future__ import annotations

import re
import time
from collections import Counter
from typing import Callable, Optional

from .core import textmatch
from .core.elements import Element, Snapshot

TEXTUAL = {"text", "link", "heading", "cell", "headeritem", "button", "listitem", "image"}
CONTAINERS_TO_SKIP = {"toolbar", "menubar", "menu", "tab", "titlebar", "scrollbar"}
PRICE = re.compile(r"(?:₹|\$|€|£|¥|Rs\.?|INR|USD|EUR|GBP|SGD|S\$|RM|฿|₩|₫)\s?\d[\d,]*(?:\.\d+)?"
                   r"|\d[\d,]*(?:\.\d+)?\s?(?:₹|\$|€|£|USD|INR|EUR)")
RATING = re.compile(r"(\d(?:[.,]\d)?)\s*(?:out of|/)\s*5")
COUNT = re.compile(r"^\(?\s*([\d,]+(?:\.\d+)?[kKmM]?)\s*\)?\s*(?:ratings?|reviews?)?$")


# ---- reading text ------------------------------------------------------------------------
def text_of(e: Element) -> str:
    """What an element shows: its name, else its value, else the text inside it.
    A grid cell's value is its content; its name is often just a label such as "Amount Row 3"."""
    if e.role == "cell" and e.value is not None:
        return "" if e.value.strip().casefold() in NULLS else e.value
    if e.name:
        return e.name
    if e.value:
        return e.value
    inner = [c.name or c.value or "" for c in e.walk() if c is not e and c.role in TEXTUAL | {"input"}]
    return " ".join(t for t in inner if t).strip()


# ---- tables ------------------------------------------------------------------------------
NULLS = {"(null)", "null", "none"}


def normalize_table(table: Element) -> None:
    """Give any grid the same shape: rows, with header items or cells inside.

    Older grids (WinForms DataGridView, MSAA list views) expose rows as generic elements
    named "Row 3" and cells as data items named "Customer Row 3" holding the data in their
    value; HTML tables already arrive as rows and cells."""
    for child in table.children:
        kids = child.children
        if len(kids) < 2:
            continue
        if all(k.role in ("header", "headeritem") for k in kids):
            child.role = "row"
            for k in kids:
                k.role = "headeritem"
        elif child.role in ("custom", "row", "listitem", "group", "pane") and \
                sum(1 for k in kids if k.role in ("row", "cell", "custom", "text", "input")) >= 0.6 * len(kids):
            child.role = "row"
            for k in kids:
                if k.role in ("row", "custom", "text", "input"):
                    k.role = "cell"
    for header in [c for c in table.children if c.role == "header"]:
        for k in header.children:
            k.role = "headeritem"

def _rows_of(table: Element) -> list[Element]:
    rows: list[Element] = []

    def visit(node: Element) -> None:
        for child in node.children:
            if child.role == "row":
                rows.append(child)
            elif child.role != "table":  # a nested table is a table of its own
                visit(child)

    visit(table)
    return rows


def _cells_of(row: Element) -> list[Element]:
    cells: list[Element] = []

    def visit(node: Element) -> None:
        for child in node.children:
            if child.role in ("cell", "headeritem"):
                cells.append(child)
            elif child.role not in ("row", "table"):
                visit(child)

    visit(row)
    if not cells:  # list-style rows: each child that shows text is a column
        cells = [c for c in row.children if text_of(c)]
    return cells


def is_table(e: Element) -> bool:
    if e.role == "table":
        return True
    if e.role == "list":  # a details-view list (Explorer, WinForms ListView in report mode)
        items = [c for c in e.children if c.role in ("listitem", "row")]
        return len(items) >= 2 and sum(1 for c in items if len(_cells_of(c)) >= 2) >= 0.6 * len(items)
    return False


def find_tables(roots: list[Element]) -> list[Element]:
    found = []
    for root in roots:
        for e in root.walk():
            if is_table(e) and not any(is_table(a) for a in _ancestors(e)):
                found.append(e)
    return sorted(found, key=lambda t: -len(_rows_of(t) or t.children))


def read_table(table: Element) -> dict:
    """Columns and rows of a table, including rows scrolled out of view if they are in the tree."""
    rows = _rows_of(table) or [c for c in table.children if c.role in ("listitem", "row")]
    columns: list[str] = []
    header = [h for h in table.walk() if h.role == "headeritem" and not any(r.role == "row" for r in _ancestors(h, table))]
    body = []
    for row in rows:
        cells = _cells_of(row)
        if cells and all(c.role == "headeritem" for c in cells) and not columns:
            columns = [text_of(c) for c in cells]
            continue
        values = [text_of(c) for c in cells]
        if any(values):
            body.append(values)
    if not columns and header:
        columns = [text_of(h) for h in header]
    width = max([len(columns)] + [len(r) for r in body] or [0])
    if not columns:
        columns = [f"column {n + 1}" for n in range(width)]
    elif len(columns) < width:
        columns += [f"column {n + 1}" for n in range(len(columns), width)]
    body = [r + [""] * (width - len(r)) for r in body]
    return {"name": table.title, "columns": columns, "rows": body}


# ---- repeated items (cards, tiles, result rows) --------------------------------------------
def _ancestors(e: Element, stop: Optional[Element] = None):
    node = e.parent
    while node is not None and node is not stop:
        yield node
        node = node.parent


def _shape(item: Element) -> Counter:
    """Which kinds of content an item holds, e.g. 1 image, 1 link, 3 texts, 1 button."""
    shape: Counter = Counter()
    for e in item.walk():
        if e is item:
            continue
        if e.role in ("link", "image", "button", "heading"):
            shape[e.role] += 1
        elif e.role in ("text", "cell") and (e.name or e.value):
            shape["text"] += 1
    return shape


def _similar(a: Counter, b: Counter) -> bool:
    keys = set(a) | set(b)
    if not keys:
        return False
    same = sum(min(a[k], b[k]) for k in keys)
    total = sum(max(a[k], b[k]) for k in keys)
    # Same kinds of content in similar amounts. A wrapper holding two cards has the same
    # kinds as one card but twice as much, and must not pass for a card.
    return same / total >= 0.7 or (set(a) == set(b) and same / total >= 0.6)


def _fields_in(item: Element) -> int:
    return sum(1 for e in item.walk() if e is not item and (e.name or e.value) and e.role in TEXTUAL)


def find_item_groups(roots: list[Element], minimum: int = 3) -> list[tuple[Element, list[Element]]]:
    """Containers whose children repeat one shape: product cards, search results, tiles.

    A group needs at least `minimum` similar children, each showing two or more pieces of
    information, so navigation bars and plain lists of links are not mistaken for items."""
    groups = []
    for root in roots:
        for container in root.walk():
            if container.role in CONTAINERS_TO_SKIP or container.role in ("table", "row") or len(container.children) < minimum:
                continue
            kids = [c for c in container.children if _fields_in(c) >= 2]
            if len(kids) < minimum:
                continue
            shapes = [_shape(c) for c in kids]
            common = Counter(tuple(sorted(s)) for s in shapes).most_common(1)[0][0]
            reference = next(s for s in shapes if tuple(sorted(s)) == common)
            members = [c for c, s in zip(kids, shapes) if _similar(s, reference)]
            if len(members) >= minimum:
                groups.append((container, members))
    # Prefer many rich items; drop a group nested inside a better one with the same items.
    groups.sort(key=lambda g: -(len(g[1]) * min(8, sum(_fields_in(m) for m in g[1]) / len(g[1]))))
    kept: list[tuple[Element, list[Element]]] = []
    for container, members in groups:
        if any(container in list(_ancestors(k[0])) + [k[0]] or k[0] in list(_ancestors(container))
               for k in kept if set(map(id, members)) & set(map(id, k[1]))):
            continue
        kept.append((container, members))
    return kept


def items_in(container: Element, reference: Counter, min_fields: int = 2) -> list[Element]:
    """The children of a known item container that look like its items, however few there
    are (the last page of results may hold only one or two)."""
    if min_fields == 1:
        return list_entries(container)
    return [c for c in container.children if _fields_in(c) >= 2 and _similar(_shape(c), reference)]


def list_entries(container: Element) -> list[Element]:
    """The entries of a plain list or tree: one line of text each."""
    return [c for c in container.children if c.role in ("listitem", "treeitem", "row") and text_of(c)]


def shape_of(members: list[Element]) -> Counter:
    shapes = [_shape(m) for m in members] or [Counter()]
    common = Counter(tuple(sorted(s)) for s in shapes).most_common(1)[0][0]
    return next(s for s in shapes if tuple(sorted(s)) == common)


def read_item(item: Element) -> dict:
    """The fields of one card: title, link, prices, rating, image, and all of its text."""
    texts, links, images, buttons, headings = [], [], [], [], []
    from_tree = any(e.source != "cv" and (e.name or e.value) for e in item.walk() if e is not item)
    for e in item.walk():
        if e is item or (from_tree and e.source == "cv"):
            continue  # OCR of icons and star glyphs adds noise to an item the tree already describes
        label = (e.name or e.value or "").strip()
        if e.role == "link":
            links.append((label, e.value or ""))
            if label:
                texts.append(label)
        elif e.role == "image" and label:
            images.append(label)
        elif e.role == "button" and label:
            buttons.append(label)
        elif e.role == "heading" and label:
            headings.append(label)
            texts.append(label)
        elif e.role in ("text", "cell", "listitem") and label:
            texts.append(label)
        elif label and e.role in ("custom", "group", "pane") and not e.children:
            texts.append(label)  # e.g. a star rating given as a label: "4.2 out of 5 stars"
    seen = set()
    texts = [t for t in texts if _meaningful(t) and not (t in seen or seen.add(t))]
    record: dict = {}
    title = next((h for h in headings), None) or next((name for name, _ in links if len(name) > 3), None)
    if title and title.rstrip().endswith(("...", "…")):
        stem = title.rstrip(" .…").casefold()
        fuller = next((i for i in images if i.casefold().startswith(stem) and len(i) > len(stem)), None)
        title = fuller or title  # sites cut long names; the picture's description often has it whole
    if not title and item.name and not _is_detail(item.name):
        title = item.name  # the card's own accessible name
    if not title:
        plain = [t for t in texts if not _is_detail(t)]
        title = plain[0] if plain else (texts[0] if texts else None)
    if title:
        record["title"] = title
    url = next((u for _, u in links if u.startswith(("http", "/", "file:"))), None)
    if url:
        record["url"] = url
    joined = " ".join(texts + [item.name])
    prices = PRICE.findall(joined)
    if prices:
        record["price"] = prices[0].strip()
        if len(prices) > 1:
            record["other_prices"] = [p.strip() for p in prices[1:]]
    if not texts and item.name:
        texts = [item.name]  # a one-line list entry
        record_title = item.name
    every_name = [item.name] + texts + images + [e.name for e in item.walk() if e.name]
    rating = next((m.group(1) for t in every_name for m in [RATING.search(t)] if m), None)
    if rating:
        record["rating"] = rating.replace(",", ".")
    reviews = next((m.group(1) for t in texts for m in [COUNT.match(t)] if m and not PRICE.search(t)), None)
    if reviews:
        record["reviews"] = reviews
    if images:
        record["image"] = images[0]
    if buttons:
        record["actions"] = buttons
    record["texts"] = texts
    return record


def _is_detail(text: str) -> bool:
    """A price, rating or count rather than a name."""
    return bool(PRICE.search(text) or RATING.search(text) or COUNT.match(text.strip()))


def _meaningful(text: str) -> bool:
    """Real content: a letter or digit, or a run of rating stars. Not stray punctuation."""
    return any(ch.isalnum() for ch in text) or set(text.strip()) <= set("★☆✩✪⭐ ") and "★" in text


def read_items(members: list[Element]) -> list[dict]:
    return [read_item(m) for m in members]


# ---- paging -------------------------------------------------------------------------------
def _key(record) -> tuple:
    if isinstance(record, dict):
        return tuple((k, str(v)) for k, v in sorted(record.items()) if k != "texts") or tuple(record.get("texts", []))
    return tuple(record)


def find_control(snapshot: Snapshot, name: str) -> Optional[Element]:
    """A "next page" control by its name: exact first, then a name that starts with it."""
    wanted = name.strip()
    kinds = ("button", "link", "listitem", "menuitem", "tabitem", "text")
    pool = [e for root in snapshot.roots for e in root.walk() if e.role in kinds and (e.name or e.label)]
    # Real controls first: "next" is often both a list entry and the link inside it, and
    # only the link does anything when clicked.
    rank = {"button": 0, "link": 0, "menuitem": 1, "tabitem": 1, "listitem": 2, "text": 3}
    order = lambda e: (rank.get(e.role, 4), 0 if e.actions else 1)
    exact = sorted((e for e in pool if textmatch.same(e.title, wanted)), key=order)
    if exact:
        return exact[0]
    starts = sorted((e for e in pool if e.title.casefold().startswith(wanted.casefold())), key=order)
    return starts[0] if starts else None


def collect(call: Callable, read: Callable[[Snapshot], Optional[list]], next_name: Optional[str] = None,
            scroll: Optional[Callable[[Snapshot], Optional[int]]] = None, max_pages: int = 50,
            max_scrolls: int = 40, wait: float = 20.0, cv: Optional[bool] = None) -> dict:
    """Read records page after page and/or scroll after scroll, without duplicates.

    `read(snapshot)` returns this page's records (None if the data is gone). `scroll`
    returns the index of the element to scroll, or None to stop. Paging stops when the next
    control is missing or disabled, when a page brings nothing new, or at the limits."""
    records: list = []
    seen: set = set()
    pages = scrolls = 0
    stopped = "end of data"

    def add(batch) -> int:
        fresh = 0
        for record in batch or []:
            key = _key(record)
            if key not in seen:
                seen.add(key)
                records.append(record)
                fresh += 1
        return fresh

    def look(e):
        return e.see(cv)

    snap = call(look)
    first = read(snap)
    if first is None:
        return {"records": [], "pages": 0, "scrolls": 0, "stopped": "nothing to read"}
    add(first)
    pages = 1
    while True:
        if scroll is not None:
            idle = 0
            while scrolls < max_scrolls and idle < 2:
                target = scroll(snap)
                if target is None:
                    break
                before = [_key(r) for r in (read(snap) or [])]
                call(lambda e, t=target, s=snap.id: e.act("scroll", index=t, value="down 5", snapshot_id=s, settle=False))
                scrolls += 1
                snap = _wait_for_change(call, read, before, wait=2.5, look=look)
                if add(read(snap)):
                    idle = 0
                elif call(lambda e, t=scroll(snap): t is None or e.at_bottom(t)):
                    idle += 1  # at the bottom and nothing new arrived
                # otherwise the scroll did not reach the end yet: keep going
        if not next_name or pages >= max_pages:
            stopped = "page limit" if next_name else stopped
            break
        control = find_control(snap, next_name)
        for _ in range(3):  # a page that has just loaded may still be adding its paging links
            if control is not None:
                break
            time.sleep(0.7)
            snap = call(look)
            control = find_control(snap, next_name)
        if control is not None and control.index < 0 and control.enabled:
            # Out of view (below the fold, inside a scrolled panel): scroll to it first.
            call(lambda e, c=control: e.reveal(c))
            snap = call(look)
            control = find_control(snap, next_name)
        if control is None or not control.enabled:
            stopped = f'no enabled "{next_name}" control'
            break
        if control.index < 0:
            stopped = f'the "{next_name}" control could not be brought into view'
            break
        before = [_key(r) for r in (read(snap) or [])]
        clicked = time.perf_counter()
        call(lambda e, i=control.index, s=snap.id: e.act("click", index=i, snapshot_id=s, settle=False))
        snap = _wait_for_change(call, read, before, wait, look=look)
        batch = read(snap)
        if batch is None or [_key(r) for r in batch] == before:
            stopped = (f"the data did not change within {round(time.perf_counter() - clicked, 1)} s of "
                       f'clicking "{next_name}"' + (" (the page was still loading)" if _loading(snap) else ""))
            break
        pages += 1
        if not add(batch):
            stopped = "the next page brought nothing new"
            break
    return {"records": records, "pages": pages, "scrolls": scrolls, "stopped": stopped}


def _loading(snap: Snapshot) -> bool:
    """A browser shows a Stop button in place of Reload while a page is loading."""
    for root in snap.roots:
        for e in root.walk():
            if e.role == "document":
                continue
            if e.role == "button" and e.name.casefold().startswith(("stop", "stop loading")):
                return True
    return False


def _wait_for_change(call, read, before: list, wait: float, look=None) -> Snapshot:
    """Wait until the data differs from `before`, then until it stops changing: pages are
    often filled in gradually, and reading too soon would miss the last items."""
    deadline = time.perf_counter() + wait
    previous = None
    look = look or (lambda e: e.see())
    while True:
        snap = call(look)
        now = [_key(r) for r in (read(snap) or [])]
        # No data at all means the next page is still loading, not that it is empty.
        changed = now != before and bool(now)
        if changed and now == previous and not _loading(snap):
            return snap  # changed, finished loading, and the same on two reads in a row
        if time.perf_counter() >= deadline:
            return snap
        previous = now if changed else None
        time.sleep(0.15)
