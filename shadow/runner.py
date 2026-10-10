"""Replay harness: runs a compiled skill with no LLM, and heals a step when it fails.

`h` in a generated skill file is a Runner. Each call resolves the element from its
fingerprint, acts, and checks the expected result. On failure the healer is asked
once per attempt; an accepted fix becomes a patch that is saved as a new version.
"""
from __future__ import annotations

import copy
import time
from typing import Callable, Optional

from .core import expect as expectation
from .core import fingerprint
from .core.skill import Skill, apply_patches
from .skill_api import ElRef

ELEMENT_ACTIONS = ("click", "doubleclick", "rightclick", "type", "append", "select", "toggle",
                   "expand", "collapse", "focus", "scroll")
ALWAYS_ALLOWED = {"click", "doubleclick", "rightclick", "focus", "scroll"}
LOCATE_TIMEOUT = 1.5
POLL = 0.05

Healer = Callable[[dict], dict]
EngineCall = Callable[[Callable], object]  # runs fn(engine) on the engine thread


class NeedsHuman(Exception):
    """The run cannot continue without a person: a business exception or a failed heal."""

    def __init__(self, reason: str, packet: Optional[dict] = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.packet = packet or {}


def describe_fp(fp: dict) -> str:
    title = fp.get("name") or fp.get("label") or fp.get("automation_id") or ""
    return f'{fp.get("role", "element")} "{title}"'


class Runner:
    def __init__(self, skill: Skill, call: EngineCall, params: Optional[dict] = None,
                 healer: Optional[Healer] = None, on_event: Optional[Callable[[dict], None]] = None,
                 max_heals: int = 2) -> None:
        self.skill = skill
        self.call = call
        self.params = params or {}
        self.healer = healer
        self.on_event = on_event
        self.max_heals = max_heals
        self.locators = copy.deepcopy(skill.locators)
        self.patches: list[dict] = []
        self.events: list[dict] = []
        self.llm_calls = 0
        self._n = 0

    # ---- the API generated code calls ----------------------------------
    def key(self, combo: str, expect: Optional[str] = None, timeout: float = 3.0, irreversible: bool = False):
        self._step("key", None, combo, expect, timeout, irreversible)

    def text(self, value: str, expect: Optional[str] = None, timeout: float = 3.0, irreversible: bool = False):
        self._step("text", None, value, expect, timeout, irreversible)

    def __getattr__(self, name: str):
        if name not in ELEMENT_ACTIONS:
            raise AttributeError(name)

        def method(ref: ElRef, value: Optional[str] = None, expect: Optional[str] = None,
                   timeout: float = 3.0, irreversible: bool = False):
            self._step(name, ref, value, expect, timeout, irreversible)

        return method

    # ---- internals -----------------------------------------------------
    def _emit(self, event: dict) -> None:
        self.events.append(event)
        if self.on_event:
            self.on_event(event)

    def _attempt(self, action: str, fp: Optional[dict], value, spec, timeout: float) -> dict:
        def run(e):
            try:
                if fp is None:
                    result = e.act(action, value=value, expect=spec, timeout=timeout, settle=False)
                    how = "keys"
                else:
                    snap = e.see()
                    element, how = fingerprint.resolve(fp, snap)
                    if element is None:
                        return {"status": "locator", "detail": "element not found on screen",
                                "screen": snap.text, "body": snap.body}
                    result = e.act(action, index=element.index, value=value, snapshot_id=snap.id,
                                   expect=spec, timeout=timeout, settle=False)
            except (RuntimeError, ValueError, LookupError) as exc:
                snap = e.see()
                return {"status": "action", "detail": str(exc), "screen": snap.text, "body": snap.body}
            if result["ok"]:
                return {"status": "ok", "locator": how, "route": result["route"]}
            return {"status": "expect", "detail": f"expected result not seen: {spec}",
                    "screen": e.snapshot.text, "body": e.snapshot.body}

        return self.call(run)

    def _locate_and_act(self, action, fp, value, spec, timeout) -> dict:
        """Retry while the element is missing and the screen is still changing."""
        deadline = time.perf_counter() + LOCATE_TIMEOUT
        previous = None
        while True:
            outcome = self._attempt(action, fp, value, spec, timeout)
            if outcome["status"] != "locator":
                return outcome
            if outcome["body"] == previous or time.perf_counter() >= deadline:
                return outcome  # the screen has settled without the element
            previous = outcome["body"]
            time.sleep(POLL)

    def _step(self, action: str, ref: Optional[ElRef], value, spec, timeout: float, irreversible: bool) -> None:
        n = self._n
        self._n += 1
        recorded = self.skill.steps[n] if n < len(self.skill.steps) else {}
        intent = recorded.get("intent") or (self.locators[ref.id].get("intent") if ref else "") or ""
        started = time.perf_counter()
        fp = None
        if ref is not None:
            if ref.id not in self.locators:
                raise KeyError(f"skill has no element '{ref.id}'")
            fp = self.locators[ref.id]["fingerprint"]

        outcome = self._locate_and_act(action, fp, value, spec, timeout)
        heals = 0
        while outcome["status"] != "ok":
            packet = {
                "skill": self.skill.name, "version": self.skill.version, "step": n, "intent": intent,
                "action": action, "value": value, "expect": spec, "irreversible": irreversible,
                "failure": outcome["status"], "detail": outcome["detail"],
                "fingerprint": fp, "screen": outcome["screen"], "attempt": heals + 1,
            }
            if irreversible and outcome["status"] == "expect":
                raise NeedsHuman("an irreversible step did not show its expected result", packet)
            if self.healer is None or heals >= self.max_heals:
                raise NeedsHuman(f"step {n + 1} failed: {outcome['detail']}", packet)
            heals += 1
            decision = self.healer(packet)
            if decision.get("llm"):
                self.llm_calls += 1
            self._emit({"type": "heal", "step": n, "failure": outcome["status"],
                        "decision": decision.get("decision"), "reason": decision.get("reason", "")})
            if decision.get("decision") == "escalate":
                raise NeedsHuman(decision.get("reason") or "the healer asked for a person", packet)
            outcome = self._apply(decision, packet, ref, action, value, spec, timeout)
            if outcome["status"] == "ok" and outcome.get("expect"):
                spec = outcome["expect"]

        self._emit({"type": "step", "step": n, "action": action, "el": ref.id if ref else None,
                    "ms": round((time.perf_counter() - started) * 1000, 1), "locator": outcome.get("locator"),
                    "route": outcome.get("route"), "healed": heals > 0})

    def _apply(self, decision: dict, packet: dict, ref, action, value, spec, timeout) -> dict:
        """Validate and execute a healer's decision. Never trusts it blindly."""
        kind = decision.get("decision")
        n = packet["step"]
        reason = decision.get("reason", "")

        if kind == "update_expect":
            new_spec = decision.get("expect") or ""

            def run(e):
                try:
                    expectation.parse(new_spec)
                except ValueError as exc:
                    return {"status": "expect", "detail": str(exc)}
                snap = e.see()
                if packet["failure"] != "expect" or not expectation.check(new_spec, snap):
                    return {"status": "expect", "detail": f"proposed expectation is not true on screen: {new_spec}",
                            "screen": snap.text, "body": snap.body}
                return {"status": "ok", "locator": "healed", "route": "none", "expect": new_spec}

            outcome = self.call(run)
            if outcome["status"] == "ok":
                self.patches.append({"kind": "expect", "step": n, "expect": new_spec, "reason": reason,
                                     "params": self.params, "before": spec, "after": new_spec})
            else:
                outcome.setdefault("screen", packet["screen"])
            return outcome

        if kind == "retarget" and ref is not None:
            index = decision.get("index")

            def run(e):
                snap = e.snapshot or e.see()
                wanted = decision.get("snapshot")
                if wanted is not None and wanted != snap.id:
                    return {"status": "locator", "detail": f"snapshot {wanted} is stale; the latest is {snap.id}",
                            "screen": snap.text, "body": snap.body}
                try:
                    element = snap.get(int(index))
                except (KeyError, TypeError, ValueError):
                    return {"status": "locator", "detail": f"no element [{index}] on screen",
                            "screen": snap.text, "body": snap.body}
                if action not in ALWAYS_ALLOWED and action not in element.actions:
                    return {"status": "locator", "screen": snap.text, "body": snap.body,
                            "detail": f"element [{index}] does not accept '{action}'"}
                new_fp = fingerprint.make(element, snap)
                try:
                    result = e.act(action, index=element.index, value=value, snapshot_id=snap.id,
                                   expect=spec, timeout=timeout, settle=False)
                except (RuntimeError, ValueError, LookupError) as exc:
                    after = e.see()
                    return {"status": "action", "detail": str(exc), "screen": after.text, "body": after.body}
                if not result["ok"]:
                    return {"status": "expect", "detail": f"expected result not seen: {spec}",
                            "screen": e.snapshot.text, "body": e.snapshot.body}
                return {"status": "ok", "locator": "healed", "route": result["route"], "fingerprint": new_fp}

            outcome = self.call(run)
            if outcome["status"] == "ok":
                old = self.locators[ref.id]["fingerprint"]
                self.locators[ref.id]["fingerprint"] = outcome["fingerprint"]
                self.patches.append({"kind": "retarget", "step": n, "el": ref.id, "reason": reason,
                                     "fingerprint": outcome["fingerprint"],
                                     "before": describe_fp(old), "after": describe_fp(outcome["fingerprint"])})
            return outcome

        return {"status": packet["failure"], "detail": f"healer gave an unusable decision: {decision}",
                "screen": packet["screen"], "body": ""}


def run_skill(skill: Skill, code: str, call: EngineCall, params: Optional[dict] = None,
              healer: Optional[Healer] = None, on_event: Optional[Callable[[dict], None]] = None) -> dict:
    """Execute a skill file. Returns timing, LLM calls, heal patches and the next version if healed."""
    values = {**skill.params, **(params or {})}
    unknown = set(values) - set(skill.params)
    if unknown:
        raise ValueError(f"unknown parameter(s): {', '.join(sorted(unknown))}")
    runner = Runner(skill, call, values, healer, on_event)
    started = time.perf_counter()
    result = {"ok": True, "status": "done", "error": None, "packet": None}
    try:
        call(lambda e: e.set_target_window(skill.window) if skill.window else None)
        namespace: dict = {}
        exec(compile(code, f"{skill.name}.py", "exec"), namespace)
        namespace["run"](runner, **values)
    except NeedsHuman as exc:
        result.update(ok=False, status="needs_human", error=exc.reason, packet=exc.packet)
    except LookupError as exc:
        result.update(ok=False, status="failed", error=str(exc))
    steps = [e for e in runner.events if e["type"] == "step"]
    result.update(
        skill=skill.name, version=skill.version, ms=round((time.perf_counter() - started) * 1000, 1),
        steps=steps, steps_done=len(steps), llm_calls=runner.llm_calls,
        heals=[e for e in runner.events if e["type"] == "heal"],
        patches=runner.patches,
        healed=apply_patches(skill, runner.patches) if runner.patches else None,
    )
    return result
