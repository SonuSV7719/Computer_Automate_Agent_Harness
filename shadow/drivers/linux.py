"""Linux (X11) driver, used by the cloud sandbox.

X11 gives windows and input but no element tree here, so every window is treated as
pixels: the vision layer supplies all of its elements. Needs xdotool and wmctrl.
"""
from __future__ import annotations

import subprocess
import time
from typing import Optional

from ..core.elements import Box, Element, WindowInfo

KEYS = {
    "enter": "Return", "return": "Return", "esc": "Escape", "escape": "Escape", "backspace": "BackSpace",
    "delete": "Delete", "del": "Delete", "tab": "Tab", "home": "Home", "end": "End", "pageup": "Prior",
    "pgup": "Prior", "pagedown": "Next", "pgdn": "Next", "left": "Left", "right": "Right", "up": "Up",
    "down": "Down", "space": "space", "ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift",
    "win": "super", "insert": "Insert",
}
SKIP_TITLES = {"Desktop", "xfce4-panel", "Openbox"}


def _run(*args: str, check: bool = True) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    if check and result.returncode != 0:
        raise RuntimeError(f"{args[0]} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


class LinuxDriver:
    # ---- windows -------------------------------------------------------
    def list_windows(self) -> list[WindowInfo]:
        out = []
        for line in _run("wmctrl", "-lpG", check=False).splitlines():
            parts = line.split(None, 8)
            if len(parts) < 8:
                continue
            wid, _desktop, pid, x, y, w, h = parts[0], parts[1], parts[2], *parts[3:7]
            title = parts[8] if len(parts) > 8 else ""
            if not title or title in SKIP_TITLES or int(w) < 20 or int(h) < 20:
                continue
            out.append(self._info(int(wid, 16), title, int(pid)))
        return out

    def _info(self, hwnd: int, title: Optional[str] = None, pid: int = 0) -> WindowInfo:
        geometry = dict(line.split("=", 1) for line in
                        _run("xdotool", "getwindowgeometry", "--shell", str(hwnd)).splitlines() if "=" in line)
        x, y, w, h = (int(geometry[k]) for k in ("X", "Y", "WIDTH", "HEIGHT"))
        if title is None:
            title = _run("xdotool", "getwindowname", str(hwnd), check=False).strip()
        if not pid:
            raw = _run("xdotool", "getwindowpid", str(hwnd), check=False).strip()
            pid = int(raw) if raw.isdigit() else 0
        process = ""
        if pid:
            try:
                with open(f"/proc/{pid}/comm", encoding="utf-8") as handle:
                    process = handle.read().strip()
            except OSError:
                pass
        return WindowInfo(hwnd=hwnd, title=title, process=process, pid=pid, box=(x, y, x + w, y + h),
                          class_name="x11")

    def window_info(self, hwnd: int) -> WindowInfo:
        return self._info(hwnd)

    def foreground(self) -> int:
        raw = _run("xdotool", "getactivewindow", check=False).strip()
        return int(raw) if raw.isdigit() else 0

    def exists(self, hwnd: int) -> bool:
        return subprocess.run(["xdotool", "getwindowname", str(hwnd)], capture_output=True).returncode == 0

    def _focus(self, hwnd: int) -> None:
        if hwnd and self.foreground() != hwnd:
            _run("xdotool", "windowactivate", "--sync", str(hwnd), check=False)
            time.sleep(0.05)

    # ---- tree ----------------------------------------------------------
    def tree(self, hwnd: int) -> list[Element]:
        """No element tree on this platform: one root whose whole area is read by vision."""
        info = self._info(hwnd)
        root = Element(role="window", name=info.title, box=info.box, root_hwnd=hwnd)
        root.children.append(Element(role="pane", box=info.box, parent=root, root_hwnd=hwnd))
        return [root]

    # ---- actions -------------------------------------------------------
    def _click(self, x: int, y: int, button: int = 1, repeat: int = 1) -> None:
        _run("xdotool", "mousemove", "--sync", str(x), str(y))
        _run("xdotool", "click", "--repeat", str(repeat), "--delay", "60", str(button))

    @staticmethod
    def _points(e: Element, value: Optional[str]) -> list[tuple[int, int]]:
        l, t, r, b = e.box
        points = []
        for token in (value or "").replace(";", " ").split():
            x, sep, y = token.partition(",")
            if not sep:
                raise ValueError(f'bad position "{token}"; write x,y with each from 0 to 1000')
            fx, fy = float(x), float(y)
            if not (0 <= fx <= 1000 and 0 <= fy <= 1000):
                raise ValueError(f'position "{token}" is outside 0-1000')
            points.append((round(l + (r - l) * fx / 1000), round(t + (b - t) * fy / 1000)))
        return points

    def perform(self, e: Element, action: str, value: Optional[str] = None) -> str:
        self._focus(e.root_hwnd)
        x, y = e.center
        if action == "click" or action in ("toggle", "select", "focus", "expand", "collapse"):
            self._click(x, y)
        elif action == "doubleclick":
            self._click(x, y, repeat=2)
        elif action == "rightclick":
            self._click(x, y, button=3)
        elif action in ("type", "append"):
            self._click(x, y)
            if action == "type":
                _run("xdotool", "key", "--clearmodifiers", "ctrl+a", "Delete", "End", "shift+Home", "Delete")
            else:
                _run("xdotool", "key", "--clearmodifiers", "End")
            self.type_text(value or "")
        elif action == "scroll":
            direction, _, amount = (value or "down 3").partition(" ")
            _run("xdotool", "mousemove", "--sync", str(x), str(y))
            _run("xdotool", "click", "--repeat", str(int(amount or 3)), "4" if direction == "up" else "5")
        elif action in ("click_at", "drag"):
            points = self._points(e, value)
            if action == "click_at":
                if len(points) != 1:
                    raise ValueError('click_at needs one position, e.g. "500,500"')
                self._click(*points[0])
            else:
                if len(points) < 2:
                    raise ValueError('drag needs at least two positions, e.g. "100,100 400,300"')
                _run("xdotool", "mousemove", "--sync", str(points[0][0]), str(points[0][1]))
                _run("xdotool", "mousedown", "1")
                for px, py in points[1:]:
                    _run("xdotool", "mousemove", "--sync", str(px), str(py))
                    time.sleep(0.03)
                _run("xdotool", "mouseup", "1")
        else:
            raise ValueError(f"unknown action: {action}")
        return "mouse" if action not in ("type", "append") else "keys"

    def press(self, combo: str, hwnd: int = 0) -> None:
        self._focus(hwnd)
        keys = "+".join(KEYS.get(part, part) for part in combo.lower().replace(" ", "").split("+"))
        _run("xdotool", "key", "--clearmodifiers", keys)

    def type_text(self, text: str, hwnd: int = 0) -> None:
        self._focus(hwnd)
        if text:
            _run("xdotool", "type", "--clearmodifiers", "--delay", "8", "--", text)

    # ---- capture -------------------------------------------------------
    def capture(self, box: Box):
        import mss
        from PIL import Image

        l, t, r, b = box
        with mss.mss() as sct:
            shot = sct.grab({"left": l, "top": t, "width": max(1, r - l), "height": max(1, b - t)})
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    def capture_window(self, hwnd: int, box: Box):
        self._focus(hwnd)  # the screen is what gets read, so the window must be on top
        return self.capture(box)
