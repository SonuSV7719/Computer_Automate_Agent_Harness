"""The engine: snapshots, actions with post-conditions, and step recording.

Everything above it (CLI, daemon, learn loop, replay) goes through this class.
"""
from __future__ import annotations

import os
import re
import time
from typing import Optional

from .core import expect as expectation
from .core import fingerprint
from .core.diff import diff
from .core.elements import Element, Snapshot, WindowInfo
from .core.serialize import serialize

_DIGITS = re.compile(r"\d+")
SETTLE_TIMEOUT = 1.2
POLL = 0.08
ELEMENTLESS = {"key", "text"}


class StaleSnapshot(Exception):
    pass


class Engine:
    def __init__(self, driver=None, perceiver=None, cv: bool = True) -> None:
        if driver is None:
            from .drivers.windows import WindowsDriver
            driver = WindowsDriver()
        self.driver = driver
        self.cv_enabled = cv and os.environ.get("SHADOW_CV", "1") != "0"
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
        info = self.driver.window_info(hwnd)
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
        while True:
            snap = self.see(cv)
            if spec:
                if expectation.check(spec, snap):
                    return snap, True
            elif snap.body == previous_body:
                return snap, None
            if time.perf_counter() >= deadline:
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
            route = self.driver.perform(element, action, value)

        if expect or settle:
            after, met = self._settle(expect, timeout, cv)
        else:
            after, met = None, None
        ms = (time.perf_counter() - started) * 1000
        ok = met is not False
        step = {
            "action": action, "value": value, "fingerprint": fp, "expect": expect,
            "intent": intent, "route": route, "ok": ok, "ms": round(ms, 1),
        }
        if self.recording and ok:
            self.trace.append(step)
        return {
            "ok": ok,
            "route": route,
            "ms": round(ms, 1),
            "snapshot": (after or before).id,
            "expect": {"spec": expect, "met": met} if expect else None,
            "changes": diff(before, after) if after else [],
            "element": element.to_dict() if element else None,
        }

    # ---- recording -----------------------------------------------------
    def record_start(self) -> None:
        self.trace = []
        self.recording = True

    def record_stop(self) -> list[dict]:
        self.recording = False
        return self.trace
