"""Runs: each skill run is a thread that can pause for a heal decision.

A paused run waits for whoever is driving (the cloud brain, a host agent through
`shadow heal`, or a person) and then continues from the failed step.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from .core.skill import SkillStore
from .runner import run_skill

HEAL_WAIT = 600.0  # how long a paused run waits for an answer
HOME = Path(os.environ.get("SHADOW_HOME", Path.home() / ".shadow"))


class RunState:
    def __init__(self, run_id: str, skill: str, version: int, params: dict) -> None:
        self.id = run_id
        self.skill = skill
        self.version = version
        self.params = params
        self.status = "running"  # running | needs_heal | done | needs_human | failed | stopped
        self.packet: Optional[dict] = None
        self.result: Optional[dict] = None
        self.events: list[dict] = []
        self.started = time.time()
        self._changed = threading.Condition()
        self._decision: Optional[dict] = None

    def _set(self, status: str, **fields) -> None:
        with self._changed:
            self.status = status
            for key, value in fields.items():
                setattr(self, key, value)
            self._changed.notify_all()

    def wait(self, timeout: float) -> dict:
        """Block until the run pauses or ends, then describe it."""
        with self._changed:
            self._changed.wait_for(lambda: self.status != "running", timeout)
        return self.summary()

    def ask(self, packet: dict) -> dict:
        """Called on the run thread: pause for a heal decision."""
        with self._changed:
            self._decision = None
            self.status, self.packet = "needs_heal", packet
            self._changed.notify_all()
            answered = self._changed.wait_for(lambda: self._decision is not None, HEAL_WAIT)
            self.status, self.packet = "running", None
            if not answered:
                return {"decision": "escalate", "reason": "no heal decision arrived in time"}
            return self._decision

    def answer(self, decision: dict) -> None:
        with self._changed:
            if self.status != "needs_heal":
                raise LookupError(f"run {self.id} is not waiting for a heal (status: {self.status})")
            self._decision = decision
            self.status = "running"
            self._changed.notify_all()

    def summary(self) -> dict:
        out = {"ok": self.status in ("done", "running"), "run": self.id, "skill": self.skill,
               "version": self.version, "status": self.status, "params": self.params}
        if self.status == "needs_heal":
            out.update(ok=False, kind="needs_heal", packet=self.packet,
                       error=f"step {self.packet['step'] + 1} needs a heal: {self.packet['detail']}")
        if self.result:
            out.update({k: self.result[k] for k in ("ms", "steps", "steps_done", "llm_calls", "heals",
                                                    "patches", "error", "new_version")})
            if self.status == "needs_human":
                out.update(kind="needs_human", packet=self.result.get("packet"))
            elif self.status in ("failed", "stopped"):
                out.update(kind="error")
        return out


class RunManager:
    def __init__(self) -> None:
        self.runs: dict[str, RunState] = {}
        self.llm_healer: Optional[Callable[[dict], dict]] = None  # set when a brain or Gemini key is configured
        self.on_finish: Optional[Callable[[dict], None]] = None

    def get(self, run_id: str) -> RunState:
        if run_id not in self.runs:
            raise LookupError(f"no run '{run_id}'")
        return self.runs[run_id]

    def answer(self, run_id: str, decision: dict) -> RunState:
        state = self.get(run_id)
        state.answer(decision)
        return state

    def start(self, store: SkillStore, name: str, params: dict, heal: str, call: Callable) -> RunState:
        if heal not in ("auto", "host", "off"):
            raise ValueError("heal must be auto, host or off")
        skill = store.load(name)
        code = store.code(skill.name, skill.version)
        unknown = set(params) - set(skill.params)
        if unknown:
            raise ValueError(f"unknown parameter(s): {', '.join(sorted(unknown))}; "
                             f"this skill takes: {', '.join(skill.params) or 'none'}")
        state = RunState(uuid.uuid4().hex[:8], skill.name, skill.version, params)
        self.runs[state.id] = state

        healer = None
        if heal == "auto" and self.llm_healer is not None:
            healer = self.llm_healer
        elif heal != "off":
            healer = state.ask  # the host agent or a person answers through `shadow heal`

        def work() -> None:
            from .drivers.com import init_thread
            init_thread()
            try:
                result = run_skill(skill, code, call, params, healer, state.events.append)
            except Exception as exc:  # a broken skill file must not take the daemon down
                result = {"ok": False, "status": "failed", "error": f"{type(exc).__name__}: {exc}",
                          "ms": 0, "steps": [], "steps_done": 0, "llm_calls": 0, "heals": [],
                          "patches": [], "healed": None, "packet": None}
            healed = result.pop("healed", None)
            result["new_version"] = None
            if healed is not None and result["status"] == "done":
                store.save(healed)
                result["new_version"] = healed.version
            self._save(state, result)
            state._set(result["status"], result=result)
            if self.on_finish:
                try:
                    self.on_finish(state.summary())
                except Exception:
                    pass

        threading.Thread(target=work, name=f"run-{state.id}", daemon=True).start()
        return state

    @staticmethod
    def _save(state: RunState, result: dict) -> None:
        folder = HOME / "runs"
        folder.mkdir(parents=True, exist_ok=True)
        record = {"run": state.id, "skill": state.skill, "version": state.version, "params": state.params,
                  "started": state.started, **{k: v for k, v in result.items() if k != "packet"}}
        (folder / f"{int(state.started)}-{state.id}.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def recent(folder: Path, limit: int = 20) -> list[dict]:
        if not folder.exists():
            return []
        out = []
        for path in sorted(folder.glob("*.json"), reverse=True)[:limit]:
            data = json.loads(path.read_text(encoding="utf-8"))
            out.append({k: data.get(k) for k in ("run", "skill", "version", "status", "ms", "steps_done",
                                                 "llm_calls", "new_version", "started", "error")}
                       | {"heals": len(data.get("heals") or [])})
        return out


MANAGER = RunManager()
