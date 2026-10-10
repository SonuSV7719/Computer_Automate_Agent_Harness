"""The driver interface every platform implements."""
from __future__ import annotations

from typing import Optional, Protocol

from ..core.elements import Box, Element, WindowInfo


class Driver(Protocol):
    def list_windows(self) -> list[WindowInfo]: ...

    def foreground(self) -> int: ...

    def window_info(self, hwnd: int) -> WindowInfo: ...

    def tree(self, hwnd: int) -> list[Element]:
        """Element trees for the window and its popups. The main window is first."""

    def perform(self, element: Element, action: str, value: Optional[str] = None) -> str:
        """Run one action. Returns the route used, e.g. 'uia.invoke' or 'mouse'."""

    def press(self, combo: str, hwnd: int = 0) -> None: ...

    def capture(self, box: Box):
        """A PIL image of the screen region."""
