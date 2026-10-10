"""The Linux driver's command building, with xdotool and wmctrl replaced by a recorder.

The real thing is exercised inside the sandbox container; this keeps its logic honest
on any OS.
"""
import pytest

from shadow.core.elements import Element
from shadow.cv.merge import needs_cv
from shadow.drivers import linux


@pytest.fixture
def driver(monkeypatch):
    calls = []
    replies = {
        ("wmctrl", "-lpG"): "0x01200003  0 4242 140  140  560  430  sandbox Legacy Invoice Entry\n"
                            "0x00a00001 -1 0    0    0    1024 720  sandbox Desktop\n",
        ("xdotool", "getwindowgeometry"): "WINDOW=18874371\nX=140\nY=140\nWIDTH=560\nHEIGHT=430\nSCREEN=0\n",
        ("xdotool", "getwindowname"): "Legacy Invoice Entry\n",
        ("xdotool", "getwindowpid"): "4242\n",
        ("xdotool", "getactivewindow"): "18874371\n",
    }

    def fake_run(*args, check=True):
        calls.append(args)
        return replies.get(args[:2], "")

    monkeypatch.setattr(linux, "_run", fake_run)
    d = linux.LinuxDriver()
    d.calls = calls
    return d


def test_windows_are_listed_without_the_desktop(driver):
    windows = driver.list_windows()
    assert [w.title for w in windows] == ["Legacy Invoice Entry"]
    assert windows[0].hwnd == 0x01200003 and windows[0].box == (140, 140, 700, 570)


def test_a_window_is_one_pane_of_pixels_for_vision(driver):
    roots = driver.tree(18874371)
    assert roots[0].role == "window" and roots[0].name == "Legacy Invoice Entry"
    assert [c.role for c in roots[0].children] == ["pane"] and roots[0].children[0].box == (140, 140, 700, 570)
    assert needs_cv(roots)


def test_typing_clicks_the_field_clears_it_and_types(driver):
    field = Element(role="input", box=(200, 200, 400, 230), source="cv", root_hwnd=18874371)
    driver.calls.clear()
    assert driver.perform(field, "type", "INV-1") == "keys"
    commands = [c for c in driver.calls if c[1] != "getactivewindow"]
    assert commands[0] == ("xdotool", "mousemove", "--sync", "300", "215")
    assert commands[1][:2] == ("xdotool", "click")
    assert commands[2] == ("xdotool", "key", "--clearmodifiers", "ctrl+a", "Delete", "End", "shift+Home", "Delete")
    assert commands[3] == ("xdotool", "type", "--clearmodifiers", "--delay", "8", "--", "INV-1")


def test_keys_are_translated_to_x11_names(driver):
    driver.calls.clear()
    driver.press("ctrl+enter")
    driver.press("shift+pgdn")
    keys = [c[-1] for c in driver.calls if c[1] == "key"]
    assert keys == ["ctrl+Return", "shift+Next"]


def test_drag_presses_moves_through_each_point_and_releases(driver):
    region = Element(role="region", box=(100, 100, 600, 400), root_hwnd=18874371)
    driver.calls.clear()
    driver.perform(region, "drag", "0,0 500,500 1000,1000")
    moves = [c for c in driver.calls if c[1] in ("mousemove", "mousedown", "mouseup")]
    assert [c[1] for c in moves] == ["mousemove", "mousedown", "mousemove", "mousemove", "mouseup"]
    assert moves[0][-2:] == ("100", "100") and moves[2][-2:] == ("350", "250") and moves[3][-2:] == ("600", "400")
    with pytest.raises(ValueError):
        driver.perform(region, "drag", "10,10")
    with pytest.raises(ValueError):
        driver.perform(region, "click_at", "1500,10")
