"""Local daemon: keeps the driver, caches and models warm so every CLI call is instant.

Listens on localhost only. All engine work runs on one COM-initialised thread.
"""
from __future__ import annotations

import os
import sys
import threading
import time
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
    reply = run(fn)
    if reply.get("ok"):
        _report_skill(reply["skill"])
    return reply


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


_link = None  # connection to the cloud brain, when paired


def _llm():
    """Gemini: directly with a local key, else through the paired brain, else none."""
    from .llm.client import default_client
    client = default_client()
    if client is not None:
        return client
    return _link


def _engine_call(fn):
    return _worker.submit(lambda: fn(_get_engine())).result()


def _report(kind: str, payload: dict) -> None:
    if _link is not None:
        threading.Thread(target=_link.push, args=(kind, payload), daemon=True).start()


def _report_skill(name: str) -> None:
    if _link is None:
        return
    store = _store()
    skill = store.load(name)
    _report("skill", {"skill": skill.to_dict(), "code": store.code(name, skill.version),
                      "versions": store.versions(name)})


@app.post("/learn")
def learn_task(goal: str = Body(...), name: str = Body(...), description: str = Body(""),
               max_steps: int = Body(30), command: Optional[str] = Body(None)) -> dict:
    """Gemini performs the task once while it is recorded, then it is compiled into a skill."""
    from .core.skill import compile_trace, render_code
    from .learn import learn

    client = _llm()
    if client is None:
        return {"ok": False, "kind": "invalid",
                "error": "no Gemini access: set GEMINI_API_KEY or pair with the cloud (`shadow pair`)"}

    def on_event(event: dict) -> None:
        if command:
            _report("event", {"id": command, "event": event})

    try:
        result = learn(_engine_call, client, goal, max_steps, on_event)
    except Exception as exc:
        return {"ok": False, "kind": "error", "error": f"{type(exc).__name__}: {exc}"}
    reply = {"ok": result["ok"], "status": result["status"], "error": result["error"],
             "result": result["result"], "ms": result["ms"], "llm_calls": result["llm_calls"],
             "steps": result["steps"], "history": result["history"], "goal": goal}
    if result["ok"] and result["trace"]:
        skill = compile_trace(result["trace"], name, description or goal)
        _store().save(skill)
        reply.update(skill=skill.name, version=skill.version, params=skill.params, code=render_code(skill))
        _report_skill(skill.name)
    elif not result["ok"]:
        reply["kind"] = "error"
    _report("run", {"run": f"learn-{int(time.time())}", "kind": "learn", "skill": reply.get("skill", name),
                    "version": reply.get("version"), "status": reply["status"], "ms": reply["ms"],
                    "llm_calls": reply["llm_calls"], "steps_done": reply["steps"], "heals": 0,
                    "error": reply["error"]})
    return reply


@app.post("/extract")
def extract(skill: str = Body(...), text: str = Body(...)) -> dict:
    """Read a document's text into a skill's inputs with Gemini."""
    from .llm import planner
    client = _llm()
    if client is None:
        return {"ok": False, "kind": "invalid", "error": "no Gemini access"}
    try:
        params = _store().load(skill).params
        return {"ok": True, "values": planner.extract_params(client, params, text)}
    except LookupError as exc:
        return {"ok": False, "kind": "invalid", "error": str(exc)}


@app.post("/pair")
def pair(url: str = Body(...), code: str = Body(...), name: str = Body("")) -> dict:
    from .cloud import BrainLink
    try:
        device = BrainLink.pair(url, code, name)
    except Exception as exc:
        return {"ok": False, "kind": "error", "error": str(exc)}
    _connect()
    return {"ok": True, "device_id": device["device_id"], "url": device["url"]}


@app.post("/status")
def status() -> dict:
    return {"ok": True, "pid": os.getpid(), "paired": _link is not None,
            "brain": _link.url if _link else None, "device_id": _link.device_id if _link else None,
            "gemini": "local key" if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            else ("through the brain" if _link else "not configured")}


def _handle_command(command: dict) -> dict:
    """Run a command sent from the web harness. Same operations as the CLI."""
    args = command.get("args") or {}
    kind = command.get("type")
    if kind == "windows":
        return windows()
    if kind == "target":
        return target(args["query"])
    if kind == "see":
        return see(bool(args.get("elements")))
    if kind == "act":
        return act(args["action"], args.get("index"), args.get("value"), args.get("snapshot"),
                   args.get("expect"), float(args.get("timeout", 3.0)), args.get("intent", ""))
    if kind == "learn":
        return learn_task(args["goal"], args["name"], args.get("description", ""),
                          int(args.get("max_steps", 30)), command["id"])
    if kind == "run":
        return run_start(args["skill"], args.get("params") or {}, args.get("heal", "auto"),
                         float(args.get("wait", 300.0)))
    if kind == "heal":
        return run_heal(args["run"], args["decision"], float(args.get("wait", 300.0)))
    if kind == "skills":
        return skills_list()
    if kind == "skill":
        return skills_show(args["name"], args.get("version"))
    if kind == "rollback":
        return skills_rollback(args["name"], int(args["version"]))
    if kind == "extract":
        return extract(args["skill"], args["text"])
    if kind == "runs":
        return runs_list(None)
    return {"ok": False, "kind": "invalid", "error": f"unknown command type '{kind}'"}


def _connect() -> None:
    """Start (or restart) the link to the brain if this machine is paired."""
    global _link
    from .cloud import BrainLink, load_device
    from .llm import planner
    from .llm.client import default_client
    from .runs import MANAGER

    device = load_device()
    if _link is not None:
        _link.stop()
        _link = None
    if device:
        _link = BrainLink(device["url"], device["token"], device.get("device_id", ""))
        _link.start(_handle_command)
    client = default_client() or _link
    MANAGER.llm_healer = (lambda packet: planner.heal(client, packet)) if client is not None else None

    def finished(summary: dict) -> None:
        _report("run", {**summary, "kind": "run"})
        if summary.get("new_version"):
            _report_skill(summary["skill"])

    MANAGER.on_finish = finished


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
    try:
        _connect()
    except Exception as exc:  # the daemon works offline; say why the cloud is not connected
        print(f"cloud link not started: {exc}", file=sys.stderr)
    _server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, log_level="warning"))
    _server.run()


if __name__ == "__main__":
    main()
