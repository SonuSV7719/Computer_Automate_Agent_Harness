"""Local daemon: keeps the driver, caches and models warm so every CLI call is instant.

Listens on localhost only. All engine work runs on one COM-initialised thread.
"""
from __future__ import annotations

import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Optional

import uvicorn
from fastapi import Body, FastAPI

HOST = "127.0.0.1"
PORT = int(os.environ.get("SHADOW_PORT", "47600"))
HOME = Path(os.environ.get("SHADOW_HOME", Path.home() / ".shadow"))

app = FastAPI(title="shadow daemon")
_engine = None
_server: Optional[uvicorn.Server] = None


def _init_thread() -> None:
    from .drivers.com import init_thread
    init_thread()


_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="engine", initializer=_init_thread)


def _get_engine():
    global _engine
    if _engine is None:
        from .engine import Engine
        _engine = Engine()
    return _engine


def run(fn: Callable[[Any], dict]) -> dict:
    """Run fn(engine) on the engine thread and turn exceptions into error replies."""
    from .engine import StaleSnapshot

    def call():
        try:
            return {"ok": True, **fn(_get_engine())}
        except StaleSnapshot as exc:
            return {"ok": False, "kind": "stale", "error": str(exc)}
        except (LookupError, ValueError) as exc:
            return {"ok": False, "kind": "invalid", "error": str(exc)}
        except Exception as exc:  # the daemon must survive any single bad call
            return {"ok": False, "kind": "error", "error": f"{type(exc).__name__}: {exc}"}

    return _worker.submit(call).result()


@app.get("/health")
def health() -> dict:
    return {"ok": True, "pid": os.getpid()}


@app.post("/windows")
def windows() -> dict:
    return run(lambda e: {"windows": [w.to_dict() for w in e.windows()]})


@app.post("/target")
def target(query: str = Body(..., embed=True)) -> dict:
    return run(lambda e: {"window": e.set_target(query).to_dict()})


@app.post("/see")
def see(elements: bool = Body(False, embed=True)) -> dict:
    def fn(e):
        snap = e.see()
        out = {"snapshot": snap.id, "text": snap.text, "ms": round(snap.ms, 1),
               "window": snap.window.to_dict()}
        if elements:
            out["elements"] = [el.to_dict() for el in snap.elements]
        return out
    return run(fn)


@app.post("/act")
def act(action: str = Body(...), index: Optional[int] = Body(None), value: Optional[str] = Body(None),
        snapshot: Optional[int] = Body(None), expect: Optional[str] = Body(None),
        timeout: float = Body(3.0), intent: str = Body("")) -> dict:
    def fn(e):
        result = e.act(action, index=index, value=value, snapshot_id=snapshot, expect=expect,
                       timeout=timeout, intent=intent)
        if not result["ok"]:
            result["kind"] = "expect"
            result["error"] = f"expected result not seen: {expect}"
        return result
    reply = run(fn)
    if reply.get("kind") == "expect":
        reply["ok"] = False
    return reply


@app.post("/record")
def record(cmd: str = Body(..., embed=True)) -> dict:
    def fn(e):
        if cmd == "start":
            e.record_start()
            return {"recording": True}
        if cmd == "stop":
            return {"recording": False, "steps": e.record_stop()}
        return {"recording": e.recording, "steps": e.trace}
    return run(fn)


def _store():
    from .core.skill import SkillStore
    return SkillStore(HOME / "skills")


@app.post("/skills/compile")
def skills_compile(name: str = Body(...), description: str = Body("")) -> dict:
    """Stop recording and save the trace as a skill."""
    from .core.skill import compile_trace, render_code

    def fn(e):
        trace = e.record_stop()
        if not trace:
            raise ValueError("nothing was recorded; run `shadow record start` and perform the task first")
        skill = compile_trace(trace, name, description)
        _store().save(skill)
        return {"skill": skill.name, "version": skill.version, "params": skill.params,
                "steps": len(skill.steps), "code": render_code(skill)}
    return run(fn)


@app.post("/skills/list")
def skills_list() -> dict:
    return {"ok": True, "skills": _store().list()}


@app.post("/skills/show")
def skills_show(name: str = Body(...), version: Optional[int] = Body(None)) -> dict:
    try:
        store = _store()
        skill = store.load(name, version)
        return {"ok": True, "skill": skill.to_dict(), "code": store.code(name, skill.version),
                "versions": store.versions(name), "current": store.current(name)}
    except LookupError as exc:
        return {"ok": False, "kind": "invalid", "error": str(exc)}


@app.post("/skills/rollback")
def skills_rollback(name: str = Body(...), version: int = Body(...)) -> dict:
    try:
        skill = _store().rollback(name, version)
        return {"ok": True, "skill": skill.name, "version": skill.version}
    except LookupError as exc:
        return {"ok": False, "kind": "invalid", "error": str(exc)}


@app.post("/skills/diff")
def skills_diff(name: str = Body(...), old: int = Body(...), new: int = Body(...)) -> dict:
    try:
        return {"ok": True, "diff": _store().diff(name, old, new)}
    except (LookupError, OSError) as exc:
        return {"ok": False, "kind": "invalid", "error": str(exc)}


@app.post("/run")
def run_start(skill: str = Body(...), params: dict = Body(default_factory=dict),
              heal: str = Body("auto"), wait: float = Body(120.0)) -> dict:
    from .runs import MANAGER
    try:
        state = MANAGER.start(_store(), skill, params, heal, lambda fn: _worker.submit(lambda: fn(_get_engine())).result())
    except (LookupError, ValueError) as exc:
        return {"ok": False, "kind": "invalid", "error": str(exc)}
    return state.wait(wait)


@app.post("/heal")
def run_heal(run_id: str = Body(..., alias="run"), decision: dict = Body(...), wait: float = Body(120.0)) -> dict:
    from .runs import MANAGER
    try:
        state = MANAGER.answer(run_id, decision)
    except LookupError as exc:
        return {"ok": False, "kind": "invalid", "error": str(exc)}
    return state.wait(wait)


@app.post("/runs")
def runs_list(run_id: Optional[str] = Body(None, alias="run", embed=True)) -> dict:
    from .runs import MANAGER
    if run_id:
        try:
            return MANAGER.get(run_id).summary()
        except LookupError as exc:
            return {"ok": False, "kind": "invalid", "error": str(exc)}
    return {"ok": True, "runs": MANAGER.recent(HOME / "runs")}


@app.post("/shutdown")
def shutdown() -> dict:
    if _server is not None:
        threading.Timer(0.2, lambda: setattr(_server, "should_exit", True)).start()
    return {"ok": True}


def main() -> None:
    global _server
    HOME.mkdir(parents=True, exist_ok=True)
    def warm() -> None:
        from .drivers.com import init_thread
        init_thread()
        try:
            _worker.submit(_get_engine).result()
            _get_engine().warm_up()
        except Exception:
            pass  # vision is optional; the first read will report any real problem

    threading.Thread(target=warm, name="warm-up", daemon=True).start()
    _server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, log_level="warning"))
    _server.run()


if __name__ == "__main__":
    main()
