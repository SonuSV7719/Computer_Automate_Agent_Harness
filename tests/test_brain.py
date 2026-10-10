"""The cloud brain and the agent's link to it, over real HTTP on a local port.

Storage is the in-memory store and Gemini is a scripted stand-in, so these run anywhere.
"""
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest
import uvicorn

from brain.app.main import create_app
from brain.app.store import MemoryStore
from shadow.cloud import BrainLink
from shadow.core.skill import compile_trace, render_code
from shadow.learn import learn
from test_learn import ScriptedLlm


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Brain:
    def __init__(self, llm, monkeypatch, tmp_path):
        monkeypatch.setenv("SHADOW_SANDBOX_TOKEN", "sandbox-secret")
        monkeypatch.setenv("SHADOW_HOME", str(tmp_path))
        monkeypatch.setattr("shadow.cloud.HOME", tmp_path)
        self.store = MemoryStore()
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.server = uvicorn.Server(uvicorn.Config(create_app(self.store, llm, "dev"), host="127.0.0.1",
                                                    port=self.port, log_level="error"))
        threading.Thread(target=self.server.run, daemon=True).start()
        for _ in range(100):
            try:
                urllib.request.urlopen(self.url + "/healthz", timeout=0.5)
                break
            except OSError:
                time.sleep(0.05)

    def call(self, method, path, body=None, uid="alice", token=None):
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {token}" if token else f"Bearer dev:{uid}"}
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def pair(self, uid="alice", name="Test PC") -> BrainLink:
        _, started = self.call("POST", "/v1/pair/start", {}, uid)
        device = BrainLink.pair(self.url, started["code"], name)
        return BrainLink(device["url"], device["token"], device["device_id"])

    def wait_for(self, command_id, uid="alice"):
        for _ in range(200):
            _, doc = self.call("GET", f"/v1/commands/{command_id}", uid=uid)
            if doc["status"] not in ("queued", "running"):
                return doc
            time.sleep(0.05)
        raise AssertionError("command did not finish")


@pytest.fixture
def brain(monkeypatch, tmp_path):
    b = Brain(ScriptedLlm(), monkeypatch, tmp_path)
    yield b
    b.server.should_exit = True


def test_pairing_gives_a_device_token_and_a_code_works_once(brain, tmp_path):
    _, started = brain.call("POST", "/v1/pair/start", {})
    assert len(started["code"]) == 6
    device = BrainLink.pair(brain.url, started["code"].lower(), "Front desk PC")
    assert device["token"] and json.loads((tmp_path / "device.json").read_text())["device_id"] == device["device_id"]
    with pytest.raises(RuntimeError, match="wrong or has expired"):
        BrainLink.pair(brain.url, started["code"])
    _, listing = brain.call("GET", "/v1/devices")
    names = {d["name"]: d for d in listing["devices"]}
    assert names["Front desk PC"]["online"] and not names["Front desk PC"]["shared"]
    assert names["Cloud sandbox"]["shared"]  # every user sees the shared sandbox
    _, other = brain.call("GET", "/v1/devices", uid="bob")
    assert [d["name"] for d in other["devices"]] == ["Cloud sandbox"]


def test_requests_without_valid_credentials_are_refused(brain):
    assert brain.call("POST", "/v1/pair/start", {}, token="not-a-dev-token")[0] == 401
    assert brain.call("GET", "/v1/agent/next?wait=0", token="made-up-device-token")[0] == 401
    assert brain.call("POST", "/v1/llm", {"system": "s", "user": "u", "schema": {}}, token="nope")[0] == 401


def test_command_reaches_the_agent_and_its_result_comes_back(brain):
    link = brain.pair()
    seen = []

    def handle(command):
        seen.append(command)
        link.push("event", {"id": command["id"], "event": {"type": "step", "n": 1}})
        return {"ok": True, "text": 'WINDOW "Demo"'}

    link.start(handle)
    try:
        _, created = brain.call("POST", "/v1/commands", {"device_id": link.device_id, "type": "see", "args": {}})
        done = brain.wait_for(created["id"])
        assert done["status"] == "done" and done["result"]["text"] == 'WINDOW "Demo"'
        assert done["events"][0]["type"] == "step" and "agent_ms" in done["result"]
        assert seen[0]["type"] == "see"
        # another user can neither read it nor command this device
        assert brain.call("GET", f"/v1/commands/{created['id']}", uid="bob")[0] == 404
        assert brain.call("POST", "/v1/commands", {"device_id": link.device_id, "type": "see"}, uid="bob")[0] == 404
        assert brain.call("POST", "/v1/commands", {"device_id": link.device_id, "type": "format_disk"})[0] == 400
    finally:
        link.stop()


def test_a_failing_handler_is_reported_and_the_agent_keeps_serving(brain):
    link = brain.pair()
    link.start(lambda command: 1 / 0 if command["type"] == "see" else {"ok": True})
    try:
        _, first = brain.call("POST", "/v1/commands", {"device_id": link.device_id, "type": "see"})
        assert "ZeroDivisionError" in brain.wait_for(first["id"])["result"]["error"]
        _, second = brain.call("POST", "/v1/commands", {"device_id": link.device_id, "type": "windows"})
        assert brain.wait_for(second["id"])["status"] == "done"
    finally:
        link.stop()


def test_agent_learns_a_task_using_gemini_through_the_brain(brain, engine, driver):
    """The agent has no key of its own: every model call goes to the brain."""
    link = brain.pair()
    result = learn(lambda fn: fn(engine), link, "Enter invoice INV-9001 for Northwind Paper, amount 640")
    assert result["ok"] and result["llm_calls"] == 5
    assert driver.saved == ["INV-9001 | Northwind Paper | 640 | tax excl."]
    assert len(brain.store.query("llm_calls", {"device_id": link.device_id})) == 5  # usage is counted

    skill = compile_trace(result["trace"], "enter invoice")
    link.push("skill", {"skill": skill.to_dict(), "code": render_code(skill), "versions": [1]})
    link.push("run", {"run": "r1", "kind": "learn", "skill": skill.name, "version": 1, "status": "done",
                      "ms": 41000, "llm_calls": 5, "steps_done": 4, "heals": 0})
    for n, ms in enumerate((520, 480)):
        link.push("run", {"run": f"r{n + 2}", "kind": "run", "skill": skill.name, "version": 1, "status": "done",
                          "ms": ms, "llm_calls": 0, "steps_done": 4, "heals": []})
    _, listing = brain.call("GET", f"/v1/skills?device_id={link.device_id}")
    assert listing["skills"][0]["name"] == "enter_invoice" and "def run(h" in listing["skills"][0]["code"]
    _, runs = brain.call("GET", f"/v1/runs?device_id={link.device_id}")
    assert runs["metrics"] == {"learn_ms": 41000.0, "learn_llm_calls": 5.0, "replay_ms": 500.0, "replays": 2,
                               "replays_without_llm": 2, "heals": 0, "runs_total": 3}
    assert brain.call("GET", f"/v1/skills?device_id={link.device_id}", uid="bob")[0] == 404


def test_shared_sandbox_takes_commands_from_any_user(brain):
    link = BrainLink(brain.url, "sandbox-secret", "sandbox")
    link.start(lambda command: {"ok": True, "who": "sandbox"})
    try:
        _, created = brain.call("POST", "/v1/commands", {"device_id": "sandbox", "type": "windows"}, uid="judge")
        assert brain.wait_for(created["id"], uid="judge")["result"]["who"] == "sandbox"
    finally:
        link.stop()


def test_dropped_document_is_read_into_skill_inputs(monkeypatch, tmp_path):
    class ExtractLlm:
        def json(self, system, user, schema, files=None):
            assert "Total due 88.50" in user
            return {"values": [{"name": "amount", "value": "88.50"}, {"name": "unknown", "value": "x"}]}

    b = Brain(ExtractLlm(), monkeypatch, tmp_path)
    try:
        import base64
        body = {"params": {"invoice_no": "INV-1", "amount": "10"}, "mime": "text/plain",
                "file_b64": base64.b64encode(b"Invoice INV-77. Total due 88.50").decode()}
        status, reply = b.call("POST", "/v1/extract", body)
        assert status == 200 and reply == {"values": {"amount": "88.50"}}
        assert b.call("POST", "/v1/extract", {"params": {}, "mime": "application/zip", "file_b64": "AAAA"})[0] == 415
    finally:
        b.server.should_exit = True
