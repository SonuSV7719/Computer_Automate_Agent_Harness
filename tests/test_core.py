"""Serialiser, labels, post-conditions, change summary and fingerprints."""
import pytest

from shadow.core import expect as expectation
from shadow.core import fingerprint
from shadow.core.diff import diff
from shadow.core.elements import Element, WindowInfo
from shadow.core.serialize import serialize
from shadow.engine import StaleSnapshot

WIN = WindowInfo(hwnd=1, title="Test", process="t.exe", pid=1, box=(0, 0, 400, 300))


def tree(*children):
    root = Element(role="window", name="Test", box=(0, 0, 400, 300))
    for c in children:
        c.parent = root
        root.children.append(c)
    return [root]


def test_screen_text_shows_label_value_state_and_actions(engine):
    text = engine.see().text
    assert 'WINDOW "Invoice Entry - Acme ERP" (erp.exe) | snapshot: 1' in text
    assert 'input "Invoice No" = ""  actions: type' in text  # label inferred from the text on its left
    assert 'dropdown "Vendor" = "Select..."  actions: select' in text
    assert 'checkbox "Tax inclusive" (unchecked)  actions: click, toggle' in text
    assert 'text "Ready"' in text


def test_label_prefers_same_row_over_text_above():
    above = Element(role="text", name="Section", box=(100, 60, 200, 78))
    left = Element(role="text", name="Email:", box=(10, 100, 90, 120))
    field = Element(role="input", box=(100, 100, 300, 120), actions=["type"], value="")
    _, body, _ = serialize(tree(above, left, field), WIN, 1)
    assert 'input "Email"' in body


def test_unnamed_containers_are_flattened_and_captions_not_repeated():
    pane = Element(role="pane", box=(0, 0, 400, 300))
    button = Element(role="button", name="OK", box=(10, 10, 60, 30), actions=["click"], parent=pane)
    button.children.append(Element(role="text", name="OK", box=(12, 12, 58, 28), parent=button))
    pane.children.append(button)
    _, body, indexed = serialize(tree(pane), WIN, 1)
    assert body == '[0] button "OK"  actions: click'
    assert len(indexed) == 1


def test_offscreen_and_scrollbars_are_hidden_and_counted():
    lst = Element(role="list", name="Rows", box=(0, 0, 100, 100))
    lst.children = [
        Element(role="listitem", name="a", box=(0, 0, 100, 20), actions=["click"], parent=lst),
        Element(role="listitem", name="b", box=(0, 0, 100, 20), offscreen=True, parent=lst),
        Element(role="scrollbar", name="Vertical", box=(90, 0, 100, 100), parent=lst),
    ]
    _, body, _ = serialize(tree(lst), WIN, 1)
    assert "1 more off-screen" in body and '"b"' not in body and "scrollbar" not in body


def test_password_value_is_masked():
    field = Element(role="input", name="Password", box=(0, 0, 100, 20), value="hunter2", masked=True, actions=["type"])
    _, body, _ = serialize(tree(field), WIN, 1)
    assert "hunter2" not in body and "••••" in body


def test_cv_elements_show_confidence():
    icon = Element(role="icon", name="printer", box=(0, 0, 20, 20), source="cv", confidence=0.84, actions=["click"])
    _, body, _ = serialize(tree(icon), WIN, 1)
    assert body == '[0] icon "printer" (cv 0.84)  actions: click'


def test_expectations(engine):
    snap = engine.see()
    assert expectation.check("text:Ready", snap)
    assert expectation.check("gone:Saved INV", snap)
    assert expectation.check("window:invoice entry && value:Vendor=Select...", snap)
    assert not expectation.check("text:Saved INV", snap)
    with pytest.raises(ValueError):
        expectation.parse("nonsense")


def test_action_reports_changes_and_checks_expectation(engine):
    snap = engine.see()
    idx = next(e.index for e in snap.elements if e.role == "input" and e.label == "Invoice No")
    result = engine.act("type", idx, "INV-7", snapshot_id=snap.id, expect="value:Invoice No=INV-7")
    assert result["ok"] and result["expect"]["met"]
    assert any('= "INV-7"' in line for line in result["changes"])


def test_failed_expectation_is_reported_not_raised(engine):
    snap = engine.see()
    idx = next(e.index for e in snap.elements if e.name == "Save")
    result = engine.act("click", idx, expect="text:Saved INV", timeout=0.2)
    assert not result["ok"] and result["expect"]["met"] is False


def test_stale_snapshot_is_refused(engine, driver):
    first = engine.see()
    engine.see()
    with pytest.raises(StaleSnapshot):
        engine.act("click", 0, snapshot_id=first.id)
    assert driver.actions == []


def test_diff_lists_added_changed_removed(engine, driver):
    before = engine.see()
    driver.status = "Saved INV-1"
    driver.saved.append("INV-1 | Contoso Supplies | 10 | tax excl.")
    after = engine.see()
    lines = diff(before, after)
    assert any(line.startswith("+ ") and "INV-1 | Contoso" in line for line in lines)
    assert any(line.startswith("+ ") and "Saved INV-1" in line for line in lines)
    assert any(line.startswith("- ") and '"Ready"' in line for line in lines)


def test_fingerprint_resolves_same_element_after_layout_shift(engine, driver):
    snap = engine.see()
    save = next(e for e in snap.elements if e.name == "Save")
    fp = fingerprint.make(save, snap)
    assert fp["automation_id"] == "btnSave" and fp["window"]["process"] == "erp.exe"
    driver.status = "Something else"
    found, how = fingerprint.resolve(fp, engine.see())
    assert found is not None and found.name == "Save" and how == "automation_id"


def test_fingerprint_falls_back_to_role_and_name_when_id_changes(engine):
    snap = engine.see()
    fp = fingerprint.make(next(e for e in snap.elements if e.name == "Clear"), snap)
    fp["automation_id"] = "oldClearButton"
    found, how = fingerprint.resolve(fp, engine.see())
    assert found.name == "Clear" and how == "role+name"


def test_fingerprint_does_not_guess_when_element_is_gone(engine, driver):
    snap = engine.see()
    fp = fingerprint.make(next(e for e in snap.elements if e.name == "Save"), snap)
    driver.variant = "v2"  # Save became Submit with a new id
    found, how = fingerprint.resolve(fp, engine.see())
    assert found is None and how == "not found"


def test_ambiguous_match_is_rejected():
    a = Element(role="button", name="Delete", box=(0, 0, 50, 20), actions=["click"])
    b = Element(role="button", name="Delete", box=(0, 40, 50, 60), actions=["click"])
    roots = tree(a, b)
    from shadow.core.elements import Snapshot
    snap = Snapshot(id=1, window=WIN, roots=roots)
    snap.header, snap.body, snap.elements = serialize(roots, WIN, 1)
    fp = fingerprint.make(a, snap)
    found, _ = fingerprint.resolve(fp, snap)
    assert found is None  # two identical buttons: refuse rather than click the wrong one


def test_volatile_automation_ids_are_dropped():
    assert fingerprint.stable_id("btnSave") == "btnSave"
    assert fingerprint.stable_id("12345") == ""
    assert fingerprint.stable_id("3f2504e0-4f89-11d3-9a0c-0305e82c3301") == ""
