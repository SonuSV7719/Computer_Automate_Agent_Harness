"""Compile a recorded trace, replay it with no LLM, and heal it when the UI changes."""
import re

import pytest

from conftest import record_invoice
from fake import FakeInvoiceDriver

from shadow.core.skill import SkillStore, compile_trace, render_code
from shadow.engine import Engine
from shadow.runner import run_skill


def direct(engine):
    return lambda fn: fn(engine)


def compiled(engine):
    skill = compile_trace(record_invoice(engine), "Enter invoice", "Enter one invoice into Acme ERP.")
    return skill, render_code(skill)


def find_healer(calls):
    """Stands in for the LLM: picks the element whose line matches the step's intent."""
    wanted = {"Enter the invoice number": r'\[(\d+)\] input "Invoice number"',
              "Save the invoice": r'\[(\d+)\] button "Submit"'}

    def heal(packet):
        calls.append(packet)
        match = re.search(wanted.get(packet["intent"], r"$^"), packet["screen"])
        if not match:
            return {"decision": "escalate", "reason": "cannot find it", "llm": True}
        return {"decision": "retarget", "index": int(match.group(1)), "reason": "control was renamed", "llm": True}

    return heal


def test_compile_names_elements_and_parameters(engine):
    skill, code = compiled(engine)
    assert list(skill.params) == ["invoice_no", "vendor", "amount"]
    assert skill.params["invoice_no"] == "INV-1042"
    assert set(skill.locators) == {"inp_invoice_no", "dd_vendor", "inp_amount", "btn_save"}
    assert "def run(h, invoice_no='INV-1042', vendor='Contoso Supplies', amount='1200'):" in code
    assert 'h.type(el("inp_invoice_no"), invoice_no)' in code
    assert "h.click(el(\"btn_save\"), expect=f'text:Saved {invoice_no}')" in code
    assert "# Save the invoice" in code
    compile(code, "skill.py", "exec")  # the generated file is valid Python


def test_replay_uses_no_llm_and_new_parameters(engine, driver):
    skill, code = compiled(engine)
    result = run_skill(skill, code, direct(engine),
                       {"invoice_no": "INV-2000", "vendor": "Fabrikam Tools", "amount": "75"})
    assert result["ok"] and result["llm_calls"] == 0 and result["steps_done"] == 4
    assert driver.saved[-1] == "INV-2000 | Fabrikam Tools | 75 | tax excl."
    assert [s["locator"] for s in result["steps"]] == ["automation_id"] * 4
    assert result["healed"] is None


def test_unknown_parameter_is_rejected(engine):
    skill, code = compiled(engine)
    with pytest.raises(ValueError):
        run_skill(skill, code, direct(engine), {"invoice": "x"})


def test_ui_change_without_healer_stops_and_asks(engine, driver):
    skill, code = compiled(engine)
    saved_before = len(driver.saved)
    driver.variant = "v2"
    result = run_skill(skill, code, direct(engine), {"invoice_no": "INV-3"})
    assert not result["ok"] and result["status"] == "needs_human"
    assert result["packet"]["failure"] == "locator" and result["packet"]["step"] == 0
    assert len(driver.saved) == saved_before  # nothing was entered in the wrong place


def test_ui_change_is_healed_then_replays_clean(engine, driver, tmp_path):
    store = SkillStore(tmp_path)
    skill, code = compiled(engine)
    store.save(skill)
    driver.variant = "v2"

    calls = []
    result = run_skill(skill, code, direct(engine), {"invoice_no": "INV-4", "amount": "10"}, find_healer(calls))
    assert result["ok"], result["error"]
    assert result["llm_calls"] == 2 and len(calls) == 2  # one per renamed control, not per step
    assert driver.saved[-1].startswith("INV-4 | Contoso Supplies | 10")
    assert [p["after"] for p in result["patches"]] == ['input "Invoice number"', 'button "Submit"']

    healed = result["healed"]
    store.save(healed)
    assert healed.version == 2 and store.versions(skill.name) == [1, 2]
    assert [h["kind"] for h in healed.history] == ["retarget", "retarget"]

    again = run_skill(healed, store.code(skill.name), direct(engine), {"invoice_no": "INV-5", "amount": "11"})
    assert again["ok"] and again["llm_calls"] == 0 and again["patches"] == []
    assert driver.saved[-1].startswith("INV-5")

    rolled = store.rollback(skill.name, 1)
    assert rolled.version == 1 and store.current(skill.name) == 1


def test_business_exception_is_escalated_not_healed(engine, driver):
    skill, code = compiled(engine)
    seen = []

    def healer(packet):
        seen.append(packet)
        assert "exceeds purchase order limit" in packet["screen"]
        return {"decision": "escalate", "reason": "amount exceeds the purchase order", "llm": True}

    result = run_skill(skill, code, direct(engine), {"invoice_no": "INV-6", "amount": "9000"}, healer)
    assert result["status"] == "needs_human" and "purchase order" in result["error"]
    assert seen[0]["failure"] == "expect" and result["patches"] == []
    assert not any(row.startswith("INV-6") for row in driver.saved)


def test_changed_success_message_updates_the_expectation(tmp_path):
    driver = FakeInvoiceDriver()
    engine = Engine(driver)
    skill, code = compiled(engine)
    driver.variant = "v3"  # same controls, but the app now says "Submitted"

    def healer(packet):
        return {"decision": "update_expect", "expect": "text:Submitted INV-7", "reason": "wording changed", "llm": True}

    result = run_skill(skill, code, direct(engine), {"invoice_no": "INV-7"}, healer)
    assert result["ok"] and result["patches"][0]["kind"] == "expect"
    healed = result["healed"]
    assert "expect=f'text:Submitted {invoice_no}'" in render_code(healed)  # re-templated on the parameter


def test_healer_cannot_point_at_an_element_that_rejects_the_action(engine, driver):
    skill, code = compiled(engine)
    driver.variant = "v2"

    def bad_healer(packet):
        index = int(re.search(r'\[(\d+)\] text "Vendor"', packet["screen"]).group(1))
        return {"decision": "retarget", "index": index, "llm": True}

    result = run_skill(skill, code, direct(engine), {"invoice_no": "INV-8"}, bad_healer)
    assert result["status"] == "needs_human" and result["llm_calls"] == 2  # two attempts, then a person
    assert driver.invoice == ""


def test_irreversible_step_is_never_auto_healed(engine, driver):
    trace = record_invoice(engine)
    trace[-1]["irreversible"] = True
    skill = compile_trace(trace, "post invoice")
    code = render_code(skill)
    assert "irreversible=True" in code
    called = []
    result = run_skill(skill, code, direct(engine), {"amount": "9000"}, lambda p: called.append(p) or {})
    assert result["status"] == "needs_human" and called == []
