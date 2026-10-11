"""Kill switch, application allowlist, and passwords never being recorded."""
import pytest

from conftest import record_invoice
from fake import FakeInvoiceDriver

from shadow.core.elements import Element
from shadow.core.skill import compile_trace, render_code
from shadow.engine import Engine, Halted, NotAllowed
from shadow.runner import run_skill


def test_halt_blocks_every_action_until_resumed(engine, driver):
    snap = engine.see()
    engine.halt()
    with pytest.raises(Halted):
        engine.act("click", 0, snapshot_id=snap.id)
    with pytest.raises(Halted):
        engine.act("key", value="enter")
    assert driver.actions == []
    engine.resume()
    engine.see()
    assert engine.act("key", value="enter")["ok"]


def test_a_run_stopped_midway_ends_as_stopped_and_does_not_heal(engine, driver):
    skill = compile_trace(record_invoice(engine), "enter invoice")
    healer_calls = []
    original = driver.perform

    def perform_then_stop(element, action, value=None):
        route = original(element, action, value)
        if element.handle == "vendor":
            engine.halt()  # the user hits the kill switch after the second step
        return route

    driver.perform = perform_then_stop
    result = run_skill(skill, render_code(skill), lambda fn: fn(engine), {"invoice_no": "INV-STOP"},
                       lambda packet: healer_calls.append(packet) or {"decision": "escalate"})
    assert result["status"] == "stopped" and result["steps_done"] == 2
    assert healer_calls == [] and not any(row.startswith("INV-STOP") for row in driver.saved)


def test_allowlist_refuses_other_applications(monkeypatch, driver):
    monkeypatch.setenv("SHADOW_ALLOW", "notepad.exe, Calc.exe")
    engine = Engine(driver)  # the fake window belongs to erp.exe
    with pytest.raises(NotAllowed, match="erp.exe is not in the allowlist"):
        engine.see()
    with pytest.raises(NotAllowed):
        engine.set_target("Invoice")
    monkeypatch.setenv("SHADOW_ALLOW", "ERP.EXE")
    assert Engine(driver).see().window.process == "erp.exe"


def test_a_typed_password_is_never_recorded():
    class LoginDriver(FakeInvoiceDriver):
        def tree(self, hwnd):
            roots = super().tree(hwnd)
            roots[0].children.append(Element(
                role="input", name="Password", box=(240, 60, 440, 80), parent=roots[0], handle="amount",
                value="hunter2", masked=True, actions=["type"], automation_id="txtPassword", root_hwnd=hwnd))
            return roots

    engine = Engine(LoginDriver())
    snap = engine.see()
    assert "hunter2" not in snap.text
    index = next(e.index for e in snap.elements if e.name == "Password")
    engine.record_start()
    engine.act("type", index, "s3cret-value")
    trace = engine.record_stop()
    code = render_code(compile_trace(trace, "login"))
    assert "s3cret-value" not in str(trace) and "s3cret-value" not in code
    assert "def run(h, password=''):" in code  # asked for at run time instead


def test_a_canvas_position_under_a_floating_panel_is_refused():
    """Excalidraw shows a properties panel over the left of its canvas once a tool is picked."""
    from shadow.core.elements import WindowInfo

    class Board:
        def __init__(self):
            self.performed = []

        def list_windows(self):
            return [self.window_info(1)]

        def foreground(self):
            return 1

        def exists(self, hwnd):
            return True

        def window_info(self, hwnd):
            return WindowInfo(hwnd=1, title="Board", process="chrome.exe", pid=1, box=(0, 0, 1000, 800))

        def tree(self, hwnd):
            root = Element(role="window", name="Board", box=(0, 0, 1000, 800), root_hwnd=1)
            doc = Element(role="document", name="Board", box=(0, 0, 1000, 800), parent=root, root_hwnd=1)
            canvas = Element(role="region", box=(0, 0, 1000, 800), source="merged", actions=["click_at", "drag"],
                             parent=doc, root_hwnd=1)
            panel = Element(role="button", name="Stroke colour", box=(20, 100, 220, 500), actions=["click"],
                            parent=doc, root_hwnd=1)
            doc.children = [canvas, panel]
            root.children = [doc]
            return [root]

        def perform(self, element, action, value=None):
            self.performed.append((action, value))
            return "mouse"

    board = Board()
    engine = Engine(board, cv=False)
    snap = engine.see()
    region = next(e.index for e in snap.elements if e.role == "region")
    with pytest.raises(ValueError, match='position 150,250 is covered by button "Stroke colour"'):
        engine.act("drag", region, "150,250 350,420", snapshot_id=snap.id)
    with pytest.raises(ValueError, match="covered by"):
        engine.act("click_at", region, "100,300", snapshot_id=snap.id)
    assert board.performed == []
    assert engine.act("drag", region, "400,250 600,420", snapshot_id=snap.id, settle=False)["ok"]
    assert board.performed == [("drag", "400,250 600,420")]
