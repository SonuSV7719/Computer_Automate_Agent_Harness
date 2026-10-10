"""The agent's link to the cloud brain: pairing, Gemini through the brain, and commands
sent from the web harness.

All connections are outbound from this machine. Screens stay local; what leaves is the
indexed screen text sent to Gemini for learning and healing, and run results.
"""
from __future__ import annotations

import json
import os
import platform
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from . import net

HOME = Path(os.environ.get("SHADOW_HOME", Path.home() / ".shadow"))
DEVICE_FILE = "device.json"


def load_device() -> Optional[dict]:
    """The saved pairing, or one given through the environment (used by the cloud sandbox)."""
    url, token = os.environ.get("SHADOW_BRAIN_URL"), os.environ.get("SHADOW_DEVICE_TOKEN")
    if url and token:
        return {"url": url.rstrip("/"), "token": token, "device_id": os.environ.get("SHADOW_DEVICE_ID", "sandbox")}
    path = HOME / DEVICE_FILE
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


class BrainLink:
    def __init__(self, url: str, token: str, device_id: str = "") -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.device_id = device_id
        self._stop = threading.Event()

    # ---- http ----------------------------------------------------------
    def _request(self, method: str, path: str, body: Optional[dict] = None, timeout: float = 60.0):
        return net.request(method, self.url + path, body, {"Authorization": f"Bearer {self.token}"}, timeout)

    @staticmethod
    def pair(url: str, code: str, name: str = "") -> dict:
        """Exchange a pairing code from the web harness for this device's token."""
        try:
            reply = net.request("POST", url.rstrip("/") + "/v1/pair/claim",
                                {"code": code.strip().upper(), "name": name or platform.node()}, timeout=30)
        except net.HttpError as exc:
            raise RuntimeError(f"pairing failed ({exc.status}): {exc.body}") from None
        device = {"url": url.rstrip("/"), "token": reply["token"], "device_id": reply["device_id"]}
        HOME.mkdir(parents=True, exist_ok=True)
        (HOME / DEVICE_FILE).write_text(json.dumps(device, indent=2), encoding="utf-8")
        return device

    # ---- Gemini through the brain (the LlmClient interface) -------------
    def json(self, system: str, user: str, schema: dict, files: Optional[list] = None) -> dict:
        if files:
            raise ValueError("file reading goes through the web harness, not the agent")
        return self._request("POST", "/v1/llm", {"system": system, "user": user, "schema": schema}, timeout=120)

    # ---- reporting -----------------------------------------------------
    def push(self, kind: str, payload: dict) -> None:
        """Send a skill, run or event to the brain. Never lets a network error break local work."""
        try:
            self._request("POST", f"/v1/agent/{kind}", payload, timeout=20)
        except Exception:
            pass

    # ---- commands from the web harness ---------------------------------
    def serve(self, handle: Callable[[dict], dict]) -> None:
        """Long-poll for commands and run them until stop() is called."""
        backoff = 1.0
        while not self._stop.is_set():
            try:
                command = self._request("GET", "/v1/agent/next?wait=25", timeout=40)
                backoff = 1.0
            except Exception:
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            if not command:
                continue
            started = time.perf_counter()
            try:
                result = handle(command)
            except Exception as exc:  # a bad command must not end the loop
                result = {"ok": False, "kind": "error", "error": f"{type(exc).__name__}: {exc}"}
            result["agent_ms"] = round((time.perf_counter() - started) * 1000, 1)
            self.push("result", {"id": command["id"], "result": result})

    def start(self, handle: Callable[[dict], dict]) -> threading.Thread:
        thread = threading.Thread(target=self.serve, args=(handle,), name="brain-link", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()
