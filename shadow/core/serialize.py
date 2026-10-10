"""Turn a merged element tree into the indexed text an LLM reads.

One line per element. Indentation shows structure. Every interactive element
carries its label, current value, state and the actions it accepts, so the
model never needs the pixels to understand a tree-backed window.
"""
from __future__ import annotations

from .elements import Element, WindowInfo

SKIP_ROLES = {"scrollbar", "thumb", "separator", "tooltip"}
# Children of these add nothing the parent line does not already say.
LEAF_ROLES = {
    "button", "checkbox", "radio", "link", "input", "dropdown", "slider",
    "spinner", "splitbutton", "progressbar", "image", "text",
}
STRUCT_ALWAYS = {
    "window", "dialog", "menu", "menubar", "toolbar", "tab", "list", "tree",
    "table", "statusbar", "region",
}
# Shown only when named or interactive; otherwise their children are promoted.
FLATTEN = {"pane", "group", "custom", "document", "header", "appbar", "titlebar"}
NEEDS_LABEL = {"input", "dropdown", "spinner", "slider", "checkbox", "radio", "document"}
COUNT_HIDDEN = {"list", "tree", "table"}

MAX_TEXT = 80


def _clip(text: str, limit: int = MAX_TEXT) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "⏎")
    if len(text) > limit:
        return f"{text[:limit]}… (+{len(text) - limit} chars)"
    return text


def visible(e: Element) -> bool:
    return not e.offscreen and e.area > 0 and e.role not in SKIP_ROLES


def is_shown(e: Element) -> bool:
    if e.actions or e.role in STRUCT_ALWAYS:
        return True
    return bool(e.title)


def is_indexed(e: Element) -> bool:
    return bool(e.actions) or e.role not in (STRUCT_ALWAYS | FLATTEN)


def infer_labels(roots: list[Element]) -> None:
    """Give unnamed inputs the nearest text to their left or above."""
    texts, targets = [], []
    for root in roots:
        for e in root.walk():
            if not visible(e):
                continue
            if e.role == "text" and e.name:
                texts.append(e)
            elif e.role in NEEDS_LABEL and not e.name and not e.label:
                targets.append(e)
    for e in targets:
        l, t, r, b = e.box
        best, best_cost = None, None
        for tx in texts:
            tl, tt, tr, tb = tx.box
            v_overlap = min(b, tb) - max(t, tt)
            h_overlap = min(r, tr) - max(l, tl)
            cost = None
            if tr <= l + 4 and v_overlap > 0.5 * min(b - t, tb - tt):
                gap = l - tr
                if gap < 300:
                    cost = gap
            elif tb <= t + 4 and h_overlap > 0:
                gap = t - tb
                if gap < 60:
                    cost = gap + 40  # prefer a label on the same row
            if cost is not None and (best_cost is None or cost < best_cost):
                best, best_cost = tx, cost
        if best is not None:
            e.label = best.name.rstrip(":* ").strip()


def describe(e: Element, with_index: bool = True) -> str:
    """The single line for one element. Without the index it is stable across snapshots."""
    parts = []
    if with_index and e.index >= 0:
        parts.append(f"[{e.index}]")
    parts.append(e.role)
    if e.title:
        parts.append(f'"{_clip(e.title)}"')
    if e.value is not None and e.role not in ("checkbox", "radio"):
        parts.append('= ••••' if e.masked else f'= "{_clip(e.value)}"')

    flags = []
    if e.checked:
        flags.append(e.checked)
    if e.source == "merged" and e.confidence is not None:
        flags.append(f"read by ocr {e.confidence:.2f}")
    if e.selected:
        flags.append("selected")
    if e.expanded is not None:
        flags.append("expanded" if e.expanded else "collapsed")
    if not e.enabled:
        flags.append("disabled")
    if e.focused:
        flags.append("focused")
    if e.required:
        flags.append("required")
    if e.readonly and e.role in ("input", "document", "dropdown"):
        flags.append("read-only")
    if e.masked:
        flags.append("masked")
    if e.role in COUNT_HIDDEN:
        hidden = sum(1 for c in e.children if c.offscreen)
        if hidden:
            flags.append(f"{hidden} more off-screen")
    if e.role == "dropdown":
        options = [c.name for c in e.walk() if c.role == "listitem" and c.name]
        if options:
            shown = " | ".join(_clip(o, 30) for o in options[:12])
            flags.append(f"options: {shown}" + (f" | +{len(options) - 12} more" if len(options) > 12 else ""))
    if e.role == "region":
        flags.append("no tree, read by CV")
    if e.source == "cv":
        flags.append(f"cv {e.confidence:.2f}" if e.confidence is not None else "cv")
    if flags:
        parts.append(f"({', '.join(flags)})")

    line = " ".join(parts)
    if e.actions:
        line += f"  actions: {', '.join(e.actions)}"
    return line


def serialize(roots: list[Element], window: WindowInfo, snapshot_id: int, ms: float = 0.0):
    """Assign indexes and return (header, body, indexed elements)."""
    infer_labels(roots)
    indexed: list[Element] = []
    lines: list[str] = []

    def emit(e: Element, depth: int) -> None:
        if not visible(e):
            return
        parent = e.parent
        if e.role == "text" and parent is not None and parent.title == e.name and is_shown(parent):
            return  # a button's own caption
        child_depth = depth
        if is_shown(e):
            if is_indexed(e):
                e.index = len(indexed)
                indexed.append(e)
            lines.append("  " * depth + describe(e))
            child_depth = depth + 1
        if e.role in LEAF_ROLES:
            return
        for child in e.children:
            emit(child, child_depth)

    main = roots[0] if roots else None
    for root in roots[1:]:  # popups and open menus block the main window; show them first
        lines.append(f'POPUP "{_clip(root.title)}"' if root.title else "POPUP")
        for child in root.children:
            emit(child, 1)
    if main is not None:
        for child in main.children:
            emit(child, 0)

    focused = next((e for e in indexed if e.focused), None)
    dialog = None
    for root in roots:
        for e in root.walk():
            if e.role == "dialog" and visible(e):
                dialog = e
                break
        if dialog:
            break
    if dialog is None and len(roots) > 1:
        dialog = roots[1]
    header = (
        f'WINDOW "{_clip(window.title)}" ({window.process}) | snapshot: {snapshot_id}'
        f" | focused: {'[' + str(focused.index) + ']' if focused else 'none'}"
        f" | dialog: {(chr(34) + _clip(dialog.title) + chr(34)) if dialog else 'none'}"
        f" | {len(indexed)} elements, {ms:.0f} ms"
    )
    return header, "\n".join(lines), indexed
