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
             "splitbutton", "headeritem", "row", "switch"}
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
    P.UIA_AriaRolePropertyId, P.UIA_IsRangeValuePatternAvailablePropertyId, P.UIA_RangeValueValuePropertyId,
    P.UIA_RangeValueMinimumPropertyId, P.UIA_RangeValueMaximumPropertyId,
    P.UIA_IsSelectionPatternAvailablePropertyId, P.UIA_SelectionCanSelectMultiplePropertyId,
    P.UIA_IsGridItemPatternAvailablePropertyId, P.UIA_IsTableItemPatternAvailablePropertyId,
    P.UIA_IsLegacyIAccessiblePatternAvailablePropertyId, P.UIA_LegacyIAccessibleValuePropertyId,
]
# Web pages say exactly what an element is through its ARIA role; it is more precise than
# the generic control type (a table cell and a table row are both "data items" otherwise).
ARIA_ROLES = {
    "columnheader": "headeritem", "rowheader": "headeritem", "cell": "cell", "gridcell": "cell", "row": "row",
    "grid": "table", "table": "table", "treegrid": "table", "switch": "switch", "searchbox": "input",
    "spinbutton": "spinner", "slider": "slider", "heading": "heading", "listbox": "list", "option": "listitem",
    "tab": "tabitem", "tablist": "tab", "menuitemcheckbox": "checkbox", "menuitemradio": "radio",
}
MAX_NODES = 4000
PATTERN_TIMEOUT = 2.0


DATE_PART_NAMES = {"day": "day", "month": "month", "year": "year"}
LEGACY_FRAMEWORKS = {"WinForm", "Win32"}


def _date_parts(e: Element) -> list:
    """The day, month and year parts of a date field, in the order they are shown."""
    found = []
    for child in e.children:
        if child.role != "spinner" or "range" not in child.patterns:
            continue
        first = (child.name.split() or [""])[0].casefold()
        if first in DATE_PART_NAMES:
            found.append((DATE_PART_NAMES[first], child))
    kinds = {kind for kind, _ in found}
    return sorted(found, key=lambda kp: kp[1].box[0]) if kinds == {"day", "month", "year"} else []


def _parse_date(value: str, order: list) -> Optional[dict]:
    """Read 2024-07-15, or 15/07/2024 in the field's own day/month order."""
    import re as _re
    iso = _re.fullmatch(r"\s*(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\s*", value)
    if iso:
        return {"year": int(iso.group(1)), "month": int(iso.group(2)), "day": int(iso.group(3))}
    local = _re.fullmatch(r"\s*(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})\s*", value)
    if local:
        first, second, year = int(local.group(1)), int(local.group(2)), int(local.group(3))
        small = [k for k in order if k != "year"]
        values = dict(zip(small, (first, second)))
        values["year"] = year
        return values
    return None


def _re_digits(text: str) -> list[str]:
    import re as _re
    return _re.findall(r"\d+", text)


def _looks_like_date(text: str) -> bool:
    groups = _re_digits(text)
    return len(groups) == 3 and any(len(g) == 4 for g in groups)


def _date_order(shown: str) -> list:
    """Field order of a date as the control shows it: a 4-digit group is the year; the other
    two follow the user's short-date setting (day first, or month first)."""
    groups = _re_digits(shown)
    if len(groups) != 3:
        return []
    import ctypes as _ct
    buffer = _ct.create_unicode_buffer(80)
    _ct.windll.kernel32.GetLocaleInfoEx(None, 0x1F, buffer, 80)  # LOCALE_SSHORTDATE
    pattern = buffer.value.lower()
    month_first = pattern.find("m") < pattern.find("d") if "m" in pattern and "d" in pattern else False
    small = ["month", "day"] if month_first else ["day", "month"]
    if len(groups[0]) == 4:
        return ["year", "month", "day"]
    return small + ["year"]


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _has_web_content(roots: list[Element]) -> bool:
    """True when a web page inside the window exposes more than an empty document."""
    for root in roots:
        for e in root.walk():
            if e.role == "document" and sum(1 for _ in e.walk()) > 3:
                return True
    return False


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
        self._woken: set[int] = set()

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
        roots = self._tree(hwnd)
        if hwnd not in self._woken and win_input.window_info(hwnd).class_name.startswith("Chrome_WidgetWin"):
            self._woken.add(hwnd)
            if win_input.wake_accessibility(hwnd) and not _has_web_content(roots):
                for _ in range(10):  # the page's tree arrives a moment after the request
                    time.sleep(0.2)
                    roots = self._tree(hwnd)
                    if _has_web_content(roots):
                        break
        return roots

    def _tree(self, hwnd: int) -> list[Element]:
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
        aria = (prop(P.UIA_AriaRolePropertyId) or "").strip().lower()
        if aria in ARIA_ROLES:
            role = ARIA_ROLES[aria]
        elif prop(P.UIA_IsTableItemPatternAvailablePropertyId, False) or (
                prop(P.UIA_IsGridItemPatternAvailablePropertyId, False) and role in ("custom", "row", "text", "edit")):
            role = "cell"  # a desktop grid's cell (WinForms, WPF) exposes its row and column
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
            if role in TYPABLE or role in ("slider", "progressbar", "link", "cell", "row", "custom", "listitem",
                                           "treeitem"):
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
        if prop(P.UIA_IsRangeValuePatternAvailablePropertyId, False) and role in ("slider", "spinner", "progressbar"):
            e.patterns.add("range")
            number = prop(P.UIA_RangeValueValuePropertyId)
            if isinstance(number, (int, float)):
                low, high = prop(P.UIA_RangeValueMinimumPropertyId), prop(P.UIA_RangeValueMaximumPropertyId)
                e.value = _number(number)
                if isinstance(low, (int, float)) and isinstance(high, (int, float)) and high > low:
                    e.value_range = (_number(low), _number(high))
        if prop(P.UIA_IsSelectionPatternAvailablePropertyId, False) and \
                prop(P.UIA_SelectionCanSelectMultiplePropertyId, False):
            e.patterns.add("multiselect")
        # Older Win32 and WinForms controls only speak MSAA ("legacy accessibility"):
        # their value is there even when no modern pattern offers it.
        if e.value is None and prop(P.UIA_IsLegacyIAccessiblePatternAvailablePropertyId, False):
            legacy = (prop(P.UIA_LegacyIAccessibleValuePropertyId) or "").strip()
            if legacy and role in ("slider", "spinner", "pane", "custom", "dropdown", "input"):
                e.value = legacy
        if "DateTimePick" in e.class_name or (role == "pane" and e.value and _looks_like_date(e.value)):
            role = e.role = "input"
            e.patterns.add("date")
        if role == "switch" and "toggle" in e.patterns:
            state = prop(P.UIA_ToggleToggleStatePropertyId, 0)
            e.checked = {0: "off", 1: "on", 2: "mixed"}.get(state, "off")

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
        if role == "table":
            from ..extract import normalize_table
            normalize_table(e)
        if role == "dropdown" and e.enabled:
            editable = any(c.role == "input" for c in e.children)
            editable = editable or ("value" in e.patterns and not e.readonly and focusable
                                    and "expand" not in e.patterns)
            e.actions = (["select"] + (["type"] if editable and not e.readonly else [])
                         + [a for a in e.actions if a in ("expand", "collapse")])
        if role == "list" and e.enabled and ("multiselect" in e.patterns
                                             or any("select" in c.patterns for c in e.children)):
            e.actions = ["select"] + [a for a in e.actions if a != "select"]  # value: "A | B | C"
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
        if "toggle" in e.patterns and e.role in ("checkbox", "radio", "switch"):
            actions.append("toggle")
        if e.role == "slider" or ("range" in e.patterns and not e.readonly and e.role != "progressbar"):
            actions.append("set")
        if "date" in e.patterns and "type" not in actions:
            actions.append("type")
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

    def scroll_percent(self, e: Element) -> Optional[float]:
        """How far down an element is scrolled, 0-100, or None if it does not say."""
        if e.handle is None or "scroll" not in e.patterns:
            return None
        try:
            value = e.handle.GetCurrentPropertyValue(UIA.UIA_ScrollVerticalScrollPercentPropertyId)
        except Exception:
            return None
        return float(value) if isinstance(value, (int, float)) and value >= 0 else None

    def scroll_into_view(self, e: Element) -> bool:
        """Scroll whatever contains the element until it is visible."""
        if e.handle is None:
            return False
        return self._pattern(e, UIA.UIA_ScrollItemPatternId, UIA.IUIAutomationScrollItemPattern,
                             lambda p: p.ScrollIntoView())

    def element_at(self, x: int, y: int) -> Optional[dict]:
        """What the application itself reports under a screen point (live hit-testing)."""
        from ctypes import wintypes
        try:
            el = self._uia.ElementFromPoint(wintypes.POINT(int(x), int(y)))
            rect = el.CurrentBoundingRectangle
            return {"role": ROLES.get(el.CurrentControlType, "custom"), "name": (el.CurrentName or "").strip(),
                    "box": (rect.left, rect.top, rect.right, rect.bottom)}
        except Exception:
            return None

    def _set_number(self, e: Element, value: Optional[str]) -> str:
        """Move a slider or spinner to a number, by its range pattern, else its value."""
        try:
            number = float(value or "")
        except ValueError:
            raise ValueError(f'"set" needs a number, got "{value}"') from None
        if e.handle is not None and "range" in e.patterns and self._pattern(
                e, UIA.UIA_RangeValuePatternId, UIA.IUIAutomationRangeValuePattern, lambda p: p.SetValue(number)):
            return "uia.range"
        if e.handle is not None and "value" in e.patterns and self._pattern(
                e, UIA.UIA_ValuePatternId, UIA.IUIAutomationValuePattern, lambda p: p.SetValue(_number(number))):
            if self._number_now(e) == number:
                return "uia.value"
        if e.role == "slider":
            # Keyboard, as a person would: to the start, then one step at a time.
            self._focus(e)
            low = float(e.value_range[0]) if e.value_range else 0.0
            win_input.press("home")
            for _ in range(int(round(number - low))):
                win_input.press("right")
            if self._number_now(e) in (None, number):
                return "keys"
        raise RuntimeError(f"{e.role} \"{e.title}\" cannot be set to {_number(number)}")

    def _number_now(self, e: Element) -> Optional[float]:
        sources = ([UIA.UIA_RangeValueValuePropertyId] if "range" in e.patterns else []) + \
                  ([UIA.UIA_ValueValuePropertyId] if "value" in e.patterns else []) + \
                  [UIA.UIA_LegacyIAccessibleValuePropertyId]
        for pid in sources:
            try:
                value = e.handle.GetCurrentPropertyValue(pid)
                if value not in (None, ""):
                    return float(value)
            except Exception:
                continue
        return None

    def _type_date(self, e: Element, value: str) -> str:
        """A desktop date picker (Win32 SysDateTimePick32): type each field, move right between them."""
        current = e.value or ""
        order = _date_order(current)
        numbers = _parse_date(value, [k for k in order if k != "year"] if order else ["day", "month"])
        if numbers is None:
            raise ValueError(f'"{value}" is not a date; write it as 2024-07-15 or 15/07/2024')
        win_input.focus_window(e.root_hwnd)
        l, t, r, b = e.box
        win_input.click(l + 10, (t + b) // 2)  # clicking the left edge selects the first field
        time.sleep(0.05)
        for n, kind in enumerate(order or ["day", "month", "year"]):
            win_input.type_text(str(numbers[kind]).zfill(4 if kind == "year" else 2))
            if n < 2:
                win_input.press("right")
        time.sleep(0.1)
        try:
            shown = str(e.handle.GetCurrentPropertyValue(UIA.UIA_LegacyIAccessibleValuePropertyId) or "")
        except Exception:
            shown = ""
        digits = lambda t: [int(x) for x in _re_digits(t)]
        if shown and sorted(digits(shown)) != sorted([numbers["year"], numbers["month"], numbers["day"]]):
            raise RuntimeError(f'the date picker shows "{shown}" after typing {value}')
        return "keys"

    def _set_date(self, e: Element, parts: list, value: str) -> str:
        """Fill a date field made of day, month and year parts (browsers, many desktop pickers).

        Each part is focused and its digits typed, as a person would: writing the parts'
        values directly changes what is shown without the application receiving the date."""
        order = [kind for kind, _ in parts]
        numbers = _parse_date(value, order)
        if numbers is None:
            raise ValueError(f'"{value}" is not a date; write it as 2024-07-15 or 15/07/2024')
        win_input.focus_window(e.root_hwnd)
        for kind, part in parts:
            try:
                part.handle.SetFocus()
            except Exception:
                win_input.click(*part.center)
            time.sleep(0.04)
            win_input.type_text(str(numbers[kind]).zfill(4 if kind == "year" else 2))
            time.sleep(0.04)
        for kind, part in parts:  # read the parts back
            try:
                shown = part.handle.GetCurrentPropertyValue(UIA.UIA_RangeValueValuePropertyId)
            except Exception:
                continue
            if isinstance(shown, (int, float)) and int(shown) != numbers[kind]:
                raise RuntimeError(f'the {kind} of "{e.title}" shows {int(shown)} after typing {numbers[kind]}')
        return "uia.parts"

    def _select_many(self, e: Element, value: str) -> str:
        """Choose one or several options of a list: "English | Japanese". The first replaces
        the selection, the rest are added to it."""
        wanted = [v.strip() for v in value.split("|") if v.strip()]
        if not wanted:
            raise ValueError("select needs at least one option")
        # Clear options that are chosen but not wanted: Select() alone does not always do it.
        for child in e.walk():
            if child.role == "listitem" and child.selected and child.name not in wanted and child.handle is not None:
                self._pattern(child, UIA.UIA_SelectionItemPatternId, UIA.IUIAutomationSelectionItemPattern,
                              lambda p: p.RemoveFromSelection())
        if e.framework in LEGACY_FRAMEWORKS:
            return self._ctrl_click_options(e, wanted)
        for n, option in enumerate(wanted):
            condition = self._uia.CreateAndCondition(
                self._uia.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId, 50007),
                self._uia.CreatePropertyCondition(UIA.UIA_NamePropertyId, option))
            item = e.handle.FindFirst(UIA.TreeScope_Descendants, condition)
            if not item:
                raise RuntimeError(f'list "{e.title}" has no option "{option}"')
            call = (lambda p: p.Select()) if n == 0 else (lambda p: p.AddToSelection())
            if not self._pattern(item, UIA.UIA_SelectionItemPatternId, UIA.IUIAutomationSelectionItemPattern, call):
                raise RuntimeError(f'could not select "{option}" in list "{e.title}"')
        if len(wanted) == 1 or sorted(self._selected_names(e)) == sorted(wanted):
            return "uia.select"
        # The list ignored "add to selection" (common in older controls): Ctrl+click instead.
        return self._ctrl_click_options(e, wanted)

    def _ctrl_click_options(self, e: Element, wanted: list[str]) -> str:
        win_input.focus_window(e.root_hwnd)
        for n, option in enumerate(wanted):
            child = next((c for c in e.children if c.name == option), None)
            if child is None:
                raise RuntimeError(f'list "{e.title}" has no option "{option}"')
            if child.offscreen:
                self.scroll_into_view(child)
            win_input.click(*child.center, hold="" if n == 0 else "ctrl")
            time.sleep(0.05)
        chosen = self._selected_names(e)
        if sorted(chosen) != sorted(wanted):
            raise RuntimeError(f'list "{e.title}" has {chosen} chosen, not {wanted}')
        return "mouse"

    def _selected_names(self, e: Element) -> list[str]:
        names = []
        for child in e.children:
            try:
                if child.handle.GetCurrentPropertyValue(UIA.UIA_SelectionItemIsSelectedPropertyId):
                    names.append(child.name)
            except Exception:
                continue
        return names

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

    def type_with_keys(self, e: Element, value: str) -> str:
        """Type into a field with real keystrokes: focus, select all, delete, type."""
        if e.source == "tree" and e.handle is not None:
            self._focus(e)
        else:
            self._mouse_click(e)
        win_input.press("ctrl+a")
        win_input.press("delete")
        if e.role != "document":
            win_input.press("end")
            win_input.press("shift+home")
            win_input.press("delete")
        win_input.type_text(value)
        return "keys"

    def perform(self, e: Element, action: str, value: Optional[str] = None) -> str:
        if not e.enabled:
            raise RuntimeError(f"element [{e.index}] is disabled")
        tree_backed = e.source != "cv" and e.handle is not None
        if action == "type" and e.source == "merged" and "value" not in e.patterns:
            pass  # typed with real keys below

        legacy = e.framework in LEGACY_FRAMEWORKS
        if action in ("select", "click") and legacy and e.role in ("radio", "listitem", "tabitem", "treeitem"):
            # WinForms and Win32 accept UIA selection calls without telling the program: the
            # control looks changed but the application never hears of it. Click instead.
            return self._mouse_click(e)

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

        if action == "type" and tree_backed:
            parts = _date_parts(e)
            if parts:
                return self._set_date(e, parts, value or "")
            if "date" in e.patterns:
                return self._type_date(e, value or "")

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

        if action == "toggle" and e.role == "switch" and tree_backed and "toggle" not in e.patterns:
            return self._mouse_click(e)

        if action == "toggle":
            if tree_backed and self._pattern(e, UIA.UIA_TogglePatternId, UIA.IUIAutomationTogglePattern,
                                             lambda p: p.Toggle()):
                return "uia.toggle"
            return self._mouse_click(e)

        if action == "select" and e.role == "dropdown" and tree_backed:
            return self._select_option(e, value or "")

        if action == "select" and e.role == "list" and tree_backed:
            return self._select_many(e, value or "")

        if action == "set":
            return self._set_number(e, value)

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
            if tree_backed and "scroll" in e.patterns:
                # The element's own scroll control: no mouse, and it scrolls this element even
                # when the pointer would be over something else.
                vertical = UIA.ScrollAmount_LargeDecrement if direction == "up" else UIA.ScrollAmount_LargeIncrement
                steps = max(1, notches // 3)

                def scroll(p):
                    for _ in range(steps):
                        p.Scroll(UIA.ScrollAmount_NoAmount, vertical)

                if self._pattern(e, UIA.UIA_ScrollPatternId, UIA.IUIAutomationScrollPattern, scroll):
                    return "uia.scroll"
            if e.offscreen:
                self.scroll_into_view(e)
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
