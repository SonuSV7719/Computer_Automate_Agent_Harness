"""Windows driver: cached UI Automation tree, semantic actions, real input fallback."""
from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Optional

sys.coinit_flags = 0  # COM multithreaded apartment; must be set before comtypes is imported

import comtypes  # noqa: E402
import comtypes.client  # noqa: E402

comtypes.client.GetModule("UIAutomationCore.dll")
from comtypes.gen import UIAutomationClient as UIA  # noqa: E402

from ..core.elements import Box, Element, WindowInfo  # noqa: E402
from . import win_input  # noqa: E402

ROLES = {
    50000: "button", 50001: "calendar", 50002: "checkbox", 50003: "dropdown", 50004: "input",
    50005: "link", 50006: "image", 50007: "listitem", 50008: "list", 50009: "menu", 50010: "menubar",
    50011: "menuitem", 50012: "progressbar", 50013: "radio", 50014: "scrollbar", 50015: "slider",
    50016: "spinner", 50017: "statusbar", 50018: "tab", 50019: "tabitem", 50020: "text",
    50021: "toolbar", 50022: "tooltip", 50023: "tree", 50024: "treeitem", 50025: "custom",
    50026: "group", 50027: "thumb", 50028: "table", 50029: "row", 50030: "document",
    50031: "splitbutton", 50032: "window", 50033: "pane", 50034: "header", 50035: "headeritem",
    50036: "table", 50037: "titlebar", 50038: "separator", 50039: "custom", 50040: "appbar",
}
CLICKABLE = {"button", "link", "menuitem", "tabitem", "listitem", "treeitem", "checkbox", "radio",
             "splitbutton", "headeritem", "row"}
TYPABLE = {"input", "document", "dropdown", "spinner"}

P = UIA  # short alias for property and pattern ids
CACHED_PROPERTIES = [
    P.UIA_NamePropertyId, P.UIA_ControlTypePropertyId, P.UIA_AutomationIdPropertyId,
    P.UIA_ClassNamePropertyId, P.UIA_BoundingRectanglePropertyId, P.UIA_IsEnabledPropertyId,
    P.UIA_IsOffscreenPropertyId, P.UIA_HasKeyboardFocusPropertyId, P.UIA_IsKeyboardFocusablePropertyId,
    P.UIA_IsPasswordPropertyId, P.UIA_FrameworkIdPropertyId, P.UIA_IsRequiredForFormPropertyId,
    P.UIA_IsInvokePatternAvailablePropertyId, P.UIA_IsValuePatternAvailablePropertyId,
    P.UIA_IsTogglePatternAvailablePropertyId, P.UIA_IsSelectionItemPatternAvailablePropertyId,
    P.UIA_IsExpandCollapsePatternAvailablePropertyId, P.UIA_IsScrollPatternAvailablePropertyId,
    P.UIA_ValueValuePropertyId, P.UIA_ValueIsReadOnlyPropertyId, P.UIA_ToggleToggleStatePropertyId,
    P.UIA_SelectionItemIsSelectedPropertyId, P.UIA_ExpandCollapseExpandCollapseStatePropertyId,
]
MAX_NODES = 4000
PATTERN_TIMEOUT = 2.0


def _com_init() -> None:
    from .com import init_thread
    init_thread()


class WindowsDriver:
    def __init__(self) -> None:
        win_input.set_dpi_aware()
        self._uia = comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)
        self._cache = self._uia.CreateCacheRequest()
        for pid in CACHED_PROPERTIES:
            self._cache.AddProperty(pid)
        self._cache.TreeScope = UIA.TreeScope_Subtree
        self._cache.AutomationElementMode = UIA.AutomationElementMode_Full
        # Pattern calls such as Invoke can block while the app shows a modal dialog,
        # so they run on their own threads with a timeout.
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="uia-act", initializer=_com_init)

    # ---- windows -------------------------------------------------------
    def list_windows(self) -> list[WindowInfo]:
        return win_input.list_windows()

    def foreground(self) -> int:
        return win_input.foreground()

    def window_info(self, hwnd: int) -> WindowInfo:
        return win_input.window_info(hwnd)

    def exists(self, hwnd: int) -> bool:
        return win_input.exists(hwnd)

    # ---- tree ----------------------------------------------------------
    def tree(self, hwnd: int) -> list[Element]:
        roots = []
        for handle in [hwnd] + win_input.popups_of(hwnd):
            try:
                com_root = self._uia.ElementFromHandleBuildCache(handle, self._cache)
            except comtypes.COMError:
                continue
            budget = [MAX_NODES]
            root = self._build(com_root, None, handle, budget)
            if root is not None:
                roots.append(root)
        return roots

    def _build(self, el, parent: Optional[Element], root_hwnd: int, budget: list[int]) -> Optional[Element]:
        if budget[0] <= 0:
            return None
        budget[0] -= 1

        def prop(pid, default=None):
            try:
                return el.GetCachedPropertyValue(pid)
            except Exception:
                return default

        try:
            rect = el.CachedBoundingRectangle
            box = (rect.left, rect.top, rect.right, rect.bottom)
            control_type = el.CachedControlType
        except Exception:
            return None

        role = ROLES.get(control_type, "custom")
        if role == "window" and parent is not None:
            role = "dialog"
        e = Element(
            role=role,
            name=(prop(P.UIA_NamePropertyId) or "").strip(),
            box=box,
            enabled=bool(prop(P.UIA_IsEnabledPropertyId, True)),
            focused=bool(prop(P.UIA_HasKeyboardFocusPropertyId, False)),
            offscreen=bool(prop(P.UIA_IsOffscreenPropertyId, False)),
            required=bool(prop(P.UIA_IsRequiredForFormPropertyId, False)),
            masked=bool(prop(P.UIA_IsPasswordPropertyId, False)),
            automation_id=prop(P.UIA_AutomationIdPropertyId) or "",
            class_name=prop(P.UIA_ClassNamePropertyId) or "",
            framework=prop(P.UIA_FrameworkIdPropertyId) or "",
            parent=parent,
            handle=el,
            root_hwnd=root_hwnd,
        )
        focusable = bool(prop(P.UIA_IsKeyboardFocusablePropertyId, False))

        if prop(P.UIA_IsInvokePatternAvailablePropertyId, False):
            e.patterns.add("invoke")
        if prop(P.UIA_IsValuePatternAvailablePropertyId, False):
            e.patterns.add("value")
            e.readonly = bool(prop(P.UIA_ValueIsReadOnlyPropertyId, False))
            if role in TYPABLE or role in ("slider", "progressbar", "link"):
                e.value = str(prop(P.UIA_ValueValuePropertyId) or "").rstrip("\r\n")
        if prop(P.UIA_IsTogglePatternAvailablePropertyId, False):
            e.patterns.add("toggle")
            if role in ("checkbox", "button", "menuitem", "radio"):
                state = prop(P.UIA_ToggleToggleStatePropertyId, 0)
                if role != "button" or state:
                    e.checked = {0: "unchecked", 1: "checked", 2: "mixed"}.get(state, "unchecked")
        if prop(P.UIA_IsSelectionItemPatternAvailablePropertyId, False):
            e.patterns.add("select")
            e.selected = bool(prop(P.UIA_SelectionItemIsSelectedPropertyId, False))
        if prop(P.UIA_IsExpandCollapsePatternAvailablePropertyId, False):
            state = prop(P.UIA_ExpandCollapseExpandCollapseStatePropertyId, 3)
            if state != 3:  # 3 = leaf node, nothing to expand
                e.patterns.add("expand")
                e.expanded = state in (1, 2)
        if prop(P.UIA_IsScrollPatternAvailablePropertyId, False):
            e.patterns.add("scroll")

        e.actions = self._actions(e, focusable)

        try:
            kids = el.GetCachedChildren()
            count = kids.Length if kids else 0
        except Exception:
            kids, count = None, 0
        for i in range(count):
            child = self._build(kids.GetElement(i), e, root_hwnd, budget)
            if child is not None:
                e.children.append(child)
        if role == "dropdown" and e.enabled:
            editable = any(c.role == "input" for c in e.children)
            e.actions = ["select"] + (["type"] if editable and not e.readonly else [])                 + [a for a in e.actions if a in ("expand", "collapse")]
        return e

    @staticmethod
    def _actions(e: Element, focusable: bool) -> list[str]:
        if not e.enabled:
            return []
        actions = []
        if "invoke" in e.patterns or e.role in CLICKABLE:
            actions.append("click")
        if e.role in TYPABLE and not e.readonly and ("value" in e.patterns or focusable):
            actions.append("type")
        if "toggle" in e.patterns and e.role in ("checkbox", "radio"):
            actions.append("toggle")
        if "select" in e.patterns:
            actions.append("select")
        if "expand" in e.patterns:
            actions.append("collapse" if e.expanded else "expand")
        if "scroll" in e.patterns:
            actions.append("scroll")
        return actions

    # ---- actions -------------------------------------------------------
    def _pattern(self, e: Element, pattern_id: int, interface, call) -> bool:
        """Run a UIA pattern call with a timeout. False means the pattern is not there."""
        handle = getattr(e, "handle", e)  # an Element, or a raw UIA element

        def run():
            raw = handle.GetCurrentPattern(pattern_id)
            if not raw:
                return False
            call(raw.QueryInterface(interface))
            return True

        future = self._pool.submit(run)
        try:
            return bool(future.result(timeout=PATTERN_TIMEOUT))
        except FutureTimeout:
            return True  # the call is blocked behind a dialog the action itself opened
        except comtypes.COMError:
            return False

    def _mouse_click(self, e: Element, **kwargs) -> str:
        win_input.focus_window(e.root_hwnd)
        win_input.click(*e.center, **kwargs)
        return "mouse"

    def _focus(self, e: Element) -> None:
        win_input.focus_window(e.root_hwnd)
        if e.source != "tree":  # a surface the tree cannot describe: only a real click focuses it reliably
            win_input.click(*e.center)
        else:
            try:
                e.handle.SetFocus()
            except Exception:
                win_input.click(*e.center)
        time.sleep(0.03)

    @staticmethod
    def _points(e: Element, value: Optional[str]) -> list[tuple[int, int]]:
        """Positions inside an element, written as "x,y x,y ..." on a 0-1000 grid."""
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

    def read_value(self, e: Element) -> Optional[str]:
        """The value a field holds right now, or None if it cannot be read."""
        if "value" not in e.patterns or e.handle is None:
            return None
        try:
            return str(e.handle.GetCurrentPropertyValue(UIA.UIA_ValueValuePropertyId) or "").rstrip("\r\n")
        except Exception:
            return None

    def _current_value(self, e: Element) -> str:
        try:
            return str(e.handle.GetCurrentPropertyValue(UIA.UIA_ValueValuePropertyId) or "").strip()
        except Exception:
            return ""

    def _select_option(self, e: Element, option: str) -> str:
        """Choose an option in a dropdown by its text, trying the cheapest route first."""
        want = option.strip().casefold()

        def chosen() -> bool:
            return self._current_value(e).casefold() == want

        if "expand" in e.patterns:
            self._pattern(e, UIA.UIA_ExpandCollapsePatternId, UIA.IUIAutomationExpandCollapsePattern,
                          lambda p: p.Expand())
            time.sleep(0.05)
        try:
            condition = self._uia.CreateAndCondition(
                self._uia.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId, 50007),
                self._uia.CreatePropertyCondition(UIA.UIA_NamePropertyId, option))
            item = e.handle.FindFirst(UIA.TreeScope_Descendants, condition)
        except comtypes.COMError:
            item = None
        if item and self._pattern(item, UIA.UIA_SelectionItemPatternId,
                                  UIA.IUIAutomationSelectionItemPattern, lambda p: p.Select()):
            if "expand" in e.patterns:
                self._pattern(e, UIA.UIA_ExpandCollapsePatternId, UIA.IUIAutomationExpandCollapsePattern,
                              lambda p: p.Collapse())
            time.sleep(0.03)
            if chosen() or not self._current_value(e):
                return "uia.select"

        if "value" in e.patterns and not e.readonly:
            self._pattern(e, UIA.UIA_ValuePatternId, UIA.IUIAutomationValuePattern,
                          lambda p: p.SetValue(option))
            time.sleep(0.03)
            if chosen():
                return "uia.value"

        self._focus(e)  # a closed list selects the option whose text is typed
        win_input.type_text(option)
        time.sleep(0.08)
        if chosen():
            return "keys"
        raise RuntimeError(f'could not select "{option}" in dropdown "{e.title}"')

    def perform(self, e: Element, action: str, value: Optional[str] = None) -> str:
        if not e.enabled:
            raise RuntimeError(f"element [{e.index}] is disabled")
        tree_backed = e.source != "cv" and e.handle is not None
        if action == "type" and e.source == "merged" and "value" not in e.patterns:
            pass  # typed with real keys below

        if action == "click":
            # A control the tree could not even name often ignores UIA calls; click it for real.
            if tree_backed and e.source == "tree":
                if "invoke" in e.patterns and self._pattern(
                        e, UIA.UIA_InvokePatternId, UIA.IUIAutomationInvokePattern, lambda p: p.Invoke()):
                    return "uia.invoke"
                if e.role in ("checkbox",) and "toggle" in e.patterns and self._pattern(
                        e, UIA.UIA_TogglePatternId, UIA.IUIAutomationTogglePattern, lambda p: p.Toggle()):
                    return "uia.toggle"
                if e.role in ("radio", "tabitem", "listitem", "treeitem") and "select" in e.patterns \
                        and self._pattern(e, UIA.UIA_SelectionItemPatternId,
                                          UIA.IUIAutomationSelectionItemPattern, lambda p: p.Select()):
                    return "uia.select"
            return self._mouse_click(e)

        if action in ("click_at", "drag"):
            points = self._points(e, value)
            if action == "click_at" and len(points) != 1:
                raise ValueError('click_at needs one position, e.g. "500,500"')
            if action == "drag" and len(points) < 2:
                raise ValueError('drag needs at least two positions, e.g. "100,100 400,300"')
            win_input.focus_window(e.root_hwnd)
            if action == "click_at":
                win_input.click(*points[0])
            else:
                win_input.drag(points)
            return "mouse"

        if action in ("doubleclick", "rightclick"):
            return self._mouse_click(e, double=action == "doubleclick",
                                     button="right" if action == "rightclick" else "left")

        if action in ("type", "append"):
            text = value or ""
            if action == "type" and tree_backed and "value" in e.patterns and not e.readonly \
                    and self._pattern(e, UIA.UIA_ValuePatternId, UIA.IUIAutomationValuePattern,
                                      lambda p: p.SetValue(text)):
                return "uia.value"
            if tree_backed:
                self._focus(e)
            else:
                self._mouse_click(e)
            if action == "type":
                # Select-all differs by toolkit (Ctrl+A in Win32, End then Shift+Home in Tk); do both.
                win_input.press("ctrl+a")
                win_input.press("delete")
                if e.role != "document":
                    win_input.press("end")
                    win_input.press("shift+home")
                    win_input.press("delete")
            else:
                win_input.press("ctrl+end")
            win_input.type_text(text)
            return "keys"

        if action == "toggle":
            if tree_backed and self._pattern(e, UIA.UIA_TogglePatternId, UIA.IUIAutomationTogglePattern,
                                             lambda p: p.Toggle()):
                return "uia.toggle"
            return self._mouse_click(e)

        if action == "select" and e.role == "dropdown" and tree_backed:
            return self._select_option(e, value or "")

        if action == "select":
            if tree_backed and self._pattern(e, UIA.UIA_SelectionItemPatternId,
                                             UIA.IUIAutomationSelectionItemPattern, lambda p: p.Select()):
                return "uia.select"
            return self._mouse_click(e)

        if action in ("expand", "collapse"):
            call = (lambda p: p.Expand()) if action == "expand" else (lambda p: p.Collapse())
            if tree_backed and self._pattern(e, UIA.UIA_ExpandCollapsePatternId,
                                             UIA.IUIAutomationExpandCollapsePattern, call):
                return "uia.expand"
            return self._mouse_click(e)

        if action == "focus":
            self._focus(e)
            return "uia.focus"

        if action == "scroll":
            direction, _, amount = (value or "down 3").partition(" ")
            notches = int(amount or 3)
            win_input.focus_window(e.root_hwnd)
            win_input.scroll(*e.center, notches if direction == "up" else -notches)
            return "wheel"

        raise ValueError(f"unknown action: {action}")

    def press(self, combo: str, hwnd: int = 0) -> None:
        if hwnd:
            win_input.focus_window(hwnd)
        win_input.press(combo)

    def type_text(self, text: str, hwnd: int = 0) -> None:
        if hwnd:
            win_input.focus_window(hwnd)
        win_input.type_text(text)

    # ---- capture -------------------------------------------------------
    def capture_window(self, hwnd: int, box: Box):
        """The window's own content, so vision is not fooled by whatever covers it."""
        from PIL import Image

        shot = win_input.capture_window(hwnd)
        if shot is not None:
            w, h, raw = shot
            image = Image.frombuffer("RGB", (w, h), raw, "raw", "BGRX", 0, 1)
            if image.getbbox() is not None:  # an all-black frame means the window refused
                size = (box[2] - box[0], box[3] - box[1])
                return image if image.size == size else image.resize(size, Image.BILINEAR)
        win_input.focus_window(hwnd)
        return self.capture(box)

    def capture(self, box: Box):
        import mss
        from PIL import Image

        l, t, r, b = box
        with mss.mss() as sct:
            shot = sct.grab({"left": l, "top": t, "width": max(1, r - l), "height": max(1, b - t)})
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
