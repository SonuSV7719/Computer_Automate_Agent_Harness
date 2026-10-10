"""The learn loop and LLM healing, with a scripted stand-in for Gemini.

These check the harness around the model (what it is shown, how replies are validated,
what is recorded). They do not check the quality of Gemini's own decisions.
"""
import re

import pytest

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
    assert "1. type input" in third and "'INV-9001'" in third and "-> performed." in third


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
    assert result["steps"] == 4  # the Save click happened, so it is in the trace; nothing is compiled


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


class WrongGuessLlm(ScriptedLlm):
    """Like a real model on a bad day: predicts a result for Save that is wrong or already true."""

    def __init__(self, guess, **kw):
        super().__init__(**kw)
        self.guess = guess

    def json(self, system, user, schema, files=None):
        reply = super().json(system, user, schema, files)
        if reply.get("action") == "click":
            reply["expect"] = self.guess
        if reply.get("action") in ("type", "select"):
            reply["expect"] = "value:Some Old Label=" + reply["value"]  # fragile and redundant
        return reply


@pytest.mark.parametrize("guess", ["text:Invoice stored OK", "text:Ready", ""])
def test_a_performed_step_is_recorded_with_what_the_screen_really_showed(engine, driver, guess):
    """A wrong, already-true or missing prediction must not drop the step or weaken its check."""
    result = learn(direct(engine), WrongGuessLlm(guess), "Enter invoice INV-9001")
    assert result["ok"] and result["steps"] == 4
    save = result["trace"][-1]
    assert save["action"] == "click" and save["expect"] == "text:Saved INV-9001"  # observed, and quotes the input
    assert all(step["expect"] is None for step in result["trace"][:3])  # typed values are verified by the harness

    skill = compile_trace(result["trace"], "enter invoice")
    assert "expect=f'text:Saved {invoice_no}'" in render_code(skill)
    replay = run_skill(skill, render_code(skill), direct(engine), {"invoice_no": "INV-9002", "amount": "9000"})
    assert replay["status"] == "needs_human"  # the over-limit invoice is caught by that check


def test_an_empty_repair_is_rejected(engine, driver):
    result = learn(direct(engine), ScriptedLlm(), "Enter invoice INV-9001")
    skill = compile_trace(result["trace"], "enter invoice")
    replay = run_skill(skill, render_code(skill), direct(engine), {"invoice_no": "INV-9", "amount": "9000"},
                       lambda packet: {"decision": "update_expect", "expect": "", "llm": True})
    assert replay["status"] == "needs_human" and replay["patches"] == []


def test_a_right_retarget_with_a_stale_check_keeps_both_repairs(engine, driver):
    """v2 renames the field AND the app's wording changes: retarget first, then fix the check."""
    result = learn(direct(engine), ScriptedLlm(), "Enter invoice INV-9001")
    skill = compile_trace(result["trace"], "enter invoice")
    skill.steps[0]["expect"] = skill.steps[0]["expect_template"] = "value:Invoice No=INV-9001"
    skill.steps[0]["expect_template"] = "value:Invoice No={invoice_no}"
    driver.variant = "v2"

    def healer(packet):
        if packet["step"] == 0 and packet["failure"] == "locator":
            return {"decision": "retarget", "index": index_of(packet["screen"], 'input "Invoice number"'), "llm": True}
        if packet["step"] == 0:
            return {"decision": "update_expect", "expect": "value:Invoice number=INV-9", "llm": True}
        return {"decision": "retarget", "index": index_of(packet["screen"], 'button "Submit"'), "llm": True}

    replay = run_skill(skill, render_code(skill), direct(engine), {"invoice_no": "INV-9", "amount": "5"}, healer)
    assert replay["ok"], replay["error"]
    assert [(p["kind"], p["step"]) for p in replay["patches"]] == [("retarget", 0), ("expect", 0), ("retarget", 3)]
    again = run_skill(replay["healed"], render_code(replay["healed"]), direct(engine),
                      {"invoice_no": "INV-10", "amount": "6"})
    assert again["ok"] and again["llm_calls"] == 0
    assert [row.split(" | ")[0] for row in driver.saved[-2:]] == ["INV-9", "INV-10"]  # typed once each, not twice
