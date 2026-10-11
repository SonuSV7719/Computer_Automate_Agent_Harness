"""The engine: snapshots, actions with post-conditions, and step recording.

Everything above it (CLI, daemon, learn loop, replay) goes through this class.
"""
from __future__ import annotations

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
                self._verify_value(element, action, value)

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

    def _verify_value(self, element: Element, action: str, value: Optional[str]) -> None:
        """A typed or selected value must actually be in the field afterwards."""
        read = getattr(self.driver, "read_value", None)
        if read is None or action not in ("type", "select") or element.masked or element.source != "tree" \
                or element.role not in ("input", "document", "dropdown", "spinner"):
            return
        deadline = time.perf_counter() + VERIFY_TIMEOUT
        while True:  # browsers update their accessibility values a moment after the input changes
            shown = read(element)
            if shown is None or textmatch.squash(shown) == textmatch.squash(value or ""):
                return
            if time.perf_counter() >= deadline:
                raise RuntimeError(f'the field shows "{shown}" after {action} "{value}"')
            time.sleep(0.05)

    # ---- recording -----------------------------------------------------
    def record_start(self) -> None:
        self.trace = []
        self.recording = True

    def record_stop(self) -> list[dict]:
        self.recording = False
        return self.trace
