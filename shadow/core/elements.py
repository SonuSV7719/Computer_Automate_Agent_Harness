"""The merged element model shared by every driver, the serialiser and replay."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

Box = tuple[int, int, int, int]  # left, top, right, bottom in physical screen pixels


@dataclass(eq=False)
class Element:
    role: str
    name: str = ""
    box: Box = (0, 0, 0, 0)
    source: str = "tree"  # tree | cv | merged
    index: int = -1  # valid for one snapshot only
    label: str = ""  # inferred from nearby text when the element has no name
    value: Optional[str] = None
    enabled: bool = True
    focused: bool = False
    offscreen: bool = False
    checked: Optional[str] = None  # checked | unchecked | mixed
    expanded: Optional[bool] = None
    selected: Optional[bool] = None
    required: bool = False
    masked: bool = False
    readonly: bool = False
    actions: list[str] = field(default_factory=list)
    patterns: set[str] = field(default_factory=set)
    automation_id: str = ""
    class_name: str = ""
    framework: str = ""
    confidence: Optional[float] = None  # CV elements only
    value_range: Optional[tuple] = None  # (minimum, maximum) of a slider or spinner
    item_count: int = 0  # set on a container of repeated items (cards, tiles, results)
    cv_confirmed: bool = False
    parent: Optional["Element"] = field(default=None, repr=False)
    children: list["Element"] = field(default_factory=list, repr=False)
    handle: Any = field(default=None, repr=False)  # driver-specific live reference
    root_hwnd: int = 0

    @property
    def title(self) -> str:
        return self.name or self.label

    @property
    def center(self) -> tuple[int, int]:
        l, t, r, b = self.box
        return (l + r) // 2, (t + b) // 2

    @property
    def area(self) -> int:
        l, t, r, b = self.box
        return max(0, r - l) * max(0, b - t)

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "source": self.source,
            "role": self.role,
            "name": self.name,
            "label": self.label,
            "value": self.value,
            "box": list(self.box),
            "enabled": self.enabled,
            "focused": self.focused,
            "checked": self.checked,
            "expanded": self.expanded,
            "selected": self.selected,
            "required": self.required,
            "masked": self.masked,
            "readonly": self.readonly,
            "actions": self.actions,
            "automation_id": self.automation_id,
            "class_name": self.class_name,
            "framework": self.framework,
            "confidence": self.confidence,
        }


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    process: str
    pid: int
    box: Box
    class_name: str = ""

    def to_dict(self) -> dict:
        return {
            "hwnd": self.hwnd,
            "title": self.title,
            "process": self.process,
            "pid": self.pid,
            "box": list(self.box),
            "class_name": self.class_name,
        }


@dataclass
class Snapshot:
    id: int
    window: WindowInfo
    roots: list[Element]  # main window first, then its popups and menus
    elements: list[Element] = field(default_factory=list)  # position == index
    header: str = ""
    body: str = ""
    ms: float = 0.0

    @property
    def text(self) -> str:
        return f"{self.header}\n{self.body}"

    def get(self, index: int) -> Element:
        if not 0 <= index < len(self.elements):
            raise KeyError(f"no element [{index}] in snapshot {self.id}")
        return self.elements[index]
