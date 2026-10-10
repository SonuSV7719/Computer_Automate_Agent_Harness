"""`shadow` command line: a thin, fast client to the local daemon.

Only the standard library is imported here so each call starts in milliseconds.

Exit codes: 0 ok, 1 error, 2 expected result not seen, 3 stale snapshot.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PORT = int(os.environ.get("SHADOW_PORT", "47600"))
BASE = f"http://127.0.0.1:{PORT}"
HOME = Path(os.environ.get("SHADOW_HOME", Path.home() / ".shadow"))
EXIT = {"expect": 2, "stale": 3}


def _request(path: str, body: dict | None = None, timeout: float = 60.0) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method="GET" if body is None else "POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def daemon_running() -> bool:
    try:
        return bool(_request("/health", timeout=0.5).get("ok"))
    except (urllib.error.URLError, OSError, ValueError):
        return False


def start_daemon(wait: float = 30.0) -> bool:
    if daemon_running():
        return True
    HOME.mkdir(parents=True, exist_ok=True)
    log = open(HOME / "daemon.log", "ab")
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    subprocess.Popen([sys.executable, "-m", "shadow.daemon"], stdout=log, stderr=log,
                     stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    deadline = time.time() + wait
    while time.time() < deadline:
        if daemon_running():
            return True
        time.sleep(0.2)
    return False


def call(path: str, body: dict | None = None) -> dict:
    if not start_daemon():
        return {"ok": False, "kind": "error", "error": f"daemon did not start; see {HOME / 'daemon.log'}"}
    try:
        return _request(path, body if body is not None else {})
    except urllib.error.HTTPError as exc:
        return {"ok": False, "kind": "error",
                "error": f"daemon returned HTTP {exc.code}; see {HOME / 'daemon.log'}"}


def _finish(reply: dict, as_json: bool, text: str | None = None) -> int:
    if as_json or text is None:
        print(json.dumps(reply, ensure_ascii=False, indent=2))
    else:
        print(text)
    if reply.get("ok"):
        return 0
    if not as_json and text is not None:
        print(f"error: {reply.get('error')}", file=sys.stderr)
    return EXIT.get(reply.get("kind", ""), 1)


def _act_text(reply: dict) -> str | None:
    if "route" not in reply:
        return None
    lines = [f"{'ok' if reply['ok'] else 'FAILED'} via {reply['route']} in {reply['ms']} ms"
             f" | snapshot: {reply['snapshot']}"]
    if reply.get("expect"):
        lines.append(f"expect {reply['expect']['spec']}: {'met' if reply['expect']['met'] else 'NOT met'}")
    lines += reply.get("changes") or ["(no visible change)"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="shadow", description="Desktop automation engine.")
    parser.add_argument("--json", action="store_true", help="print the raw JSON reply")
    sub = parser.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("daemon", help="start, stop or check the local daemon")
    d.add_argument("op", choices=["start", "stop", "status"])

    sub.add_parser("windows", help="list open windows")

    t = sub.add_parser("target", help="choose the window to work on")
    t.add_argument("query", help="part of the title, the process name, or a window handle")

    s = sub.add_parser("see", help="print the current screen as indexed text")
    s.add_argument("--elements", action="store_true", help="include every element's properties (JSON)")

    a = sub.add_parser("act", help="perform an action on an element")
    a.add_argument("index", type=int)
    a.add_argument("action", choices=["click", "doubleclick", "rightclick", "type", "append", "toggle",
                                      "select", "expand", "collapse", "focus", "scroll"])
    a.add_argument("value", nargs="?")

    k = sub.add_parser("key", help="press a key or combination, e.g. ctrl+s")
    k.add_argument("combo")

    for p in (a, k):
        p.add_argument("--snapshot", type=int, help="snapshot the index came from")
        p.add_argument("--expect", help="post-condition, e.g. 'text:Saved' or 'window:Invoice'")
        p.add_argument("--timeout", type=float, default=3.0)
        p.add_argument("--intent", default="", help="what this step is for, in plain words")

    r = sub.add_parser("record", help="record actions into a trace")
    r.add_argument("op", choices=["start", "stop", "show"])

    args = parser.parse_args(argv)
    as_json = args.json

    if args.cmd == "daemon":
        if args.op == "start":
            ok = start_daemon()
            return _finish({"ok": ok, "error": None if ok else "daemon did not start"}, as_json,
                           "daemon running" if ok else "daemon did not start")
        if args.op == "status":
            ok = daemon_running()
            return _finish({"ok": ok, "error": "not running"}, as_json, "running" if ok else "not running")
        if daemon_running():
            _request("/shutdown", {})
        return _finish({"ok": True}, as_json, "daemon stopped")

    if args.cmd == "windows":
        reply = call("/windows")
        text = "\n".join(f"{w['hwnd']:>9}  {w['process']:<24} {w['title']}" for w in reply.get("windows", []))
        return _finish(reply, as_json, text if reply.get("ok") else "")

    if args.cmd == "target":
        reply = call("/target", {"query": args.query})
        w = reply.get("window") or {}
        return _finish(reply, as_json, f"target: \"{w.get('title')}\" ({w.get('process')})" if w else "")

    if args.cmd == "see":
        reply = call("/see", {"elements": args.elements})
        return _finish(reply, as_json or args.elements, reply.get("text", ""))

    if args.cmd in ("act", "key"):
        body = {"action": args.action if args.cmd == "act" else "key",
                "index": getattr(args, "index", None),
                "value": args.value if args.cmd == "act" else args.combo,
                "snapshot": args.snapshot, "expect": args.expect, "timeout": args.timeout,
                "intent": args.intent}
        reply = call("/act", body)
        return _finish(reply, as_json, _act_text(reply) or "")

    if args.cmd == "record":
        reply = call("/record", {"cmd": args.op})
        return _finish(reply, True)

    return 1


if __name__ == "__main__":
    sys.exit(main())
