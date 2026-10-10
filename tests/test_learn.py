"""The learn loop and LLM healing, with a scripted stand-in for Gemini.

These check the harness around the model (what it is shown, how replies are validated,
what is recorded). They do not check the quality of Gemini's own decisions.
"""
import re

from fake import FakeInvoiceDriver

from shadow.core.skill import compile_trace, render_code
from shadow.engine import Engine
from shadow.learn import learn
from shadow.llm import planner
from shadow.runner import run_skill


def direct(engine):
    return lambda fn: fn(engine)


def index_of(screen: str, pattern: str) -> int:
    match = re.search(r"\[(\d+)\] " + pattern, screen)
    assert match, f"{pattern} not on screen:\n{screen}"
    return int(match.group(1))


class ScriptedLlm:
    """Fills the invoice form the way a sensible model would, reading only the screen text."""

    def __init__(self, invoice="INV-9001", vendor="Northwind Paper", amount="640", first_reply=None):
        self.want = {"invoice": invoice, "vendor": vendor, "amount": amount}
        self.first_reply = first_reply
        self.prompts: list[tuple[str, str]] = []

    def json(self, system, user, schema, files=None):
        self.prompts.append((system, user))
        if self.first_reply is not None:
            reply, self.first_reply = self.first_reply, None
            return reply
        screen = user.split("CURRENT SCREEN\n", 1)[1]
        steps = user.split("STEPS SO FAR\n", 1)[1].split("\n\nCURRENT SCREEN", 1)[0]
        if "Saved " + self.want["invoice"] in screen:
            return {"done": True, "result": f"Invoice {self.want['invoice']} was saved."}
        if "Error:" in screen or "Blocked:" in screen:
            return {"done": True, "result": "FAILED: the application refused the invoice."}
        if 'input "Invoice No" = ""' in screen:
            return {"done": False, "index": index_of(screen, 'input "Invoice No"'), "action": "type",
                    "value": self.want["invoice"], "intent": "Enter the invoice number"}
        if 'dropdown "Vendor" = "Select..."' in screen:
            return {"done": False, "index": index_of(screen, 'dropdown "Vendor"'), "action": "select",
                    "value": self.want["vendor"], "intent": "Choose the vendor"}
        if 'input "Amount" = ""' in screen:
            return {"done": False, "index": index_of(screen, 'input "Amount"'), "action": "type",
                    "value": self.want["amount"], "intent": "Enter the amount"}
        assert "click button" not in steps or "NOT seen" in steps or "ERROR" in steps
        return {"done": False, "index": index_of(screen, 'button "Save"'), "action": "click",
                "expect": f"text:Saved {self.want['invoice']}", "intent": "Save the invoice", "irreversible": False}


def test_learn_records_a_trace_that_compiles_and_replays(engine, driver):
    llm = ScriptedLlm()
    result = learn(direct(engine), llm, "Enter invoice INV-9001 for Northwind Paper, amount 640")
    assert result["ok"] and result["status"] == "done", result["error"]
    assert result["steps"] == 4 and result["llm_calls"] == 5  # four actions, then "done"
    assert driver.saved == ["INV-9001 | Northwind Paper | 640 | tax excl."]

    skill = compile_trace(result["trace"], "enter invoice")
    assert list(skill.params) == ["invoice_no", "vendor", "amount"]
    assert [s["intent"] for s in skill.steps][0] == "Enter the invoice number"

    replay = run_skill(skill, render_code(skill), direct(engine), {"invoice_no": "INV-9002", "amount": "7"})
    assert replay["ok"] and replay["llm_calls"] == 0
    assert driver.saved[-1] == "INV-9002 | Northwind Paper | 7 | tax excl."


def test_model_sees_the_goal_the_screen_and_what_already_happened(engine):
    llm = ScriptedLlm()
    learn(direct(engine), llm, "Enter invoice INV-9001")
    system, first = llm.prompts[0]
    assert "one action at a time" in system and "Never invent an index" in system
    assert "GOAL\nEnter invoice INV-9001" in first and "(nothing yet)" in first
    assert 'input "Invoice No" = ""  actions: type' in first
    _, third = llm.prompts[2]
    assert "1. type input" in third and "'INV-9001'" in third and "-> ok." in third


def test_an_invented_index_is_reported_back_and_the_model_recovers(engine, driver):
    llm = ScriptedLlm(first_reply={"done": False, "index": 999, "action": "click", "intent": "?"})
    result = learn(direct(engine), llm, "Enter invoice INV-9001")
    assert result["ok"] and result["llm_calls"] == 6
    assert "ERROR: 'no element [999]" in llm.prompts[1][1]
    assert result["steps"] == 4  # the bad reply was never recorded


def test_application_refusal_ends_learning_as_failed(driver):
    engine = Engine(driver)
    llm = ScriptedLlm(amount="99999")  # over the purchase order limit
    result = learn(direct(engine), llm, "Enter invoice INV-9001 for 99999")
    assert not result["ok"] and result["status"] == "failed"
    assert result["error"].startswith("FAILED:")
    assert driver.saved == []
    assert result["steps"] == 3  # the Save click that did not show its result is not in the trace


def test_learning_stops_after_repeated_unusable_replies(engine):
    class Stuck:
        calls = 0

        def json(self, *a, **k):
            self.calls += 1
            return {"done": False, "action": "click"}  # never gives an index

    stuck = Stuck()
    result = learn(direct(engine), stuck, "anything")
    assert not result["ok"] and "gave up" in result["error"] and stuck.calls == 4


def test_gemini_healer_reads_the_packet_and_its_answer_is_validated(engine, driver):
    result = learn(direct(engine), ScriptedLlm(), "Enter invoice INV-9001")
    skill = compile_trace(result["trace"], "enter invoice")
    driver.variant = "v2"
    prompts = []

    class HealLlm:
        def json(self, system, user, schema, files=None):
            prompts.append(user)
            screen = user.split("CURRENT SCREEN\n", 1)[1]
            name = 'input "Invoice number"' if "Enter the invoice number" in user else 'button "Submit"'
            return {"decision": "retarget", "index": index_of(screen, name), "reason": "renamed"}

    healed = run_skill(skill, render_code(skill), direct(engine), {"invoice_no": "INV-9003"},
                       lambda packet: planner.heal(HealLlm(), packet))
    assert healed["ok"] and healed["llm_calls"] == 2
    assert 'intent: Enter the invoice number' in prompts[0]
    assert '"automation_id": "txtInvoiceNo"' in prompts[0] and "FAILURE (locator" in prompts[0]
    assert healed["healed"].version == 2


def test_document_extraction_keeps_only_the_skills_own_inputs():
    class ExtractLlm:
        def json(self, system, user, schema, files=None):
            assert "invoice_no (example: 'INV-1')" in user and "Total due 88.50" in user
            return {"values": [{"name": "invoice_no", "value": "INV-77"}, {"name": "amount", "value": "88.50"},
                               {"name": "iban", "value": "XX00"}]}

    values = planner.extract_params(ExtractLlm(), {"invoice_no": "INV-1", "amount": "10"},
                                    "Invoice INV-77 ... Total due 88.50")
    assert values == {"invoice_no": "INV-77", "amount": "88.50"}
