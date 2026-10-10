"""Learning: the LLM performs the task once while every step is recorded."""
from __future__ import annotations

import time
from typing import Callable, Optional

from .engine import StaleSnapshot
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


def learn(call: Callable, client: LlmClient, goal: str, max_steps: int = MAX_STEPS,
          on_event: Optional[Callable[[dict], None]] = None) -> dict:
    """Drive the target window towards `goal`. Returns the recorded trace and what it cost.

    `call(fn)` runs fn(engine) on the engine thread."""
    started = time.perf_counter()
    history: list[dict] = []
    llm_calls = 0
    errors = 0
    result = {"ok": False, "status": "failed", "error": None, "result": "", "trace": []}

    call(lambda e: e.record_start())
    try:
        for _ in range(max_steps):
            snap = call(lambda e: e.see())
            step = planner.plan_step(client, goal, snap.text, history)
            llm_calls += 1

            if step.get("done"):
                summary = step.get("result") or "done"
                if summary.upper().startswith("FAILED"):
                    result.update(status="failed", error=summary, result=summary)
                else:
                    result.update(ok=True, status="done", result=summary)
                break

            action = step.get("action")
            needs_index = action not in ("key", "text")
            if not action or (needs_index and step.get("index") is None):
                errors += 1
                history.append({"summary": f"invalid reply {step}", "outcome": "ignored: give an action and an index"})
            else:
                try:
                    outcome = call(lambda e: e.act(
                        action, index=step.get("index") if needs_index else None, value=step.get("value"),
                        snapshot_id=snap.id, expect=step.get("expect") or None, intent=step.get("intent", "")))
                except (StaleSnapshot, KeyError, ValueError, RuntimeError, LookupError) as exc:
                    errors += 1
                    history.append({"summary": _summary(step, None), "outcome": f"ERROR: {exc}"})
                    outcome = None
                if outcome is not None:
                    if outcome["ok"]:
                        errors = 0
                        if step.get("irreversible"):
                            call(lambda e: e.trace[-1].update(irreversible=True) if e.trace else None)
                        changes = "; ".join(outcome["changes"][:6]) or "no visible change"
                        history.append({"summary": _summary(step, outcome.get("element")), "outcome": f"ok. {changes}"})
                    else:
                        errors += 1
                        changes = "; ".join(outcome["changes"][:6]) or "no visible change"
                        history.append({"summary": _summary(step, outcome.get("element")),
                                        "outcome": f"action ran but the expected result was NOT seen "
                                                   f"({step.get('expect')}). Screen changes: {changes}"})
                    if on_event:
                        on_event({"type": "learn_step", "n": len(history), "ok": outcome["ok"],
                                  "summary": history[-1]["summary"], "intent": step.get("intent", ""),
                                  "ms": outcome["ms"], "route": outcome["route"]})
            if errors >= MAX_ERRORS:
                result.update(error=f"gave up after {MAX_ERRORS} failed attempts in a row: {history[-1]['outcome']}")
                break
        else:
            result.update(error=f"goal not reached within {max_steps} steps")
    finally:
        trace = call(lambda e: e.record_stop())
    result.update(trace=trace, llm_calls=llm_calls, steps=len(trace), history=history,
                  ms=round((time.perf_counter() - started) * 1000, 1))
    return result
