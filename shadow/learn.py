"""Learning: the LLM performs the task once while every step is recorded."""
from __future__ import annotations

import time
from typing import Callable, Optional

from .core import expect as expectation
from .core import textmatch
from .engine import Halted, StaleSnapshot
from .llm import planner
from .llm.client import LlmClient

MAX_STEPS = 30
MAX_ERRORS = 4  # consecutive unusable replies before giving up


def _summary(step: dict, element: Optional[dict]) -> str:
    target = ""
    if element:
        target = f' {element["role"]} "{element.get("name") or element.get("label") or ""}"'
    value = f" {step['value']!r}" if step.get("value") else ""
    return f"{step['action']}{target}{value}"


def _usable_expectation(step: dict, snap) -> Optional[str]:
    """The model's predicted result, unless it cannot tell success from failure."""
    spec = (step.get("expect") or "").strip()
    if not spec:
        return None
    try:
        if expectation.check(spec, snap):
            return None  # already true before the action: it would prove nothing
    except ValueError:
        return None
    if step.get("action") in ("type", "append", "select") and spec.startswith("value:"):
        return None  # the harness verifies typed values itself, and by element, not by label
    return spec


def _keep_prediction(spec: str, outcome: dict, typed: list[str]) -> bool:
    """A prediction that came true is kept, unless it is a counter that will differ next run."""
    counters = outcome.get("counter_text") or []
    for kind, text in expectation.parse(spec):
        if kind == "text" and any(text in counter for counter in counters)                 and not any(v and v in text for v in typed):
            return False
    return True


def _observed_expectation(action: str, outcome: dict, typed: list[str]) -> Optional[str]:
    """A post-condition taken from what really appeared after a click or key press."""
    if action in ("type", "append", "select", "scroll", "focus"):
        return None
    new_text = [t for t in outcome.get("new_text") or [] if 3 <= len(t) <= 120]
    quoting = [t for t in new_text if any(v and v in t for v in typed)]
    if quoting:
        return "text:" + quoting[0]  # e.g. "Saved INV-1042": follows the input on every run
    counters = set(outcome.get("counter_text") or [])
    steady = [t for t in new_text if t not in counters]
    return "text:" + steady[0] if steady else None


def _settle_action(step: dict, snap) -> Optional[str]:
    """The model's action, corrected when the element plainly calls for another one."""
    action = step.get("action") or _infer_action(step, snap)
    if action == "click" and step.get("value"):
        try:
            actions = snap.get(int(step["index"])).actions
        except (KeyError, TypeError, ValueError):
            return action
        if "click" not in actions and "select" in actions:
            return "select"  # "click the dropdown with this option" means choosing the option
    return action


def _settle_target(step: dict, snap) -> None:
    """If the model pointed at a label, act on the field that label names instead."""
    action = step.get("action")
    if action not in ("type", "append", "select"):
        return
    try:
        chosen = snap.get(int(step["index"]))
    except (KeyError, TypeError, ValueError):
        return
    if action in chosen.actions or not chosen.title:
        return
    fields = [e for e in snap.elements if action in e.actions and textmatch.same(e.title, chosen.title, ocr=True)]
    if len(fields) == 1:
        step["index"] = fields[0].index


def _infer_action(step: dict, snap) -> Optional[str]:
    """A reply that names an element and a value but no action has one sensible reading."""
    try:
        element = snap.get(int(step["index"]))
    except (KeyError, TypeError, ValueError):
        return None
    if step.get("value"):
        return next((a for a in ("select", "type") if a in element.actions), None)
    return next((a for a in ("click", "toggle") if a in element.actions), None)


def learn(call: Callable, client: LlmClient, goal: str, max_steps: int = MAX_STEPS,
          on_event: Optional[Callable[[dict], None]] = None) -> dict:
    """Drive the target window towards `goal`. Returns the recorded trace and what it cost.

    `call(fn)` runs fn(engine) on the engine thread."""
    started = time.perf_counter()
    history: list[dict] = []
    llm_calls = 0
    errors = 0
    result = {"ok": False, "status": "failed", "error": None, "result": "", "trace": []}

    trace: list[dict] = []
    typed: list[str] = []  # values entered so far; a confirmation that quotes one is the best check
    try:
        for _ in range(max_steps):
            snap = call(lambda e: e.see())
            step = planner.plan_step(client, goal, snap.text, history)
            llm_calls += 1

            if step.get("done") or step.get("action") == "done":
                summary = step.get("result") or "done"
                if summary.upper().startswith("FAILED"):
                    result.update(status="failed", error=summary, result=summary)
                else:
                    result.update(ok=True, status="done", result=summary)
                break

            action = _settle_action(step, snap)
            step["action"] = action
            _settle_target(step, snap)
            needs_index = action not in ("key", "text")
            if not action or (needs_index and step.get("index") is None):
                errors += 1
                missing = "an action" if not action else "an index"
                history.append({"summary": f"invalid reply {step}",
                                "outcome": f"ignored: the reply had no {missing}; every reply needs \"action\""})
            else:
                wanted = _usable_expectation(step, snap)
                try:
                    outcome = call(lambda e: e.act(
                        action, index=step.get("index") if needs_index else None, value=step.get("value"),
                        snapshot_id=snap.id, expect=wanted, intent=step.get("intent", "")))
                except (StaleSnapshot, KeyError, ValueError, RuntimeError, LookupError) as exc:
                    errors += 1
                    history.append({"summary": _summary(step, None), "outcome": f"ERROR: {exc}"})
                    outcome = None
                if outcome is not None:
                    # The action was performed, so it belongs in the recording whatever the model
                    # predicted. Its check is taken from what the screen actually did.
                    recorded = dict(outcome["step"])
                    recorded["irreversible"] = bool(step.get("irreversible"))
                    if not (wanted and outcome["ok"] and _keep_prediction(wanted, outcome, typed)):
                        recorded["expect"] = _observed_expectation(action, outcome, typed)
                    trace.append(recorded)
                    if step.get("value") and action in ("type", "append", "select"):
                        typed.append(step["value"])
                    errors = 0
                    changes = "; ".join(outcome["changes"][:6]) or "no visible change"
                    note = "" if (outcome["ok"] or not wanted) else \
                        f" (your expected result {wanted!r} did not appear; judge by the changes)"
                    history.append({"summary": _summary(step, outcome.get("element")),
                                    "outcome": f"performed{note}. Screen changes: {changes}"})
                    if on_event:
                        on_event({"type": "learn_step", "n": len(history), "ok": outcome["ok"],
                                  "summary": history[-1]["summary"], "intent": step.get("intent", ""),
                                  "ms": outcome["ms"], "route": outcome["route"]})
            if errors >= MAX_ERRORS:
                result.update(error=f"gave up after {MAX_ERRORS} failed attempts in a row: {history[-1]['outcome']}")
                break
        else:
            result.update(error=f"goal not reached within {max_steps} steps")
    except Halted as exc:
        result.update(status="stopped", error=str(exc))
    result.update(trace=trace, llm_calls=llm_calls, steps=len(trace), history=history,
                  ms=round((time.perf_counter() - started) * 1000, 1))
    return result
