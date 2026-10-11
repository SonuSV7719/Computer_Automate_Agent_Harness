"""The engine: snapshots, actions with post-conditions, and step recording.

Everything above it (CLI, daemon, learn loop, replay) goes through this class.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Optional

from .core import expect as expectation
from .core import fingerprint, textmatch
from .core.diff import diff
from .core.elements import Element, Snapshot, WindowInfo
from .core.serialize import serialize

_DIGITS = re.compile(r"\d+")
SETTLE_TIMEOUT = 1.2
VERIFY_TIMEOUT = 1.5
POLL = 0.08
ELEMENTLESS = {"key", "text"}


def _new_text(before: Snapshot, after: Snapshot) -> list[str]:
    """Text that is on screen after an action and was not there before, status text first."""
    def texts(snap: Snapshot) -> dict[str, str]:
        return {e.name: e.role for e in snap.elements if e.name and e.role in ("text", "listitem", "statusbar")}

    old, new = texts(before), texts(after)
    added = [name for name in new if name not in old]
    return sorted(added, key=lambda name: new[name] != "text")


def _counters(before: Snapshot, after: Snapshot) -> list[str]:
    """New text that is an old text with only its digits changed ("Strokes: 0" -> "Strokes: 1").
    Such text differs on every run, so it is not a dependable sign that a step worked."""
    shapes = {_DIGITS.sub("#", e.name) for e in before.elements if e.name}
    return [name for name in _new_text(before, after) if _DIGITS.sub("#", name) in shapes]


def _check_uncovered(snapshot: Snapshot, region: Element, value: Optional[str], driver=None) -> None:
    """A position inside a region must be on the region itself. Whiteboards float toolbars
    and side panels over their canvas; pressing there would operate the panel, not draw."""
    token = (value or "").replace(";", " ").split()[:1]
    if not token or "," not in token[0]:
        return  # the driver reports malformed positions
    try:
        fx, fy = (float(part) for part in token[0].split(",", 1))
    except ValueError:
        return
    l, t, r, b = region.box
    x, y = l + (r - l) * fx / 1000, t + (b - t) * fy / 1000

    def refuse(role: str, name: str) -> None:
        what = f'{role} "{name}"' if name else role
        raise ValueError(f"position {token[0]} is covered by {what}; choose a point on the open "
                         f"part of the region")

    hit_test = getattr(driver, "element_at", None)
    if hit_test is not None:
        # Ask the application what is under the point right now. The tree can hold stale
        # entries (a text editor that has already closed), so its geometry is not proof.
        hit = hit_test(x, y)
        if hit is None:
            return
        hl, ht, hr, hb = hit["box"]
        area = max(0, hr - hl) * max(0, hb - ht)
        # Anything clearly smaller than the region is something lying on top of it: a
        # button, or the unnamed body of a floating panel.
        if area < 0.9 * region.area:
            refuse(hit["role"], hit["name"])
        return

    ancestors = set()
    node = region.parent
    while node is not None:
        ancestors.add(id(node))
        node = node.parent
    for other in snapshot.elements:
        if other is region or id(other) in ancestors or other.source == "cv" or other.offscreen:
            continue
        ol, ot, o_r, ob = other.box
        if ol <= x <= o_r and ot <= y <= ob and other.area < 0.9 * region.area:
            refuse(other.role, other.title)


class StaleSnapshot(Exception):
    pass


class Halted(Exception):
    """The user pressed the kill switch. Nothing may touch the desktop until resumed."""


class NotAllowed(LookupError):
    """The window belongs to an application outside the allowlist."""


class Engine:
    def __init__(self, driver=None, perceiver=None, cv: bool = True) -> None:
        if driver is None:
            if sys.platform == "win32":
                from .drivers.windows import WindowsDriver
                driver = WindowsDriver()
            else:
                from .drivers.linux import LinuxDriver
                driver = LinuxDriver()
        self.driver = driver
        self.cv_enabled = cv and os.environ.get("SHADOW_CV", "1") != "0"
        self.halted = False
        # SHADOW_ALLOW=erp.exe,chrome.exe limits the agent to those applications.
        self.allow = {name.strip().casefold() for name in os.environ.get("SHADOW_ALLOW", "").split(",")
                      if name.strip()}
        self._perceiver = perceiver
        self.cv_stats: dict = {}
        self.target: Optional[int] = None
        self.snapshot: Optional[Snapshot] = None
        self._counter = 0
        self.trace: list[dict] = []
        self.recording = False

    # ---- windows -------------------------------------------------------
    def windows(self) -> list[WindowInfo]:
        return self.driver.list_windows()

    def halt(self) -> None:
        self.halted = True

    def resume(self) -> None:
        self.halted = False

    def _check_allowed(self, window: WindowInfo) -> WindowInfo:
        if self.allow and window.process.casefold() not in self.allow:
            raise NotAllowed(f"{window.process or 'this application'} is not in the allowlist "
                             f"({', '.join(sorted(self.allow))})")
        return window

    def set_target(self, query: str) -> WindowInfo:
        """Pick the window to work on by title, process name or handle."""
        wanted = query.strip().casefold()
        windows = self.windows()
        match = None
        if wanted.isdigit():
            match = next((w for w in windows if w.hwnd == int(wanted)), None)
        if match is None:
            match = next((w for w in windows if wanted in w.title.casefold()), None)
        if match is None:
            match = next((w for w in windows if wanted in w.process.casefold()), None)
        if match is None:
            raise LookupError(f"no window matches '{query}'")
        self._check_allowed(match)
        self.target = match.hwnd
        self.snapshot = None
        return match

    def set_target_window(self, window: dict) -> WindowInfo:
        """Find the window a skill was recorded on: same process, closest title."""
        process = (window.get("process") or "").casefold()
        pattern = window.get("title") or ""
        same = [w for w in self.windows() if w.process.casefold() == process]
        if not same:
            raise LookupError(f"the window for this skill is not open ({window.get('process')}: {pattern})")
        match = next((w for w in same if _DIGITS.sub("#", w.title) == pattern), same[0])
        self._check_allowed(match)
        self.target = match.hwnd
        self.snapshot = None
        return match

    def _hwnd(self) -> int:
        if self.target is not None and self.driver.exists(self.target):
            return self.target
        hwnd = self.driver.foreground()
        if not hwnd:
            raise LookupError("no target window; use `shadow target <title>`")
        return hwnd

    # ---- perception ----------------------------------------------------
    def _merge_cv(self, hwnd: int, info: WindowInfo, roots: list[Element]) -> None:
        """Fill what the tree cannot describe from the pixels."""
        import numpy as np

        from .cv.merge import cv_areas, known_boxes, merge
        from .cv.perceive import Perceiver

        if self._perceiver is None:
            self._perceiver = Perceiver()
        started = time.perf_counter()
        grab = getattr(self.driver, "capture_window", None)
        shot = grab(hwnd, info.box) if grab else self.driver.capture(info.box)
        image = np.ascontiguousarray(np.asarray(shot)[:, :, ::-1])
        areas = cv_areas(roots, info.box)
        reading = image
        if areas is not None:  # blank out everything the tree already describes
            reading = np.full_like(image, 255)
            for l, t, r, b in areas:
                reading[t:b, l:r] = image[t:b, l:r]
        lines = self._perceiver.lines(hwnd, reading, known_boxes(roots, info.box))
        self.cv_stats = merge(roots, info.box, image, lines)
        self.cv_stats.update(self._perceiver.last, ms=round((time.perf_counter() - started) * 1000, 1))

    def warm_up(self) -> None:
        """Load the OCR models ahead of the first read (about two seconds)."""
        if self.cv_enabled:
            import numpy as np

            from .cv.perceive import Perceiver
            if self._perceiver is None:
                self._perceiver = Perceiver()
            self._perceiver.ocr.read(np.full((48, 160, 3), 255, np.uint8))

    def see(self, cv: Optional[bool] = None) -> Snapshot:
        """Read the target window. cv=None runs vision only when the tree needs it;
        cv=False skips it, which replay uses for skills that never touched a CV element."""
        started = time.perf_counter()
        hwnd = self._hwnd()
        info = self._check_allowed(self.driver.window_info(hwnd))
        roots = self.driver.tree(hwnd)
        if not roots:
            raise LookupError(f"window '{info.title}' has no readable UI tree")
        self.cv_stats = {}
        if cv is not False and self.cv_enabled:
            from .cv.merge import needs_cv
            if needs_cv(roots):
                self._merge_cv(hwnd, info, roots)
        self._counter += 1
        ms = (time.perf_counter() - started) * 1000
        snap = Snapshot(id=self._counter, window=info, roots=roots, ms=ms)
        snap.header, snap.body, snap.elements = serialize(roots, info, snap.id, ms)
        self.snapshot = snap
        return snap

    def _settle(self, spec: Optional[str], timeout: float,
                cv: Optional[bool] = None) -> tuple[Snapshot, Optional[bool]]:
        """Wait for the expected result, or for the screen to stop changing."""
        deadline = time.perf_counter() + (timeout if spec else SETTLE_TIMEOUT)
        previous_body = None
        reads = 0
        while True:
            snap = self.see(cv)
            reads += 1
            if spec:
                if expectation.check(spec, snap):
                    return snap, True
            elif snap.body == previous_body:
                return snap, None
            # A slow read (vision on a large canvas) can use up the whole timeout while showing
            # the screen as it was just after the action. Never give up on a single read.
            if time.perf_counter() >= deadline and (reads >= 2 or not spec):
                return snap, (False if spec else None)
            previous_body = snap.body
            time.sleep(POLL)

    # ---- actions -------------------------------------------------------
    def act(self, action: str, index: Optional[int] = None, value: Optional[str] = None,
            snapshot_id: Optional[int] = None, expect: Optional[str] = None,
            timeout: float = 3.0, intent: str = "", settle: bool = True,
            cv: Optional[bool] = None) -> dict:
        """Perform one action. With settle=False and no expectation the call returns
        straight after the action, which is what replay uses between checkpoints."""
        if self.halted:
            raise Halted("stopped by the user; run `shadow resume` to continue")
        if expect:
            expectation.parse(expect)  # fail before touching the UI
        started = time.perf_counter()
        before = self.snapshot
        element: Optional[Element] = None
        fp = None

        if action in ELEMENTLESS:
            if before is None:
                before = self.see()
            hwnd = before.window.hwnd
            if action == "key":
                self.driver.press(value or "", hwnd)
            else:
                self.driver.type_text(value or "", hwnd)
            route = "keys"
        else:
            if before is None:
                raise StaleSnapshot("no snapshot yet; run `shadow see` first")
            if snapshot_id is not None and snapshot_id != before.id:
                raise StaleSnapshot(f"snapshot {snapshot_id} is stale; the latest is {before.id}")
            if index is None:
                raise ValueError(f"action '{action}' needs an element index")
            element = before.get(index)
            fp = fingerprint.make(element, before)
            if action in ("click_at", "drag"):
                _check_uncovered(before, element, value, self.driver)
            route = self.driver.perform(element, action, value)
            if route != "uia.parts":  # a date set part by part is shown in the field's own format
                try:
                    self._verify_value(element, action, value)
                except RuntimeError:
                    retype = getattr(self.driver, "type_with_keys", None)
                    if action != "type" or retype is None or route == "keys":
                        raise
                    # The direct route did not stick (common in older controls): type it for real.
                    route = retype(element, value or "")
                    self._verify_value(element, action, value, typed=True)

        if expect or settle:
            after, met = self._settle(expect, timeout, cv)
        else:
            after, met = None, None
        ms = (time.perf_counter() - started) * 1000
        ok = met is not False
        secret = element is not None and element.masked
        step = {
            # A password is never written to a trace or a skill; it is asked for at run time.
            "action": action, "value": "" if secret else value, "fingerprint": fp, "expect": expect,
            "intent": intent, "route": route, "ok": ok, "ms": round(ms, 1),
        }
        if self.recording and ok:
            self.trace.append(step)
        return {
            "ok": ok,
            "step": step,
            "new_text": _new_text(before, after) if after else [],
            "counter_text": _counters(before, after) if after else [],
            "route": route,
            "ms": round(ms, 1),
            "snapshot": (after or before).id,
            "expect": {"spec": expect, "met": met} if expect else None,
            "changes": diff(before, after) if after else [],
            "element": element.to_dict() if element else None,
        }

    def _verify_value(self, element: Element, action: str, value: Optional[str], typed: bool = False) -> None:
        """A typed or selected value must actually be in the field afterwards.

        `typed` means real keystrokes were used: then a field that reports nothing at all
        (a composite such as a number box) cannot be checked, which is not a failure; its
        inner text box is checked instead when it has one."""
        read = getattr(self.driver, "read_value", None)
        if read is None or action not in ("type", "select") or element.masked or element.source != "tree" \
                or element.role not in ("input", "document", "dropdown", "spinner"):
            return
        want = textmatch.squash(value or "")
        deadline = time.perf_counter() + VERIFY_TIMEOUT
        while True:  # browsers update their accessibility values a moment after the input changes
            shown = read(element)
            if shown is None or textmatch.squash(shown) == want:
                return
            if typed and shown == "":
                inner = [read(c) for c in element.walk() if c is not element and c.role in ("input", "spinner")]
                if any(v is not None and textmatch.squash(v) == want for v in inner) or \
                        all(v in (None, "") for v in inner):
                    return
            if time.perf_counter() >= deadline:
                raise RuntimeError(f'the field shows "{shown}" after {action} "{value}"')
            time.sleep(0.05)

    # ---- structured data ----------------------------------------------
    def _target(self, index: Optional[int], snapshot_id: Optional[int]) -> tuple[Snapshot, Optional[Element]]:
        snap = self.snapshot or self.see()
        if snapshot_id is not None and snapshot_id != snap.id:
            raise StaleSnapshot(f"snapshot {snapshot_id} is stale; the latest is {snap.id}")
        return snap, (snap.get(index) if index is not None else None)

    def at_bottom(self, index: int) -> bool:
        """True when the element is scrolled all the way down (or cannot say otherwise)."""
        snap = self.snapshot or self.see()
        try:
            element = snap.get(index)
        except KeyError:
            return True
        percent = getattr(self.driver, "scroll_percent", lambda e: None)(element)
        return percent is None or percent >= 99.5

    def reveal(self, element: Element) -> bool:
        """Bring an element that is out of view into view, so it can be acted on."""
        scroll = getattr(self.driver, "scroll_into_view", None)
        if scroll is None or not scroll(element):
            return False
        time.sleep(0.15)
        return True

    def _scroll_target(self, snap: Snapshot, element: Optional[Element]) -> Optional[int]:
        """The element to scroll to bring more of `element` into view: itself, or the
        nearest scrollable ancestor; for a whole page, the scrollable document."""
        node = element
        while node is not None:
            if "scroll" in node.patterns and node.index >= 0:
                return node.index
            node = node.parent
        return next((e.index for e in snap.elements if "scroll" in e.patterns and e.role == "document"), None)

    def _unrecorded(self, work):
        """Run `work` without recording the clicks and scrolls it makes: an extraction is
        recorded as one step that pages by itself when replayed."""
        recording, self.recording = self.recording, False
        try:
            return work()
        finally:
            self.recording = recording

    def _record(self, action: str, element: Optional[Element], snap: Snapshot, options: dict) -> None:
        if self.recording:
            self.trace.append({"action": action, "value": json.dumps(options) if options else None,
                               "fingerprint": fingerprint.make(element, snap) if element is not None else None,
                               "expect": None, "intent": options.pop("intent", "") if options else "",
                               "route": "read", "ok": True, "ms": 0})

    def extract_table(self, index: Optional[int] = None, snapshot_id: Optional[int] = None,
                      next: Optional[str] = None, scroll: bool = False, max_pages: int = 50,
                      fp: Optional[dict] = None) -> dict:
        """All rows of a table, following "next page" and/or scrolling until the data ends."""
        from . import extract
        if self.halted:
            raise Halted("stopped by the user; run `shadow resume` to continue")
        snap, table = self._target(index, snapshot_id)
        if table is None and fp is not None:
            table, _how = fingerprint.resolve(fp, snap)
        if table is None:
            tables = extract.find_tables(snap.roots)
            for _ in range(5):  # a page that has just opened may not have built its content yet
                if tables:
                    break
                time.sleep(0.6)
                snap = self.see()
                tables = extract.find_tables(snap.roots)
            if not tables:
                raise LookupError("no table on screen")
            table = tables[0]
        if not extract.is_table(table):
            raise ValueError(f"element [{table.index}] is a {table.role}, not a table")
        anchor = fp or (fingerprint.make(table, snap) if table.index >= 0 else None)
        started = time.perf_counter()
        first = extract.read_table(table)

        def locate(s: Snapshot) -> Optional[Element]:
            found = fingerprint.resolve(anchor, s)[0] if anchor else None
            if found is None or not extract.is_table(found):
                tables = extract.find_tables(s.roots)
                found = tables[0] if tables else None
            return found

        def read(s: Snapshot):
            found = locate(s)
            return extract.read_table(found)["rows"] if found is not None else None

        scroller = (lambda s: self._scroll_target(s, locate(s))) if scroll else None
        from_tree = all(e.source == "tree" for e in table.walk())  # no vision needed to read it
        result = self._unrecorded(lambda: extract.collect(lambda fn: fn(self), read, next, scroller, max_pages,
                                                          cv=False if from_tree else None))
        self._record("extract_table", table, snap, {"next": next, "scroll": scroll} if next or scroll else {})
        return {"name": first["name"], "columns": first["columns"], "rows": result["records"],
                "pages": result["pages"], "scrolls": result["scrolls"], "stopped": result["stopped"],
                "ms": round((time.perf_counter() - started) * 1000, 1)}

    def extract_items(self, index: Optional[int] = None, snapshot_id: Optional[int] = None,
                      next: Optional[str] = None, scroll: bool = False, max_pages: int = 50,
                      fp: Optional[dict] = None) -> dict:
        """All repeated items (product cards, results, tiles), across pages and scrolling."""
        from . import extract
        if self.halted:
            raise Halted("stopped by the user; run `shadow resume` to continue")
        snap, container = self._target(index, snapshot_id)
        if container is None and fp is not None:
            container, _how = fingerprint.resolve(fp, snap)
        groups = extract.find_item_groups(snap.roots)
        for _ in range(4):  # a page that has just opened may not have built its content yet
            if groups or container is not None:
                break
            time.sleep(0.6)
            snap = self.see()
            groups = extract.find_item_groups(snap.roots)
        min_fields = 2
        if container is not None:
            group = [g for g in groups if g[0] is container]
            if group:
                chosen = group[0]
            elif extract.list_entries(container):
                chosen, min_fields = (container, extract.list_entries(container)), 1
            else:
                raise ValueError(f"element [{container.index}] does not hold repeated items")
        elif groups:
            chosen = groups[0]
        else:
            raise LookupError("no repeated items (cards, tiles or results) on screen")
        anchor = fp or (fingerprint.make(chosen[0], snap) if chosen[0].index >= 0 else None)
        reference = extract.shape_of(chosen[1])
        started = time.perf_counter()

        def read(s: Snapshot):
            if anchor:
                target = fingerprint.resolve(anchor, s)[0]
                if target is not None:
                    members = extract.items_in(target, reference, min_fields)
                    if members:
                        return extract.read_items(members)
            found = [g for g in extract.find_item_groups(s.roots, minimum=1)
                     if extract._similar(extract.shape_of(g[1]), reference)]
            found.sort(key=lambda g: -len(g[1]))
            return extract.read_items(found[0][1]) if found else None

        def scroller(s: Snapshot):
            target = fingerprint.resolve(anchor, s)[0] if anchor else None
            return self._scroll_target(s, target or chosen[0])

        from_tree = all(e.source == "tree" for m in chosen[1] for e in m.walk())
        result = self._unrecorded(lambda: extract.collect(lambda fn: fn(self), read, next,
                                                          scroller if scroll else None, max_pages,
                                                          cv=False if from_tree else None))
        self._record("extract_items", chosen[0] if chosen[0].index >= 0 else None, snap,
                     {"next": next, "scroll": scroll} if next or scroll else {})
        items = result["records"]
        fields = sorted({k for item in items for k in item if k != "texts"})
        return {"items": items, "count": len(items), "fields": fields, "pages": result["pages"],
                "scrolls": result["scrolls"], "stopped": result["stopped"],
                "ms": round((time.perf_counter() - started) * 1000, 1)}

    # ---- recording -----------------------------------------------------
    def record_start(self) -> None:
        self.trace = []
        self.recording = True

    def record_stop(self) -> list[dict]:
        self.recording = False
        return self.trace
