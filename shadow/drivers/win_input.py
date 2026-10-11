"""Real mouse and keyboard input, window listing and focus on Windows (ctypes only)."""
from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes

from ..core.elements import WindowInfo

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")

ULONG_PTR = ctypes.c_size_t

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0001, 0x0002, 0x0004
# Navigation keys must be sent as extended keys, or Shift+Home is read as a numpad key.
EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E}
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_WHEEL = 0x0800


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
user32.VkKeyScanW.restype = ctypes.c_short
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]

_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]

GW_OWNER = 4
MENU_CLASS = "#32768"

VK = {
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "backspace": 0x08,
    "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23, "left": 0x25, "up": 0x26,
    "right": 0x27, "down": 0x28, "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22,
    "space": 0x20, "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B,
}
VK.update({f"f{n}": 0x6F + n for n in range(1, 13)})


def set_dpi_aware() -> None:
    """Per-monitor DPI awareness, so UI tree rectangles and screenshot pixels agree."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def _send(*inputs: INPUT) -> None:
    array = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))


def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    return INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags))


def _mouse(flags: int, data: int = 0) -> INPUT:
    return INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(dwFlags=flags, mouseData=data & 0xFFFFFFFF))


def click(x: int, y: int, button: str = "left", double: bool = False, hold: str = "") -> None:
    """Click at a point; `hold` keeps keys down meanwhile, e.g. "ctrl" to add to a selection."""
    held = [VK[k] for k in hold.lower().split("+") if k] if hold else []
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.02)
    if held:
        _send(*[_key(vk=c) for c in held])
    down, up = ((MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP) if button == "right"
                else (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP))
    _send(_mouse(down), _mouse(up))
    if double:
        _send(_mouse(down), _mouse(up))
    if held:
        _send(*[_key(vk=c, flags=KEYEVENTF_KEYUP) for c in reversed(held)])


def hover(x: int, y: int) -> None:
    """Rest the pointer on a point, so menus open and tooltips appear."""
    MOUSEEVENTF_MOVE = 0x0001
    user32.SetCursorPos(int(x) - 3, int(y) - 3)
    _send(_mouse(MOUSEEVENTF_MOVE))
    time.sleep(0.03)
    user32.SetCursorPos(int(x), int(y))
    _send(_mouse(MOUSEEVENTF_MOVE))


def drag(points: list[tuple[int, int]], steps: int = 12) -> None:
    """Press at the first point, move through the others, release at the last."""
    MOUSEEVENTF_MOVE = 0x0001
    user32.SetCursorPos(*points[0])
    time.sleep(0.03)
    _send(_mouse(MOUSEEVENTF_LEFTDOWN))
    time.sleep(0.03)
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        for i in range(1, steps + 1):
            user32.SetCursorPos(int(x0 + (x1 - x0) * i / steps), int(y0 + (y1 - y0) * i / steps))
            _send(_mouse(MOUSEEVENTF_MOVE))  # a zero-distance move, so the app receives a move event
            time.sleep(0.006)
    time.sleep(0.03)
    _send(_mouse(MOUSEEVENTF_LEFTUP))


def scroll(x: int, y: int, notches: int) -> None:
    """Positive notches scroll up, negative scroll down."""
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.02)
    _send(_mouse(MOUSEEVENTF_WHEEL, 120 * notches))


def type_text(text: str) -> None:
    for ch in text.replace("\r\n", "\n"):
        if ch == "\n":
            _send(_key(vk=VK["enter"]), _key(vk=VK["enter"], flags=KEYEVENTF_KEYUP))
        elif ch == "\t":
            _send(_key(vk=VK["tab"]), _key(vk=VK["tab"], flags=KEYEVENTF_KEYUP))
        else:
            units = ch.encode("utf-16-le")
            for i in range(0, len(units), 2):
                code = int.from_bytes(units[i:i + 2], "little")
                _send(_key(scan=code, flags=KEYEVENTF_UNICODE),
                      _key(scan=code, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))


def press(combo: str) -> None:
    """Press a key or combination such as 'enter', 'ctrl+s', 'alt+f4'."""
    codes = []
    for part in combo.lower().replace(" ", "").split("+"):
        if part in VK:
            codes.append(VK[part])
        elif len(part) == 1:
            scan = user32.VkKeyScanW(part)
            if scan == -1:
                raise ValueError(f"unknown key: {part}")
            codes.append(scan & 0xFF)
        else:
            raise ValueError(f"unknown key: {part}")
    def flags(code: int) -> int:
        return KEYEVENTF_EXTENDEDKEY if code in EXTENDED else 0

    _send(*[_key(vk=c, flags=flags(c)) for c in codes],
          *[_key(vk=c, flags=flags(c) | KEYEVENTF_KEYUP) for c in reversed(codes)])


def _text(fn, hwnd, size: int = 512) -> str:
    buf = ctypes.create_unicode_buffer(size)
    fn(hwnd, buf, size)
    return buf.value


def _cloaked(hwnd) -> bool:
    value = wintypes.DWORD(0)
    dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(value), ctypes.sizeof(value))
    return bool(value.value)


def _process_name(pid: int) -> str:
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(handle)


def window_info(hwnd: int) -> WindowInfo:
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return WindowInfo(
        hwnd=int(hwnd), title=_text(user32.GetWindowTextW, hwnd), process=_process_name(pid.value),
        pid=pid.value, box=(rect.left, rect.top, rect.right, rect.bottom),
        class_name=_text(user32.GetClassNameW, hwnd),
    )


def _top_level() -> list[int]:
    found: list[int] = []

    @_WNDENUMPROC
    def callback(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd) and not _cloaked(hwnd):
            found.append(int(hwnd))
        return True

    user32.EnumWindows(callback, 0)
    return found  # z-order, topmost first


def list_windows() -> list[WindowInfo]:
    out = []
    for hwnd in _top_level():
        info = window_info(hwnd)
        l, t, r, b = info.box
        if info.title and r - l > 1 and b - t > 1 and info.class_name not in ("Progman", "WorkerW"):
            out.append(info)
    return out


def _root_owner(hwnd: int) -> int:
    seen = set()
    while hwnd and hwnd not in seen:
        seen.add(hwnd)
        owner = user32.GetWindow(hwnd, GW_OWNER)
        if not owner:
            break
        hwnd = int(owner)
    return hwnd


def popups_of(hwnd: int) -> list[int]:
    """Dialogs and open menus that belong to this window, topmost first."""
    pid = window_info(hwnd).pid
    out = []
    for other in _top_level():
        if other == hwnd:
            continue
        info = window_info(other)
        l, t, r, b = info.box
        if info.pid != pid or r - l <= 1 or b - t <= 1:
            continue
        if info.class_name == MENU_CLASS or _root_owner(other) == hwnd:
            out.append(other)
    return out


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
user32.GetWindowDC.argtypes = [wintypes.HWND]
user32.GetWindowDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                            ctypes.c_void_p, wintypes.UINT]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]


def capture_window(hwnd: int):
    """The window's own pixels as (width, height, BGRX bytes), even when other windows cover it.
    Returns None when the window cannot render itself this way.

    A window that is not DPI-aware renders at its own smaller size and Windows stretches it,
    so we read it in the window's coordinate space; the caller scales it to screen pixels."""
    if user32.IsIconic(hwnd):
        return None
    previous_context = None
    try:
        user32.GetWindowDpiAwarenessContext.argtypes = [wintypes.HWND]
        user32.GetWindowDpiAwarenessContext.restype = ctypes.c_void_p
        user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        previous_context = user32.SetThreadDpiAwarenessContext(user32.GetWindowDpiAwarenessContext(hwnd))
    except (AttributeError, OSError):
        pass  # older Windows: everything shares one coordinate space
    try:
        return _print_window(hwnd)
    finally:
        if previous_context:
            user32.SetThreadDpiAwarenessContext(previous_context)


def _print_window(hwnd: int):
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None
    window_dc = user32.GetWindowDC(hwnd)
    if not window_dc:
        return None
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, w, h)
    previous = gdi32.SelectObject(memory_dc, bitmap)
    try:
        if not user32.PrintWindow(hwnd, memory_dc, 2):  # 2 = render full content, including GPU surfaces
            return None
        header = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=w, biHeight=-h,
                                  biPlanes=1, biBitCount=32, biCompression=0)
        buffer = ctypes.create_string_buffer(w * h * 4)
        if not gdi32.GetDIBits(memory_dc, bitmap, 0, h, buffer, ctypes.byref(header), 0):
            return None
        return w, h, buffer.raw
    finally:
        gdi32.SelectObject(memory_dc, previous)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, window_dc)


def child_windows(hwnd: int, class_name: str) -> list[int]:
    found: list[int] = []

    @_WNDENUMPROC
    def callback(child, _lparam):
        if _text(user32.GetClassNameW, child) == class_name:
            found.append(int(child))
        return True

    user32.EnumChildWindows.argtypes = [wintypes.HWND, _WNDENUMPROC, wintypes.LPARAM]
    user32.EnumChildWindows(hwnd, callback, 0)
    return found


def wake_accessibility(hwnd: int) -> bool:
    """Ask a Chromium window (Chrome, Edge, Electron apps) for its accessibility object.

    Chromium only builds the accessibility tree of a web page once it sees an assistive
    tool asking for it; until then the page is one blank pane. Requesting the client
    object of each page surface is the signal screen readers use."""
    import comtypes
    from comtypes.automation import IDispatch  # noqa: F401  (loads the type library)

    oleacc = ctypes.WinDLL("oleacc")
    iid = comtypes.GUID("{618736E0-3C3D-11CF-810C-00AA00389B71}")  # IID_IAccessible
    woke = False
    for child in child_windows(hwnd, "Chrome_RenderWidgetHostHWND"):
        pointer = ctypes.c_void_p()
        if oleacc.AccessibleObjectFromWindow(wintypes.HWND(child), wintypes.DWORD(0xFFFFFFFC),
                                             ctypes.byref(iid), ctypes.byref(pointer)) == 0:
            woke = True
            if pointer.value:
                ctypes.cast(pointer, ctypes.POINTER(comtypes.IUnknown)).Release()
    return woke


def foreground() -> int:
    return int(user32.GetForegroundWindow() or 0)


def exists(hwnd: int) -> bool:
    return bool(user32.IsWindow(hwnd))


def focus_window(hwnd: int) -> bool:
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)
    if foreground() == hwnd:
        return True
    current = kernel32.GetCurrentThreadId()
    fg_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
    attached = bool(fg_thread and fg_thread != current and user32.AttachThreadInput(current, fg_thread, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(current, fg_thread, False)
    if foreground() != hwnd:  # Windows refused; an Alt tap lifts the foreground lock
        _send(_key(vk=VK["alt"]), _key(vk=VK["alt"], flags=KEYEVENTF_KEYUP))
        user32.SetForegroundWindow(hwnd)
    time.sleep(0.05)
    return foreground() == hwnd
