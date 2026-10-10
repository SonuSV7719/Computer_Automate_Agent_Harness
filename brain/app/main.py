"""The cloud brain (Cloud Run).

It holds the Gemini key, stores skills and runs in Firestore, pairs machines with
users, and relays commands from the web harness to each machine's agent. Agents
connect outbound and long-poll; the brain never reaches into a machine.

Environment:
  GEMINI_API_KEY        Gemini key (required for learning, healing and file reading)
  SHADOW_STORE          "firestore" (default on Cloud Run) or "memory"
  SHADOW_AUTH           "firebase" (default) verifies Firebase ID tokens; "dev" trusts "Bearer dev:<uid>"
  SHADOW_SANDBOX_TOKEN  device token of the shared cloud sandbox every user may drive
  SHADOW_SANDBOX_VIEW   address of the sandbox's live screen, shown in the web harness
  SHADOW_CORS           allowed web origins, comma separated (default *)
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
import threading
import time
from typing import Optional

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware

from .store import FirestoreStore, MemoryStore, Store

PAIR_CODE_TTL = 600
SANDBOX_ID = "sandbox"
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no look-alike characters
USER_COMMANDS = {"windows", "target", "see", "act", "learn", "run", "heal", "skills", "skill",
                 "rollback", "extract", "runs", "demo_app"}
MAX_UPLOAD = 8 * 1024 * 1024


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_app(store: Optional[Store] = None, llm=None, auth_mode: Optional[str] = None) -> FastAPI:
    app = FastAPI(title="Shadow brain")
    origins = [o.strip() for o in os.environ.get("SHADOW_CORS", "*").split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"])

    if store is None:
        store = MemoryStore() if os.environ.get("SHADOW_STORE") == "memory" else FirestoreStore()
    mode = auth_mode or os.environ.get("SHADOW_AUTH", "firebase")
    state = {"llm": llm, "firebase": None}
    waiting = threading.Condition()  # wakes long-polling agents when a command arrives

    sandbox_token = os.environ.get("SHADOW_SANDBOX_TOKEN")
    if sandbox_token:
        store.update("devices", SANDBOX_ID, {"uid": "*", "name": "Cloud sandbox", "token_hash": _hash(sandbox_token),
                                             "shared": True, "view": os.environ.get("SHADOW_SANDBOX_VIEW", "")})

    # ---- who is calling ------------------------------------------------
    def user(authorization: str = Header("")) -> str:
        token = authorization.removeprefix("Bearer ").strip()
        if not token:
            raise HTTPException(401, "sign in first")
        if mode == "dev":
            if not token.startswith("dev:"):
                raise HTTPException(401, "dev auth expects 'Bearer dev:<uid>'")
            return token[4:]
        if state["firebase"] is None:
            import firebase_admin
            from firebase_admin import auth as firebase_auth
            if not firebase_admin._apps:
                firebase_admin.initialize_app()
            state["firebase"] = firebase_auth
        try:
            return state["firebase"].verify_id_token(token)["uid"]
        except Exception:
            raise HTTPException(401, "invalid or expired sign-in") from None

    def device(authorization: str = Header("")) -> dict:
        token = authorization.removeprefix("Bearer ").strip()
        found = store.query("devices", {"token_hash": _hash(token)}, limit=1) if token else []
        if not found:
            raise HTTPException(401, "unknown device; pair it again")
        store.update("devices", found[0]["id"], {"last_seen": time.time()})
        return found[0]

    def own_device(device_id: str, uid: str) -> dict:
        doc = store.get("devices", device_id)
        if doc is None or (doc.get("uid") != uid and not doc.get("shared")):
            raise HTTPException(404, "no such device")
        return doc

    def gemini():
        if state["llm"] is None:
            from shadow.llm.client import GeminiClient
            try:
                state["llm"] = GeminiClient()
            except RuntimeError as exc:
                raise HTTPException(503, str(exc)) from None
        return state["llm"]

    # ---- basics --------------------------------------------------------
    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "store": type(store).__name__, "auth": mode,
                "gemini": bool(state["llm"] or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))}

    # ---- pairing -------------------------------------------------------
    @app.post("/v1/pair/start")
    def pair_start(uid: str = Depends(user)) -> dict:
        code = "".join(secrets.choice(ALPHABET) for _ in range(6))
        store.set("pair_codes", code, {"uid": uid, "expires": time.time() + PAIR_CODE_TTL})
        return {"code": code, "expires_in": PAIR_CODE_TTL}

    @app.post("/v1/pair/claim")
    def pair_claim(code: str = Body(...), name: str = Body("")) -> dict:
        code = code.strip().upper()
        doc = store.get("pair_codes", code)
        if doc is None or doc["expires"] < time.time():
            raise HTTPException(404, "pairing code is wrong or has expired")
        store.delete("pair_codes", code)  # one use only
        token = secrets.token_urlsafe(32)
        device_id = store.add("devices", {"uid": doc["uid"], "name": name[:60] or "My PC", "token_hash": _hash(token),
                                          "created": time.time(), "last_seen": time.time(), "shared": False})
        return {"device_id": device_id, "token": token}

    @app.get("/v1/devices")
    def devices(uid: str = Depends(user)) -> dict:
        found = store.query("devices", {"uid": uid}) + store.query("devices", {"shared": True})
        now = time.time()
        return {"devices": [{"id": d["id"], "name": d.get("name"), "shared": bool(d.get("shared")),
                             "view": d.get("view", ""), "online": now - (d.get("last_seen") or 0) < 60}
                            for d in found]}

    # ---- commands: web harness -> agent ---------------------------------
    @app.post("/v1/commands")
    def command_create(device_id: str = Body(...), type: str = Body(...), args: dict = Body(default_factory=dict),
                       uid: str = Depends(user)) -> dict:
        own_device(device_id, uid)
        if type not in USER_COMMANDS:
            raise HTTPException(400, f"unknown command type '{type}'")
        command_id = store.add("commands", {"uid": uid, "device_id": device_id, "type": type, "args": args,
                                            "status": "queued", "created": time.time(), "events": [], "result": None})
        with waiting:
            waiting.notify_all()
        return {"id": command_id}

    @app.get("/v1/commands/{command_id}")
    def command_get(command_id: str, uid: str = Depends(user)) -> dict:
        doc = store.get("commands", command_id)
        if doc is None or doc["uid"] != uid:
            raise HTTPException(404, "no such command")
        return {"id": command_id, **{k: doc.get(k) for k in ("type", "args", "status", "events", "result", "created")}}

    @app.get("/v1/commands")
    def command_list(device_id: str = Query(...), uid: str = Depends(user)) -> dict:
        found = store.query("commands", {"uid": uid, "device_id": device_id}, "created", True, 30)
        return {"commands": [{k: c.get(k) for k in ("id", "type", "args", "status", "created")} for c in found]}

    # ---- agent side ----------------------------------------------------
    @app.get("/v1/agent/next")
    def agent_next(response: Response, wait: float = Query(25.0, le=50.0), dev: dict = Depends(device)):
        deadline = time.time() + wait
        while True:
            queued = store.query("commands", {"device_id": dev["id"], "status": "queued"}, "created", False, 1)
            if queued:
                command = queued[0]
                store.update("commands", command["id"], {"status": "running", "started": time.time()})
                return {"id": command["id"], "type": command["type"], "args": command.get("args") or {}}
            remaining = deadline - time.time()
            if remaining <= 0:
                response.status_code = 204
                return None
            with waiting:
                waiting.wait(min(remaining, 2.0))  # also re-check: another instance may have queued it

    def _own_command(command_id: str, dev: dict) -> dict:
        doc = store.get("commands", command_id)
        if doc is None or doc["device_id"] != dev["id"]:
            raise HTTPException(404, "no such command for this device")
        return doc

    @app.post("/v1/agent/result")
    def agent_result(id: str = Body(...), result: dict = Body(...), dev: dict = Depends(device)) -> dict:
        _own_command(id, dev)
        status = "done" if result.get("ok") else (result.get("kind") or "failed")
        store.update("commands", id, {"status": status, "result": result, "finished": time.time()})
        return {"ok": True}

    @app.post("/v1/agent/event")
    def agent_event(id: str = Body(...), event: dict = Body(...), dev: dict = Depends(device)) -> dict:
        _own_command(id, dev)
        store.append("commands", id, "events", {**event, "at": time.time()})
        return {"ok": True}

    @app.post("/v1/agent/skill")
    def agent_skill(skill: dict = Body(...), code: str = Body(...), versions: list = Body(default_factory=list),
                    dev: dict = Depends(device)) -> dict:
        key = f"{dev['id']}__{skill['name']}"
        store.set("skills", key, {"uid": dev["uid"], "device_id": dev["id"], "name": skill["name"],
                                  "version": skill["version"], "description": skill.get("description", ""),
                                  "params": skill.get("params", {}), "steps": skill.get("steps", []),
                                  "locators": skill.get("locators", {}), "history": skill.get("history", []),
                                  "window": skill.get("window", {}), "code": code, "versions": versions,
                                  "updated": time.time()})
        store.set("skill_versions", f"{key}__v{skill['version']}", {
            "uid": dev["uid"], "device_id": dev["id"], "name": skill["name"], "version": skill["version"],
            "code": code, "history": skill.get("history", []), "saved": time.time()})
        return {"ok": True}

    @app.post("/v1/agent/run")
    def agent_run(payload: dict = Body(...), dev: dict = Depends(device)) -> dict:
        keep = ("run", "kind", "skill", "version", "status", "ms", "steps_done", "llm_calls", "new_version",
                "error", "steps", "patches")
        doc = {k: payload.get(k) for k in keep}
        doc["heals"] = len(payload["heals"]) if isinstance(payload.get("heals"), list) else payload.get("heals", 0)
        doc.update(uid=dev["uid"], device_id=dev["id"], at=time.time())
        store.add("runs", doc)
        return {"ok": True}

    @app.post("/v1/llm")
    def llm_proxy(system: str = Body(...), user_text: str = Body(..., alias="user"), schema: dict = Body(...),
                  dev: dict = Depends(device)) -> dict:
        """Gemini for a paired agent: the learn and heal prompts are built on the agent."""
        reply = gemini().json(system, user_text, schema)
        store.add("llm_calls", {"device_id": dev["id"], "uid": dev["uid"], "at": time.time(),
                                "chars": len(system) + len(user_text)})
        return reply

    # ---- web harness reads ---------------------------------------------
    @app.get("/v1/skills")
    def skills(device_id: str = Query(...), uid: str = Depends(user)) -> dict:
        own_device(device_id, uid)
        found = store.query("skills", {"device_id": device_id}, "updated", True)
        return {"skills": [{k: s.get(k) for k in ("name", "version", "description", "params", "code", "history",
                                                   "versions", "updated")} | {"steps": len(s.get("steps") or [])}
                           for s in found]}

    @app.get("/v1/runs")
    def runs(device_id: str = Query(...), uid: str = Depends(user)) -> dict:
        own_device(device_id, uid)
        found = store.query("runs", {"device_id": device_id}, "at", True, 100)
        replays = [r for r in found if r.get("kind") == "run" and r.get("status") == "done"]
        learns = [r for r in found if r.get("kind") == "learn" and r.get("status") == "done"]

        def average(rows, field):
            values = [r[field] for r in rows if isinstance(r.get(field), (int, float))]
            return round(sum(values) / len(values), 1) if values else None

        clean = [r for r in replays if not r.get("llm_calls")]
        return {"runs": found[:40], "metrics": {
            "learn_ms": average(learns, "ms"), "learn_llm_calls": average(learns, "llm_calls"),
            "replay_ms": average(clean, "ms"), "replays": len(replays), "replays_without_llm": len(clean),
            "heals": sum(r.get("heals") or 0 for r in found if r.get("kind") == "run"),
            "runs_total": len(found)}}

    @app.post("/v1/extract")
    def extract(params: dict = Body(...), text: str = Body(""), file_b64: str = Body(""),
                mime: str = Body(""), uid: str = Depends(user)) -> dict:
        """Read a dropped document (text, PDF or image) into a skill's inputs with Gemini."""
        from shadow.llm import planner
        files = None
        if file_b64:
            data = base64.b64decode(file_b64)
            if len(data) > MAX_UPLOAD:
                raise HTTPException(413, "file is larger than 8 MB")
            if mime.startswith("text/") or mime in ("application/json", "application/csv"):
                text = data.decode("utf-8", errors="replace")
            elif mime == "application/pdf" or mime.startswith("image/"):
                files = [(mime, data)]
            else:
                raise HTTPException(415, "send text, a PDF or an image")
        if not text and not files:
            raise HTTPException(400, "nothing to read")
        return {"values": planner.extract_params(gemini(), params, text, files)}

    return app


def app_factory() -> FastAPI:
    return create_app()
