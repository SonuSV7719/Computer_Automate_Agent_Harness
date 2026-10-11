"""The two jobs the LLM does: choose the next step while learning, and repair a failed step.

It never sees or returns coordinates. It reads the indexed screen text and answers
with an element index, so every decision can be recorded, compiled and checked.
"""
from __future__ import annotations

import json

from .client import LlmClient

ACTIONS = ["click", "doubleclick", "rightclick", "type", "append", "select", "toggle", "expand",
           "collapse", "scroll", "click_at", "drag", "set", "key", "text"]

SCREEN_FORMAT = """\
The screen is given as text, one element per line:
  [12] button "Save" (disabled)  actions: click
  [5] input "Amount" = "120" (focused, required)  actions: type
  [7] dropdown "Vendor" = "Select..." (options: A | B | C)  actions: select
The number in brackets is the element's index for THIS screen only. After the name comes the
current value, then state in parentheses, then the actions the element accepts.
"(cv 0.84)" or "(read by ocr 0.97)" means the element was read from pixels with that confidence.
Lines without an index are structure (windows, lists) and cannot be acted on.
Indented lines belong to the line above them.
A "region" is a surface with no structure of its own, such as a canvas, a whiteboard or a
remote desktop. What is inside it was read from pixels; each item shows "at x,y", its centre
on a grid from 0,0 (top-left of the region) to 1000,1000 (bottom-right). To act on empty
space in a region use "click_at" with value "x,y", or "drag" with value "x1,y1 x2,y2"
(more points draw a path). Use these only on a region."""

LEARN_SYSTEM = f"""\
You operate a desktop application for a user, one action at a time, to achieve their goal.

{SCREEN_FORMAT}

Reply with exactly one next action as JSON. Every reply must have "action".
- "action": what to do. Choose one the element lists under "actions". "type" replaces the
  field's content, "append" adds to it, "select" chooses a dropdown option or list item (put
  the option text in "value"; for a list marked "several allowed" give options as "A | B").
  "set" moves a slider or number spinner to a number. "key" presses a key or combination such as "enter" or "ctrl+s"
  and needs no index. Use "done" when the goal is fully achieved or cannot be achieved.
- "index": the element to act on. Use only an index that appears on the current screen.
- "value": the text to type, the option to select, or the key to press.
- "expect": what will be newly true on screen afterwards. Give this for every action that
  submits, saves or navigates, and not for typing or selecting (those are verified for you).
  It must not already be true now. Forms: "text:Saved INV-1" (this text is visible), "gone:Saving" (this
  text is no longer visible), "window:Invoice" (window title contains this),
  "value:Amount=120" (the element named Amount holds 120). Quote text you expect to appear
  exactly, including identifiers from the goal.
- "intent": a short plain sentence saying what this step is for, e.g. "Enter the invoice number".
  It is stored with the step and used later to repair it if the application changes.
- "irreversible": true only for steps that cannot be undone, such as paying, posting, sending
  or deleting.
- "result": only with action "done": a one-sentence summary of what was achieved.

Rules:
- One action per reply. Never invent an index.
- Fill fields before pressing the button that submits them.
- If the application shows an error or refuses the task, stop: use action "done" and explain
  in "result", starting with "FAILED:".
- Check the screen before finishing: use "done" only when the screen shows the goal was met.
- Do not repeat an action that already succeeded."""

HEAL_SYSTEM = f"""\
A recorded automation step failed during replay, probably because the application changed.
Decide how to repair it.

{SCREEN_FORMAT}

You are given the step's intent, its action, the fingerprint of the element it used when it
was recorded, why it failed, and the current screen. Reply with JSON:
- "decision": "retarget"      the element still exists but was renamed, moved or re-identified.
                              Give "index" of the element on the current screen that serves
                              the same intent and accepts the same action.
- "decision": "update_expect" the action worked but the confirmation wording changed. Give the
                              new "expect" (same forms: text:, gone:, window:, value:). It must
                              be true on the current screen.
- "decision": "escalate"      anything else: the application is showing an error or a business
                              rule (limit exceeded, record missing, permission denied), no
                              element serves the intent, or you are not confident.
- "reason": one short sentence.

Prefer "escalate" over a guess. A wrong repair is worse than stopping."""

LEARN_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "action": {"type": "STRING", "enum": ACTIONS + ["done"]},
        "result": {"type": "STRING"},
        "index": {"type": "INTEGER"},
        "value": {"type": "STRING"},
        "expect": {"type": "STRING"},
        "intent": {"type": "STRING"},
        "irreversible": {"type": "BOOLEAN"},
    },
    "required": ["action"],
    "propertyOrdering": ["intent", "action", "index", "value", "expect", "irreversible", "result"],
}

HEAL_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "decision": {"type": "STRING", "enum": ["retarget", "update_expect", "escalate"]},
        "index": {"type": "INTEGER"},
        "expect": {"type": "STRING"},
        "reason": {"type": "STRING"},
    },
    "required": ["decision", "reason"],
}

EXTRACT_SYSTEM = """\
You read a business document (an invoice, an order, a row of a sheet) and fill in the inputs
of an automation. You are given the input names with example values. Reply with JSON holding
"values": a list of {"name", "value"} for each input you can find in the document, formatted
like its example. Leave out any input the document does not state. Never guess."""

EXTRACT_SCHEMA = {
    "type": "OBJECT",
    "properties": {"values": {"type": "ARRAY", "items": {
        "type": "OBJECT",
        "properties": {"name": {"type": "STRING"}, "value": {"type": "STRING"}},
        "required": ["name", "value"]}}},
    "required": ["values"],
}


def plan_step(client: LlmClient, goal: str, screen: str, history: list[dict]) -> dict:
    """Ask for the next action towards the goal."""
    done = "\n".join(
        f"{n + 1}. {h['summary']} -> {h['outcome']}" for n, h in enumerate(history)) or "(nothing yet)"
    user = f"GOAL\n{goal}\n\nSTEPS SO FAR\n{done}\n\nCURRENT SCREEN\n{screen}"
    return client.json(LEARN_SYSTEM, user, LEARN_SCHEMA)


def heal(client: LlmClient, packet: dict) -> dict:
    """Ask how to repair a failed step. The reply is validated by the runner before use."""
    user = (
        f"STEP {packet['step'] + 1} OF SKILL \"{packet['skill']}\"\n"
        f"intent: {packet.get('intent') or '(none recorded)'}\n"
        f"action: {packet['action']}" + (f" value={packet['value']!r}" if packet.get("value") else "") + "\n"
        f"expected result: {packet.get('expect') or '(none)'}\n"
        f"irreversible: {bool(packet.get('irreversible'))}\n"
        f"element when recorded: {json.dumps(packet.get('fingerprint'), ensure_ascii=False)}\n\n"
        f"FAILURE ({packet['failure']}, attempt {packet.get('attempt', 1)})\n{packet['detail']}\n\n"
        f"CURRENT SCREEN\n{packet['screen']}"
    )
    decision = client.json(HEAL_SYSTEM, user, HEAL_SCHEMA)
    decision["llm"] = True
    return decision


def extract_params(client: LlmClient, params: dict, text: str = "", files: list | None = None) -> dict:
    """Read a document into a skill's inputs. Only names the skill has are returned."""
    listing = "\n".join(f"- {name} (example: {example!r})" for name, example in params.items())
    user = f"INPUTS\n{listing}\n\nDOCUMENT\n{text or '(attached)'}"
    reply = client.json(EXTRACT_SYSTEM, user, EXTRACT_SCHEMA, files)
    return {item["name"]: str(item["value"]) for item in reply.get("values", []) if item.get("name") in params}
